"""Grant scheduling against real SDK/SQLite, with explicitly controlled playback."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.agent_teams.schema.meeting import SpeechRequestStatus
from openjiuwen.core.foundation.llm import AssistantMessage

from tests.unit_tests.agentserver.test_meeting_publication import (
    _post_call,
    meeting as meeting,
    native_factory as native_factory,
    publication_host as publication_host,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("meeting", ["file"], indirect=True)]


class _Consumer:
    def __init__(self):
        self.ready = asyncio.Queue()
        self.errors = asyncio.Queue()
        self.stops = 0
        self.fail_delivery = False
        self.fail_stop = False

    async def speech_ready(self, request):
        if self.fail_delivery:
            raise RuntimeError("consumer unavailable")
        self.ready.put_nowait(request)

    async def speech_error(self, request, error):
        self.errors.put_nowait((request, error))

    async def stop(self):
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("audio stop unconfirmed")


async def _attach(host, consumer=None):
    consumer = consumer or _Consumer()
    await host.manager.request_start("publication-session", "owner")
    driver = await host.manager.enable_grant_driver(
        "publication-session", host.state.scope.meeting_id, consumer=consumer,
    )
    return driver, consumer


async def _queue(driver, member="leader", content="Full result", mentions=("analyst",)):
    posted = await driver.coordinator.for_member(member).post_message(
        content, speech_text=f"Spoken {content}", mentions=mentions,
    )
    driver.wake()
    return posted.speech_request


async def _next(queue):
    return await asyncio.wait_for(queue.get(), 3)


async def _complete(host, request, **overrides):
    values = dict(
        session_id=request.scope.session_id, meeting_id=request.scope.meeting_id,
        request_id=request.request_id, lease_token=request.lease_token, owner_id="owner",
    )
    values.update(overrides)
    return await host.manager.complete_playback(**values)


async def test_native_posts_wake_driver_and_only_matching_completion_advances(
    meeting, publication_host, monkeypatch, native_factory,
):
    # Hold the SDK grant, so both native agents can finish tool work while their
    # posts remain unpublished. The host does not choose their queue order.
    allow_grant = asyncio.Event()
    coordinator = publication_host.manager._sessions["publication-session"].coordinator
    original = coordinator.grant_next

    async def held_grant():
        await allow_grant.wait()
        return await original()

    monkeypatch.setattr(coordinator, "grant_next", held_grant)
    driver, consumer = await _attach(publication_host)
    leader = await native_factory(participant=True)
    member = await native_factory("analyst", participant=True)
    leader.model.invoke.side_effect = [_post_call(content="Leader report"), AssistantMessage(content="")]
    member.model.invoke.side_effect = [_post_call(content="Member report", mentions=["leader"]), AssistantMessage(content="")]
    try:
        results = await asyncio.gather(*(
            native.agent.invoke({"query": "Report"}, session=native.session, _streaming=True)
            for native in (leader, member)
        ))
        assert all(result["meeting_queued"] for result in results)
        pending = await coordinator.list_pending()
        assert len(pending) == 2
        assert await meeting.db.message.get_team_messages("publication-team") == []
        assert await meeting.db.message.get_broadcast_messages("publication-team", "analyst") == []
        assert not meeting.conversation.history_path.exists()

        allow_grant.set()
        first = await _next(consumer.ready)
        assert first.request_id == pending[0].request_id
        assert first.source_message_id and not first.delivery_error
        assert len(await meeting.db.message.get_team_messages("publication-team")) == 1
        assert (await coordinator.get_speaker()).lease_token == first.lease_token
        assert len(await coordinator.list_pending()) == 1
        assert consumer.ready.empty()  # Returning from speech_ready did not complete.
        assert not await _complete(publication_host, first, lease_token="stale")
        assert not await _complete(publication_host, first, request_id="other")
        assert not await _complete(publication_host, first, owner_id="observer")
        assert not await _complete(publication_host, first, session_id="other-session")
        assert not await _complete(publication_host, first, meeting_id="old-meeting")
        assert not await driver.complete_playback(replace(first.scope, team_name="other"), first.request_id, first.lease_token)
        assert (await coordinator.get_speaker()).request_id == first.request_id

        assert await _complete(publication_host, first)
        second = await _next(consumer.ready)
        assert second.request_id == pending[1].request_id
        assert second.lease_token != first.lease_token
        assert not await _complete(publication_host, first)
        assert (await coordinator.get_speaker()).request_id == second.request_id
        assert await _complete(publication_host, second)
        assert await coordinator.get_speaker() is None
        assert len(await meeting.db.message.get_team_messages("publication-team")) == 2
        assert len(meeting.conversation.history_path.read_text(encoding="utf-8").splitlines()) == 2
    finally:
        allow_grant.set()


async def test_failed_publication_holds_lease_and_retry_reuses_committed_message(
    meeting, publication_host, monkeypatch,
):
    driver, consumer = await _attach(publication_host)
    # Fail durable queue linkage AFTER normal storage/history/notification has
    # committed. Recovery must find that source instead of creating another row.
    dao = meeting.db.meeting
    record = dao.record_publication
    monkeypatch.setattr(dao, "record_publication", AsyncMock(side_effect=OSError("link unavailable")))
    queued = await _queue(driver)
    request, error = await _next(consumer.errors)
    assert error == "message_publication_failed"
    assert request.request_id == queued.request_id and driver.state.phase == "publication_failed"
    assert consumer.ready.empty()
    stored, = await meeting.db.message.get_team_messages("publication-team")
    assert not await _complete(publication_host, request)  # Never play unpublished/uncertain text.
    next_queued = await _queue(driver, "analyst", "Next result", ("leader",))
    assert (await driver.coordinator.get_speaker()).lease_token == request.lease_token
    assert not await publication_host.manager.retry_publication(
        "publication-session", request.scope.meeting_id, request.request_id, request.lease_token, owner_id="observer",
    )

    monkeypatch.setattr(dao, "record_publication", record)
    assert await publication_host.manager.retry_publication(
        "publication-session", request.scope.meeting_id, request.request_id, request.lease_token, owner_id="owner",
    )
    recovered = await _next(consumer.ready)
    assert (recovered.request_id, recovered.lease_token) == (request.request_id, request.lease_token)
    assert recovered.source_message_id == stored.message_id
    assert len(await meeting.db.message.get_team_messages("publication-team")) == 1
    assert len(meeting.conversation.history_path.read_text(encoding="utf-8").splitlines()) == 1
    assert await _complete(publication_host, recovered)
    assert (await _next(consumer.ready)).request_id == next_queued.request_id


@pytest.mark.parametrize("failure", ["consumer", "playback"])
async def test_audio_delivery_or_playback_failure_never_releases_the_lease(publication_host, failure):
    consumer = _Consumer()
    consumer.fail_delivery = failure == "consumer"
    driver, _ = await _attach(publication_host, consumer)
    first = await _queue(driver)
    if failure == "playback":
        granted = await _next(consumer.ready)
        assert not await publication_host.manager.fail_playback(
            "publication-session", granted.scope.meeting_id, granted.request_id, granted.lease_token, owner_id="observer",
        )
        assert await publication_host.manager.fail_playback(
            "publication-session", granted.scope.meeting_id, granted.request_id, granted.lease_token, owner_id="owner",
        )
    request, error = await _next(consumer.errors)
    assert error == ("consumer_delivery_failed" if failure == "consumer" else "playback_failed")
    assert (await driver.coordinator.get_speaker()).request_id == first.request_id
    await _queue(driver, "analyst", "Waiting result", ("leader",))
    assert not await _complete(publication_host, request)
    assert len(await driver.coordinator.list_pending()) == 1
    assert consumer.ready.empty()


@pytest.mark.parametrize("closure", ["end", "release", "shutdown", "feature_off", "runtime_replaced"])
async def test_closure_stops_consumer_cancels_unsent_work_and_preserves_history(
    meeting, publication_host, closure,
):
    driver, consumer = await _attach(publication_host)
    await _queue(driver)
    current = await _next(consumer.ready)
    pending = await _queue(driver, "analyst", "Unsent", ("leader",))
    if closure == "end":
        assert await publication_host.manager.end("publication-session", current.scope.meeting_id, owner_id="owner")
    elif closure == "release":
        assert await publication_host.manager.release("publication-session", "owner")
    elif closure == "shutdown":
        await publication_host.manager.aclose()
    else:
        if closure == "feature_off":
            publication_host.flags.enabled = False
        else:
            publication_host.flags.active = False
        # Invalidity is detected on an operation, never by active polling.
        assert not await _complete(publication_host, current)
    async with asyncio.timeout(3):
        while consumer.stops == 0 or publication_host.manager._sessions:
            await asyncio.sleep(0)
    assert driver.state.phase == "closed"
    assert driver._task.done()
    assert (await driver.coordinator.get_request(current.request_id)).status == SpeechRequestStatus.INTERRUPTED
    assert (await driver.coordinator.get_request(pending.request_id)).status == SpeechRequestStatus.CANCELLED
    assert len(await meeting.db.message.get_team_messages("publication-team")) == 1
    assert len(meeting.conversation.history_path.read_text(encoding="utf-8").splitlines()) == 1
    assert not await _complete(publication_host, current)
    assert consumer.ready.empty()


async def test_end_during_publication_does_not_offer_revoked_audio(meeting, publication_host, monkeypatch):
    driver, consumer = await _attach(publication_host)
    committed = asyncio.Event()
    original = driver.coordinator._send_message

    async def delayed_send(*args):
        result = await original(*args)
        committed.set()
        await asyncio.Event().wait()
        return result

    monkeypatch.setattr(driver.coordinator, "_send_message", delayed_send)
    queued = await _queue(driver)
    await asyncio.wait_for(committed.wait(), 3)
    assert await publication_host.manager.end("publication-session", driver.coordinator.scope.meeting_id)
    assert consumer.ready.empty() and consumer.stops == 1
    assert (await driver.coordinator.get_request(queued.request_id)).status == SpeechRequestStatus.INTERRUPTED
    assert len(await meeting.db.message.get_team_messages("publication-team")) == 1
    assert driver._task.done()


async def test_failed_consumer_stop_retains_cleanup_for_retry(publication_host):
    driver, consumer = await _attach(publication_host)
    await _queue(driver)
    current = await _next(consumer.ready)
    consumer.fail_stop = True
    with pytest.raises(RuntimeError, match="stop unconfirmed"):
        await publication_host.manager.end("publication-session", current.scope.meeting_id)
    assert await publication_host.manager.lifecycle_status("publication-session") == {"state": "cleanup_failed"}
    assert not (await driver.coordinator.get_state()).closed
    assert not await _complete(publication_host, current)
    consumer.fail_stop = False
    # Existing lifecycle retries cleanup on the next startup request.
    with pytest.raises(RuntimeError, match="cleanup is being retried"):
        await publication_host.manager.start("publication-session")
    async with asyncio.timeout(3):
        while publication_host.manager._sessions:
            await asyncio.sleep(0)
    assert consumer.stops == 2 and (await driver.coordinator.get_state()).closed


async def test_driver_attachment_is_internal_and_has_one_consumer(publication_host):
    with pytest.raises(ValueError, match="matching active"):
        await publication_host.manager.enable_grant_driver("publication-session", "stale", consumer=_Consumer())
    with pytest.raises(ValueError, match="playback consumer"):
        await publication_host.manager.enable_grant_driver("publication-session", publication_host.state.scope.meeting_id, consumer=object())
    driver, consumer = await _attach(publication_host)
    assert await publication_host.manager.enable_grant_driver(
        "publication-session", publication_host.state.scope.meeting_id, consumer=consumer,
    ) is driver
    with pytest.raises(ValueError, match="already has a consumer"):
        await publication_host.manager.enable_grant_driver("publication-session", publication_host.state.scope.meeting_id, consumer=_Consumer())


async def test_consumer_can_complete_inline_without_deadlocking(publication_host):
    class _InlineConsumer(_Consumer):
        async def speech_ready(self, request):
            assert await _complete(publication_host, request)
            await super().speech_ready(request)

    driver, consumer = await _attach(publication_host, _InlineConsumer())
    await _queue(driver)
    first = await _next(consumer.ready)
    assert (await driver.coordinator.get_request(first.request_id)).status == SpeechRequestStatus.COMPLETED
    assert await driver.coordinator.get_speaker() is None


@pytest.mark.parametrize("existing", ["lease", "interruption", "closed"])
async def test_driver_does_not_resume_old_coordination_state(publication_host, existing):
    coordinator = publication_host.manager._sessions["publication-session"].coordinator
    if existing == "lease":
        await coordinator.for_member("leader").post_message("Old result", speech_text="Old speech")
        assert await coordinator.grant_next() is not None
    elif existing == "interruption":
        assert await coordinator.interrupt_speech() is not None
    else:
        await coordinator.close()
    with pytest.raises(ValueError, match="without an existing lease or interruption"):
        await _attach(publication_host)
    assert publication_host.manager._sessions["publication-session"].publication is None


async def test_separate_session_drivers_cannot_release_each_others_grants(meeting, publication_host):
    from openjiuwen.agent_teams.group_chat.conversation import GroupConversationLog
    from openjiuwen.agent_teams.meeting import MeetingCoordinator, MeetingScope
    from jiuwenswarm.extensions.meeting.grant_driver import MeetingGrantDriver

    driver, consumer = await _attach(publication_host)
    await _queue(driver)
    first = await _next(consumer.ready)
    other_coordinator = MeetingCoordinator(
        meeting.db, MeetingScope("other-session", "publication-team", "other-meeting"),
        message_manager=meeting.manager,
        conversation=GroupConversationLog("publication-team", "other-session"),
    )
    await other_coordinator.start()

    async def current():
        return True

    other_consumer = _Consumer()
    other_driver = MeetingGrantDriver(other_coordinator, other_consumer, is_current=current)
    try:
        other_driver.start()
        await _queue(other_driver, content="Other session result")
        second = await _next(other_consumer.ready)
        assert first.lease_token != second.lease_token
        assert not await other_driver.complete_playback(first.scope, first.request_id, first.lease_token)
        assert not await _complete(publication_host, second)
        assert await _complete(publication_host, first)
        assert (await other_coordinator.get_speaker()).request_id == second.request_id
        assert await other_driver.complete_playback(second.scope, second.request_id, second.lease_token)
        assert await other_coordinator.get_speaker() is None
    finally:
        await other_driver.aclose()
        await other_coordinator.close()


async def test_unexpected_coordination_error_is_reported_without_regranting(publication_host, monkeypatch):
    coordinator = publication_host.manager._sessions["publication-session"].coordinator
    failed_grant = AsyncMock(side_effect=OSError("database unavailable"))
    monkeypatch.setattr(coordinator, "grant_next", failed_grant)
    driver, consumer = await _attach(publication_host)
    request, error = await _next(consumer.errors)
    assert request is None and error == "coordination_failed"
    assert driver.state.phase == "coordination_failed"
    await _queue(driver)
    # The error state cannot turn new queue admissions into repeated claims.
    assert failed_grant.await_count == 1
    assert consumer.ready.empty()


async def test_scope_invalidation_drains_a_blocked_consumer_callback(publication_host):
    cancelled = asyncio.Event()

    class _BlockedConsumer(_Consumer):
        async def speech_ready(self, request):
            await super().speech_ready(request)
            try:
                await asyncio.Event().wait()
            finally:
                # Yield during unwind to prove repeated close() calls do not
                # inject a second cancellation into consumer/SQL cleanup.
                await asyncio.sleep(0)
                cancelled.set()

    driver, consumer = await _attach(publication_host, _BlockedConsumer())
    await _queue(driver)
    await _next(consumer.ready)
    publication_host.flags.enabled = False
    assert publication_host.manager.publication_for_session("publication-session", "publication-team") is None
    async with asyncio.timeout(3):
        await cancelled.wait()
        while publication_host.manager._sessions:
            await asyncio.sleep(0)
    assert consumer.stops == 1 and driver.state.phase == "closed"
    assert (await driver.coordinator.get_state()).closed
