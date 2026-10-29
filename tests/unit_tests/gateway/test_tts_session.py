"""General TTS interface: provider selection and shared credential forwarding."""

import pytest

from jiuwenswarm.gateway.voice.providers import qwen_omni
from jiuwenswarm.gateway.voice.tts import TtsSession
from tests.unit_tests.gateway.test_qwen_tts import RealtimeSocket


@pytest.mark.asyncio
async def test_general_tts_interface_routes_qwen_using_shared_tts_credentials(monkeypatch):
    for key, value in {
        "TTS_PROVIDER": "qwen_omni", "TTS_API_BASE": "wss://speech.example/realtime",
        "TTS_API_KEY": "speech-only-key", "TTS_MODEL_NAME": "qwen3.8-omni-flash-realtime",
        "TTS_VOICE": "Tina", "QWEN_OMNI_API_KEY": "conversation-key",
        "QWEN_OMNI_REALTIME_URL": "wss://conversation.example/realtime",
    }.items():
        monkeypatch.setenv(key, value)
    requests = []
    socket = RealtimeSocket()

    def connect(url, **kwargs):
        requests.append((url, kwargs))
        return socket

    monkeypatch.setattr(qwen_omni.websockets, "connect", connect)
    session = TtsSession()
    try:
        await session.start()
        await session.synthesize("The meeting is ready.", "Tina")
        assert len(requests) == 1
        url, options = requests[0]
        assert url == "wss://speech.example/realtime?model=qwen3.8-omni-flash-realtime"
        assert options["additional_headers"]["Authorization"] == "Bearer speech-only-key"
    finally:
        await session.aclose()
