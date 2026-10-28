"""Persistent transport and supplied-text readings across owned turns."""

import asyncio
import base64
import json

import pytest

from jiuwenswarm.gateway.voice.providers import qwen_omni
from jiuwenswarm.gateway.voice.tts import QwenOmniTtsSession, TtsError

CONFIG = qwen_omni.QwenOmniRealtimeConfig("wss://example.test/realtime", "private-key", "qwen3.8-omni-flash-realtime", "Tina")


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
    session = QwenOmniTtsSession(CONFIG)
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
    session = QwenOmniTtsSession(CONFIG)
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
