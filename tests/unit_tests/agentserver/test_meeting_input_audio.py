"""Meeting VAD, bounded streaming, provider reuse, and transport regression checks."""

import asyncio
import base64
import io
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest

from jiuwenswarm.extensions.meeting import gateway_input as gateway
from jiuwenswarm.extensions.meeting.input_audio import (
    FRAME_SAMPLES, UtteranceSegmenter,
)
from jiuwenswarm.gateway.channel_manager.web import task_asr


def frame(value=1000):
    return np.full(FRAME_SAMPLES, value, dtype="<i2").tobytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("transcript, expected", [
    (" meeting transcript ", "meeting transcript"),
    ("嗯。", ""),
    ("", ""),
])
async def test_meeting_uses_speech_transcription_settings_with_joyai_configured(
    monkeypatch, transcript, expected,
):
    monkeypatch.setenv("ASR_API_BASE", "https://speech.example/v1/audio/transcriptions")
    monkeypatch.setenv("ASR_API_KEY", "speech-secret")
    monkeypatch.setenv("ASR_MODEL_NAME", "speech-model")
    monkeypatch.setenv("VIDEO_LIVE_MODE", "joyai")
    monkeypatch.setenv("VOICE_PROTOCOL", "native_ws")
    monkeypatch.setenv("VOICE_ASR_ENDPOINT", "ws://127.0.0.1:8994/ws/asr")
    monkeypatch.setenv("VOICE_ASR_MODEL", "unrelated-voice-model")
    monkeypatch.setenv("VOICE_API_KEY", "unrelated-voice-secret")
    monkeypatch.setattr(task_asr, "_decrypt_api_key", lambda value: value)
    pcm = frame() * 10
    requests = []

    async def handler(request):
        requests.append(request)
        assert str(request.url) == "https://speech.example/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer speech-secret"
        body = await request.aread()
        assert b"speech-model" in body
        assert b"Content-Type: audio/wav" in body
        wav_start = body.index(b"RIFF")
        with wave.open(io.BytesIO(body[wav_start:]), "rb") as recording:
            assert recording.getnchannels() == 1
            assert recording.getsampwidth() == 2
            assert recording.getframerate() == 16_000
            assert recording.readframes(recording.getnframes()) == pcm
        return httpx.Response(200, json={"text": transcript})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(task_asr.httpx, "AsyncClient", lambda **_kwargs: client)
        assert await gateway.transcribe_utterance(pcm) == expected
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_meeting_requires_speech_transcription_config_even_with_joyai(monkeypatch):
    monkeypatch.delenv("ASR_API_BASE", raising=False)
    monkeypatch.setenv("VIDEO_LIVE_MODE", "joyai")
    monkeypatch.setenv("VOICE_PROTOCOL", "native_ws")
    monkeypatch.setenv("VOICE_ASR_ENDPOINT", "ws://127.0.0.1:8994/ws/asr")
    with pytest.raises(task_asr.TaskAsrError) as error:
        await gateway.transcribe_utterance(frame())
    assert error.value.code == "ASR_NOT_CONFIGURED"


class Probability:
    value = 0.99

    def reset(self):
        pass

    def __call__(self, _frame):
        return self.value


@pytest.fixture(autouse=True)
def isolate_speech_detection_settings(monkeypatch):
    monkeypatch.delenv("MEETING_VAD_START_THRESHOLD", raising=False)
    monkeypatch.delenv("MEETING_VAD_START_FRAMES", raising=False)


def test_segmentation_confirms_speech_preserves_onset_and_waits_for_silence():
    probability = Probability()
    vad = UtteranceSegmenter(probability)
    probability.value = 0.1
    assert vad.feed(frame(20000) * 100) == []  # Loud noise is not speech.
    probability.value = 0.5
    vad.feed(frame() * 4)
    assert not vad.frames
    vad.feed(frame())
    assert len(vad.frames) == 31  # Confirmation retains the pre-roll.
    probability.value = 0.1
    assert vad.feed(frame(0) * 19) == []
    assert len(vad.feed(frame(0))) == 1
    probability.value = 0.99
    assert vad.feed(frame() * 8) == []
    assert vad.flush() == frame() * 8  # Ready for the next utterance.


