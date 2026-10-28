"""Connection-owned microphone streams, server VAD, and ordered utterance ASR."""

from __future__ import annotations

import asyncio
import base64
import logging
from functools import partial
from uuid import uuid4

from .input_audio import UtteranceSegmenter, pcm_wav

MAX_CHUNK_BYTES = 6_400  # Up to 200 ms of signed little-endian PCM16.
MAX_PENDING_UTTERANCES = 4
logger = logging.getLogger(__name__)


async def transcribe_utterance(pcm: bytes) -> str:
    from jiuwenswarm.gateway.channel_manager.web.task_asr import (
        TaskAsrError, transcribe_task_audio,
    )
    from jiuwenswarm.extensions.video_duplex.backend.video_voice import is_ignorable_asr_filler

    # Meetings use Settings > Speech transcription, independently of video duplex.
    try:
        transcript = await transcribe_task_audio({
            "audio_base64": base64.b64encode(pcm_wav(pcm)).decode("ascii"),
            "mime_type": "audio/wav",
        })
    except TaskAsrError as exc:
        if exc.code == "ASR_EMPTY_TRANSCRIPT":
            return ""
        raise
    return "" if is_ignorable_asr_filler(transcript) else transcript.strip()


class MeetingInputStream:
    def __init__(self, channel, ws, session_id, meeting_id):
        self.channel, self.ws = channel, ws
        self.session_id, self.meeting_id = session_id, meeting_id
        self.id = uuid4().hex
        self.closed = False
        self.listening = True
        self.audio_lock = asyncio.Lock()
        self.pending_audio_bytes = 0
        self.sequence = 0
        self.utterance = 0
        self.segmenter = None
        self.queue = asyncio.Queue(maxsize=MAX_PENDING_UTTERANCES)
        self.worker = None
        self.initializing = None
        self.prepare_transcript = None
        self.admit_transcript = None
        self.admissions: dict[int, str] = {}

    async def start(self):
        if self.initializing is None:
            self.initializing = asyncio.create_task(self.initialize())
        await asyncio.shield(self.initializing)

    async def initialize(self):
        self.segmenter = await asyncio.to_thread(UtteranceSegmenter)
        if self.closed:
            raise ValueError("Meeting microphone stream has closed")
        self.worker = asyncio.create_task(self.transcribe())

    def envelope(self):
        return {"session_id": self.session_id, "meeting_id": self.meeting_id, "stream_id": self.id}

    async def emit(self, event, **payload):
        if not self.closed:
            await self.channel.send_event(self.ws, event, {**self.envelope(), **payload})

    async def append(self, params):
        sequence, encoded = params.get("sequence"), params.get("audio_base64")
        if type(sequence) is not int or sequence != self.sequence:
            raise ValueError("Out-of-order meeting microphone audio")
        if not isinstance(encoded, str) or not 0 < len(encoded) <= 8_536:
            raise ValueError("Invalid meeting audio chunk size")
        try:
            pcm = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ValueError("Invalid meeting PCM encoding") from None
        if not 0 < len(pcm) <= MAX_CHUNK_BYTES or len(pcm) % 2:
            raise ValueError("Expected mono PCM16 audio at 16000 Hz")
        if self.closed or not self.listening or self.segmenter is None:
            raise ValueError("Meeting microphone stream is not ready")
        if self.pending_audio_bytes + len(pcm) > 64_000:
            raise ValueError("Meeting microphone backlog is full")
        # Reserve sequence/buffer space before yielding; concurrent RPCs may
        # arrive while the preceding frame is being inferred off-thread.
        self.sequence += 1
        self.pending_audio_bytes += len(pcm)
        try:
            async with self.audio_lock:
                if self.closed:
                    raise ValueError("Meeting microphone stream has closed")
                utterances = await asyncio.to_thread(self.segmenter.feed, pcm)
                if self.closed:
                    raise ValueError("Meeting microphone stream has closed")
                self.enqueue(utterances)
        finally:
            self.pending_audio_bytes -= len(pcm)

    def enqueue(self, utterances):
        for pcm in utterances:
            if self.queue.full():
                raise ValueError("Meeting transcription backlog is full")
            self.utterance += 1
            self.queue.put_nowait((self.utterance, pcm))

    async def listening_control(self, listening):
        if self.closed or self.pending_audio_bytes or self.audio_lock.locked():
            raise ValueError("Meeting microphone stream is not ready")
        async with self.audio_lock:
            self.listening = False
            pcm = await asyncio.to_thread(self.segmenter.flush)
            if self.closed:
                raise ValueError("Meeting microphone stream has closed")
            if pcm:
                self.enqueue([pcm])
            self.listening = listening

    async def transcribe(self):
        while not self.closed:
            utterance_id, pcm = await self.queue.get()
            try:
                await self.emit("meeting.input.transcribing", utterance_id=utterance_id)
                async with asyncio.timeout(60):
                    text = await transcribe_utterance(pcm)
                prepared = {}
                if text and not self.closed and self.prepare_transcript is not None:
                    try:
                        prepared = await self.prepare_transcript(text)
                    except Exception:
                        # ASR already succeeded. An unavailable interruption
                        # control must not discard the human's actual message.
                        logger.warning("Meeting interruption preparation failed; delivering transcript", exc_info=True)
                    if prepared.get("interruption_id"):
                        self.admissions[utterance_id] = prepared["interruption_id"]
                await self.emit("meeting.input.transcript", utterance_id=utterance_id, transcript=text, **prepared)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self.emit("meeting.input.error", utterance_id=utterance_id, code="transcriptionFailed")
            finally:
                self.queue.task_done()

    def revoke(self):
        if self.closed:
            return
        self.closed = True
        for task in (self.worker, self.initializing):
            if task is not None:
                task.cancel()

    async def close(self):
        self.revoke()
        tasks = [task for task in (self.worker, self.initializing) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()

    async def admitted(self, params):
        utterance_id, interruption_id = params.get("utterance_id"), params.get("interruption_id")
        if type(utterance_id) is not int or self.admissions.get(utterance_id) != interruption_id or not interruption_id:
            raise ValueError("Unknown human-input interruption")
        await self.admit_transcript(interruption_id)



def bind_input_handlers(channel, route_for):
    """Use the meeting audio route's existing ownership and cleanup lifecycle."""
    async def control(ws, req_id, params, session_id, user_id=None, *, operation):
        stream = None
        try:
            params = params if isinstance(params, dict) else {}
            candidate = route_for(ws, params, session_id).input
            if candidate.closed or params.get("stream_id") != candidate.id:
                raise ValueError("Unknown or stale meeting microphone stream")
            stream = candidate
            if operation == "append":
                await stream.append(params)
            elif operation == "admitted":
                await stream.admitted(params)
            elif operation == "stop":
                await stream.close()
            else:
                await stream.listening_control(operation == "resume")
            await channel.send_response(ws, req_id, ok=True, payload={"accepted": True})
        except Exception as exc:
            if stream is not None and operation != "admitted":
                await stream.close()
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="MEETING_INPUT_FAILED")

    for operation in ("append", "pause", "resume", "stop", "admitted"):
        channel.register_method(f"meeting.input.{operation}", partial(control, operation=operation), local_only=True)
