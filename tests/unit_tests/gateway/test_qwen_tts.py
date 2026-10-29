"""Qwen TTS synthesis, persistent sessions and cancellation boundaries."""

import asyncio
import base64
from contextlib import asynccontextmanager
import io
import json
import wave

import pytest

from jiuwenswarm.gateway.voice import tts
from jiuwenswarm.gateway.voice.providers import qwen_omni
from jiuwenswarm.gateway.voice.tts import QwenOmniTtsSession, TtsError
from jiuwenswarm.gateway.voice.providers.qwen_omni import QwenOmniRealtimeConfig

ONE_SHOT_CONFIG = QwenOmniRealtimeConfig("wss://example.test/realtime", "private-key", "qwen3.8-omni-flash-realtime", "Default")
SESSION_CONFIG = QwenOmniRealtimeConfig("wss://example.test/realtime", "private-key", "qwen3.8-omni-flash-realtime", "Tina")
PCM = b"\x01\x00\xff\xff" * 20


def events(text="Read this.", *, status="completed", audio=PCM, voice=None, alias=False):
    prefix = "response.output_audio" if alias else "response.audio"
    return [
        {"type": "session.created"},
        {"type": "session.updated", "session": {"audio": {"output": {"voice": voice}}}},
        {"type": f"{prefix}.delta", "delta": base64.b64encode(audio).decode()},
        {"type": f"{prefix}_transcript.delta", "delta": text[:3]},
        {"type": f"{prefix}_transcript.done", "transcript": text},
        {"type": "response.done", "response": {"status": status}},
    ]


