"""Real SDK/SQLite grants through Gateway routing, with controlled Qwen audio."""

import asyncio
import json
import os
import base64
import io
import wave
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.extensions.meeting import gateway_playback as gateway
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.server import agent_ws_server as server_module
from jiuwenswarm.extensions.meeting.playback import IDENTITY_FIELDS, speech_chunks
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers
from jiuwenswarm.gateway.voice.providers.qwen_omni import QwenOmniRealtimeConfig
from tests.unit_tests.agentserver.test_meeting_publication import (
    meeting as meeting, publication_host as publication_host,
)
from tests.unit_tests.gateway.test_app_web_handlers import FakeWebChannel

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("meeting", ["file"], indirect=True)]


@pytest.fixture
async def browser(publication_host, monkeypatch, tmp_path, request):
    host = publication_host
    config = QwenOmniRealtimeConfig("wss://example.invalid/realtime", "test-secret", "qwen3.8-omni-flash-realtime", "Tina")
    live = request.node.originalname == "test_live_qwen_gateway_smoke"
    if live:
        from dotenv import load_dotenv
        load_dotenv(Path.home() / ".jiuwenswarm/config/.env", override=False)
        config = QwenOmniRealtimeConfig.from_environment()
        config.validate()
    monkeypatch.setattr(QwenOmniRealtimeConfig, "from_environment", staticmethod(lambda: config))
    audio_calls = []

    class Tts:
        def __init__(self, config):
            self.config = config

        async def synthesize(self, text, voice):
            audio_calls.append((text, voice))
            return SimpleNamespace(audio=b"wav", audio_mime="audio/wav")

    if not live:
        monkeypatch.setattr(gateway, "QwenOmniTts", Tts)

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
            if event == "meeting.playback.stop" and self.auto_ack:
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
    assert (await browser.call("meeting.playback.ready", {"_qwen_model": "forged", "_meeting_route_id": "forged"}))["ok"]
    meeting = browser.host.manager._sessions[browser.base["session_id"]]
    return meeting.driver, meeting.driver.consumer


async def test_end_drains_asr_without_delaying_browser_stop(browser, monkeypatch):
    from jiuwenswarm.extensions.meeting import gateway_input
    from tests.unit_tests.agentserver.test_meeting_input_audio import Probability, UtteranceSegmenter, frame

    monkeypatch.setattr(gateway_input, "UtteranceSegmenter", lambda: UtteranceSegmenter(Probability()))
    started, cancelled, finish_cancellation = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def transcribe(_pcm):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await finish_cancellation.wait()
            return "Late"

    monkeypatch.setattr(gateway_input, "transcribe_utterance", transcribe)
    driver, _ = await activate(browser)
    await queue(browser, driver)
    await next_speech(browser)
    stream = browser.ws._meeting_playback_routes[(browser.base["session_id"], browser.base["meeting_id"])].input
    for sequence in range(2):
        assert (await browser.call("meeting.input.append", {
            "stream_id": stream.id, "sequence": sequence,
            "audio_base64": base64.b64encode(frame() * 4).decode(),
        }))["ok"]
    assert (await browser.call("meeting.input.pause", {"stream_id": stream.id}))["ok"]
    await asyncio.wait_for(started.wait(), 3)
    ending = asyncio.create_task(browser.call("meeting.end"))
    async with asyncio.timeout(3):
        while not any(event == "meeting.playback.stop" for _, event, _ in browser.channel.events):
            await asyncio.sleep(0.005)
    assert not ending.done()  # Browser receives Stop while provider cancellation is draining.
    finish_cancellation.set()
    assert (await asyncio.wait_for(ending, 3))["ok"]
    assert cancelled.is_set() and stream.closed
    assert not browser.ws._meeting_playback_routes
    assert not any(event == "meeting.input.transcript" for _, event, _ in browser.channel.events)