def test_segmentation_uses_saved_start_settings_for_new_streams(monkeypatch):
    monkeypatch.setenv("MEETING_VAD_START_THRESHOLD", "0.7")
    monkeypatch.setenv("MEETING_VAD_START_FRAMES", "3")
    probability = Probability()
    vad = UtteranceSegmenter(probability)
    probability.value = 0.69
    vad.feed(frame() * 20)
    assert not vad.frames
    probability.value = 0.7
    vad.feed(frame() * 2)
    assert not vad.frames
    vad.feed(frame())
    assert vad.frames
    # A new setting does not alter segmentation in the middle of an utterance.
    monkeypatch.setenv("MEETING_VAD_START_THRESHOLD", "0.9")
    assert vad.start_threshold == 0.7
    assert UtteranceSegmenter(probability).start_threshold == 0.9


@pytest.fixture
async def stream(monkeypatch):
    monkeypatch.setattr(gateway, "UtteranceSegmenter", lambda: UtteranceSegmenter(Probability()))
    monkeypatch.setattr(gateway, "BATCH_WAIT_SECONDS", 0.02)
    stream = gateway.MeetingInputStream(SimpleNamespace(send_event=AsyncMock()), None, "session", "meeting")
    await stream.start()
    yield stream
    await stream.close()


async def append(stream, sequence, pcm):
    await stream.append({"sequence": sequence, "audio_base64": base64.b64encode(pcm).decode()})


@pytest.mark.asyncio
async def test_capture_continues_during_asr_and_transcripts_remain_ordered(stream, monkeypatch):
    pending = asyncio.Event()
    both_started = asyncio.Event()
    calls = []

    async def transcribe(pcm):
        calls.append(pcm)
        number = len(calls)
        if number == 2:
            both_started.set()
        await pending.wait()
        return f"Utterance {number}"

    monkeypatch.setattr(gateway, "transcribe_utterance", transcribe)
    sequence = 0
    for _ in range(2):
        for probability, pcm, chunks in [(0.99, frame(), 2), (0.1, frame(0), 5)]:
            stream.segmenter.probability.value = probability
            for _ in range(chunks):
                await append(stream, sequence, pcm * 4)
                sequence += 1
    await asyncio.wait_for(both_started.wait(), 1)
    assert len(calls) == 2
    pending.set()
    await asyncio.wait_for(stream.queue.join(), 3)
    results = [call.args[2] for call in stream.channel.send_event.call_args_list if call.args[1] == "meeting.input.transcript"]
    assert [r["transcript"] for r in results] == ["Utterance 1 Utterance 2"]
    assert [r["utterance_id"] for r in results] == [2]
    assert all(r["stream_id"] == stream.id for r in results)


