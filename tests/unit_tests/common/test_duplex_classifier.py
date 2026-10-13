from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest
import yaml
from choice_classifier import (
    CRITERIA, INSTRUCTIONS, ChoiceAnswer, ClassifierTimeoutError, InvalidResponseError, ProviderError,
)
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

from jiuwenswarm.common import duplex_classifier as dc
from jiuwenswarm.common.config import migrate_config_from_template
from jiuwenswarm.common.duplex_router import ControlSnapshot, InboundMessage, migrate_legacy_router_settings

SNAPSHOT = ControlSnapshot("v1", "r1", "c1", "tool", goal="订单系统", next_action="Install Kafka",
                           last_action="Read requirements")
MESSAGES = (InboundMessage("m1", "A1", "客户禁止 Kafka。"), InboundMessage("m2", "user", "Use PostgreSQL."))
TEMPLATE = Path(dc.__file__).resolve().parents[1] / "resources" / "config.yaml"


def typed_answer(questions, p_first=0.95):
    answers = {}
    for name, question in questions.items():
        first, second = list(question["criteria"])
        answers[name] = {"type": "choice", "choice": first if p_first >= 0.5 else second,
                         "confidence": 0.8, "probabilities": {first: p_first, second: 1 - p_first}}
    return {"answers": answers}


def logprob_answer(p_a=0.95):
    return {"choices": [{"message": {"content": "A"}, "logprobs": {"content": [{"top_logprobs": [
        {"token": "A", "logprob": math.log(p_a)}, {"token": "B", "logprob": math.log(1 - p_a)}]}]}}]}


@pytest.fixture
def server(monkeypatch):
    """Route every client the providers create to an in-process handler."""
    state = NS(requests=[], respond=None, clients=[])

    async def handle(request):
        state.requests.append(request)
        return state.respond(request)

    original = httpx.AsyncClient

    def factory(**kwargs):
        state.clients.append(kwargs)
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setenv("DUPLEX_ROUTER_API_KEY", "test-only-secret")
    return state


def team_host(provider="OpenAI", api_base="http://team.local/v1", **client):
    config = TeamModelConfig(
        model_client_config=ModelClientConfig(client_provider=provider, api_base=api_base,
                                              api_key="team-secret", **client),
        model_request_config=ModelRequestConfig(model_name="deployed-fast"))
    return NS(tiny_agent_model_resolver=lambda name: config if name == "fast" else None)


@pytest.mark.asyncio
async def test_jev_asks_only_requested_question_with_snapshot_context(server):
    server.respond = lambda request: httpx.Response(200, json=typed_answer(json.loads(request.content)["questions"]))
    config = {"provider": "typed_choice", "typed_choice": {"preset": "jev"}}
    answers = await dc.classify(None, config, SNAPSHOT, MESSAGES, (dc.URGENCY,))
    answer, = answers.values()
    assert set(answers) == {dc.URGENCY} and isinstance(answer, ChoiceAnswer)
    assert answer.choice == "INTERRUPT" and answer.probabilities["INTERRUPT"] == pytest.approx(0.95)
    request, = server.requests
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer test-only-secret"
    body = json.loads(request.content)
    assert body["model"] == "jev-1.13.0"
    assert body["questions"] == {dc.URGENCY: {"type": "choice", "instructions": INSTRUCTIONS,
                                              "criteria": CRITERIA}}
    assert body["state"]["messages"] == ["客户禁止 Kafka。", "Use PostgreSQL."]
    assert json.loads(body["state"]["context"]) == {
        "goal": "订单系统", "current_plan": "Install Kafka", "last_action": "Read requirements", "phase": "tool"}


@pytest.mark.asyncio
async def test_both_questions_use_their_own_labels(server):
    server.respond = lambda request: httpx.Response(200, json=typed_answer(json.loads(request.content)["questions"]))
    config = {"provider": "typed_choice", "typed_choice": {"preset": "jev"}}
    answers = await dc.classify(None, config, SNAPSHOT, MESSAGES, (dc.URGENCY, dc.RELEVANCE))
    assert answers[dc.RELEVANCE].choice == "DELIVER"
    assert set(answers[dc.RELEVANCE].probabilities) == {"DELIVER", "SKIP"}
    question = json.loads(server.requests[0].content)["questions"][dc.RELEVANCE]
    assert "If evidence is insufficient, choose DELIVER." in question["instructions"]


