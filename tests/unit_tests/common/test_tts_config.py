"""Shared TTS persistence, provider switching and credential boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.common.security import base_crypto
from jiuwenswarm.gateway.voice.config import TtsConfig
from jiuwenswarm.server.control import config_service


@pytest.mark.asyncio
async def test_shared_settings_round_trip_and_atomic_provider_switch(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ASR_MODEL_NAME=keep\n", encoding="utf-8")
    monkeypatch.setattr(handlers, "ENV_FILE", env_file)
    monkeypatch.setattr(handlers, "get_config_raw", lambda: {})
    monkeypatch.setattr(handlers, "get_config", lambda: {})
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: {})
    crypto = SimpleNamespace(encrypt=lambda value: "encoded:" + value, decrypt=lambda value: value.removeprefix("encoded:"))
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: crypto)
    monkeypatch.setattr(base_crypto, "get_crypto_provider", lambda: crypto)
    for key in ("TTS_PROVIDER", "TTS_API_BASE", "TTS_API_KEY", "TTS_MODEL_NAME", "TTS_VOICE"):
        monkeypatch.setenv(key, "")
    for provider, url, model, voice in (
        ("cosyvoice", "https://speech.example/v1", "FunAudioLLM/CosyVoice2-0.5B", "FunAudioLLM/CosyVoice2-0.5B:anna"),
        ("qwen_omni", "wss://speech.example/realtime", "qwen3.8-omni-flash-realtime", "Tina"),
    ):
        result = handlers.apply_config_payload({
            "tts_provider": provider, "tts_api_base": url, "tts_api_key": "speech-secret",
            "tts_model": model, "tts_voice": voice,
        })
        config = TtsConfig.from_environment()
        config.validate()
        assert config == TtsConfig(url, "speech-secret", model, voice, provider)
        stored = env_file.read_text(encoding="utf-8")
        assert stored.startswith("ASR_MODEL_NAME=keep\n") and '"speech-secret"' not in stored
        assert handlers.ConfigChangeSet(result.env_updates, []).reload_scopes == {"web_ui"}
        channel = SimpleNamespace(send_response=AsyncMock())
        await handlers.config_get_handler(channel, None, "read", {}, None)
        for panel in (channel.send_response.call_args.kwargs["payload"], config_service.get_panel()):
            assert panel["tts_api_key"] == "" and panel["tts_key_configured"] is True
            assert panel["tts_api_base"] == url and panel["tts_model"] == model
        other = "qwen_omni" if provider == "cosyvoice" else "cosyvoice"
        with pytest.raises(handlers.ConfigPanelBadRequest):
            handlers.apply_config_payload({"tts_provider": other})
        assert env_file.read_text(encoding="utf-8") == stored and TtsConfig.from_environment() == config


def test_missing_tts_key_does_not_use_other_credentials(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "cosyvoice")
    for key in ("TTS_API_KEY", "TTS_API_BASE", "TTS_MODEL_NAME", "TTS_VOICE"):
        monkeypatch.delenv(key, raising=False)
    for key in ("QWEN_OMNI_API_KEY", "ASR_API_KEY", "VOICE_API_KEY"):
        monkeypatch.setenv(key, "other-service-key")
    with pytest.raises(ValueError, match="separate TTS API key"):
        TtsConfig.from_environment().validate()
