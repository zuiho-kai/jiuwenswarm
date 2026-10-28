"""Grant scheduling against real SDK/SQLite, with explicitly controlled playback."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

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
