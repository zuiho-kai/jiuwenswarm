from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.common.config_panel.models_handlers import ConfigPanelBadRequest
from jiuwenswarm.common.duplex_router import env_secret


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("preferred_language: en\nother: keep\nduplex_router: {}\n")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(handlers, "get_config_raw", lambda: yaml.safe_load(path.read_text()))
    monkeypatch.setattr(handlers, "get_config", lambda: yaml.safe_load(path.read_text()))
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    monkeypatch.setattr(handlers, "persist_env_updates", lambda updates: None)
    monkeypatch.setenv("DUPLEX_ROUTER_API_KEY", "")
    return path


async def _settings_payload():
    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_get_handler(channel, None, "r1", {}, "s1")
    return channel.send_response.await_args.kwargs["payload"]


@pytest.mark.asyncio
async def test_typed_choice_settings_round_trip(settings_file):
    values = {"enabled": "true", "mode": "active", "provider": "typed_choice", "timeout_seconds": "4",
              "typed_preset": "jev", "typed_endpoint": "https://api.typesafe.ai/v1/decisions",
              "typed_model": "jev-1.13.0"}
    result = handlers.apply_config_payload({"duplex_router_" + key: value for key, value in values.items()})
    raw = yaml.safe_load(settings_file.read_text())
    assert raw["other"] == "keep"
    assert raw["duplex_router"]["enabled"] is True
    assert raw["duplex_router"]["timeout_seconds"] == 4
    assert raw["duplex_router"]["typed_choice"] == {
        "preset": "jev", "endpoint": values["typed_endpoint"], "model": "jev-1.13.0"}
    assert len(result.yaml_updated) == len(values)
    payload = await _settings_payload()
    for key, value in values.items():
        expected = "4.0" if key == "timeout_seconds" else value
        assert payload["duplex_router_" + key] == expected


@pytest.mark.asyncio
async def test_logprob_llm_settings_round_trip(settings_file):
    handlers.apply_config_payload({
        "duplex_router_provider": "logprob_llm", "duplex_router_llm_preset": "openai",
        "duplex_router_llm_api_base": "https://api.openai.com/v1", "duplex_router_llm_model": "gpt-4o-mini"})
    raw = yaml.safe_load(settings_file.read_text())["duplex_router"]
    assert raw["provider"] == "logprob_llm"
    assert raw["logprob_llm"] == {"preset": "openai", "api_base": "https://api.openai.com/v1",
                                  "model": "gpt-4o-mini"}
    assert "typed_choice" not in raw
    payload = await _settings_payload()
    assert payload["duplex_router_llm_preset"] == "openai"
    assert payload["duplex_router_llm_model"] == "gpt-4o-mini"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy,expected", [
    ({"backend": "sdk", "model_name": "team-a"},
     {"provider": "logprob_llm", "llm_preset": "team_model", "llm_model_name": "team-a"}),
    ({"backend": "mindshub", "model_name": "", "mindshub": {"api_base": "https://m.example/v1"}},
     {"provider": "logprob_llm", "llm_preset": "mindshub", "llm_api_base": "https://m.example/v1",
      "llm_model": "mindshub_air"}),
    ({"backend": "jev", "model_name": "jev-2", "jev": {"api_base": "https://j.example/v1/",
                                                      "endpoint_path": "decisions"}},
     {"provider": "typed_choice", "typed_preset": "jev", "typed_endpoint": "https://j.example/v1/decisions",
      "typed_model": "jev-2"}),
    ({"backend": "clef", "clef": {"model": "clef-flash"}},
     {"provider": "typed_choice", "typed_preset": "clef", "typed_model": "clef-flash"}),
])
async def test_legacy_backend_config_reads_as_kind_and_preset(settings_file, legacy, expected):
    settings_file.write_text(yaml.safe_dump({"duplex_router": {"mode": "active", **legacy}}))
    payload = await _settings_payload()
    for key, value in expected.items():
        assert payload["duplex_router_" + key] == value


def test_supervisor_can_be_disabled_without_clearing_settings(settings_file):
    handlers.apply_config_payload({"duplex_router_mode": "active", "duplex_router_enabled": "false"})
    raw = yaml.safe_load(settings_file.read_text())["duplex_router"]
    assert raw["enabled"] is False
    assert raw["mode"] == "active"


@pytest.mark.parametrize("params", [
    {"duplex_router_mode": "shadow"}, {"duplex_router_provider": "sdk"},
    {"duplex_router_typed_preset": "mindshub"}, {"duplex_router_llm_preset": "jev"},
    {"duplex_router_timeout_seconds": "nan"}, {"duplex_router_timeout_seconds": "0"},
    {"duplex_router_llm_api_base": "file:///tmp/secret"},
    {"duplex_router_typed_endpoint": "https://user:pw@api.example/v1"},
    {"duplex_router_typed_preset": "clef", "duplex_router_typed_model": "gpt-4o"},
    {"duplex_router_mode": "active", "duplex_router_llm_api_base": "ftp://x"},
])
def test_invalid_settings_are_rejected_without_writing(settings_file, params):
    before = settings_file.read_bytes()
    with pytest.raises(ConfigPanelBadRequest):
        handlers.apply_config_payload(params)
    assert settings_file.read_bytes() == before


def test_api_key_uses_existing_environment_key_flow(settings_file):
    result = handlers.apply_config_payload({"duplex_router_api_key": "unit-test-key"})
    assert result.env_updates == {"DUPLEX_ROUTER_API_KEY": "unit-test-key"}
    assert env_secret("DUPLEX_ROUTER_API_KEY") == "unit-test-key"
    raw = yaml.safe_load(settings_file.read_text())["duplex_router"]
    assert raw["typed_choice"]["api_key_env"] == raw["logprob_llm"]["api_key_env"] == "DUPLEX_ROUTER_API_KEY"
    assert "unit-test-key" not in settings_file.read_text()


def test_clef_account_and_model_use_runtime_settings(settings_file, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "")
    result = handlers.apply_config_payload({"duplex_router_typed_preset": "clef",
                                           "duplex_router_account_id": "test-account",
                                           "duplex_router_typed_model": "clef-flash"})
    assert result.env_updates["CLOUDFLARE_ACCOUNT_ID"] == "test-account"
    typed = yaml.safe_load(settings_file.read_text())["duplex_router"]["typed_choice"]
    assert typed["model"] == "clef-flash"
    assert typed["account_id_env"] == "CLOUDFLARE_ACCOUNT_ID"
    assert "test-account" not in settings_file.read_text()


def test_clear_disables_supervisor_and_clears_shared_secrets(settings_file, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    result = handlers.apply_config_payload({
        "duplex_router_mode": "off", "duplex_router_enabled": "false", "duplex_router_llm_model_name": "",
        "duplex_router_api_key": "", "duplex_router_account_id": ""})
    assert result.env_updates == {"DUPLEX_ROUTER_API_KEY": "", "CLOUDFLARE_ACCOUNT_ID": ""}
    raw = yaml.safe_load(settings_file.read_text())["duplex_router"]
    assert raw["enabled"] is False and raw["mode"] == "off"
