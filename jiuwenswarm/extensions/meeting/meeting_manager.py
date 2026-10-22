"""Team-ready waiting and manager-owned meeting cleanup on an existing Team."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from openjiuwen.agent_teams.meeting import (
    MeetingCoordinator,
    MeetingScope,
    MeetingState,
)

from jiuwenswarm.common.config import get_config

if TYPE_CHECKING:
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

logger = logging.getLogger(__name__)


@dataclass
class _Meeting:
    owner_id: str | None
    phase: Literal["pending", "starting", "active", "closing", "cleanup_failed"]
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    abandoned: bool = False
    backend: Any = None
    coordinator: MeetingCoordinator | None = None
    state: MeetingState | None = None
    task: asyncio.Task | None = None
    initializing: asyncio.Task | None = None
    startup_id: str | None = None


def _meeting_enabled() -> bool:
    experimental = get_config().get("experimental") or {}
    return (
        isinstance(experimental, dict)
        and experimental.get("team_meeting_enabled") is True
    )


class MeetingManager:
    """One lifecycle record per session; requests never own lifecycle tasks."""

    def __init__(
        self,
        team_manager: TeamManager,
        *,
        enabled: Callable[[], bool] = _meeting_enabled,
    ) -> None:
        self._team_manager = team_manager
        self._enabled = enabled
        self._sessions: dict[str, _Meeting] = {}
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._stopping = False

    def _spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(
                "Meeting lifecycle failed: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _team_backend(self, session_id: str):
        team_name = self._team_manager.get_active_team_name(session_id)
        backend = getattr(
            self._team_manager.get_team_agent(session_id), "team_backend", None
        )
        if (
            not team_name
            or not self._team_manager.is_runtime_active(session_id)
            or backend is None
            or backend.team_name != team_name
            or backend.conversation_mode != "group_chat"
            or backend.group_session_id != session_id
        ):
            raise RuntimeError(
                "An active group-chat Team is required to start a meeting"
            )
        return team_name, backend

    def _runtime_matches(self, session_id: str, meeting: _Meeting) -> bool:
        try:
            _, backend = self._team_backend(session_id)
        except RuntimeError:
            return False
        return backend is meeting.backend

    def _request_locked(
        self, session_id: str, owner_id: str | None, startup_id: str | None = None
    ) -> _Meeting:
        if not session_id or not session_id.strip():
            raise ValueError("Meeting session ID must be non-empty")
        if self._stopping or not self._enabled():
            raise RuntimeError("Team meetings are disabled or shutting down")
        meeting = self._sessions.get(session_id)
        if meeting is not None:
            if meeting.phase == "cleanup_failed":
                self._begin_close_locked(session_id, meeting)
                raise RuntimeError(
                    "Meeting cleanup is being retried; retry after it ends"
                )
            if meeting.phase == "closing":
                raise RuntimeError("Meeting is closing; retry after it ends")
            if owner_id is not None and meeting.owner_id not in (None, owner_id):
                raise RuntimeError("Another connection owns this meeting")
            if startup_id is not None and meeting.startup_id not in (None, startup_id):
                raise RuntimeError("Another startup already owns this meeting")
            if meeting.phase == "active" and not self._runtime_matches(
                session_id, meeting
            ):
                self._begin_close_locked(session_id, meeting)
                raise RuntimeError("The meeting belongs to a different Team runtime")
            if owner_id is not None:
                meeting.owner_id = owner_id
            if startup_id is not None:
                meeting.startup_id = startup_id
            if meeting.phase == "pending" and self._team_manager.is_runtime_active(
                session_id
            ):
                meeting.ready.set()
            return meeting
        active = self._team_manager.is_runtime_active(session_id)
        meeting = _Meeting(owner_id, "starting" if active else "pending")
        meeting.startup_id = startup_id
        self._sessions[session_id] = meeting
        meeting.task = self._spawn(self._run_start(session_id, meeting))
        return meeting

    async def start(self, session_id: str) -> MeetingState:
        """Wait for startup on an already active Team without owning its task."""
        async with self._lock:
            if not session_id or not session_id.strip():
                raise ValueError("Meeting session ID must be non-empty")
            if not self._enabled():
                raise RuntimeError("Team meetings are disabled")
            self._team_backend(session_id)
            meeting = self._request_locked(session_id, None)
            if meeting.phase == "active":
                assert meeting.state is not None
                return meeting.state
            task = meeting.task
        assert task is not None
        state = await asyncio.shield(task)
        if state is None:
            raise RuntimeError("Meeting startup was abandoned")
        return state

    async def request_start(
        self, session_id: str, owner_id: str, startup_id: str | None = None
    ) -> MeetingState | None:
        """Reserve startup, waiting for normal Team activation if needed."""
        if not owner_id:
            raise ValueError("Meeting owner is required")
        async with self._lock:
            meeting = self._request_locked(session_id, owner_id, startup_id)
            return meeting.state if meeting.phase == "active" else None

    async def _initialize(self, session_id: str, meeting: _Meeting) -> MeetingState:
        team_name, backend = self._team_backend(session_id)
        meeting.backend = backend
        conversation = await backend.group_conversation()
        # Retain the coordinator BEFORE start: it can persist a row and then fail.
        meeting.coordinator = MeetingCoordinator(
            backend.db,
            MeetingScope(session_id, team_name, uuid4().hex),
            message_manager=backend.message_manager,
            conversation=conversation,
        )
        return await meeting.coordinator.start()

    async def _run_start(
        self, session_id: str, meeting: _Meeting
    ) -> MeetingState | None:
        activated = False
        try:
            if meeting.phase == "pending":
                await meeting.ready.wait()
            async with self._lock:
                if meeting.abandoned or not self._enabled():
                    return None
                meeting.phase = "starting"
                meeting.initializing = self._spawn(
                    self._initialize(session_id, meeting)
                )
            state = await meeting.initializing
            async with self._lock:
                if (
                    self._sessions.get(session_id) is meeting
                    and not meeting.abandoned
                    and self._enabled()
                    and self._runtime_matches(session_id, meeting)
                ):
                    meeting.state = state
                    meeting.phase = "active"
                    activated = True
                    return state
            return None
        except TimeoutError:
            logger.info("Meeting startup timed out: session_id=%s", session_id)
            return None
        except asyncio.CancelledError:
            if not meeting.abandoned:
                raise
            return None
        finally:
            if not activated:
                # Cancellation is cooperative. Keep the session reserved while a
                # dependency unwinds, even if it suppresses cancellation briefly.
                initializing = meeting.initializing
                if initializing is not None and not initializing.done():
                    async with self._lock:
                        self._release_locked(session_id, meeting)
                    await asyncio.shield(
                        asyncio.gather(initializing, return_exceptions=True)
                    )
                meeting.initializing = None
                async with self._lock:
                    task = self._begin_close_locked(session_id, meeting)
                if task is not None:
                    await asyncio.shield(task)
            else:
                meeting.initializing = None

    def _begin_close_locked(
        self, session_id: str, meeting: _Meeting
    ) -> asyncio.Task | None:
        if self._sessions.get(session_id) is not meeting:
            return None
        meeting.phase = "closing"
        if meeting.coordinator is None:
            self._sessions.pop(session_id)
            return None
        meeting.task = self._spawn(self._close(session_id, meeting))
        return meeting.task

    async def _close(self, session_id: str, meeting: _Meeting) -> None:
        assert meeting.coordinator is not None
        try:
            # Startup might fail before a row exists. Missing/closed is already clean.
            state = await meeting.coordinator.get_state()
            if state is not None and not state.closed:
                await meeting.coordinator.close()
        except BaseException:
            async with self._lock:
                meeting.phase = "cleanup_failed"
            raise
        async with self._lock:
            if self._sessions.get(session_id) is meeting:
                self._sessions.pop(session_id)

    def _release_locked(
        self, session_id: str, meeting: _Meeting
    ) -> asyncio.Task | None:
        if meeting.phase in ("pending", "starting"):
            meeting.abandoned = True
            meeting.phase = "closing"
            meeting.ready.set()
            if meeting.initializing is not None:
                meeting.initializing.cancel()
            return meeting.task
        if meeting.phase == "closing":
            return meeting.task
        return self._begin_close_locked(session_id, meeting)

    async def release(
        self, session_id: str, owner_id: str, startup_id: str | None = None
    ) -> bool:
        """Connection/page teardown: abandon startup or close its active meeting."""
        if not owner_id:
            return False
        async with self._lock:
            meeting = self._sessions.get(session_id)
            if meeting is None:
                return True
            if meeting.owner_id != owner_id:
                return False
            if startup_id is not None and meeting.startup_id != startup_id:
                return False
            self._release_locked(session_id, meeting)
        return True

    async def end(
        self, session_id: str, meeting_id: str, owner_id: str | None = None
    ) -> bool:
        """End only a matching active meeting; repeated ends join its close task."""
        async with self._lock:
            meeting = self._sessions.get(session_id)
            if (
                meeting is None
                or meeting.state is None
                or meeting.phase not in ("active", "closing")
                or meeting.state.scope.meeting_id != meeting_id
                or (owner_id is not None and meeting.owner_id != owner_id)
            ):
                return False
            task = (
                meeting.task
                if meeting.phase == "closing"
                else self._begin_close_locked(session_id, meeting)
            )
        if task is not None:
            await asyncio.shield(task)
        return True

    async def lifecycle_status(
        self, session_id: str, startup_id: str | None = None
    ) -> dict[str, str]:
        async with self._lock:
            meeting = self._sessions.get(session_id)
            if meeting is None or (
                startup_id is not None and meeting.startup_id != startup_id
            ):
                return {"state": "idle"}
            if meeting.phase == "pending" and self._team_manager.is_runtime_active(
                session_id
            ):
                meeting.ready.set()
            if meeting.phase == "active" and (
                not self._enabled() or not self._runtime_matches(session_id, meeting)
            ):
                self._release_locked(session_id, meeting)
            if meeting.phase != "active":
                return {
                    "state": "starting" if meeting.phase == "pending" else meeting.phase
                }
            coordinator = meeting.coordinator
        assert coordinator is not None
        state = await coordinator.get_state()
        async with self._lock:
            if self._sessions.get(session_id) is not meeting:
                return {"state": "idle"}
            if meeting.phase != "active":
                return {"state": meeting.phase}
            if state is None or state.closed:
                self._sessions.pop(session_id)
                return {"state": "idle"}
            meeting.state = state
            return {
                "state": "active",
                "session_id": session_id,
                "team_name": state.scope.team_name,
                "meeting_id": state.scope.meeting_id,
            }

    async def status(self, session_id: str) -> MeetingState | None:
        async with self._lock:
            meeting = self._sessions.get(session_id)
            coordinator = (
                meeting.coordinator
                if meeting is not None and meeting.phase == "active"
                else None
            )
        return await coordinator.get_state() if coordinator is not None else None

    async def aclose(self) -> None:
        """Drain lifecycle work before the Team databases are shut down."""
        async with self._lock:
            self._stopping = True
            for session_id, meeting in list(self._sessions.items()):
                self._release_locked(session_id, meeting)
            tasks = tuple(self._tasks)
        if tasks:
            await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        if self._sessions:
            raise RuntimeError(
                "Meeting cleanup failed during shutdown; retry before closing Team databases"
            )


_server_manager: MeetingManager | None = None


def get_server_meeting_manager() -> MeetingManager:
    global _server_manager
    if _server_manager is None:
        from jiuwenswarm.agents.harness.team.team_manager import get_team_manager

        _server_manager = MeetingManager(get_team_manager())
    return _server_manager


async def close_server_meeting_manager() -> None:
    global _server_manager
    if _server_manager is not None:
        await _server_manager.aclose()
        _server_manager = None