async def test_input_uses_audio_route_ownership_and_model_readiness(browser, monkeypatch):
    from jiuwenswarm.extensions.meeting import gateway_input
    from unittest.mock import Mock

    with monkeypatch.context() as model_patch:
        model_patch.setattr(gateway_input, "UtteranceSegmenter", Mock(side_effect=RuntimeError("Model unavailable")))
        assert not (await browser.call("meeting.playback.ready"))["ok"]
        assert browser.host.manager._sessions[browser.base["session_id"]].driver is None
        assert not browser.ws._meeting_playback_routes
    driver, _ = await activate(browser)
    await queue(browser, driver)
    await next_speech(browser)
    stream = browser.ws._meeting_playback_routes[(browser.base["session_id"], browser.base["meeting_id"])].input
    for extra in ({"stream_id": "stale"}, {"stream_id": stream.id, "meeting_id": "foreign"}):
        assert not (await browser.call("meeting.input.pause", extra))["ok"]
        assert not stream.closed
    assert not (await browser.call("meeting.input.pause", {"stream_id": stream.id}, connection=SimpleNamespace()))["ok"]


async def test_end_cancels_pending_audio_readiness(browser, monkeypatch):
    from jiuwenswarm.extensions.meeting.gateway_input import MeetingInputStream
    started = asyncio.Event()

    async def initialize(_stream):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(MeetingInputStream, "initialize", initialize)
    ready = asyncio.create_task(browser.call("meeting.playback.ready"))
    await asyncio.wait_for(started.wait(), 3)
    assert (await browser.call("meeting.end"))["ok"]
    assert not (await asyncio.wait_for(ready, 3))["ok"]
    assert not browser.ws._meeting_playback_routes


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
    assert speech["voice"] == "Tina" and speech["chunk_count"] == 2
    assert "".join(speech["chunks"]) == ("Sentence. " * 100).strip()
    for index in range(speech["chunk_count"]):
        assert (await browser.call("meeting.speech.synthesize", {**identity(speech), "chunk_index": index}))["ok"]
    assert (await driver.coordinator.get_speaker()).request_id == speech["request_id"]
    assert [call[0] for call in browser.audio_calls] == speech["chunks"]
    assert all(call[1] == "Tina" for call in browser.audio_calls)
    await queue(browser, driver, "analyst")
    assert not (await browser.call("meeting.playback.result", {**identity(speech), "outcome": "completed", "completed_chunks": 1}))["ok"]
    receipt = {**identity(speech), "outcome": "completed", "completed_chunks": speech["chunk_count"]}
    assert (await browser.call("meeting.playback.result", receipt))["ok"]
    second = await next_speech(browser)
    assert second["voice"] == "Serena"
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


async def test_stop_drains_synthesis_before_receipt_and_discards_late_audio(browser, monkeypatch):
    entered, drained = asyncio.Event(), asyncio.Event()

    class Tts:
        def __init__(self, config):
            pass

        async def synthesize(self, text, voice):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                drained.set()

    monkeypatch.setattr(gateway, "QwenOmniTts", Tts)
    driver, consumer = await activate(browser)
    await queue(browser, driver)
    speech = await next_speech(browser)
    task = asyncio.create_task(browser.call("meeting.speech.synthesize", {**identity(speech), "chunk_index": 0}))
    await asyncio.wait_for(entered.wait(), 3)
    assert (await browser.call("meeting.playback.result", {**identity(speech), "outcome": "stopped", "completed_chunks": 0}))["ok"]
    assert drained.is_set()
    assert not (await task)["ok"]
    assert driver.state.phase == "playback_failed"
    assert (await browser.call("meeting.end"))["ok"]


async def test_config_change_rejects_audio_and_end_waits_for_independent_stop_ack(browser, monkeypatch):
    driver, consumer = await activate(browser)
    await queue(browser, driver)
    speech = await next_speech(browser)
    monkeypatch.setattr(QwenOmniRealtimeConfig, "from_environment", staticmethod(lambda: replace(browser.config, api_key="rotated")))
    assert not (await browser.call("meeting.speech.synthesize", {**identity(speech), "chunk_index": 0}))["ok"]
    assert not browser.audio_calls
    # End holds the driver lock while the independent stopped RPC acknowledges.
    assert (await asyncio.wait_for(browser.call("meeting.end"), 3))["ok"]
    assert any(event == "meeting.playback.stop" for _, event, _ in browser.channel.events)
    assert await browser.host.manager.lifecycle_status(browser.base["session_id"]) == {"state": "idle"}


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


