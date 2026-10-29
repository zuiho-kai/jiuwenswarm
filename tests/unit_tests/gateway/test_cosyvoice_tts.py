"""CosyVoice protocol behind the general TTS interface, with bounded cancellation."""

import asyncio
import json

import httpx
import pytest

from jiuwenswarm.gateway.voice.config import TtsConfig
from jiuwenswarm.gateway.voice import tts
from jiuwenswarm.gateway.voice.providers import cosyvoice

COSYVOICE_MODEL = "FunAudioLLM/CosyVoice2-0.5B"
CONFIG = TtsConfig("https://speech.example/v1", "private-key", COSYVOICE_MODEL, f"{COSYVOICE_MODEL}:anna")


MP3_AUDIO = b"mp3-audio"


def mock_provider(monkeypatch, handler):
    factory = httpx.AsyncClient
    clients = []

    def create(**kwargs):
        client = factory(**kwargs, transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    monkeypatch.setattr(cosyvoice.httpx, "AsyncClient", create)
    return clients


@pytest.mark.asyncio
async def test_supplied_text_voice_and_client_reuse(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=MP3_AUDIO)

    clients = mock_provider(monkeypatch, handler)
    session = tts.TtsSession(CONFIG)
    try:
        for name in ("anna", "alex"):
            text = "Do not change the deadline: 12:30."
            result = await session.synthesize(text, f"{COSYVOICE_MODEL}:{name}")
            request = requests[-1]
            assert str(request.url) == "https://speech.example/v1/audio/speech"
            assert request.headers["authorization"] == "Bearer private-key"
            assert json.loads(request.content) == {
                "model": COSYVOICE_MODEL, "input": text, "voice": f"{COSYVOICE_MODEL}:{name}",
                "response_format": "mp3",
            }
            assert result.audio == MP3_AUDIO and result.audio_mime == "audio/mpeg"
        assert len(clients) == 1
    finally:
        await session.aclose()
    assert clients[0].is_closed
    with pytest.raises(tts.TtsError, match="closed"):
        await session.synthesize("No replay", CONFIG.voice)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,code", [
    (401, b"private-key", "PROVIDER_ERROR"),
    (200, b"", "PROVIDER_ERROR"),
    (200, b"x" * (tts.MAX_AUDIO_BYTES + 1), "AUDIO_TOO_LARGE"),
], ids=["unauthorized", "empty", "oversized"])
async def test_provider_errors_and_incomplete_audio(monkeypatch, status, body, code):
    mock_provider(monkeypatch, lambda _: httpx.Response(status, content=body))
    session = tts.TtsSession(CONFIG)
    try:
        with pytest.raises(tts.TtsError) as error:
            await session.synthesize("Read this", CONFIG.voice)
        assert error.value.code == code
        assert CONFIG.api_key not in str(error.value)
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_cancellation_propagates_and_closes_http_response(monkeypatch):
    entered, closed = asyncio.Event(), asyncio.Event()
    calls = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Future()
            yield b""

        async def aclose(self):
            closed.set()

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=Stream())

    mock_provider(monkeypatch, handler)
    session = tts.TtsSession(CONFIG)
    task = asyncio.create_task(session.synthesize("Read this", CONFIG.voice))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await session.aclose()
    assert closed.is_set() and len(calls) == 1
