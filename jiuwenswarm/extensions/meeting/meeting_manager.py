"""Team-owned meeting activation and host media cleanup."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from openjiuwen.agent_teams.meeting import (
    MeetingCoordinator,
    MeetingState,
)
from openjiuwen.agent_teams.schema.events import TeamEvent, TeamTopic

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
    phase: Literal["pending", "starting", "active", "disabled", "closing", "cleanup_failed"]
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
    disable_on_cleanup: bool = True
    release_playback: bool = False
    retiring: bool = False


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
        self._runtime_listeners: dict[str, tuple[Any, Callable]] = {}
        self._queue_topics: dict[str, tuple[Any, str]] = {}
        self._runtime_lock = asyncio.Lock()
        self._stopped_sessions: set[str] = set()
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
                if not await self._refresh_enabled_locked(session_id, meeting):
                    raise ValueError("Team meeting is no longer enabled")
                if meeting.driver is not None and meeting.driver.consumer is not None:
                    consumer = meeting.driver.consumer
                    if (
                        not isinstance(consumer, BrowserPlaybackConsumer) or consumer.route_id != route_id
                        or consumer.config_revision != config_revision
                        or meeting.driver.state.phase in {"closing", "closed"}
                    ):
                        raise ValueError("Meeting already has a different playback consumer")
                    return {"ready": True, "meeting_id": meeting_id}
                cached = self._voice_registries.get(session_id)
                if (
                    cached is not None and cached[0] is meeting.backend and cached[1].model == model
                    and cached[1].get_voice(meeting.state.leader_member_name).provider_voice_id == default_voice
                ):
                    voices = cached[1]
                else:
                    voices = MemberVoiceRegistry(session_id, meeting.state.scope.team_name, model)
                    voices.assign_voice(meeting.state.leader_member_name, preference=default_voice)
                    self._voice_registries[session_id] = (meeting.backend, voices)
                consumer = BrowserPlaybackConsumer(
                    meeting.state.scope, voices, owner_id=owner_id, route_id=route_id,
                    config_revision=config_revision, emit=emit,
                )
            await self.enable_grant_driver(session_id, meeting_id, consumer=consumer, voices=voices)
            return {"ready": True, "meeting_id": meeting_id}

    async def browser_playback(self, session_id: str, meeting_id: str, owner_id: str, *, closing=False):
        from .playback import BrowserPlaybackConsumer

        async with self._lock:
            meeting = self._sessions.get(session_id)
            if (
                not owner_id or meeting is None or meeting.state is None or meeting.owner_id != owner_id
                or meeting.state.scope.meeting_id != meeting_id or meeting.driver is None
                or meeting.phase not in ({"active", "disabled", "closing", "cleanup_failed"} if closing else {"active", "disabled", "closing"})
                or not isinstance(meeting.driver.consumer, BrowserPlaybackConsumer)
            ):
                raise ValueError("Meeting playback is not owned by this connection or no longer active")
            if not closing and not self._runtime_matches(session_id, meeting):
                self._begin_close_locked(session_id, meeting)
                raise ValueError("Meeting playback runtime has ended")
            consumer = meeting.driver.consumer
        if not closing and not await self._driver_is_current(session_id, meeting):
            raise ValueError("Team meeting is no longer enabled")
        return consumer

    def publication_for_session(self, session_id: str, team_name: str) -> MeetingPublicationSession | None:
        """Resolve only the current native scope; ordinary sessions allocate nothing."""
        meeting = self._sessions.get(session_id)
        if meeting is None or meeting.publication is None:
            return None
        if (
            meeting.phase != "active" or not self._enabled()
            or not self._runtime_matches(session_id, meeting)
            or not meeting.backend.meeting_enabled
        ):
            meeting.publication.close()
            if meeting.driver is not None:
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
                if meeting.publication.voices is not None:
                    raise ValueError("Meeting publication already has a different voice registry")
                meeting.publication.voices = voices
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
        """Attach audio to the already running Team queue consumer."""
        from .playback import BrowserPlaybackConsumer

        async with self._lock:
            meeting = self._require_publication_locked(session_id, meeting_id)
            if not await self._refresh_enabled_locked(session_id, meeting):
                raise ValueError("Team meeting is no longer enabled")
            if meeting.driver is not None and meeting.driver.consumer is not None:
                if meeting.driver.consumer is not consumer or meeting.driver.state.phase in {"closing", "closed"}:
                    raise ValueError("Meeting grant driver already has a consumer")
                if voices is not None and meeting.publication.voices is not voices:
                    raise ValueError("Meeting grant driver already has a different voice registry")
                return meeting.driver
            assert meeting.coordinator is not None
            state = meeting.state
            driver = meeting.driver
            if driver is None:
                raise ValueError("The Team queue consumer has not started")
            if state.interruption_id:
                raise ValueError("Resolve the retained interruption before attaching playback")
            await self._enable_publication_locked(
                session_id, meeting, driver.wake, voices,
                on_addressed_members=getattr(consumer, "addressed_members_changed", None),
                on_interrupt=lambda member, token: driver.interrupt(token, leader=member),
            )
            meeting.driver = driver
            # Bind the browser boundary before the worker can offer its first
            # SDK grant, including work queued before playback became ready.
            if isinstance(consumer, BrowserPlaybackConsumer):
                consumer.driver = driver
            await driver.attach_consumer(consumer)
            return driver

    async def prepare_human_input(self, session_id: str, meeting_id: str, owner_id: str, text: str) -> dict:
        from .interruption import is_human_interruption

        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise ValueError("Meeting transcript must contain 1-16000 characters")
        consumer = await self.browser_playback(session_id, meeting_id, owner_id)
        driver = consumer.driver
        if not driver.audio_enabled:
            return {}
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

    async def _refresh_enabled_locked(self, session_id: str, meeting: _Meeting) -> bool:
        """Reconcile SDK-owned lifecycle at playback boundaries, without polling."""
        assert meeting.coordinator is not None
        state = await meeting.coordinator.get_state()
        if state is None or state.closed:
            return False
        meeting.state = state
        if meeting.driver is not None:
            meeting.driver.set_audio_enabled(state.enabled and self._enabled())
        if not state.enabled and meeting.publication is not None:
            meeting.publication.close()
        return state.enabled

    async def _driver_is_current(self, session_id: str, meeting: _Meeting) -> bool:
        async with self._lock:
            if self._sessions.get(session_id) is not meeting or meeting.retiring or meeting.phase in {"pending", "starting"}:
                return False
            if not self._runtime_matches(session_id, meeting):
                return False
            state = await meeting.coordinator.get_state()
            if state is None or state.closed:
                return False
            meeting.state = state
            meeting.driver.set_audio_enabled(state.enabled and self._enabled() and meeting.phase == "active")
            if not state.enabled and meeting.publication is not None:
                meeting.publication.close()
            return True

    async def _owned_driver(self, session_id: str, meeting_id: str, owner_id: str) -> MeetingGrantDriver | None:
        async with self._lock:
            meeting = self._sessions.get(session_id)
            if (
                not owner_id or meeting is None or meeting.phase not in {"active", "disabled", "closing"} or meeting.state is None
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
            or backend.group_session_id != session_id
        ):
            raise RuntimeError(
                "An active Team is required to start a meeting"
            )
        return team_name, backend

    def _runtime_matches(self, session_id: str, meeting: _Meeting) -> bool:
        try:
            _, backend = self._team_backend(session_id)
        except RuntimeError:
            return False
        return backend is meeting.backend

    def team_runtime_ready(self, session_id: str) -> None:
        """Register queue discovery once per Team activation, without creating a queue."""
        if not self._stopping:
            self._stopped_sessions.discard(session_id)
            meeting = self._sessions.get(session_id)
            if meeting is not None and meeting.phase == "pending":
                meeting.ready.set()
            self._spawn(self.attach_team_runtime(session_id))

    async def attach_team_runtime(self, session_id: str, *, wake: bool = False) -> None:
        from .grant_driver import MeetingGrantDriver

        async with self._runtime_lock:
            if self._stopping or session_id in self._stopped_sessions or not self._team_manager.is_runtime_active(session_id):
                return
            _, backend = self._team_backend(session_id)
            agent = self._team_manager.get_team_agent(session_id)
            listener = self._runtime_listeners.get(session_id)
            if listener is None or listener[0] is not agent:
                if listener is not None:
                    listener[0].remove_event_listener(listener[1])

                async def on_event(event) -> None:
                    if event.payload.get("team_name") != backend.team_name:
                        return
                    if event.event_type == TeamEvent.MEETING_QUEUE_CHANGED:
                        current = self._sessions.get(session_id)
                        if current is not None and current.driver is not None and current.driver.started:
                            current.driver.wake()
                        else:
                            self._spawn(self.attach_team_runtime(session_id, wake=True))
                    elif event.event_type == TeamEvent.MEETING_STATE_CHANGED:
                        self._spawn(self.attach_team_runtime(session_id))

                agent.add_event_listener(on_event)
                self._runtime_listeners[session_id] = (agent, on_event)
            # Listen before the queue exists, so its first admission can create
            # and wake the worker without a browser or a global runtime scan.
            queue_topic = TeamTopic.MEETING_QUEUE.build(session_id, backend.team_name)
            topic = self._queue_topics.get(session_id)
            if topic != (backend.messager, queue_topic):
                if topic is not None:
                    await topic[0].unsubscribe(topic[1])
                await backend.messager.subscribe(queue_topic, self._runtime_listeners[session_id][1])
                self._queue_topics[session_id] = (backend.messager, queue_topic)
            coordinator = await backend.get_meeting()
            if coordinator is None:
                old = self._sessions.get(session_id)
                if old is not None and old.driver is not None:
                    if old.publication is not None:
                        old.publication.close()
                    await old.driver.aclose()
                    async with self._lock:
                        if self._sessions.get(session_id) is old:
                            self._sessions.pop(session_id)
                return
            state = await coordinator.get_state()
            if state is None or state.closed:
                return
            old = self._sessions.get(session_id)
            if old is not None and old.coordinator is not None and (
                old.backend is not backend or old.coordinator.scope != coordinator.scope
            ):
                if old.publication is not None:
                    old.publication.close()
                if old.driver is not None:
                    await old.driver.aclose()
                async with self._lock:
                    if self._sessions.get(session_id) is old:
                        self._sessions.pop(session_id)
            async with self._lock:
                meeting = self._sessions.get(session_id)
                if meeting is None:
                    meeting = _Meeting(None, "active" if state.enabled else "disabled")
                    meeting.backend, meeting.coordinator, meeting.state = backend, coordinator, state
                    self._sessions[session_id] = meeting
                if meeting.backend is not backend or meeting.coordinator is None:
                    return
                meeting.state = state
                if meeting.driver is None:
                    meeting.driver = MeetingGrantDriver(
                        coordinator, is_current=lambda: self._driver_is_current(session_id, meeting),
                    )
                    if state.owner_request_id:
                        request = await coordinator.get_request(state.owner_request_id)
                        if request is not None:
                            meeting.driver.recover_publication(request)
                            wake = True
                if not meeting.driver.started and not state.owner_request_id:
                    # Start on actual queued work, not an empty enable/disable
                    # notification. This also recovers backlog on runtime reload.
                    wake = bool(await coordinator.list_pending())
                meeting.driver.set_audio_enabled(state.enabled and self._enabled() and meeting.phase == "active")
                if state.enabled and meeting.phase == "disabled":
                    meeting.phase = "active"
                    meeting.disable_on_cleanup = True
                    meeting.release_playback = False
                    meeting.driver.set_audio_enabled(self._enabled())
                consumer = meeting.driver.consumer
                if state.enabled and meeting.phase == "active" and consumer is not None and meeting.publication is None:
                    await self._enable_publication_locked(
                        session_id, meeting, meeting.driver.wake,
                        voices=getattr(consumer, "voices", None),
                        on_addressed_members=getattr(consumer, "addressed_members_changed", None),
                        on_interrupt=lambda member, token: meeting.driver.interrupt(token, leader=member),
                    )
                if not state.enabled and meeting.phase == "active":
                    meeting.disable_on_cleanup = False
                    self._begin_close_locked(session_id, meeting)
                if wake:
                    meeting.driver.wake()

    async def stop_team_runtime(self, session_id: str) -> None:
        """Stop and await the queue worker before its Team backend is torn down."""
        self._stopped_sessions.add(session_id)
        async with self._runtime_lock:
            listener = self._runtime_listeners.pop(session_id, None)
            if listener is not None:
                listener[0].remove_event_listener(listener[1])
            topic = self._queue_topics.pop(session_id, None)
            if topic is not None:
                await topic[0].unsubscribe(topic[1])
            async with self._lock:
                meeting = self._sessions.get(session_id)
                if meeting is None:
                    return
                if meeting.publication is not None:
                    meeting.publication.close()
                meeting.retiring = True
                pending = [task for task in (meeting.initializing, meeting.task) if task is not None and not task.done()]
                for task in pending:
                    task.cancel()
                driver = meeting.driver
                if driver is not None:
                    driver.close()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if driver is not None:
                await driver.aclose()
            async with self._lock:
                if self._sessions.get(session_id) is meeting:
                    self._sessions.pop(session_id)

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
            if meeting.phase == "disabled":
                meeting.phase = "starting"
                meeting.abandoned = False
                meeting.disable_on_cleanup = True
                meeting.release_playback = False
                meeting.task = self._spawn(self._run_start(session_id, meeting))
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
        _, backend = self._team_backend(session_id)
        meeting.backend = backend
        meeting.coordinator = await backend.enable_meeting()
        state = await meeting.coordinator.get_state()
        if state is None or not state.enabled:
            raise RuntimeError("Team meeting did not become active")
        return state

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
            if activated:
                await self.attach_team_runtime(session_id)
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
            if not activated and not meeting.retiring:
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
            meeting.driver.set_audio_enabled(False)
        if meeting.backend is None:
            self._sessions.pop(session_id)
            return None
        meeting.task = self._spawn(self._close(session_id, meeting))
        return meeting.task

    async def _close(self, session_id: str, meeting: _Meeting) -> None:
        try:
            # Enable may persist a row before its state-change notification fails.
            coordinator = meeting.coordinator or await meeting.backend.get_meeting()
            if coordinator is not None and meeting.disable_on_cleanup:
                disabled = await meeting.backend.disable_meeting(meeting_id=coordinator.scope.meeting_id)
                if not disabled:
                    state = await coordinator.get_state()
                    if state is not None and state.enabled:
                        raise RuntimeError("Team meeting disable did not finish; retry cleanup")
            if meeting.release_playback and meeting.driver is not None:
                await meeting.driver.detach_consumer()
        except BaseException:
            async with self._lock:
                meeting.phase = "cleanup_failed"
            raise
        async with self._lock:
            if self._sessions.get(session_id) is meeting:
                meeting.phase = "disabled"
                if meeting.release_playback:
                    meeting.owner_id = None
                    meeting.startup_id = None
                meeting.publication = None

    def _release_locked(
        self, session_id: str, meeting: _Meeting
    ) -> asyncio.Task | None:
        meeting.release_playback = True
        if meeting.phase in ("pending", "starting"):
            meeting.abandoned = True
            meeting.phase = "closing"
            meeting.ready.set()
            if meeting.initializing is not None:
                meeting.initializing.cancel()
            return meeting.task
        if meeting.phase == "closing":
            if meeting.driver is not None:
                self._spawn(meeting.driver.detach_consumer())
            return meeting.task
        return self._begin_close_locked(session_id, meeting)

    async def release(
        self, session_id: str, owner_id: str, startup_id: str | None = None
    ) -> bool:
        """Connection/page teardown: stop media and disable the Team meeting."""
        if not owner_id:
            return False
        async with self._lock:
            meeting = self._sessions.get(session_id)
            if meeting is None:
                return True
            if meeting.owner_id is None and (meeting.driver is None or meeting.driver.consumer is None):
                return True
            if meeting.owner_id != owner_id:
                return False
            if startup_id is not None and meeting.startup_id != startup_id:
                return False
            task = self._release_locked(session_id, meeting)
        if task is not None:
            await asyncio.shield(task)
        return True

    async def end(
        self, session_id: str, meeting_id: str, owner_id: str | None = None
    ) -> bool:
        """Disable a matching meeting; re-enabling reuses its persisted identity."""
        async with self._lock:
            meeting = self._sessions.get(session_id)
            if (
                meeting is None
                or meeting.state is None
                or meeting.phase not in ("active", "disabled", "closing", "cleanup_failed")
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
        # A stopped Team has no chat stream left to correct a stale busy flag.
        # Reconcile only this ended meeting; an active/resuming Team owns its
        # own processing status and must not be marked idle here.
        if (
            not self._team_manager.is_runtime_active(session_id)
            and not self._team_manager.is_runtime_pending(session_id)
        ):
            from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

            await AgentWebSocketServer.get_instance().send_push({
                "request_id": "",
                "channel_id": "web",
                "session_id": session_id,
                "payload": {
                    "event_type": "chat.processing_status",
                    "session_id": session_id,
                    "is_processing": False,
                    "is_complete": True,
                },
                "is_complete": False,
            })
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
                    "state": "starting" if meeting.phase == "pending" else "idle" if meeting.phase == "disabled" else meeting.phase
                }
            coordinator = meeting.coordinator
        assert coordinator is not None
        state = await coordinator.get_state()
        async with self._lock:
            if self._sessions.get(session_id) is not meeting:
                return {"state": "idle"}
            if meeting.phase != "active":
                return {"state": meeting.phase}
            if state is None or not state.enabled:
                meeting.disable_on_cleanup = False
                if meeting.publication is not None:
                    meeting.publication.close()
                if meeting.driver is not None:
                    self._begin_close_locked(session_id, meeting)
                    return {"state": "closing"}
                meeting.phase = "disabled"
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
        self._stopping = True
        for session_id in list(set(self._sessions) | set(self._runtime_listeners)):
            await self.stop_team_runtime(session_id)
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


async def stop_meeting_runtime(session_id: str) -> None:
    """Teardown an existing runtime without creating the global service."""
    if _server_manager is not None:
        await _server_manager.stop_team_runtime(session_id)


async def close_server_meeting_manager() -> None:
    global _server_manager
    if _server_manager is not None:
        await _server_manager.aclose()
        _server_manager = None
