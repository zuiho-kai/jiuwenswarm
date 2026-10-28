"""Meeting VAD, bounded streaming, provider reuse, and transport regression checks."""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from jiuwenswarm.extensions.meeting import gateway_input as gateway
from jiuwenswarm.extensions.meeting.input_audio import (
    FRAME_SAMPLES, UtteranceSegmenter,
)


def frame(value=1000):
    return np.full(FRAME_SAMPLES, value, dtype="<i2").tobytes()


class Probability:
    value = 0.99

    def reset(self):
        pass

    def __call__(self, _frame):
        return self.value


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
