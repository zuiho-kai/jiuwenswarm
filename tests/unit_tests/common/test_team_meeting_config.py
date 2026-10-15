"""The Team meeting toggle persists through the existing Experimental settings contract."""

import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.common.config_panel.config_set_handlers import ConfigChangeSet
from jiuwenswarm.server.control import config_service


@pytest.mark.parametrize("enabled", [False, True])
def test_meeting_toggle_preserves_existing_settings(monkeypatch, tmp_path, enabled):
    path = tmp_path / "config.yaml"
    path.write_text("# user settings\nexperimental:\n  task_asr_enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    config.update_team_meeting_in_config(enabled)
    text = path.read_text(encoding="utf-8")
    result = yaml.safe_load(text)
    assert result["experimental"]["team_meeting_enabled"] is enabled
    assert result["experimental"]["task_asr_enabled"] is True
    assert "# user settings" in text


@pytest.mark.parametrize("raw,expected", [
    ({}, "false"),
    ({"experimental": None}, "false"),
    ({"experimental": {"team_meeting_enabled": True}}, "true"),
])
def test_meeting_toggle_reads_through_front_config_service(monkeypatch, raw, expected):
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: raw)
    assert config_service.get_panel()["team_meeting_enabled"] == expected


def test_meeting_toggle_only_reloads_ui():
    assert ConfigChangeSet(env_updates={}, yaml_updated=["team_meeting_enabled"]).reload_scopes == {"web_ui"}
