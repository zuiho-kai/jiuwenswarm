"""Meeting VAD, bounded streaming, provider reuse, and transport regression checks."""

import asyncio
import base64
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave

import numpy as np
import pytest

from jiuwenswarm.extensions.meeting import gateway_input as gateway
from jiuwenswarm.extensions.meeting.input_audio import (
    FRAME_SAMPLES, MAX_UTTERANCE_FRAMES, SileroProbability, UtteranceSegmenter, pcm_wav,
)


def frame(value=1000):
    return np.full(FRAME_SAMPLES, value, dtype="<i2").tobytes()


class Probability:
    value = 0.99

    def reset(self):
        pass

    def __call__(self, _frame):
        return self.value


def test_real_model_silence_and_isolated_state():
    first, second = SileroProbability(), SileroProbability()
    assert first.model is second.model and first.state is not second.state
    state = second.state.copy()
    for _ in range(40):
        assert 0 <= first(frame(0)) < 0.8
    np.testing.assert_array_equal(second.state, state)
    assert UtteranceSegmenter().feed(frame(0) * 100) == []


def test_segmentation_confirms_speech_preserves_onset_and_waits_for_silence():
    probability = Probability()
    vad = UtteranceSegmenter(probability)
    probability.value = 0.1
    assert vad.feed(frame(20000) * 100) == []  # Loud noise is not speech.
    probability.value = 0.99
    vad.feed(frame() * 7)
    assert not vad.frames
    vad.feed(frame())
    assert len(vad.frames) == 31  # Confirmation retains the pre-roll.
    probability.value = 0.1
    assert vad.feed(frame(0) * 19) == []
    assert len(vad.feed(frame(0))) == 1
    probability.value = 0.99
    assert vad.feed(frame() * 8) == []
    assert vad.flush() == frame() * 8  # Ready for the next utterance.


def test_fragmentation_length_limit_and_wav_encoding():
    probability = Probability()
    whole, split = UtteranceSegmenter(probability), UtteranceSegmenter(probability)
    pcm = frame() * MAX_UTTERANCE_FRAMES
    expected = whole.feed(pcm)
    actual = []
    for offset in range(0, len(pcm), 200):
        actual.extend(split.feed(pcm[offset:offset + 200]))
    assert actual == expected == [pcm]
    split.feed(frame() * 7)
    assert split.flush() is None  # Unconfirmed speech is discarded.
    split.feed(frame() * 8 + frame()[:200])
    pcm = split.flush()
    with wave.open(io.BytesIO(pcm_wav(pcm)), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
        assert audio.readframes(audio.getnframes()) == pcm


@pytest.fixture
async def stream(monkeypatch):
    monkeypatch.setattr(gateway, "UtteranceSegmenter", lambda: UtteranceSegmenter(Probability()))
    stream = gateway.MeetingInputStream(SimpleNamespace(send_event=AsyncMock()), None, "session", "meeting")
    await stream.start()
    yield stream
    await stream.close()


async def append(stream, sequence, pcm):
    await stream.append({"sequence": sequence, "audio_base64": base64.b64encode(pcm).decode()})


@pytest.mark.asyncio
async def test_capture_continues_during_asr_and_transcripts_remain_ordered(stream, monkeypatch):
    pending = asyncio.Event()
    calls = []

    async def transcribe(pcm):
        calls.append(pcm)
        await pending.wait()
        return f"Utterance {len(calls)}"

    monkeypatch.setattr(gateway, "transcribe_utterance", transcribe)
    sequence = 0
    for _ in range(2):
        for probability, pcm, chunks in [(0.99, frame(), 2), (0.1, frame(0), 5)]:
            stream.segmenter.probability.value = probability
            for _ in range(chunks):
                await append(stream, sequence, pcm * 4)
                sequence += 1
    assert len(calls) == 1
    pending.set()
    await asyncio.wait_for(stream.queue.join(), 3)
    results = [call.args[2] for call in stream.channel.send_event.call_args_list if call.args[1] == "meeting.input.transcript"]
    assert [r["transcript"] for r in results] == ["Utterance 1", "Utterance 2"]
    assert [r["utterance_id"] for r in results] == [1, 2]
    assert all(r["stream_id"] == stream.id for r in results)


@pytest.mark.asyncio
async def test_pause_flushes_confirmed_speech_and_stop_discards_late_asr(stream, monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def transcribe(_pcm):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return "Late result"

    monkeypatch.setattr(gateway, "transcribe_utterance", transcribe)
    await append(stream, 0, frame() * 4)
    await append(stream, 1, frame() * 4)
    await stream.listening_control(False)
    assert not stream.listening
    await stream.listening_control(True)
    await append(stream, 2, frame())
    await asyncio.wait_for(started.wait(), 3)
    await stream.close()
    assert cancelled.is_set() and stream.closed
    assert not any(call.args[1] == "meeting.input.transcript" for call in stream.channel.send_event.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"sequence": 1, "audio_base64": "AAA="}, {"sequence": True, "audio_base64": "AAA="},
    {"sequence": 0, "audio_base64": "!"}, {"sequence": 0, "audio_base64": "AA=="},
    {"sequence": 0, "audio_base64": "AAAA" * 3000},
])
async def test_invalid_audio_is_rejected(stream, params):
    with pytest.raises(ValueError):
        await stream.append(params)


