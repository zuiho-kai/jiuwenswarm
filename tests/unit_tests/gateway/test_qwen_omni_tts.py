"""Supplied-text synthesis, per-call voices and failure/cancellation boundaries."""

import asyncio
import base64
from contextlib import asynccontextmanager
import io
import json
import wave

import pytest

from jiuwenswarm.gateway.voice import tts
from jiuwenswarm.gateway.voice.providers.qwen_omni import QwenOmniRealtimeConfig

CONFIG = QwenOmniRealtimeConfig("wss://example.test/realtime", "private-key", "qwen3.8-omni-flash-realtime", "Default")
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
    audio = await tts.QwenOmniTts(CONFIG).synthesize(text, voice="Serena")
    assert socket.closed and configs == [CONFIG]
    assert audio.voice == "Serena" and audio.model == CONFIG.model
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
        await tts.QwenOmniTts(CONFIG).synthesize("Do not release the next turn.", "Tina")
    assert error.value.code == "TEXT_MISMATCH" and error.value.transcript == changed
    assert socket.closed


@pytest.mark.asyncio
async def test_cancel_closes_provider_session_and_propagates_cancelled_error(monkeypatch):
    socket = Socket(events()[:2], wait=True)
    install(monkeypatch, socket)
    task = asyncio.create_task(tts.QwenOmniTts(CONFIG).synthesize("Read this.", "Tina"))
    await asyncio.wait_for(socket.waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.closed and socket.sent[-1] == {"type": "response.cancel"}
