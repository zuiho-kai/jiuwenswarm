"""Event-driven SDK grants; audio transport belongs to an injected consumer."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from openjiuwen.agent_teams.meeting import MeetingCoordinator
from openjiuwen.agent_teams.schema.meeting import MeetingScope, SpeechInterruption, SpeechRequest

logger = logging.getLogger(__name__)


class MeetingPlaybackConsumer(Protocol):
    """Trusted host boundary. Returning from speech_ready is NOT completion.

    The consumer must stop all matching work before stop returns. It must not
    wait for the driver to shut down from inside one of its own callbacks.
    """

    async def speech_ready(self, request: SpeechRequest) -> None: ...

    async def speech_error(self, request: SpeechRequest | None, error: str) -> None: ...

    async def stop(self) -> None: ...

    async def interrupt(self, interruption: SpeechInterruption) -> None: ...


@dataclass(frozen=True)
class GrantDriverState:
    phase: Literal[
        "idle", "publishing", "awaiting_playback", "publication_failed",
        "consumer_failed", "playback_failed", "coordination_failed", "closing", "closed",
        "interrupting", "interruption_failed",
    ] = "idle"
    request: SpeechRequest | None = None
    error: str | None = None


class MeetingGrantDriver:
    """One serialized worker per meeting, woken by admission and explicit acks.

    SDK methods alone choose the next speaker and publish full text. No timer,
    synthesis result, or consumer callback return can release a speaking lease.
    Publication failures and explicit audio stops hold coordination. Confirmed
    failed audio revokes its lease and advances. Interruption waits
    for audio stop and human input acceptance, then resumes the SDK queue.
    """

    def __init__(
        self, coordinator: MeetingCoordinator, consumer: MeetingPlaybackConsumer, *,
        is_current: Callable[[], Awaitable[bool]],
    ) -> None:
        if any(not callable(getattr(consumer, name, None)) for name in ("speech_ready", "speech_error", "stop")):
            raise ValueError("A playback consumer with ready, error, and stop callbacks is required")
        self.coordinator = coordinator
        self.consumer = consumer
        self._is_current = is_current
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._stopped = False
        self._retry_requested = False
        self._state = GrantDriverState()
        self._interruption: SpeechInterruption | None = None
        self._audio_stopped = False
        self._input_admitted = False
        self._finished_interruptions: set[str] = set()

    @property
    def state(self) -> GrantDriverState:
        return self._state

    @property
    def pending_interruption(self) -> SpeechInterruption | None:
        return self._interruption

    def start(self) -> None:
        if self._closed:
            raise ValueError("This grant driver has ended")
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"meeting-grants-{self.coordinator.scope.meeting_id}")
        self.wake()

    def wake(self) -> None:
        if not self._closed:
            self._wake.set()

    def close(self) -> None:
        """Revoke immediately; aclose drains work before the manager closes the DB scope."""
        if self._closed:
            return
        self._closed = True
        self._state = GrantDriverState("closing", self._state.request)
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()

    async def aclose(self) -> None:
        self.close()
        if self._task is not None and self._task is not asyncio.current_task():
            await asyncio.gather(self._task, return_exceptions=True)
        # Drain a completion already in progress before stopping playback and
        # letting the manager close the coordinator/database resources.
        async with self._lock:
            if not self._stopped:
                await self.consumer.stop()
                self._stopped = True
            self._state = GrantDriverState("closed", self._state.request, "meeting_closed")

    def _matches(self, scope: MeetingScope, request_id: str, lease_token: str) -> bool:
        request = self._state.request
        return bool(
            not self._closed and scope == self.coordinator.scope and request is not None
            and request.request_id == request_id and lease_token and request.lease_token == lease_token
        )

    async def complete_playback(self, scope: MeetingScope, request_id: str, lease_token: str) -> bool:
        if not await self._is_current():
            return False
        async with self._lock:
            if not self._matches(scope, request_id, lease_token) or self._state.phase != "awaiting_playback":
                return False
            if not await self.coordinator.complete_playback(lease_token):
                return False
            if not self._closed:
                self._state = GrantDriverState()
                self.wake()
            return True

    async def fail_playback(
        self, scope: MeetingScope, request_id: str, lease_token: str, *, advance: bool = False,
    ) -> bool:
        if not await self._is_current():
            return False
        async with self._lock:
            if not self._matches(scope, request_id, lease_token) or self._state.phase != "awaiting_playback":
                return False
            if advance:
                # The owner reported failed audio and Gateway drained synthesis.
                # Revoke as interrupted, never falsely acknowledge it as played.
                interruption = await self.coordinator.interrupt_speech(expected_token=lease_token)
                if interruption is None:
                    return False
                if not await self.coordinator.resume_after_interruption(interruption.interruption_id):
                    raise RuntimeError("Failed speech interruption could not be released")
                logger.warning("Skipping failed meeting audio request=%s", request_id)
                self._state = GrantDriverState()
                self.wake()
                return True
            self._state = GrantDriverState("playback_failed", self._state.request, "playback_failed")
        # No complete_playback call: failed/stopped audio is never reported as played.
        await self._report_error()
        return True

    async def retry_publication(self, scope: MeetingScope, request_id: str, lease_token: str) -> bool:
        if not await self._is_current():
            return False
        async with self._lock:
            if not self._matches(scope, request_id, lease_token) or self._state.phase != "publication_failed":
                return False
            self._retry_requested = True
            self.wake()
            return True

    async def interrupt(
        self, expected_token: str, *, leader: str | None = None, human: bool = False,
    ) -> SpeechInterruption | None:
        """Revoke exactly the observed lease; grants wait for real audio/input acks."""
        if not await self._is_current():
            return None
        async with self._lock:
            request = self._state.request
            if (
                self._closed or request is None or request.lease_token != expected_token
                or self._interruption is not None or self._state.phase not in {"awaiting_playback", "publication_failed"}
            ):
                return None
            interruption = (
                await self.coordinator.interrupt_from_leader(leader, expected_token=expected_token)
                if leader is not None else
                await self.coordinator.interrupt_speech(expected_token=expected_token)
            )
            if interruption is None:
                return None
            self._interruption = interruption
            self._audio_stopped = False
            self._input_admitted = not human
            self._state = GrantDriverState("interrupting", request)
        # Stop acknowledgements are handled independently of the driver lock.
        # The SDK's durable interruption barrier blocks all new grants meanwhile.
        try:
            await self.consumer.interrupt(interruption)
        except Exception:
            logger.exception("Meeting speech stop was not confirmed")
            async with self._lock:
                if not self._closed:
                    self._state = GrantDriverState("interruption_failed", request, "interruption_stop_failed")
            await self._report_error()
            raise RuntimeError("Meeting speech stop was not confirmed; end the meeting to recover") from None
        async with self._lock:
            self._audio_stopped = True
            await self._resume_interruption()
        return interruption

    async def input_admitted(self, interruption_id: str) -> bool:
        async with self._lock:
            if self._closed:
                return False
            if interruption_id in self._finished_interruptions:
                return True
            if self._interruption is None or self._interruption.interruption_id != interruption_id:
                return False
            self._input_admitted = True
            await self._resume_interruption()
            return True

    async def _resume_interruption(self) -> None:
        if self._closed or not self._audio_stopped or not self._input_admitted:
            return
        interruption_id = self._interruption.interruption_id
        if not await self.coordinator.resume_after_interruption(interruption_id):
            raise RuntimeError("Meeting interruption barrier is no longer current")
        self._finished_interruptions.add(interruption_id)
        self._interruption = None
        self._state = GrantDriverState()
        self.wake()

    async def _report_error(self) -> None:
        if self._closed:
            return
        try:
            await self.consumer.speech_error(self._state.request, self._state.error or "coordination_failed")
        except Exception:
            logger.exception("Meeting consumer could not receive error for %s", self.coordinator.scope)

    async def _step(self) -> None:
        if not await self._is_current():
            return
        async with self._lock:
            if self._closed:
                return
            current = self._state
            retry = current.phase == "publication_failed" and self._retry_requested
            if current.phase != "idle" and not retry:
                return
            self._retry_requested = False
            self._state = GrantDriverState("publishing", current.request)
            if retry:
                assert current.request is not None and current.request.lease_token
                request = await self.coordinator.retry_publication(current.request.lease_token)
            else:
                request = await self.coordinator.grant_next()
            if self._closed:
                return
            if request is None:
                # A revoked retry is not a fresh admission. Do not pick another
                # speaker until its lifecycle/interruption owner resolves it.
                self._state = (
                    GrantDriverState("coordination_failed", current.request, "lease_unavailable")
                    if retry else GrantDriverState()
                )
            elif request.delivery_error or request.message_error or request.source_message_id is None:
                self._state = GrantDriverState(
                    "publication_failed", request,
                    request.delivery_error or request.message_error or "source_message_unavailable",
                )
            else:
                self._state = GrantDriverState("awaiting_playback", request)
        if self._state.error:
            await self._report_error()
        elif request is not None and not self._closed and await self._is_current():
            try:
                # Do not hold the lock across consumer code: a controlled consumer
                # may acknowledge actual completion within this callback.
                await self.consumer.speech_ready(request)
            except Exception:
                logger.exception("Meeting playback delivery failed for %s", request.request_id)
                failed = False
                async with self._lock:
                    if (
                        self._matches(request.scope, request.request_id, request.lease_token)
                        and self._state.phase == "awaiting_playback"
                    ):
                        self._state = GrantDriverState("consumer_failed", request, "consumer_delivery_failed")
                        failed = True
                if failed:
                    await self._report_error()

    async def _run(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._step()
            except Exception:
                logger.exception("Meeting grant coordination failed for %s", self.coordinator.scope)
                if not self._closed:
                    self._state = GrantDriverState("coordination_failed", self._state.request, "coordination_failed")
                    await self._report_error()
