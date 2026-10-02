# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest

from jiuwenswarm.common.duplex_clef import classify_clef, decision_from_clef
from jiuwenswarm.common.duplex_router import ControlSnapshot, InboundMessage


def answer(choice, interrupt, append, confidence=None):
    return {
        "model": "clef",
        "answers": {"action": {
            "type": "choice",
            "choice": choice,
            "confidence": confidence if confidence is not None else max(interrupt, append),
            "probabilities": {"INTERRUPT": interrupt, "APPEND": append},
        }},
    }


def test_confident_interrupt_stays_interrupt():
    assert decision_from_clef(answer("INTERRUPT", 0.95, 0.05), 0.9) == {"action": "INTERRUPT"}


def test_weak_interrupt_becomes_append():
    assert decision_from_clef(answer("INTERRUPT", 0.7, 0.3), 0.9) == {"action": "APPEND"}


def test_workers_rest_envelope_is_unwrapped():
    payload = {"success": True, "errors": [], "result": answer("APPEND", 0.1, 0.9)}
    assert decision_from_clef(payload, 0.9) == {"action": "APPEND"}


def test_failed_workers_envelope_raises():
    with pytest.raises(ValueError, match="failed"):
        decision_from_clef({"success": False, "errors": [{"message": "no"}], "result": None}, 0.9)


def test_choice_that_disagrees_with_probabilities_raises():
    with pytest.raises(ValueError, match="disagrees"):
        decision_from_clef(answer("APPEND", 0.95, 0.05), 0.9)


@pytest.mark.asyncio
async def test_classify_clef_posts_a_choice_question(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_AUTH_TOKEN", "token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "abc123")
    seen = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return answer("INTERRUPT", 0.99, 0.01)

    class Client:
        async def post(self, url, headers, json):
            seen["url"] = str(url)
            seen["auth"] = headers["Authorization"]
            seen["body"] = json
            return Response()

        async def aclose(self):
            return None

    snapshot = ControlSnapshot("v", "r", "c", "model", goal="ship", next_action="use Kafka")
    result = await classify_clef(snapshot, (InboundMessage("m1", "user", "use Redis"),),
                                  settings={"interrupt_threshold": 0.9}, client=Client())
    assert result == {"action": "INTERRUPT"}
    assert seen["url"].endswith("/accounts/abc123/ai/run/@cf/cloudflare/clef")
    assert seen["auth"] == "Bearer token"
    assert seen["body"]["model"] == "clef"
    assert set(seen["body"]["questions"]["action"]["criteria"]) == {"APPEND", "INTERRUPT"}
    assert "use Redis" in seen["body"]["state"]
