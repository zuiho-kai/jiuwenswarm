# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native A2A shadow observer at the SDK's pre-delivery rendering boundary.

The SDK owns DB ordering, broadcasts, ACKs, and all delivery/lifecycle actions.
Only this adapter knows its MessageHandler and NativeHarness accessors.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import weakref
from dataclasses import asdict
from functools import wraps
from typing import Any

from jiuwenswarm.common.duplex_router import (
    ControlSnapshot, InboundMessage, SYSTEM_PROMPT, decision_schema, observe, prompt_for,
)

logger = logging.getLogger(__name__)
_MAX_PENDING = 32
_MAX_BODY = 4000


def snapshot_from_native(harness: Any) -> ControlSnapshot | None:
    """Copy only committed control data; never copy context_messages/reasoning.

    Native currently exposes a committed task plan, but no Working Intent
    protocol. Hypothesis/constraints remain unknown until that protocol exists.
    """
    active = harness.active_round
    if active is None:
        return None
    checkpoint = active.last_iter_snapshot or active.pre_round_snapshot
    if checkpoint is None:
        return None
    plan = checkpoint.deep_agent_state.get("task_plan") or {}
    current_id = plan.get("current_task_id")
    current = next((t for t in plan.get("tasks", []) if t.get("id") == current_id), {})
    query = active.original_query
    if isinstance(query, list):
        query = "\n".join(str(item) for item in query)
    goal = str(plan.get("goal") or (query if isinstance(query, str) else ""))[:2000]
    next_action = str(current.get("description") or current.get("content") or "")[:1000]
    phase = str(getattr(active.iter_phase, "value", active.iter_phase))
    # Object identities distinguish replacement snapshots/rounds with equal
    # indices. This token is process-local, not a durable checkpoint address.
    version_data = [id(active), id(checkpoint), phase, active.model_call_in_flight,
                    active.tool_started, active.pause_requested, goal, next_action]
    version = hashlib.sha256(json.dumps(version_data).encode()).hexdigest()[:20]
    return ControlSnapshot(
        context_version=version, round_id=str(active.round_id),
        checkpoint_id=f"{active.round_id}:{checkpoint.iteration_index}",
        phase=phase, goal=goal, next_action=next_action,
    )