@pytest.mark.asyncio
async def test_out_of_order_asr_batches_four_without_waiting_for_timer(stream, monkeypatch):
    monkeypatch.setattr(gateway, "BATCH_WAIT_SECONDS", 60)
    releases = {number: asyncio.Event() for number in range(1, 5)}
    started = {number: asyncio.Event() for number in range(1, 5)}
    active = 0
    peak = 0

    async def transcribe(pcm):
        nonlocal active, peak
        number = pcm[0]
        active += 1
        peak = max(peak, active)
        started[number].set()
        await releases[number].wait()
        active -= 1
        return f"Utterance {number}"

    monkeypatch.setattr(gateway, "transcribe_utterance", transcribe)
    stream.prepare_transcript = AsyncMock(return_value={"interruption_id": "batch"})
    stream.admit_transcript = AsyncMock()
    stream.enqueue([bytes([number]) for number in range(1, 5)])
    await asyncio.wait_for(asyncio.gather(*(started[number].wait() for number in (1, 2, 3))), 1)
    assert not started[4].is_set()
    releases[3].set()
    await asyncio.wait_for(started[4].wait(), 1)
    releases[2].set()
    releases[4].set()
    # No result can overtake the first utterance.
    assert not any(call.args[1] == "meeting.input.transcript" for call in stream.channel.send_event.call_args_list)
    releases[1].set()
    await asyncio.wait_for(stream.queue.join(), 1)
    results = [call.args[2] for call in stream.channel.send_event.call_args_list if call.args[1] == "meeting.input.transcript"]
    assert peak == 3
    assert [(r["utterance_id"], r["transcript"]) for r in results] == [
        (4, "Utterance 1 Utterance 2 Utterance 3 Utterance 4"),
    ]
    stream.prepare_transcript.assert_awaited_once_with(results[0]["transcript"])
    await stream.admitted({"utterance_id": 4, "interruption_id": "batch"})
    stream.admit_transcript.assert_awaited_once_with("batch")


@pytest.mark.asyncio
async def test_timeout_and_empty_results_advance_ordering(stream, monkeypatch):
    monkeypatch.setattr(gateway, "ASR_TIMEOUT_SECONDS", 0.02)

    async def transcribe(pcm):
        if pcm == b"2":
            await asyncio.Event().wait()
        return {b"1": "First", b"3": "", b"4": "Fourth"}[pcm]

    monkeypatch.setattr(gateway, "transcribe_utterance", transcribe)
    stream.enqueue([b"1", b"2", b"3", b"4"])
    await asyncio.wait_for(stream.queue.join(), 1)
    results = [(call.args[1], call.args[2]) for call in stream.channel.send_event.call_args_list
               if call.args[1] != "meeting.input.transcribing"]
    assert [(event, payload["utterance_id"]) for event, payload in results] == [
        ("meeting.input.transcript", 1), ("meeting.input.error", 2), ("meeting.input.transcript", 4),
    ]
    assert [payload["transcript"] for event, payload in results if event == "meeting.input.transcript"] == ["First", "Fourth"]
    assert not stream.closed


@pytest.mark.asyncio
async def test_slow_delivery_does_not_block_asr_and_total_backlog_is_bounded(stream, monkeypatch):
    preparing = asyncio.Event()
    completed = asyncio.Event()
    calls = 0

    async def transcribe(_pcm):
        nonlocal calls
        calls += 1
        if calls == gateway.MAX_PENDING_UTTERANCES:
            completed.set()
        return "Text"

    async def prepare(_text):
        preparing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(gateway, "transcribe_utterance", transcribe)
    stream.prepare_transcript = prepare
    stream.enqueue([frame()])
    await asyncio.wait_for(preparing.wait(), 1)
    stream.enqueue([frame()] * (gateway.MAX_PENDING_UTTERANCES - 1))
    await asyncio.wait_for(completed.wait(), 1)
    with pytest.raises(ValueError, match="backlog is full"):
        stream.enqueue([frame()])
    await stream.close()
    await asyncio.wait_for(stream.queue.join(), 1)
    assert not stream.results
    assert not any(call.args[1] == "meeting.input.transcript" for call in stream.channel.send_event.call_args_list)


@pytest.mark.asyncio
async def test_interruption_control_failure_does_not_discard_transcribed_input(stream, monkeypatch):
    monkeypatch.setattr(gateway, "transcribe_utterance", AsyncMock(return_value="Wait, change the requirement"))
    stream.prepare_transcript = AsyncMock(side_effect=TimeoutError)
    stream.enqueue([frame()])
    await asyncio.wait_for(stream.queue.join(), 3)
    transcripts = [call.args[2] for call in stream.channel.send_event.call_args_list if call.args[1] == "meeting.input.transcript"]
    assert len(transcripts) == 1 and transcripts[0]["transcript"] == "Wait, change the requirement"
    assert not stream.closed


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
