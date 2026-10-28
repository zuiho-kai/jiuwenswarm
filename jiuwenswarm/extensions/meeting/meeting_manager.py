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
    from jiuwenswarm.common.voice_matching import MemberVoiceRegistry
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager
    from .grant_driver import MeetingGrantDriver, MeetingPlaybackConsumer
    from .participants import MeetingPublicationSession

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
    publication: MeetingPublicationSession | None = None
    driver: MeetingGrantDriver | None = None


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
        self._playback_ready_lock = asyncio.Lock()
        self._voice_registries: dict[str, tuple[Any, Any]] = {}
        self._stopping = False

    async def prepare_browser_playback(
        self, session_id: str, meeting_id: str, *, owner_id: str, startup_id: str,
        route_id: str, model: str, config_revision: str, default_voice: str, emit,
    ) -> dict:
        from jiuwenswarm.common.voice_matching import MemberVoiceRegistry
        from .playback import BrowserPlaybackConsumer

        if not all((owner_id, startup_id, route_id, model, config_revision)):
            raise ValueError("Playback readiness requires complete owner/configuration identity")
        async with self._playback_ready_lock:
            async with self._lock:
                meeting = self._require_publication_locked(session_id, meeting_id)
                if meeting.owner_id != owner_id or meeting.startup_id != startup_id:
                    raise ValueError("Meeting playback is not owned by this connection/startup")
                if meeting.driver is not None:
                    consumer = meeting.driver.consumer
                    if (
                        not isinstance(consumer, BrowserPlaybackConsumer) or consumer.route_id != route_id
                        or consumer.config_revision != config_revision
                    ):
                        raise ValueError("Meeting already has a different playback consumer")
                    return {"ready": True, "meeting_id": meeting_id}
                cached = self._voice_registries.get(session_id)
                if cached is not None and cached[0] is meeting.backend and cached[1].model == model:
                    voices = cached[1]
                else:
                    voices = MemberVoiceRegistry(session_id, meeting.state.scope.team_name, model)
                    voices.assign_voice(meeting.state.leader_member_name, preference=default_voice)
                    self._voice_registries[session_id] = (meeting.backend, voices)
                consumer = BrowserPlaybackConsumer(
                    meeting.state.scope, voices, owner_id=owner_id, route_id=route_id,
                    config_revision=config_revision, emit=emit,
                )
            consumer.driver = await self.enable_grant_driver(session_id, meeting_id, consumer=consumer, voices=voices)
            return {"ready": True, "meeting_id": meeting_id}

    async def browser_playback(self, session_id: str, meeting_id: str, owner_id: str, *, closing=False):
        from .playback import BrowserPlaybackConsumer

        async with self._lock:
            meeting = self._sessions.get(session_id)
            if (
                not owner_id or meeting is None or meeting.state is None or meeting.owner_id != owner_id
                or meeting.state.scope.meeting_id != meeting_id or meeting.driver is None
                or meeting.phase not in ({"active", "closing", "cleanup_failed"} if closing else {"active"})
                or not isinstance(meeting.driver.consumer, BrowserPlaybackConsumer)
            ):
                raise ValueError("Meeting playback is not owned by this connection or no longer active")
            if not closing and (not self._enabled() or not self._runtime_matches(session_id, meeting)):
                self._begin_close_locked(session_id, meeting)
                raise ValueError("Meeting playback runtime has ended")
            return meeting.driver.consumer

    def publication_for_session(self, session_id: str, team_name: str) -> MeetingPublicationSession | None:
        """Resolve only the current native scope; ordinary sessions allocate nothing."""
        meeting = self._sessions.get(session_id)
        if meeting is None or meeting.publication is None:
            return None
        if (
            meeting.phase != "active" or not self._enabled()
            or not self._runtime_matches(session_id, meeting)
        ):
            meeting.publication.close()
            if meeting.driver is not None and meeting.driver.state.phase not in {"closing", "closed"}:
                meeting.driver.close()
                self._spawn(self._driver_is_current(session_id, meeting))
            return None
        if meeting.state is None or meeting.state.scope.team_name != team_name:
            return None
        return meeting.publication if not meeting.publication.closed else None

    async def enable_publication(
        self, session_id: str, meeting_id: str, *, on_queued: Callable[[], None],
        voices: MemberVoiceRegistry | None = None,
    ) -> MeetingPublicationSession:
        """Internal host entry, not a browser RPC or a new mode setting.

        Browser startup intentionally does not call this until playback transport
        exists. Tests can supply a controlled consumer wake-up.
        """
        if not callable(on_queued):
            raise ValueError("A meeting publication consumer wake-up callback is required")
        async with self._lock:
            meeting = self._require_publication_locked(session_id, meeting_id)
            return await self._enable_publication_locked(session_id, meeting, on_queued, voices)

    def _require_publication_locked(self, session_id: str, meeting_id: str) -> _Meeting:
        meeting = self._sessions.get(session_id)
        if (
            meeting is None or meeting.phase != "active" or meeting.state is None
            or meeting.state.scope.meeting_id != meeting_id or not self._enabled()
            or not self._runtime_matches(session_id, meeting)
        ):
            raise ValueError("A matching active meeting is required for publication")
        return meeting

    async def _enable_publication_locked(
        self, session_id: str, meeting: _Meeting, on_queued: Callable[[], None],
        voices: MemberVoiceRegistry | None = None,
        on_addressed_members=None,
        on_interrupt=None,
    ) -> MeetingPublicationSession:
        from .participants import MeetingPublicationSession

        if voices is not None and (
            voices.session_id != session_id or voices.team_name != meeting.state.scope.team_name
        ):
            raise ValueError("Voice registry belongs to a different Team/session")
        if meeting.publication is not None:
            if meeting.publication.on_queued != on_queued or meeting.publication.closed:
                raise ValueError("Meeting publication already has a consumer")
            if voices is not None and meeting.publication.voices is not voices:
                raise ValueError("Meeting publication already has a different voice registry")
            return meeting.publication
        team_agent = self._team_manager.get_team_agent(session_id)
        if getattr(getattr(team_agent, "spec", None), "spawn_mode", "inprocess") != "inprocess":
            raise ValueError("Coordinated publication supports native in-process members only")
        if getattr(getattr(team_agent, "spec", None), "enable_team_plan", False):
            raise ValueError("Finish Team plan mode before enabling coordinated publication")
        members = await meeting.backend.db.member.get_team_members(meeting.state.scope.team_name)
        if any(member.role not in {"leader", "teammate"} for member in members):
            raise ValueError("Coordinated publication supports native in-process members only")
        assert meeting.coordinator is not None
        if voices is not None:
            voices.assign_roster(
                (member.member_name for member in members),
                leader_id=meeting.state.leader_member_name,
            )
        meeting.publication = MeetingPublicationSession(
            meeting.coordinator, on_queued, voices=voices,
            on_addressed_members=on_addressed_members,
            on_interrupt=on_interrupt,
        )
        return meeting.publication

    async def enable_grant_driver(
        self, session_id: str, meeting_id: str, *, consumer: MeetingPlaybackConsumer,
        voices: MemberVoiceRegistry | None = None,
    ) -> MeetingGrantDriver:
        """Attach a playback consumer before enabling native publication and grants."""
        from .grant_driver import MeetingGrantDriver

        async with self._lock:
            meeting = self._require_publication_locked(session_id, meeting_id)
            if meeting.driver is not None:
                if meeting.driver.consumer is not consumer or meeting.driver.state.phase in {"closing", "closed"}:
                    raise ValueError("Meeting grant driver already has a consumer")
                if voices is not None and meeting.publication.voices is not voices:
                    raise ValueError("Meeting grant driver already has a different voice registry")
                return meeting.driver
            assert meeting.coordinator is not None
            state = await meeting.coordinator.get_state()
            if state is None or state.closed or state.owner_request_id or state.interruption_id:
                raise ValueError("Grant driver requires an open meeting without an existing lease or interruption")
            driver = MeetingGrantDriver(
                meeting.coordinator, consumer,
                is_current=lambda: self._driver_is_current(session_id, meeting),
            )
            await self._enable_publication_locked(
                session_id, meeting, driver.wake, voices,
                on_addressed_members=getattr(consumer, "addressed_members_changed", None),
                on_interrupt=lambda member, token: driver.interrupt(token, leader=member),
            )
            meeting.driver = driver
            driver.start()
            return driver

    async def prepare_human_input(self, session_id: str, meeting_id: str, owner_id: str, text: str) -> dict:
        from .interruption import is_human_interruption

        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise ValueError("Meeting transcript must contain 1-16000 characters")
        consumer = await self.browser_playback(session_id, meeting_id, owner_id)
        driver = consumer.driver
        speaker = await driver.coordinator.get_speaker()
        if speaker is None:
            return {}
        agent = self._team_manager.get_team_agent(session_id)
        config = getattr(agent.spec, "duplex", None)
        if not await is_human_interruption(text, speaker, config):
            return {}
        # The token observed before judgment prevents a slow answer from
        # interrupting a newer speaker. Human text is still delivered either way.
        try:
            interruption = await driver.interrupt(speaker.lease_token, human=True)
        except RuntimeError:
            interruption = driver.pending_interruption
        return {"interruption_id": interruption.interruption_id} if interruption is not None else {}

    async def human_input_admitted(self, session_id: str, meeting_id: str, owner_id: str, interruption_id: str) -> dict:
        consumer = await self.browser_playback(session_id, meeting_id, owner_id)
        if not await consumer.driver.input_admitted(interruption_id):
            raise ValueError("Human input interruption is no longer current")
        return {"accepted": True}

    async def _driver_is_current(self, session_id: str, meeting: _Meeting) -> bool:
        async with self._lock:
            if self._sessions.get(session_id) is not meeting or meeting.phase != "active":
                return False
            if (
                not self._enabled() or not self._runtime_matches(session_id, meeting)
                or meeting.publication is None or meeting.publication.closed
            ):
                self._begin_close_locked(session_id, meeting)
                return False
            return True

    async def _owned_driver(self, session_id: str, meeting_id: str, owner_id: str) -> MeetingGrantDriver | None:
        async with self._lock:
            meeting = self._sessions.get(session_id)
            if (
                not owner_id or meeting is None or meeting.phase != "active" or meeting.state is None
                or meeting.state.scope.meeting_id != meeting_id or meeting.owner_id != owner_id
            ):
                return None
            return meeting.driver

    async def complete_playback(
        self, session_id: str, meeting_id: str, request_id: str, lease_token: str, *, owner_id: str,
    ) -> bool:
        driver = await self._owned_driver(session_id, meeting_id, owner_id)
        return await driver.complete_playback(driver.coordinator.scope, request_id, lease_token) if driver else False

    async def fail_playback(
        self, session_id: str, meeting_id: str, request_id: str, lease_token: str, *, owner_id: str,
    ) -> bool:
        driver = await self._owned_driver(session_id, meeting_id, owner_id)
        return await driver.fail_playback(driver.coordinator.scope, request_id, lease_token) if driver else False

    async def retry_publication(
        self, session_id: str, meeting_id: str, request_id: str, lease_token: str, *, owner_id: str,
    ) -> bool:
        driver = await self._owned_driver(session_id, meeting_id, owner_id)
        return await driver.retry_publication(driver.coordinator.scope, request_id, lease_token) if driver else False

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
        if meeting.publication is not None:
            meeting.publication.close()
        if meeting.driver is not None:
            meeting.driver.close()
        if meeting.coordinator is None:
            self._sessions.pop(session_id)
            return None
        meeting.task = self._spawn(self._close(session_id, meeting))
        return meeting.task

    async def _close(self, session_id: str, meeting: _Meeting) -> None:
        assert meeting.coordinator is not None
        try:
            if meeting.driver is not None:
                await meeting.driver.aclose()
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
                or meeting.phase not in ("active", "closing", "cleanup_failed")
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
                if meeting.publication is not None:
                    meeting.publication.close()
                if meeting.driver is not None:
                    self._begin_close_locked(session_id, meeting)
                    return {"state": "closing"}
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
        self._voice_registries.clear()


_server_manager: MeetingManager | None = None


def get_active_publication(session_id: str, team_name: str) -> MeetingPublicationSession | None:
    return (
        _server_manager.publication_for_session(session_id, team_name)
        if _server_manager is not None else None
    )


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