class Socket:
    def __init__(self, data=None, *, wait=False):
        self.events = list(events() if data is None else data)
        self.wait = wait
        self.waiting = asyncio.Event()
        self.sent = []
        self.closed = False

    async def send(self, raw):
        self.sent.append(json.loads(raw))
        await asyncio.sleep(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self.events:
            if self.wait:
                self.waiting.set()
                await asyncio.Future()
            raise StopAsyncIteration
        event = self.events.pop(0)
        return event if isinstance(event, str) else json.dumps(event)


def install(monkeypatch, *sockets):
    pending = list(sockets)
    configs = []

    @asynccontextmanager
    async def connect(config):
        configs.append(config)
        socket = pending.pop(0)
        try:
            yield socket
        finally:
            socket.closed = True

    monkeypatch.setattr(tts.qwen_omni, "open_qwen_omni_connection", connect)
    return configs


@pytest.mark.asyncio
async def test_returns_complete_wav_and_sends_delimited_text_with_explicit_voice(monkeypatch):
    text = 'Read "this".\n你好。'
    socket = Socket(events(text, voice="Serena"))
    configs = install(monkeypatch, socket)
    audio = await tts.QwenOmniTts(ONE_SHOT_CONFIG).synthesize(text, voice="Serena")
    assert socket.closed and configs == [ONE_SHOT_CONFIG]
    assert audio.voice == "Serena" and audio.model == ONE_SHOT_CONFIG.model
    assert audio.transcript == text and audio.audio_mime == "audio/wav"
    assert 0 < audio.first_audio_ms <= audio.synthesis_ms
    with wave.open(io.BytesIO(audio.audio)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
        assert wav.readframes(wav.getnframes()) == PCM
    session = socket.sent[0]["session"]
    assert session["voice"] == session["audio"]["output"]["voice"] == "Serena"
    assert session["tools"] == [] and session["turn_detection"] is None
    item = socket.sent[1]["item"]
    assert json.loads(item["content"][0]["text"].split("\n", 1)[1]) == text
    assert socket.sent[2] == {"type": "response.create"}


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["Understood.", "Do release the next turn.", "Do not release the next turn. Here is more."])
async def test_model_answer_or_changed_negation_never_returns_audio(monkeypatch, changed):
    socket = Socket(events(changed))
    install(monkeypatch, socket)
    with pytest.raises(tts.TtsError) as error:
        await tts.QwenOmniTts(ONE_SHOT_CONFIG).synthesize("Do not release the next turn.", "Tina")
    assert error.value.code == "TEXT_MISMATCH" and error.value.transcript == changed
    assert socket.closed


@pytest.mark.asyncio
async def test_cancel_closes_provider_session_and_propagates_cancelled_error(monkeypatch):
    socket = Socket(events()[:2], wait=True)
    install(monkeypatch, socket)
    task = asyncio.create_task(tts.QwenOmniTts(ONE_SHOT_CONFIG).synthesize("Read this.", "Tina"))
    await asyncio.wait_for(socket.waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.closed and socket.sent[-1] == {"type": "response.cancel"}


class RealtimeSocket:
    """Reactive provider: output is generated only for commands actually sent."""

    def __init__(self, *, hold_response=False):
        self.events = asyncio.Queue()
        self.events.put_nowait({"type": "session.created"})
        self.sent = []
        self.voice = "Tina"
        self.text = ""
        self.items = set()
        self.turns = 0
        self.closed = False
        self.hold_response = hold_response
        self.requested = asyncio.Event()
        self.close_started = asyncio.Event()
        self.finish_close = asyncio.Event()
        self.finish_close.set()
        self.readers = self.max_readers = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.close_started.set()
        await self.finish_close.wait()
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.readers += 1
        self.max_readers = max(self.max_readers, self.readers)
        try:
            await asyncio.sleep(0)
            event = await self.events.get()
            if event is None:
                raise StopAsyncIteration
            return json.dumps(event)
        finally:
            self.readers -= 1

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        kind = event["type"]
        if kind == "session.update":
            self.voice = event["session"]["voice"]
            self.events.put_nowait({"type": "session.updated", "session": {"voice": self.voice}})
        elif kind == "conversation.item.create":
            self.text = json.loads(event["item"]["content"][0]["text"].split("\n", 1)[1])
            self.items.add(event["item"]["id"])
            self.events.put_nowait({"type": "conversation.item.created", "item": {"id": event["item"]["id"]}})
        elif kind == "response.create":
            self.turns += 1
            response_id, item_id = f"response-{self.turns}", f"output-{self.turns}"
            self.items.add(item_id)
            self.requested.set()
            self.events.put_nowait({"type": "response.created", "response": {"id": response_id}})
            self.events.put_nowait({"type": "response.output_item.added", "response_id": response_id, "item": {"id": item_id}})
            if not self.hold_response:
                for output in (
                    {"type": "response.audio.delta", "delta": base64.b64encode(b"\1\0" * 24).decode()},
                    {"type": "response.audio_transcript.done", "transcript": self.text},
                    {"type": "response.done", "response": {"id": response_id, "status": "completed", "output": [{"id": item_id}]}},
                ):
                    self.events.put_nowait({"response_id": response_id, **output})
        elif kind == "conversation.item.delete":
            assert event["item_id"] in self.items
            self.items.remove(event["item_id"])
            acknowledgement = {"type": "conversation.item.deleted", "item_id": event["item_id"]}
            self.events.put_nowait(acknowledgement)


def provider(monkeypatch, factory=RealtimeSocket):
    sockets = []

    def connect(*_args, **_kwargs):
        socket = factory()
        sockets.append(socket)
        return socket

    monkeypatch.setattr(qwen_omni.websockets, "connect", connect)
    return sockets


@pytest.mark.asyncio
async def test_warm_session_reuses_one_connection_switches_voices_and_clears_history(monkeypatch):
    sockets = provider(monkeypatch)
    session = QwenOmniTtsSession(SESSION_CONFIG)
    await session.start()
    await session.start()
    cases = (("First sentence.", "Tina"), ("Different words.", "Serena"), ("Final sentence.", "Tina"))
    results = await asyncio.gather(*(session.synthesize(text, voice) for text, voice in cases))
    for audio, (text, voice) in zip(results, cases):
        assert audio.transcript == text and audio.voice == voice and audio.audio.startswith(b"RIFF")
        assert not sockets[0].closed and not sockets[0].items
    assert len(sockets) == 1 and sockets[0].max_readers == 1
    updates = [event["session"]["voice"] for event in sockets[0].sent if event["type"] == "session.update"]
    assert updates == ["Tina", "Serena", "Tina"]
    await session.aclose()
    await session.aclose()
    assert sockets[0].closed
    with pytest.raises(TtsError) as error:
        await session.synthesize("Cannot restart.", "Tina")
    assert error.value.code == "SESSION_CLOSED" and len(sockets) == 1


@pytest.mark.asyncio
async def test_cancel_discards_connection_and_never_replays_late_audio(monkeypatch):
    count = 0

    def factory():
        nonlocal count
        count += 1
        return RealtimeSocket(hold_response=count == 1)

    sockets = provider(monkeypatch, factory)
    session = QwenOmniTtsSession(SESSION_CONFIG)
    await session.start()
    active = asyncio.create_task(session.synthesize("Cancelled.", "Tina"))
    await asyncio.wait_for(sockets[0].requested.wait(), 1)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert sockets[0].closed
    sockets[0].events.put_nowait({"type": "response.audio.delta", "delta": "AAAA"})
    audio = await session.synthesize("New turn.", "Serena")
    assert audio.transcript == "New turn." and len(sockets) == 2 and sockets[0].turns == 1
    await session.aclose()
