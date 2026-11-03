"""Host meeting policy: timely text, bounded audio, current context and fair turns."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace
from uuid import uuid4

from sqlalchemy import case, select
from openjiuwen.agent_teams.meeting import MeetingCoordinator
from openjiuwen.agent_teams.schema.meeting import SpeechRequestStatus
from openjiuwen.agent_teams.schema.status import MEMBER_DEPARTED_STATUSES
from openjiuwen.agent_teams.tools.database.engine import get_current_time
from openjiuwen.agent_teams.tools.models import _get_speech_request_model

MAX_SPEECH_CHARS = 150
MAX_SPEECH_WORDS = 65
AGING_MS = 60_000
logger = logging.getLogger(__name__)


def speech_budget_error(text):
    if not isinstance(text, str) or not text.strip():
        return "Provide a nonempty spoken summary"
    if (re.search(r"[\u3400-\u9fff]", text) and len(text) > MAX_SPEECH_CHARS) or len(text.split()) > MAX_SPEECH_WORDS or len(text) > 600:
        return "Rewrite speech_text within 150 characters for Chinese or 65 words for English. Keep the conclusion, uncertainty and next action; leave details in content. Do not truncate a sentence."
    technical_patterns = (
        (r"(?<![A-Za-z0-9])[A-Z]\d{1,3}[a-z]?(?:[-_][A-Za-z0-9]+)*(?![A-Za-z0-9])|(?i:\brev\s*\d+\b)", "task/revision identifiers"),
        (r"(?i:sha-?(?:256|512|1)\b)|(?<![A-Za-z0-9])[0-9a-fA-F]{16,}(?![A-Za-z0-9])", "hashes"),
        (r"https?://|www\.|[A-Za-z]:[\\/]|(?:outputs|work|assets)[\\/]|(?<![A-Za-z0-9])(?:[\w.-]+/)+[\w.-]+|\b[\w.-]+\.(?:pdf|pptx|docx|json|html|js|css|py|md|png|csv)\b", "paths/URLs/filenames"),
        (r"```|`[^`]+`|(?<![A-Za-z0-9])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9])|(?:节点|(?i:node[-_ ]?id))\s*\d+:\d+", "code/tool names/node identifiers"),
    )
    for pattern, category in technical_patterns:
        if re.search(pattern, text):
            return (f"Rewrite speech_text only: move {category} to content. Explain the concrete task, actual result/uncertainty "
                    "and next action in everyday language for a listener. Use task names instead of IDs, and 'awaiting another member's check' "
                    "instead of submit/revision tokens. Preserve limitations; a written specification is not an implemented fix. "
                    "Keep content unchanged and stay within the speech length limit. Do not just delete words or repeat the same post.")
    return None


class ResponsiveMeetingCoordinator(MeetingCoordinator):
    """Application policy over the SDK's durable queue and lease transactions."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.context_version = 0
        self.latest_user_input = ""
        self._latest_user_key = (0, "")
        self.decision = ""
        from .actions import MeetingActions
        self.actions = MeetingActions()
        self.on_queue_changed = None
        self._assignment_speech_ids = set()
        self._admission_lock = asyncio.Lock()

    async def notify_queue(self):
        if self.on_queue_changed is not None:
            try:
                await self.on_queue_changed(await self.queue_snapshot())
            except Exception:
                logger.exception("Meeting queue status delivery failed; committed publication retained")

    async def queue_snapshot(self):
        pending = await self.list_pending()
        now = get_current_time()
        return {"context_version": self.context_version, "decision": self.decision, **self.actions.snapshot(),
                "pending": [{"request_id": r.request_id, "member_name": r.member_name,
                             "wait_seconds": max(0, (now-r.created_at)//1000),
                             "speech_text": r.speech_text, "published": r.source_message_id is not None} for r in pending]}

    async def pause_actions(self, member_name, action_ids, note):
        from copy import deepcopy
        state = await self.get_state()
        if state is None or state.closed:
            raise ValueError("Meeting has ended")
        async with self._admission_lock:
            candidate = deepcopy(self.actions)
            affected = candidate.pause(member_name, state.leader_member_name, action_ids, note)
            # Commit the stop gate before any notification/network work can yield.
            self.actions = candidate
            cancelled = 0
            for request in await self.list_pending():
                if request.member_name in affected | {member_name}:
                    cancelled += bool(await self.cancel_request(request.request_id))
            recipients = tuple(sorted(affected - {member_name}))
            notification_error = None
            if recipients:
                try:
                    await self._send_message(member_name,
                        "[Meeting scope pause / 会议调整] " + json.dumps({"action_ids": action_ids, "reason": note}, ensure_ascii=False) +
                        "\nStop affected work now. Do not submit or accept old results. Keep artifacts; wait for Leader to cancel/rework/reassign. Unrelated work may continue. Do not acknowledge.",
                        recipients, uuid4().hex)
                except Exception:
                    logger.exception("Meeting pause committed; member notification failed")
                    notification_error = "pause_committed_notification_failed"
        await self.notify_queue()
        return {"phase": self.actions.phase, "paused_action_ids": action_ids,
                "affected_members": sorted(affected), "cancelled_audio": cancelled,
                "paused_actions": [{"action_id": key, "status": self.actions.items[key]["status"],
                                    "revision": self.actions.items[key]["revision"]} for key in dict.fromkeys(action_ids)],
                "notification_error": notification_error}

    async def update_action(self, member_name, data):
        from copy import deepcopy
        state = await self.get_state()
        if state is None or state.closed:
            raise ValueError("Meeting has ended")
        members = await self._database.member.get_team_members(self.scope.team_name)
        names = {m.member_name for m in members if m.status not in MEMBER_DEPARTED_STATUSES and m.role in {"leader", "teammate"}}
        if member_name not in names:
            raise ValueError("Only active meeting members may update actions")
        async with self._admission_lock:
            candidate = deepcopy(self.actions)
            snapshot, recipients = candidate.update(member_name, state.leader_member_name, names, data)
            if recipients:
                await self._send_message(member_name,
                    "[Meeting action / 会议行动] " + json.dumps(snapshot, ensure_ascii=False) +
                    "\nOwner: execute using normal tools, then submit evidence. Reviewer: inspect actual artifacts/tests before accept or rework. No acknowledgement or polling.",
                    recipients, uuid4().hex)
            self.actions = candidate
        await self.notify_queue()
        return snapshot

    async def refresh_user_context(self):
        history = await self._get_history(20)
        users = [message for message in history if message.member_name == "user"]
        if not users:
            return
        latest = max(users, key=lambda message: (message.timestamp, message.message_id))
        async with self._admission_lock:
            key = (latest.timestamp, latest.message_id)
            if key > self._latest_user_key:
                self._latest_user_key = key
                self.latest_user_input = latest.content[-4000:]
                self.context_version += 1

    async def update_context(self, member_name, summary):
        state = await self.get_state()
        if state is None or state.closed or member_name != state.leader_member_name:
            raise ValueError("Only the active meeting Leader may update decisions")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 1200:
            raise ValueError("Decision summary must contain 1-1200 characters")
        async with self._admission_lock:
            if summary.strip() == self.decision:
                return {"context_version": self.context_version, "cancelled_audio": 0}
            members = await self._database.member.get_team_members(self.scope.team_name)
            recipients = tuple(member.member_name for member in members
                               if member.member_name != member_name and member.status not in MEMBER_DEPARTED_STATUSES
                               and member.role in {"leader", "teammate"})
            await self._send_message(member_name,
                "[Meeting decision update / 会议决定更新] " + summary.strip() +
                "\n无需逐一确认；仅在有新发现、分歧或阻塞时回复。Do not acknowledge; reply only with new findings, disagreement or blockers.",
                recipients, uuid4().hex)
            self.decision = summary.strip()
            self.context_version += 1
            # Text is already delivered. Revoke only stale, unplayed audio.
            cancelled = []
            for request in await self.list_pending():
                if await self.cancel_request(request.request_id):
                    cancelled.append(request.request_id)
        await self.notify_queue()
        return {"context_version": self.context_version, "cancelled_audio": len(cancelled)}

    async def announce_assignment(self, member_name, data, speech_text):
        """Narrate committed work without replacing other waiting speech."""
        return await self._post_message(
            member_name,
            f"Assigned to {data['owner']}: {data['goal']}\nAcceptance: {data['acceptance']}",
            speech_text=speech_text, preserve_waiting=True,
        )

    async def _post_message(self, member_name, content, *, speech_text, mentions=(), preserve_waiting=False):
        if member_name == "user":
            return await super()._post_message(member_name, content, speech_text=speech_text, mentions=mentions)
        error = speech_budget_error(speech_text)
        if error:
            raise ValueError(error)
        state = await self.get_state()
        async with self._admission_lock:
            if state is not None and member_name != state.leader_member_name and self.actions.execution_paused(member_name):
                raise ValueError("Affected work is paused. Do not publish old progress; wait for Leader to cancel/rework/reassign")
            result = await super()._post_message(member_name, content, speech_text=speech_text, mentions=mentions)
            request = result.speech_request
            if preserve_waiting:
                self._assignment_speech_ids.add(request.request_id)
            try:
                # Deliver full text now. The SDK reuses this source at audio grant,
                # without publishing or notifying recipients a second time.
                posted = await self._send_message(member_name, content, tuple(mentions), request.request_id)
                dao = await self._dao()
                await dao.record_publication(self.scope, request.request_id, posted.message.message_id)
            except Exception:
                # Admission/source may already be committed. Preserve its stable
                # request ID so the grant/retry path can recover without a new post.
                logger.exception("Meeting text delivery pending; retained request %s", request.request_id)
                result = replace(result, audio_error="text_delivery_pending")
            else:
                # Keep one waiting summary per member; all full text stays available.
                for old in await self.list_pending():
                    if not preserve_waiting and old.member_name == member_name and old.request_id != request.request_id and old.request_id not in self._assignment_speech_ids:
                        await self.cancel_request(old.request_id)
                result = replace(result, message=await self._get_conversation_message(posted.message.message_id),
                                 notified_members=tuple(posted.notified_members),
                                 speech_request=await self.get_request(request.request_id))
        await self.notify_queue()
        return result

    def _ordered_queue(self, meeting, model):
        now = get_current_time()
        priority = case((model.created_at <= now-AGING_MS, 0),
                        (model.member_name == meeting.leader_member_name, 1),
                        (model.member_name.in_(json.loads(meeting.addressed_members)), 2), else_=3)
        return select(model).where(
            model.team_name == self.scope.team_name, model.meeting_id == self.scope.meeting_id,
            model.status == SpeechRequestStatus.QUEUED,
        ).order_by(priority, model.sequence)

    async def list_pending(self):
        dao = await self._dao()
        model = _get_speech_request_model(self.scope.session_id)
        async with dao._locked(self.scope) as (session, meeting):
            rows = list(await session.scalars(self._ordered_queue(meeting, model)))
        return tuple([await self._request(row) for row in rows])

    async def grant_next(self):
        async with self._admission_lock:
            return await self._grant_next_locked()

    async def _grant_next_locked(self):
        dao = await self._dao()
        model = _get_speech_request_model(self.scope.session_id)
        async with dao._locked(self.scope) as (session, meeting):
            if meeting.closed or meeting.interruption_id or meeting.owner_request_id:
                return None
            now = get_current_time()
            row = await session.scalar(self._ordered_queue(meeting, model).limit(1))
            if row is None:
                return None
            row.status = SpeechRequestStatus.GRANTED
            row.lease_token = uuid4().hex
            row.granted_at = now
            meeting.owner_request_id, meeting.lease_token = row.request_id, row.lease_token
        result = await self._publish_granted(await self._request(row))
        await self.notify_queue()
        return result
