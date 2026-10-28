"""The Team meeting toggle persists through the existing Experimental settings contract."""

import pytest
import yaml

from jiuwenswarm.common import config


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