class ShadowObserver:
    """One bounded worker per recipient; no fast-model wait on the delivery path."""

    def __init__(self, host: Any, model_name: str, timeout_seconds: float) -> None:
        self._host = weakref.ref(host)
        self._model_name = model_name
        self._timeout = timeout_seconds
        self._pending: dict[str, InboundMessage] = {}
        self._pending_snapshot: ControlSnapshot | None = None
        self._inflight: set[str] = set()
        self._task: asyncio.Task | None = None

    def submit(self, message: InboundMessage) -> None:
        if message.message_id in self._pending or message.message_id in self._inflight:
            return
        if len(self._pending) >= _MAX_PENDING:
            logger.info("duplex.shadow %s", json.dumps({
                "status": "overloaded", "message_ids": [message.message_id],
                "effective_action": "UNCHANGED",
            }))
            return
        self._pending[message.message_id] = message
        self._pending_snapshot = self._snapshot()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="duplex-shadow")

    def _snapshot(self) -> ControlSnapshot | None:
        host = self._host()
        return snapshot_from_native(host.harness) if host is not None else None

    async def _classify(self, snapshot, messages):
        from openjiuwen.agent_teams import create_tiny_agent

        host = self._host()
        if host is None:
            raise RuntimeError("recipient disposed")
        # An ephemeral TinyAgent owns a separate context for every decision.
        async with create_tiny_agent(
            system_prompt=SYSTEM_PROMPT, model_name=self._model_name,
            model_resolver=host.tiny_agent_model_resolver,
            default_schema=decision_schema(snapshot), name="duplex-shadow",
            language="en", max_iterations=1,
        ) as agent:
            return await agent.run(prompt_for(snapshot, messages))

    async def _run(self) -> None:
        try:
            while self._pending:
                messages = tuple(self._pending.values())
                self._pending.clear()
                self._inflight = {m.message_id for m in messages}
                snapshot = self._pending_snapshot
                if snapshot is None:
                    record = {"status": "inactive", "message_ids": list(self._inflight),
                              "effective_action": "UNCHANGED"}
                else:
                    observation = await observe(
                        snapshot, messages, classify=self._classify,
                        current_snapshot=self._snapshot, timeout_seconds=self._timeout,
                    )
                    record = asdict(observation)
                host = self._host()
                if host is not None:
                    record.update(session_id=host.harness.session_id,
                                  member_name=host.blueprint.member_name)
                # No message body, task content, credentials, or reasoning in logs.
                logger.info("duplex.shadow %s", json.dumps(record, ensure_ascii=False))
                self._inflight.clear()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("duplex shadow observer failed; delivery unchanged", exc_info=True)
        finally:
            self._inflight.clear()
            self._pending.clear()

    async def aclose(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._pending.clear()


def _submit(handler, msg, expanded, is_human_agent: bool) -> None:
    from openjiuwen.agent_teams.harness.native_harness import NativeHarness
    from jiuwenswarm.common.config import get_config

    config = get_config().get("duplex_router", {}) or {}
    if config.get("mode", "off") != "shadow" or is_human_agent or expanded.is_template:
        return
    host = handler._round
    if not isinstance(host.harness, NativeHarness) or host.harness.active_round is None:
        return
    # JSON control/approval protocols remain entirely outside model routing.
    if msg.protocol not in (None, "", "text"):
        return
    model = str(config.get("model_name") or "").strip()
    timeout = float(config.get("timeout_seconds", 2.0))
    if not model or not math.isfinite(timeout) or not 0 < timeout <= 30:
        return
    observer = getattr(handler, "_duplex_shadow_observer", None)
    if observer is None:
        observer = ShadowObserver(host, model, timeout)
        handler._duplex_shadow_observer = observer
    observer.submit(InboundMessage(str(msg.message_id), str(msg.from_member_name),
                                   expanded.body[:_MAX_BODY] +
                                   ("\n[message truncated]" if len(expanded.body) > _MAX_BODY else "")))


def install_shadow_observer() -> bool:
    """Install an idempotent SDK shim; unsupported SDKs keep their old behavior.

    Rendering is the narrow synchronous point after DB read/expansion and before
    deliver_input. Inheriting the drain avoids duplicating ACK/watermark logic.
    """
    try:
        from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
        from openjiuwen.agent_teams.agent.team_agent import TeamAgent
    except ImportError:
        logger.warning("duplex shadow unavailable: unsupported SDK")
        return False

    original = getattr(MessageHandler, "_format_message", None)
    dispose = getattr(TeamAgent, "_dispose_tiny_agents", None)
    if original is None or dispose is None:
        logger.warning("duplex shadow unavailable: SDK hooks missing")
        return False
    if getattr(original, "_duplex_shadow", False):
        return True
    try:
        inspect.signature(original).bind(None, None, expanded=None,
                                         is_human_agent=False, now_ms=0)
    except TypeError:
        logger.warning("duplex shadow unavailable: SDK rendering signature changed")
        return False

    @wraps(original)
    def render(self, msg, *, expanded, is_human_agent, now_ms):
        text = original(self, msg, expanded=expanded, is_human_agent=is_human_agent, now_ms=now_ms)
        try:
            _submit(self, msg, expanded, is_human_agent)
        except Exception:
            logger.warning("duplex shadow skipped; delivery unchanged", exc_info=True)
        return text

    render._duplex_shadow = True
    MessageHandler._format_message = render

    @wraps(dispose)
    async def dispose_with_shadow(self):
        try:
            dispatcher = self.coordination.dispatcher
            handler = dispatcher.message if dispatcher is not None else None
            observer = getattr(handler, "_duplex_shadow_observer", None)
            if observer is not None:
                await observer.aclose()
        finally:
            await dispose(self)

    TeamAgent._dispose_tiny_agents = dispose_with_shadow
    return True