@pytest.mark.asyncio
async def test_clef_builds_account_url_and_unwraps_workers_envelope(server, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct_1")
    server.respond = lambda request: httpx.Response(200, json={
        "success": True, "result": typed_answer(json.loads(request.content)["questions"], 0.2)})
    config = {"provider": "typed_choice", "typed_choice": {"preset": "clef", "model": "clef-flash"}}
    answers = await dc.classify(None, config, SNAPSHOT, MESSAGES, (dc.URGENCY,))
    assert answers[dc.URGENCY].choice == "APPEND"
    assert str(server.requests[0].url) == (
        "https://api.cloudflare.com/client/v4/accounts/acct_1/ai/run/@cf/cloudflare/clef-flash")
    server.respond = lambda request: httpx.Response(200, json={"success": False, "result": None})
    with pytest.raises(InvalidResponseError):
        await dc.classify(None, config, SNAPSHOT, MESSAGES, (dc.URGENCY,))


@pytest.mark.asyncio
@pytest.mark.parametrize("preset,url,tokens_field", [
    ("custom", "https://llm.example/v1/chat/completions", "max_tokens"),
    ("openai", "https://api.openai.com/v1/chat/completions", "max_completion_tokens"),
])
async def test_logprob_presets(server, preset, url, tokens_field):
    server.respond = lambda request: httpx.Response(200, json=logprob_answer(0.97))
    section = {"preset": preset, "model": "m1"}
    section["api_base"] = "" if preset == "openai" else "https://llm.example/v1"
    answers = await dc.classify(None, {"provider": "logprob_llm", "logprob_llm": section},
                                SNAPSHOT, MESSAGES, (dc.URGENCY,))
    assert answers[dc.URGENCY].choice == "INTERRUPT"
    request, = server.requests
    assert str(request.url) == url
    body = json.loads(request.content)
    assert body["model"] == "m1" and body[tokens_field] == 1 and body["logprobs"] is True


@pytest.mark.asyncio
async def test_team_model_uses_pool_endpoint_key_model_and_tls(server):
    server.respond = lambda request: httpx.Response(200, json=logprob_answer(0.5))
    host = team_host(api_base="http://team.local/v1/chat/completions", verify_ssl=False,
                     custom_headers={"X-Tenant": "t1"})
    config = {"provider": "logprob_llm", "logprob_llm": {"preset": "team_model", "model_name": "fast"}}
    answers = await dc.classify(host, config, SNAPSHOT, MESSAGES, (dc.URGENCY,))
    assert set(answers[dc.URGENCY].probabilities) == {"INTERRUPT", "APPEND"}
    request, = server.requests
    assert str(request.url) == "http://team.local/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer team-secret"
    assert request.headers["x-tenant"] == "t1"
    assert json.loads(request.content)["model"] == "deployed-fast"
    assert server.clients[0] == {"verify": False, "headers": {"X-Tenant": "t1"}}


@pytest.mark.parametrize("host,section,match", [
    (team_host(), {"preset": "team_model", "model_name": ""}, "model_name is required"),
    (team_host(), {"preset": "team_model", "model_name": "missing"}, "cannot be resolved"),
    (NS(), {"preset": "team_model", "model_name": "fast"}, "cannot be resolved"),
    (team_host(provider="Anthropic"), {"preset": "team_model", "model_name": "fast"}, "OpenAI-compatible"),
    (team_host(api_mode="responses"), {"preset": "team_model", "model_name": "fast"}, "OpenAI-compatible"),
    (None, {"preset": "custom", "api_base": "", "model": "x"}, "required"),
    (None, {"preset": "nope"}, "unsupported"),
])
def test_invalid_logprob_settings_raise_config_error(server, host, section, match):
    with pytest.raises(dc.ClassifierConfigError, match=match):
        dc.build_classifier(host, {"provider": "logprob_llm", "logprob_llm": section})


@pytest.mark.parametrize("config,match", [
    ({"provider": "other"}, "unsupported"),
    ({"provider": "typed_choice", "typed_choice": {"preset": "jev", "api_key_env": "UNSET_KEY_ENV"}},
     "UNSET_KEY_ENV"),
    ({"provider": "typed_choice", "typed_choice": {"preset": "jev", "endpoint": "file:///x"}}, "endpoint"),
    ({"provider": "typed_choice", "typed_choice": {"preset": "clef", "model": "jev-1.13.0"}}, "clef model"),
    ({"provider": "typed_choice", "typed_choice": {"preset": "clef", "account_id_env": "UNSET_ACCT"}},
     "UNSET_ACCT"),
    ({"provider": "typed_choice", "timeout_seconds": 0}, "timeout_seconds"),
])
def test_invalid_typed_choice_settings_raise_config_error(server, config, match):
    with pytest.raises(dc.ClassifierConfigError, match=match):
        dc.build_classifier(None, config)


@pytest.mark.asyncio
async def test_question_names_are_validated_before_any_request(server):
    for questions in ((), ("action",), (dc.URGENCY, dc.URGENCY)):
        with pytest.raises(dc.ClassifierConfigError):
            await dc.classify(None, {"provider": "typed_choice"}, SNAPSHOT, MESSAGES, questions)
    assert server.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("respond,error", [
    (lambda request: httpx.Response(500), ProviderError),
    (lambda request: httpx.Response(200, json={"answers": {}}), InvalidResponseError),
])
async def test_failures_raise_without_fallback(server, respond, error):
    server.respond = respond
    with pytest.raises(error):
        await dc.classify(None, {"provider": "typed_choice"}, SNAPSHOT, MESSAGES, (dc.URGENCY,))


@pytest.mark.asyncio
async def test_deadline_comes_from_timeout_seconds(server):
    import asyncio

    async def slow(**_):
        await asyncio.sleep(1)

    classifier = dc.build_classifier(None, {"provider": "typed_choice", "timeout_seconds": 0.01},
                                     questions=(dc.URGENCY,))
    classifier.provider.decide = slow
    with pytest.raises(ClassifierTimeoutError):
        await classifier.classify(context="c", messages=["m"])
    assert dc.build_classifier(None, {"provider": "typed_choice"}).config.timeout_seconds == 2.0


@pytest.mark.parametrize("legacy,expected", [
    ({"backend": "sdk", "model_name": "fast", "jev": {"interrupt_threshold": 0.95}},
     {"provider": "logprob_llm", "logprob_llm": {"preset": "team_model", "model_name": "fast"}}),
    ({"backend": "mindshub", "model_name": "", "mindshub": {"api_base": "https://m.example/v1",
                                                            "api_key_env": "MH_KEY"}},
     # MindsHub chat ignores logprobs; its JEV-compatible decisions endpoint is used instead.
     {"provider": "typed_choice", "typed_choice": {"preset": "jev",
                                                   "endpoint": "https://api.mindshub.ai/v1/decisions",
                                                   "model": "jev-1.13.0", "api_key_env": "MH_KEY"}}),
    ({"backend": "jev", "model_name": "jev-2", "jev": {"api_base": "https://j.example/v1/",
                                                       "endpoint_path": "decisions",
                                                       "interrupt_threshold": 0.95}},
     {"provider": "typed_choice", "typed_choice": {"preset": "jev", "endpoint": "https://j.example/v1/decisions",
                                                   "model": "jev-2"}}),
    ({"backend": "clef", "clef": {"model": "clef-flash", "account_id_env": "ACCT", "api_key_env": "CF",
                                  "api_base": "https://api.cloudflare.com/client/v4"}},
     {"provider": "typed_choice", "typed_choice": {"preset": "clef", "model": "clef-flash",
                                                   "account_id_env": "ACCT", "api_key_env": "CF"}}),
])
def test_legacy_backend_settings_migrate(legacy, expected):
    section = {"enabled": True, "mode": "active", "timeout_seconds": 3, **legacy}
    assert migrate_legacy_router_settings(section)
    assert section == {"enabled": True, "mode": "active", "timeout_seconds": 3, **expected}
    assert not migrate_legacy_router_settings(section)
    assert dc.router_settings({"mode": "active", **legacy}) == {"mode": "active", **expected}


def test_config_file_migration_rewrites_legacy_section(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("duplex_router:\n  enabled: true\n  mode: active\n  backend: jev\n  model_name: jev-1.13.0\n"
                    "  jev:\n    api_base: https://api.typesafe.ai/v1\n    endpoint_path: systemone\n"
                    "    interrupt_threshold: 0.9\n", encoding="utf-8")
    assert migrate_config_from_template(TEMPLATE, user)
    section = yaml.safe_load(user.read_text(encoding="utf-8"))["duplex_router"]
    assert section["enabled"] is True and section["mode"] == "active"
    assert section["provider"] == "typed_choice"
    assert section["typed_choice"]["endpoint"] == "https://api.typesafe.ai/v1/systemone"
    assert section["typed_choice"]["model"] == "jev-1.13.0"
    assert section["logprob_llm"]["preset"] == "team_model"
    assert not {"backend", "model_name", "jev", "mindshub", "clef"} & set(section)


def test_template_matches_the_facade_defaults():
    section = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))["duplex_router"]
    assert section["provider"] in dc.PROVIDERS
    assert section["typed_choice"]["preset"] in dc.TYPED_CHOICE_PRESETS
    assert section["logprob_llm"]["preset"] in dc.LOGPROB_LLM_PRESETS
    assert dc.provider_label(section) == "team_model"
