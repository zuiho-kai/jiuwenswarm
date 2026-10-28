"""Meeting-only VAD settings: validation, persistence and configuration reads."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.meeting_speech_detection import (
    SPEECH_START_ENV,
    speech_start_settings,
)
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.server.control import config_service


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch):
    for env_key in SPEECH_START_ENV.values():
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setattr(handlers, "get_config_raw", lambda: {})
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: {})


@pytest.mark.asyncio
async def test_settings_round_trip_and_reset(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("UNRELATED_SETTING=keep\n", encoding="utf-8")
    monkeypatch.setattr(handlers, "ENV_FILE", env_file)
    assert speech_start_settings() == {"startThreshold": 0.5, "startFrames": 5}

    result = handlers.apply_config_payload({
        "meeting_vad_start_threshold": "0.65",
        "meeting_vad_start_frames": "3",
    })
    assert result.env_updates == {
        "MEETING_VAD_START_THRESHOLD": "0.65", "MEETING_VAD_START_FRAMES": "3",
    }
    assert handlers.ConfigChangeSet(result.env_updates, []).reload_scopes == {"web_ui"}
    assert speech_start_settings() == {"startThreshold": 0.65, "startFrames": 3}
    assert 'MEETING_VAD_START_THRESHOLD="0.65"' in env_file.read_text(encoding="utf-8")
    assert "UNRELATED_SETTING=keep" in env_file.read_text(encoding="utf-8")

    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_get_handler(channel, None, "test", {}, None)
    for payload in (channel.send_response.call_args.kwargs["payload"], config_service.get_panel()):
        assert payload["meeting_vad_start_threshold"] == "0.65"
        assert payload["meeting_vad_start_frames"] == "3"

    handlers.apply_config_payload({"meeting_vad_start_threshold": "", "meeting_vad_start_frames": ""})
    assert speech_start_settings() == {"startThreshold": 0.5, "startFrames": 5}
    assert config_service.get_panel()["meeting_vad_start_frames"] == "5"


@pytest.mark.parametrize("key,value", [
    ("meeting_vad_start_threshold", "-0.1"),
    ("meeting_vad_start_threshold", "1.1"),
    ("meeting_vad_start_threshold", "nan"),
    ("meeting_vad_start_threshold", "inf"),
    ("meeting_vad_start_threshold", "invalid"),
    ("meeting_vad_start_frames", "0"),
    ("meeting_vad_start_frames", "11"),
    ("meeting_vad_start_frames", "5.5"),
    ("meeting_vad_start_frames", "nan"),
    ("meeting_vad_start_frames", True),
])
def test_reject_invalid_settings_without_writing(monkeypatch, key, value):
    persist = AsyncMock()
    monkeypatch.setattr(handlers, "persist_env_updates", persist)
    with pytest.raises(handlers.ConfigPanelBadRequest):
        handlers.apply_config_payload({key: value})
    persist.assert_not_called()
    assert speech_start_settings() == {"startThreshold": 0.5, "startFrames": 5}


@pytest.mark.parametrize("threshold,frames,expected", [
    ("0", "1", {"startThreshold": 0.0, "startFrames": 1}),
    ("1", "10", {"startThreshold": 1.0, "startFrames": 10}),
    ("nan", "11", {"startThreshold": 0.5, "startFrames": 5}),
])
def test_environment_bounds_and_invalid_value_fallback(monkeypatch, threshold, frames, expected):
    monkeypatch.setenv("MEETING_VAD_START_THRESHOLD", threshold)
    monkeypatch.setenv("MEETING_VAD_START_FRAMES", frames)
    assert speech_start_settings() == expected
