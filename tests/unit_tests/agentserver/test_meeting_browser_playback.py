"""Real SDK/SQLite grants through Gateway routing, with controlled CosyVoice audio."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.extensions.meeting import gateway_playback as gateway
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.server import agent_ws_server as server_module
from jiuwenswarm.extensions.meeting.playback import IDENTITY_FIELDS
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers
from jiuwenswarm.gateway.voice.config import TtsConfig
from tests.unit_tests.agentserver.test_meeting_publication import (
    meeting as meeting, publication_host as publication_host,
)
from tests.unit_tests.gateway.test_app_web_handlers import FakeWebChannel

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("meeting", ["file"], indirect=True)]
COSYVOICE_MODEL = "FunAudioLLM/CosyVoice2-0.5B"


@pytest.fixture
async def browser(publication_host, monkeypatch, tmp_path):
    host = publication_host
    config = TtsConfig("https://example.invalid/v1", "test-secret", COSYVOICE_MODEL, f"{COSYVOICE_MODEL}:anna")
    monkeypatch.setattr(TtsConfig, "from_environment", staticmethod(lambda: config))
    audio_calls = []

    class Tts:
        def __init__(self, config):
            self.config = config
            self.closed = False

        async def start(self):
            pass

        async def aclose(self):
            self.closed = True

        async def synthesize(self, text, voice):
            assert not self.closed, "Interrupted TTS sessions must be replaced before reuse"
            audio_calls.append((text, voice))
            return SimpleNamespace(audio=b"wav", audio_mime="audio/wav")

    monkeypatch.setattr(gateway, "TtsSession", Tts)

    sessions = tmp_path / "sessions"
    (sessions / "publication-session").mkdir(parents=True)
    monkeypatch.setattr(server_module, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(server_module, "get_session_metadata", lambda _: {"mode": "team"})
    server = server_module.AgentWebSocketServer.__new__(server_module.AgentWebSocketServer)
    handler = object.__new__(MessageHandler)

    async def push(message):
        await handler._handle_agent_server_push(server_module.build_server_push_wire(message))
        return True

    server.send_push = push

    class Client:
        server_ready = True

        async def send_request(self, envelope):
            class Socket:
                async def send(self, raw):
                    self.response = json.loads(raw)
            socket = Socket()
            await server._handle_meeting_lifecycle(socket, e2a_to_agent_request(envelope), asyncio.Lock())
            return parse_agent_server_wire_unary(socket.response)

    class Channel(FakeWebChannel):
        auto_ack = True

        def __init__(self):
            super().__init__()
            self.events = []
            self.ready = asyncio.Queue()
            self.acks = []

        async def send_event(self, ws, event, payload):
            self.events.append((ws, event, payload))
            if event == "meeting.speech.ready":
                self.ready.put_nowait(payload)
            if event in {"meeting.playback.stop", "meeting.speech.stop"} and self.auto_ack:
                self.acks.append(asyncio.create_task(self.methods["meeting.playback.stopped"](
                    ws, "stop-ack", payload, payload["session_id"],
                )))

    channel, ws = Channel(), SimpleNamespace()
    client = Client()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=client))
    base = {"session_id": "publication-session", "meeting_id": host.state.scope.meeting_id, "startup_id": "startup"}

    async def call(method, params=None, connection=ws):
        await channel.methods[method](connection, method, {**base, **(params or {})}, base["session_id"], "user")
        return next(response for response in reversed(channel.responses) if response["id"] == method)

    assert (await call("meeting.start"))["ok"]
    assert host.manager._sessions[base["session_id"]].driver is None
    yield SimpleNamespace(host=host, channel=channel, ws=ws, call=call, base=base, config=config, audio_calls=audio_calls)
    channel.auto_ack = True
    await host.manager.release(base["session_id"], ws._meeting_owner_id, "startup")
    if channel.acks:
        await asyncio.gather(*channel.acks)


async def activate(browser):
    assert (await browser.call("meeting.playback.ready", {"_tts_model": "forged", "_meeting_route_id": "forged"}))["ok"]
    meeting = browser.host.manager._sessions[browser.base["session_id"]]
    return meeting.driver, meeting.driver.consumer


async def queue(browser, driver, member="leader", text="Spoken summary"):
    await driver.coordinator.for_member(member).post_message(
        "Full public explanation, never synthesized", speech_text=text,
        mentions=["analyst" if member == "leader" else "leader"],
    )
    driver.wake()


async def next_speech(browser):
    return await asyncio.wait_for(browser.channel.ready.get(), 3)


def identity(speech):
    return {key: speech[key] for key in IDENTITY_FIELDS}


async def test_ready_gate_server_summary_and_per_member_voices(browser):
    driver, consumer = await activate(browser)
    assert (await browser.call("meeting.playback.ready"))["ok"]
    await queue(browser, driver, text="Sentence. " * 100)
    speech = await next_speech(browser)
    assert speech["voice"] == f"{COSYVOICE_MODEL}:anna" and speech["chunk_count"] == 2
    assert "".join(speech["chunks"]) == ("Sentence. " * 100).strip()
    for index in range(speech["chunk_count"]):
        assert (await browser.call("meeting.speech.synthesize", {**identity(speech), "chunk_index": index}))["ok"]
    assert (await driver.coordinator.get_speaker()).request_id == speech["request_id"]
    assert [call[0] for call in browser.audio_calls] == speech["chunks"]
    assert all(call[1] == f"{COSYVOICE_MODEL}:anna" for call in browser.audio_calls)
    await queue(browser, driver, "analyst")
    assert not (await browser.call("meeting.playback.result", {**identity(speech), "outcome": "completed", "completed_chunks": 1}))["ok"]
    receipt = {**identity(speech), "outcome": "completed", "completed_chunks": speech["chunk_count"]}
    assert (await browser.call("meeting.playback.result", receipt))["ok"]
    second = await next_speech(browser)
    assert second["voice"] == f"{COSYVOICE_MODEL}:bella"
    assert (await browser.call("meeting.playback.result", receipt))["ok"]
    assert not (await browser.call("meeting.playback.result", {**receipt, "outcome": "stopped"}))["ok"]
    assert (await browser.call("meeting.speech.synthesize", {**identity(second), "chunk_index": 0}))["ok"]
    assert browser.ws._meeting_owned_sessions  # Playback controls never drop disconnect ownership.
    assert (await browser.call("meeting.end"))["ok"]
    assert not browser.ws._meeting_owned_sessions


async def test_foreign_controls_overrides_and_stale_lease_are_rejected(browser):
    driver, consumer = await activate(browser)
    await queue(browser, driver)
    speech = await next_speech(browser)
    params = {**identity(speech), "chunk_index": 0}
    for override in ({"voice": "Ryan"}, {"text": "Forged"}, {"lease_token": "foreign"}, {"chunk_index": True}):
        assert not (await browser.call("meeting.speech.synthesize", {**params, **override}))["ok"]
    assert not (await browser.call("meeting.speech.synthesize", params, SimpleNamespace()))["ok"]
    assert not browser.audio_calls
    result = {**identity(speech), "outcome": "stopped", "completed_chunks": 0}
    assert (await browser.call("meeting.playback.result", result))["ok"]
    assert (await browser.call("meeting.playback.result", result))["ok"]
    assert driver.state.phase == "playback_failed"
    assert not (await browser.call("meeting.speech.synthesize", params))["ok"]
    assert (await driver.coordinator.get_speaker()).lease_token == speech["lease_token"]


@pytest.mark.parametrize("outcome", ["stopped", "failed"])
async def test_stop_drains_synthesis_before_receipt_and_discards_late_audio(browser, monkeypatch, outcome):
    entered, drained = asyncio.Event(), asyncio.Event()

    class Tts:
        def __init__(self, config):
            pass

        async def start(self):
            pass

        async def aclose(self):
            pass

        async def synthesize(self, text, voice):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                drained.set()

    monkeypatch.setattr(gateway, "TtsSession", Tts)
    driver, consumer = await activate(browser)
    await queue(browser, driver)
    speech = await next_speech(browser)
    task = asyncio.create_task(browser.call("meeting.speech.synthesize", {**identity(speech), "chunk_index": 0}))
    await asyncio.wait_for(entered.wait(), 3)
    assert (await browser.call("meeting.playback.result", {**identity(speech), "outcome": outcome, "completed_chunks": 0}))["ok"]
    assert drained.is_set()
    assert not (await task)["ok"]
    if outcome == "stopped":
        assert driver.state.phase == "playback_failed"
    else:
        assert await driver.coordinator.get_speaker() is None
        await queue(browser, driver, "analyst")
        assert (await next_speech(browser))["speaker_id"] == "analyst"
    assert (await browser.call("meeting.end"))["ok"]


async def test_failed_audio_skips_only_that_speech_and_reuses_the_meeting(browser, monkeypatch):
    from jiuwenswarm.gateway.voice.tts import TtsError

    driver, _ = await activate(browser)
    await queue(browser, driver)
    first = await next_speech(browser)
    await queue(browser, driver, "analyst", "Next reading")
    route = next(iter(browser.ws._meeting_playback_routes.values()))
    failed_tts = route.tts

    async def fail_synthesis(*args):
        raise TtsError("INVALID_AUDIO", "CosyVoice2 returned incomplete WAV audio")

    monkeypatch.setattr(failed_tts, "synthesize", fail_synthesis)
    error = await browser.call("meeting.speech.synthesize", {**identity(first), "chunk_index": 0})
    assert not error["ok"] and error["code"] == "INVALID_AUDIO"
    receipt = {**identity(first), "outcome": "failed", "completed_chunks": 0}
    assert (await browser.call("meeting.playback.result", receipt))["ok"]
    second = await next_speech(browser)
    assert second["request_id"] != first["request_id"]
    assert second["speaker_id"] == "analyst"
    assert failed_tts.closed and route.tts is not failed_tts and not route.revoked
    skipped = await driver.coordinator.get_request(first["request_id"])
    assert skipped.status == "interrupted" and skipped.source_message_id == first["source_message_id"]

    entered, finish = asyncio.Event(), asyncio.Event()

    async def next_synthesis(*args):
        entered.set()
        await finish.wait()
        return SimpleNamespace(audio=b"wav", audio_mime="audio/wav")

    monkeypatch.setattr(route.tts, "synthesize", next_synthesis)
    task = asyncio.create_task(browser.call("meeting.speech.synthesize", {**identity(second), "chunk_index": 0}))
    await asyncio.wait_for(entered.wait(), 3)
    try:
        # An acknowledgement retry for the failed reading cannot cancel its successor.
        assert (await browser.call("meeting.playback.result", receipt))["ok"]
        assert not task.done() and not route.tts.closed
    finally:
        finish.set()
    assert (await task)["ok"]
    assert (await browser.call("meeting.playback.result", {
        **identity(second), "outcome": "completed", "completed_chunks": 1,
    }))["ok"]
    assert await driver.coordinator.get_speaker() is None
    assert len(await driver.coordinator.for_member("leader").get_history()) == 2


async def test_failed_audio_discards_a_late_speech_text_fetch(browser, monkeypatch):
    from jiuwenswarm.common.schema.message import ReqMethod

    driver, _ = await activate(browser)
    await queue(browser, driver)
    first = await next_speech(browser)
    await queue(browser, driver, "analyst")
    route = next(iter(browser.ws._meeting_playback_routes.values()))
    fetch = route.fetch
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_fetch(method, params):
        payload = await fetch(method, params)
        if method == ReqMethod.MEETING_SPEECH_GET and params.get("playback_id") == first["playback_id"]:
            entered.set()
            await release.wait()
        return payload

    monkeypatch.setattr(route, "fetch", delayed_fetch)
    late = asyncio.create_task(browser.call("meeting.speech.synthesize", {**identity(first), "chunk_index": 0}))
    await asyncio.wait_for(entered.wait(), 3)
    try:
        assert (await browser.call("meeting.playback.result", {
            **identity(first), "outcome": "failed", "completed_chunks": 0,
        }))["ok"]
        second = await next_speech(browser)
    finally:
        release.set()
    assert not (await late)["ok"]
    assert not browser.audio_calls
    assert (await browser.call("meeting.speech.synthesize", {**identity(second), "chunk_index": 0}))["ok"]


async def test_end_during_http_synthesis_cancels_and_closes_the_response(browser, monkeypatch):
    import httpx
    from jiuwenswarm.gateway.voice import tts
    from tests.unit_tests.gateway.test_cosyvoice_tts import mock_provider

    entered, closed = asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Future()
            yield b""

        async def aclose(self):
            closed.set()

    clients = mock_provider(monkeypatch, lambda _: httpx.Response(200, stream=Stream()))
    monkeypatch.setattr(gateway, "TtsSession", tts.TtsSession)
    driver, _ = await activate(browser)
    await queue(browser, driver)
    speech = await next_speech(browser)
    synthesis = asyncio.create_task(browser.call("meeting.speech.synthesize", {**identity(speech), "chunk_index": 0}))
    await asyncio.wait_for(entered.wait(), 3)
    assert (await asyncio.wait_for(browser.call("meeting.end"), 3))["ok"]
    assert not (await asyncio.wait_for(synthesis, 3))["ok"]
    assert closed.is_set() and clients[0].is_closed and not browser.ws._meeting_playback_routes


async def test_missing_stop_receipt_retains_cleanup_failure_until_explicit_retry(browser):
    driver, consumer = await activate(browser)
    consumer._stop_timeout = 0.02
    browser.channel.auto_ack = False
    assert not (await browser.call("meeting.end"))["ok"]
    assert (await browser.host.manager.lifecycle_status(browser.base["session_id"]))["state"] == "cleanup_failed"
    assert browser.ws._meeting_owned_sessions
    stop = next(payload for _, event, payload in browser.channel.events if event == "meeting.playback.stop")
    assert not (await browser.call("meeting.playback.stopped", {"stop_id": "foreign"}))["ok"]
    assert (await browser.call("meeting.playback.stopped", stop))["ok"]
    assert (await browser.call("meeting.end"))["ok"]


async def test_meeting_uses_qwen_through_the_same_tts_interface(browser, monkeypatch):
    from jiuwenswarm.gateway.voice.tts import TtsSession
    from tests.unit_tests.gateway.test_qwen_tts import provider

    config = TtsConfig("wss://tts.example/realtime", "tts-key", "qwen3.8-omni-flash-realtime", "Tina", "qwen_omni")
    monkeypatch.setattr(TtsConfig, "from_environment", staticmethod(lambda: config))
    sockets = provider(monkeypatch)
    monkeypatch.setattr(gateway, "TtsSession", TtsSession)
    driver, _ = await activate(browser)
    await queue(browser, driver)
    speech = await next_speech(browser)
    assert speech["voice"] == "Tina"
    response = await browser.call("meeting.speech.synthesize", {**identity(speech), "chunk_index": 0})
    assert response["ok"] and response["payload"]["audio_mime"] == "audio/wav"
    assert (await browser.call("meeting.playback.result", {
        **identity(speech), "outcome": "completed", "completed_chunks": 1,
    }))["ok"]
    assert (await browser.call("meeting.end"))["ok"]
    assert len(sockets) == 1 and sockets[0].closed