async def test_readiness_rejects_foreign_startup_and_voice_bindings_survive_meeting_restart(browser):
    assert not (await browser.call("meeting.playback.ready", {"startup_id": "foreign"}))["ok"]
    driver, consumer = await activate(browser)
    voices = consumer.voices
    assert (await browser.call("meeting.end"))["ok"]
    assert (await browser.call("meeting.start"))["ok"]
    await browser.host.manager.start(browser.base["session_id"])
    state = await browser.host.manager.lifecycle_status(browser.base["session_id"])
    browser.base["meeting_id"] = state["meeting_id"]
    _, replacement = await activate(browser)
    assert replacement.voices is voices
    assert replacement.voices.get_voice("analyst").provider_voice_id == "Serena"


async def test_unicode_chunks_preserve_text_and_bounds(meeting):
    text = "测试🙂，不要截断。" * 180
    chunks = speech_chunks(text)
    assert "".join(chunks) == text
    assert all(len(chunk) <= 800 for chunk in chunks)


@pytest.mark.parametrize("outcome", ["completed", "stopped"])
async def test_lost_receipt_can_retry_without_revoking_the_next_speaker(browser, monkeypatch, outcome):
    driver, _ = await activate(browser)
    await queue(browser, driver)
    speech = await next_speech(browser)
    if outcome == "completed":
        assert (await browser.call("meeting.speech.synthesize", {**identity(speech), "chunk_index": 0}))["ok"]
        await queue(browser, driver, "analyst")
    route = next(iter(browser.ws._meeting_playback_routes.values()))
    fetch = route.fetch
    lost = False

    async def lose_once(method, params):
        nonlocal lost
        payload = await fetch(method, params)
        if method.value == "meeting.playback.result" and not lost:
            lost = True
            raise RuntimeError("Lost result response")
        return payload

    monkeypatch.setattr(route, "fetch", lose_once)
    receipt = {**identity(speech), "outcome": outcome, "completed_chunks": 1 if outcome == "completed" else 0}
    assert not (await browser.call("meeting.playback.result", receipt))["ok"]
    second = await next_speech(browser) if outcome == "completed" else None
    assert (await browser.call("meeting.playback.result", receipt))["ok"]
    if second:
        assert (await browser.call("meeting.speech.synthesize", {**identity(second), "chunk_index": 0}))["ok"]


async def test_disconnect_does_not_fabricate_stop_or_playback_completion(browser):
    driver, consumer = await activate(browser)
    await queue(browser, driver)
    await next_speech(browser)
    consumer._stop_timeout = 0.02
    browser.channel.auto_ack = False
    await browser.channel.disconnect_handler(browser.ws, set())
    # Disconnect release is asynchronous; joining End observes failed cleanup.
    assert not (await browser.call("meeting.end"))["ok"]
    assert not consumer._receipts
    assert not consumer._stop_waiter.done()
    browser.channel.auto_ack = True
    assert (await browser.call("meeting.end"))["ok"]
    assert not consumer._receipts  # Even teardown never marks this speech played.


@pytest.mark.skipif(os.getenv("MEETING_LIVE_TTS_SMOKE") != "1", reason="Opt-in live Qwen synthesis")
async def test_live_qwen_gateway_smoke(browser):
    driver, _ = await activate(browser)
    artifact = Path("tmp/meeting-playback-smoke")
    artifact.mkdir(parents=True, exist_ok=True)
    report = []
    for member in ("leader", "analyst"):
        await queue(browser, driver, member, "The meeting summary is ready for review.")
        speech = await next_speech(browser)
        result = await browser.call("meeting.speech.synthesize", {**identity(speech), "chunk_index": 0})
        assert result["ok"], result
        audio = base64.b64decode(result["payload"]["audio_base64"])
        with wave.open(io.BytesIO(audio)) as wav:
            assert wav.getframerate() == 24000 and wav.getnchannels() == 1
            assert wav.getnframes() > 0
        (artifact / f"{member}.wav").write_bytes(audio)
        report.append({"member": member, "voice": speech["voice"], "model": speech["model"], "audio_bytes": len(audio)})
        assert (await browser.call("meeting.playback.result", {**identity(speech), "outcome": "completed", "completed_chunks": 1}))["ok"]
    assert report[0]["voice"] != report[1]["voice"]
    assert (await browser.call("meeting.end"))["ok"]
    (artifact / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
