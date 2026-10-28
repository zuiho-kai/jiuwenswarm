"""The UI switch configures agent-core's existing Team duplex router."""

import pytest
import yaml

from openjiuwen.agent_teams.duplex.config import DuplexConfig

from jiuwenswarm.agents.harness.team.config_loader import load_team_spec_dict
from jiuwenswarm.common import config
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.server.control import config_service


@pytest.mark.parametrize("enabled", [False, True])
def test_team_jev_switch_round_trip_and_native_sdk_config(monkeypatch, tmp_path, enabled):
    path = tmp_path / "config.yaml"
    path.write_text(
        "# user configuration\n"
        "browser:\n  decision:\n    mode: llm\n"
        "modes:\n  team:\n    jiuwen_team:\n"
        "      duplex:\n        enabled: false\n        mode: off\n        format: jev\n"
        "        api_base: https://router.example.test\n"
        "        model: custom-jev\n        api_key_env: TEAM_JEV_TEST_KEY\n"
        "        timeout_seconds: 0.5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)

    def read():
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    monkeypatch.setattr(handlers, "get_config_raw", read)
    monkeypatch.setattr(config_service, "_read_raw_config", read)
    assert config_service.get_panel()["team_jev_routing_enabled"] == "false"

    result = handlers.apply_config_payload({"team_jev_routing_enabled": enabled})
    assert result.yaml_updated == ["team_jev_routing_enabled"]
    assert result.env_updates == {}
    assert handlers.ConfigChangeSet({}, result.yaml_updated).reload_scopes == {"team"}

    raw = read()
    expected = "true" if enabled else "false"
    assert config_service.get_panel()["team_jev_routing_enabled"] == expected
    assert handlers.flatten_team_dispatch_for_config_panel(raw)["team_jev_routing_enabled"] == expected
    spec = load_team_spec_dict(config_base=raw)
    native = DuplexConfig.model_validate(spec["duplex"])
    assert native.active is enabled
    assert native.format == "jev"
    assert native.api_base == "https://router.example.test"
    assert native.model == "custom-jev"
    assert native.api_key_env == "TEAM_JEV_TEST_KEY"
    assert native.timeout_seconds == 0.5
    assert raw["browser"]["decision"]["mode"] == "llm"
    assert "# user configuration" in path.read_text(encoding="utf-8")
