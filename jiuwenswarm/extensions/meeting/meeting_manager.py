"""Server-side meeting lifecycle attached to an existing Team runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from openjiuwen.agent_teams.meeting import MeetingCoordinator, MeetingScope, MeetingState

from jiuwenswarm.common.config import get_config

if TYPE_CHECKING:
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager


def _meeting_enabled() -> bool:
    experimental = get_config().get("experimental") or {}
    return isinstance(experimental, dict) and experimental.get("team_meeting_enabled") is True


class MeetingManager:
    """Own meeting instances while TeamManager continues to own their Teams."""

    def __init__(
        self,
        team_manager: TeamManager,
        *,
        enabled: Callable[[], bool] = _meeting_enabled,
    ) -> None:
        self._team_manager = team_manager
        self._enabled = enabled
        self._meetings: dict[str, MeetingCoordinator] = {}
        self._lock = asyncio.Lock()

    async def start(self, session_id: str) -> MeetingState:
        """Attach one meeting to an active group-chat Team session."""
        if not session_id or not session_id.strip():
            raise ValueError("Meeting session ID must be non-empty")
        if not self._enabled():
            raise RuntimeError("Team meetings are disabled")

        async with self._lock:
            team_name = self._team_manager.get_active_team_name(session_id)
            team_agent = self._team_manager.get_team_agent(session_id)
            backend = getattr(team_agent, "team_backend", None)
            if (
                not team_name
                or not self._team_manager.is_runtime_active(session_id)
                or backend is None
                or backend.team_name != team_name
                or backend.conversation_mode != "group_chat"
                or backend.group_session_id != session_id
            ):
                raise RuntimeError("An active group-chat Team is required to start a meeting")

            existing = self._meetings.get(session_id)
            if existing is not None:
                if existing.scope.team_name != team_name:
                    raise RuntimeError("The meeting belongs to a different Team runtime")
                state = await existing.get_state()
                if state is not None and not state.closed:
                    return state
                self._meetings.pop(session_id, None)

            conversation = await backend.group_conversation()
            scope = MeetingScope(session_id, team_name, uuid4().hex)
            coordinator = MeetingCoordinator(
                backend.db,
                scope,
                message_manager=backend.message_manager,
                conversation=conversation,
            )
            state = await coordinator.start()
            self._meetings[session_id] = coordinator
            return state

    async def status(self, session_id: str) -> MeetingState | None:
        """Read the current meeting without granting or publishing anything."""
        async with self._lock:
            coordinator = self._meetings.get(session_id)
            return await coordinator.get_state() if coordinator is not None else None

    async def end(self, session_id: str, meeting_id: str) -> bool:
        """Close a matching meeting without stopping its Team runtime."""
        async with self._lock:
            coordinator = self._meetings.get(session_id)
            if coordinator is None or coordinator.scope.meeting_id != meeting_id:
                return False
            await coordinator.close()
            self._meetings.pop(session_id, None)
            return True