@pytest.mark.asyncio
async def test_backlog_limit_is_explicit(stream, monkeypatch):
    monkeypatch.setattr(stream.segmenter, "feed", lambda pcm: [pcm])
    # Keep the first ASR pending while filling the bounded queue.
    async def transcribe(_pcm):
        await asyncio.Event().wait()
    monkeypatch.setattr(gateway, "transcribe_utterance", transcribe)
    for sequence in range(gateway.MAX_PENDING_UTTERANCES + 1):
        await append(stream, sequence, frame())
    with pytest.raises(ValueError, match="backlog"):
        await append(stream, gateway.MAX_PENDING_UTTERANCES + 1, frame())


@pytest.mark.asyncio
async def test_pipelined_frames_serialize_inference(stream, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    received = []

    def feed(pcm):
        received.append(pcm)
        if len(received) == 1:
            entered.set()
            release.wait(3)
        return []

    monkeypatch.setattr(stream.segmenter, "feed", feed)
    first = asyncio.create_task(append(stream, 0, frame(1000)))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        second = asyncio.create_task(append(stream, 1, frame(2000)))
        await asyncio.sleep(0.01)
        assert not second.done() and received == [frame(1000)]
        release.set()
        await asyncio.gather(first, second)
        assert received == [frame(1000), frame(2000)]
    finally:
        release.set()


@pytest.mark.asyncio
async def test_asr_reuses_provider_and_filters_filler(monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend import video_voice, video_live
    transcribe = AsyncMock(side_effect=["hmm", "Recognized speech"])
    monkeypatch.setattr(video_voice, "transcribe_audio", transcribe)
    monkeypatch.setattr(video_live, "_uses_joyai_voice_channel", lambda: True)
    assert await gateway.transcribe_utterance(frame()) == ""
    assert await gateway.transcribe_utterance(frame()) == "Recognized speech"
    assert transcribe.call_args.kwargs == {"use_joyai_voice": True}
    encoded = transcribe.call_args.args[0][0][0]
    assert base64.b64decode(encoded.split(",")[1])[:4] == b"RIFF"


@pytest.mark.asyncio
@pytest.mark.parametrize(("encoded", "ok"), [("AAA=", True), ("!", False)])
async def test_raw_audio_rpc_bypasses_agent_bus_and_closes_invalid_input(stream, encoded, ok):
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    from tests.unit_tests.channel.test_web_channel_user_id import FakeWebSocket

    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    gateway.bind_input_handlers(channel, lambda *_: SimpleNamespace(input=stream))
    forwarded = []
    channel.on_message(lambda message: forwarded.append(message))
    ws = FakeWebSocket(user_id="user")
    try:
        await channel._handle_raw_message(ws, json.dumps({
            "type": "req", "id": "audio", "method": "meeting.input.append",
            "params": {"session_id": "session", "meeting_id": "meeting", "stream_id": stream.id,
                       "sequence": 0, "audio_base64": encoded},
        }), {})
        async with asyncio.timeout(3):
            while not ws.sent_frames:
                await asyncio.sleep(0)
        assert ws.sent_frames[-1]["ok"] is ok and not forwarded
        assert stream.closed is not ok
    finally:
        await channel.unregister_ws(ws)
