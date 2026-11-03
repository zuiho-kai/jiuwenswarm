"""Real SDK interruption barriers through Gateway and scoped native tools."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.meeting import gateway_input
from jiuwenswarm.extensions.meeting.publication import create_meeting_publication_rail
from tests.unit_tests.agentserver.test_meeting_browser_playback import (
    activate, browser as browser, identity, next_speech, queue,
)
from tests.unit_tests.agentserver.test_meeting_publication import (
    meeting as meeting, publication_host as publication_host,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("meeting"), pytest.mark.parametrize("meeting", ["file"], indirect=True)]


async def test_human_interruption_waits_for_input_acceptance_then_continues(browser, monkeypatch):
    driver, consumer = await activate(browser)
    await queue(browser, driver, "analyst")
    first = await next_speech(browser)
    await queue(browser, driver, "leader", "Next speaker")
    monkeypatch.setattr(gateway_input, "transcribe_utterance", AsyncMock(return_value="Wait, use last year's figures"))
    route = browser.ws._meeting_playback_routes[(browser.base["session_id"], browser.base["meeting_id"])]
    route.input.enqueue([b"pcm"])
    await asyncio.wait_for(route.input.queue.join(), 3)
    transcript = next(p for _, event, p in browser.channel.events if event == "meeting.input.transcript")
    stop_id = transcript["interruption_id"]
    assert driver.state.phase == "interrupting"
    assert (await driver.coordinator.get_state()).interruption_id == stop_id
    assert (await driver.coordinator.get_request(first["request_id"])).status == "interrupted"
    assert browser.channel.ready.empty()
    assert not route.revoked and not route.input.closed and route.input.listening
    assert (await driver.coordinator.get_source_message(first["source_message_id"])).content
    assert not (await browser.call("meeting.speech.synthesize", {**identity(first), "chunk_index": 0}))["ok"]
    # Simulate acceptance by the existing group_chat delivery before its receipt.
    await driver.coordinator.post_human_message(transcript["transcript"], mentions=["leader"])
    admission = {"stream_id": route.input.id, "utterance_id": transcript["utterance_id"], "interruption_id": stop_id}
    assert not (await browser.call("meeting.input.admitted", {**admission, "interruption_id": "foreign"}))["ok"]
    assert (await browser.call("meeting.input.admitted", admission))["ok"]
    second = await next_speech(browser)
    assert second["speaker_id"] == "leader"
    assert (await browser.call("meeting.input.admitted", admission))["ok"]
    assert (await browser.call("meeting.speech.synthesize", {**identity(second), "chunk_index": 0}))["ok"]
    assert not (await browser.call("meeting.playback.result", {
        **identity(first), "outcome": "completed", "completed_chunks": 1,
    }))["ok"]
    assert (await driver.coordinator.get_speaker()).lease_token == second["lease_token"]


async def test_stale_confirmed_speech_does_not_interrupt_new_speaker(browser):
    driver, _ = await activate(browser)
    await queue(browser, driver, "analyst")
    first = await next_speech(browser)
    await queue(browser, driver, "leader")
    await browser.call("meeting.speech.synthesize", {**identity(first), "chunk_index": 0})
    assert (await browser.call("meeting.playback.result", {
        **identity(first), "outcome": "completed", "completed_chunks": 1,
    }))["ok"]
    second = await next_speech(browser)
    args = (browser.base["session_id"], browser.base["meeting_id"], browser.ws._meeting_owner_id)
    assert await browser.host.manager.prepare_human_input(
        *args, "", confirmed_speech=True, expected_token=first["lease_token"],
    ) == {}
    assert (await driver.coordinator.get_speaker()).lease_token == second["lease_token"]


async def test_leader_tool_stops_audio_preserves_text_and_advances(browser):
    driver, _ = await activate(browser)
    await queue(browser, driver, "analyst")
    first = await next_speech(browser)
    await queue(browser, driver, "leader")
    session = browser.host.manager._sessions[browser.base["session_id"]].publication
    leader = await create_meeting_publication_rail(driver.coordinator, "leader", on_interrupt=session.on_interrupt)
    member = await create_meeting_publication_rail(driver.coordinator, "analyst", on_interrupt=session.on_interrupt)
    assert "meeting_interrupt_speech" not in member._tools
    tool = leader._tools["meeting_interrupt_speech"]
    assert not (await tool.invoke({"expected_token": first["lease_token"], "member_name": "leader"})).success
    result = await tool.invoke({"expected_token": first["lease_token"]})
    assert result.success and result.data["lease_token"] == first["lease_token"]
    second = await next_speech(browser)
    assert second["speaker_id"] == "leader"
    assert (await driver.coordinator.get_source_message(first["source_message_id"])).content
    assert (await driver.coordinator.get_state()).interruption_id is None
    assert (await tool.invoke({"expected_token": first["lease_token"]})).data is None
    assert (await driver.coordinator.get_speaker()).lease_token == second["lease_token"]
    leader.active = False
    assert not (await tool.invoke({"expected_token": second["lease_token"]})).success


async def test_unconfirmed_audio_stop_holds_queue_and_barrier(browser):
    driver, consumer = await activate(browser)
    await queue(browser, driver, "analyst")
    first = await next_speech(browser)
    await queue(browser, driver, "leader")
    browser.channel.auto_ack = False
    consumer._stop_timeout = 0.02
    with pytest.raises(RuntimeError, match="stop was not confirmed"):
        await driver.interrupt(first["lease_token"], leader="leader")
    assert driver.state.phase == "interruption_failed"
    assert (await driver.coordinator.get_state()).interruption_id
    assert browser.channel.ready.empty()


async def test_interrupt_during_synthesis_drains_provider_and_discards_late_audio(browser, monkeypatch):
    driver, _ = await activate(browser)
    route = browser.ws._meeting_playback_routes[(browser.base["session_id"], browser.base["meeting_id"])]
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def synthesize(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return SimpleNamespace(audio=b"late", audio_mime="audio/wav")

    monkeypatch.setattr(route.tts, "synthesize", synthesize)
    await queue(browser, driver, "analyst")
    first = await next_speech(browser)
    work = asyncio.create_task(browser.call("meeting.speech.synthesize", {**identity(first), "chunk_index": 0}))
    await entered.wait()
    await queue(browser, driver, "leader")
    assert await driver.interrupt(first["lease_token"], leader="leader")
    assert cancelled.is_set() and not (await work)["ok"]
    second = await next_speech(browser)
    assert (await browser.call("meeting.speech.synthesize", {**identity(second), "chunk_index": 0}))["ok"]


async def test_interruption_before_ready_delivery_cannot_publish_late_audio(browser, monkeypatch):
    driver, consumer = await activate(browser)
    observed, release = asyncio.Event(), asyncio.Event()
    get_speaker = driver.coordinator.get_speaker

    async def delayed_speaker():
        snapshot = await get_speaker()
        if not observed.is_set():
            observed.set()
            await release.wait()
        return snapshot

    monkeypatch.setattr(driver.coordinator, "get_speaker", delayed_speaker)
    await queue(browser, driver, "analyst")
    await observed.wait()
    token = (await driver.coordinator.get_state()).lease_token
    await queue(browser, driver, "leader")
    assert await driver.interrupt(token, leader="leader")
    assert not consumer._records  # No audio has been offered to the browser yet.
    release.set()
    next_ready = await next_speech(browser)
    assert next_ready["speaker_id"] == "leader"
    assert len(consumer._records) == 1


async def test_confirmed_vad_interrupts_before_asr_and_holds_next_speech_until_admitted(browser, monkeypatch):
    driver, _ = await activate(browser)
    await queue(browser, driver, "analyst")
    first = await next_speech(browser)
    await queue(browser, driver, "leader", "Next speaker")
    route = browser.ws._meeting_playback_routes[(browser.base["session_id"], browser.base["meeting_id"])]
    stream = route.input
    response = await browser.call("meeting.input.speech_started", {"stream_id": stream.id, "expected_token": first["lease_token"]})
    assert response["ok"] and driver.state.phase == "interrupting"
    assert stream.vad_interruption
    assert not any(e == "meeting.input.transcript" for _, e, _ in browser.channel.events)
    assert browser.channel.ready.empty()
    assert not stream.closed and stream.listening
    monkeypatch.setattr(gateway_input, "transcribe_utterance", AsyncMock(return_value="Okay"))
    stream.enqueue([b"pcm"])
    await asyncio.wait_for(stream.queue.join(), 3)
    transcript = next(p for _, e, p in browser.channel.events if e == "meeting.input.transcript")
    assert transcript["interruption_id"] == stream.vad_interruption
    assert browser.channel.ready.empty()
    await driver.coordinator.post_human_message(transcript["transcript"], mentions=["leader"])
    assert (await browser.call("meeting.input.admitted", {"stream_id": stream.id, "utterance_id": transcript["utterance_id"], "interruption_id": transcript["interruption_id"]}))["ok"]
    assert (await next_speech(browser))["speaker_id"] == "leader"
    assert stream.vad_interruption is None


@pytest.mark.parametrize("transcript", ["", None])
async def test_vad_empty_or_failed_asr_releases_audio_without_fabricating_user_input(browser, monkeypatch, transcript):
    driver, _ = await activate(browser)
    await queue(browser, driver, "analyst")
    first = await next_speech(browser)
    await queue(browser, driver, "leader", "Next speaker")
    route = browser.ws._meeting_playback_routes[(browser.base["session_id"], browser.base["meeting_id"])]
    stream = route.input
    assert (await browser.call("meeting.input.speech_started", {"stream_id": stream.id, "expected_token": "stale"}))["ok"]
    assert driver.state.phase == "awaiting_playback"
    assert (await browser.call("meeting.input.speech_started", {"stream_id": stream.id, "expected_token": first["lease_token"]}))["ok"]
    monkeypatch.setattr(gateway_input, "transcribe_utterance", AsyncMock(return_value=transcript))
    stream.enqueue([b"pcm"])
    await asyncio.wait_for(stream.queue.join(), 3)
    assert (await next_speech(browser))["speaker_id"] == "leader"
    assert stream.vad_interruption is None and not stream.closed
