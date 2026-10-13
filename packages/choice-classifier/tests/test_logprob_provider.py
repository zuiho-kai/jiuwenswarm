import asyncio
import json
import math
from dataclasses import replace

import httpx
import pytest

from choice_classifier import (
    ChoiceAnswer, ClassifierConfig, InvalidInputError, InvalidResponseError,
    ChoiceClassifier, ClassifierTimeoutError, LogprobLLMConfig, LogprobLLMProvider, ProviderError,
)
from choice_classifier.logprob_provider import parse_logprobs

CONFIG = LogprobLLMConfig("https://llm.example/v1/", "test-model", "test-secret")


def response(entries=None):
    if entries is None:
        entries = [(" A ", math.log(0.3)), ("B", math.log(0.1)), ("unrelated", math.log(0.6))]
    return {"choices": [{"message": {"content": "B"}, "logprobs": {"content": [{
        "top_logprobs": [{"token": token, "logprob": value} for token, value in entries],
    }]}}]}


async def classify(handler, config=CONFIG, classifier_config=None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        try:
            return await ChoiceClassifier(LogprobLLMProvider(config, client=client), classifier_config).classify(
                context="Working on a report.", messages=["Use v2.", "Keep CSV."],
            )
        finally:
            assert not client.is_closed


def test_request_contract_and_normalized_answer_ignores_text_choice():
    def handler(request):
        assert str(request.url) == "https://llm.example/v1/chat/completions"
        assert request.method == "POST"
        assert request.headers["authorization"] == "Bearer test-secret"
        payload = json.loads(request.content)
        assert {k: v for k, v in payload.items() if k != "messages"} == {
            "model": "test-model", "max_tokens": 1, "logprobs": True,
            "top_logprobs": 20, "temperature": 0,
            "thinking": {"type": "disabled"},
        }
        assert "A = INTERRUPT:" in payload["messages"][0]["content"]
        assert "B = APPEND:" in payload["messages"][0]["content"]
        assert "Output only one letter." in payload["messages"][0]["content"]
        assert json.loads(payload["messages"][1]["content"]) == {
            "context": "Working on a report.", "messages": ["Use v2.", "Keep CSV."],
        }
        return httpx.Response(200, json=response())

    answer = asyncio.run(classify(handler))["action"]
    assert answer.choice == "INTERRUPT"
    assert answer.probabilities == pytest.approx({"INTERRUPT": 0.75, "APPEND": 0.25})
    assert answer.confidence == pytest.approx(0.75)
    assert "test-secret" not in repr(CONFIG)


@pytest.mark.parametrize("entries,expected", [
    ([("B", -2)], ChoiceAnswer("APPEND", {"INTERRUPT": 0, "APPEND": 1}, 1)),
    ([("A", -10000), ("B", -10000)], ChoiceAnswer("INTERRUPT", {"INTERRUPT": 0.5, "APPEND": 0.5}, 0.5)),
    ([("A", math.log(0.3)), (" A", math.log(0.3)), ("B", math.log(0.4))],
     ChoiceAnswer("INTERRUPT", {"INTERRUPT": 0.6, "APPEND": 0.4}, 0.6)),
])
def test_missing_labels_underflow_and_whitespace_variants(entries, expected):
    result = asyncio.run(classify(lambda req: httpx.Response(200, json=response(entries))))
    answer = result["action"]
    assert answer.choice == expected.choice
    assert answer.probabilities == pytest.approx(expected.probabilities)
    assert answer.confidence == pytest.approx(expected.confidence)


def test_small_nonzero_probabilities_are_preserved():
    answer = parse_logprobs(response([("A", -40), ("B", 0)]), CONFIG.questions["action"])
    assert answer.probabilities["INTERRUPT"] > 0
    assert answer.probabilities["INTERRUPT"] == pytest.approx(math.exp(-40), rel=1e-12, abs=0)
    assert answer.choice == "APPEND"


@pytest.mark.parametrize("body", [
    None, {}, {"choices": []}, {"choices": [{"message": {"content": "A"}}]},
    {"choices": [{"logprobs": None}]}, {"choices": [{"logprobs": {"content": []}}]},
    response([]), response([("other", -1)]),
    response([("A", None)]), response([("A", True)]), response([("A", 0.1)]),
])
def test_missing_or_invalid_logprobs_are_not_text_fallbacks(body):
    with pytest.raises(InvalidResponseError):
        asyncio.run(classify(lambda req: httpx.Response(200, json=body)))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 10**1000])
def test_nonfinite_or_huge_logprob(value):
    with pytest.raises(InvalidResponseError):
        parse_logprobs(response([("A", value)]), CONFIG.questions["action"])


def test_questions_run_concurrently_and_keep_their_criteria():
    async def run():
        both_entered = asyncio.Event()
        requests = []
        config = CONFIG.add_question("relevance", "Is it relevant?", {"DELIVER": "Relevant", "SKIP": "Unrelated"})

        async def handler(request):
            prompt = json.loads(request.content)["messages"][0]["content"]
            requests.append(prompt)
            if len(requests) == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), 1)
            return httpx.Response(200, json=response([("B" if "A = DELIVER" in prompt else "A", -1)]))

        result = await classify(handler, config)
        assert result == {
            "action": ChoiceAnswer("INTERRUPT", {"INTERRUPT": 1, "APPEND": 0}, 1),
            "relevance": ChoiceAnswer("SKIP", {"DELIVER": 0, "SKIP": 1}, 1),
        }
        assert len(requests) == 2

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["error", "timeout", "cancel"])
def test_failure_and_cancellation_stop_all_question_requests(failure):
    async def run():
        started = asyncio.Event()
        stopped = []
        count = 0

        async def handler(request):
            nonlocal count
            count += 1
            index = count
            if count == 2:
                started.set()
            try:
                await started.wait()
                if failure == "error" and index == 1:
                    return httpx.Response(200, json=response([]))
                await asyncio.Event().wait()
            finally:
                stopped.append(index)

        config = CONFIG.add_question("second")
        task = asyncio.create_task(classify(handler, config, ClassifierConfig(timeout_seconds=0.1)))
        await asyncio.wait_for(started.wait(), 1)
        if failure == "cancel":
            task.cancel()
        error = {"error": InvalidResponseError, "timeout": ClassifierTimeoutError, "cancel": asyncio.CancelledError}[failure]
        with pytest.raises(error):
            await task
        assert sorted(stopped) == [1, 2]

    asyncio.run(run())


def test_config_limits_and_empty_questions_make_no_request():
    bare = LogprobLLMConfig.without_questions(CONFIG.endpoint, CONFIG.model, "secret")
    with pytest.raises(ValueError, match="26"):
        bare.add_question("many", "Choose", {str(i): "option" for i in range(27)})
    with pytest.raises(InvalidInputError):
        asyncio.run(classify(lambda req: pytest.fail("unexpected request"), bare))
    with pytest.raises(ValueError, match="disable_thinking"):
        replace(CONFIG, disable_thinking=1)


def test_thinking_field_can_be_omitted():
    def handler(request):
        assert "thinking" not in json.loads(request.content)
        return httpx.Response(200, json=response())

    result = asyncio.run(classify(handler, replace(CONFIG, disable_thinking=False)))
    assert result["action"].choice == "INTERRUPT"


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_http_failures_not_retried(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://other.example/"})

    with pytest.raises(ProviderError):
        asyncio.run(classify(handler))
    assert len(calls) == 1


def test_response_size_limit():
    with pytest.raises(InvalidResponseError, match="max_response_bytes"):
        asyncio.run(classify(lambda req: httpx.Response(200, content=b"x" * 20),
                             replace(CONFIG, max_response_bytes=10)))


def test_owned_client_closed(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response())))
    monkeypatch.setattr("choice_classifier.logprob_provider.httpx.AsyncClient", lambda: client)
    result = asyncio.run(ChoiceClassifier(LogprobLLMProvider(CONFIG)).classify(context="working", messages=["update"]))
    assert result["action"].choice == "INTERRUPT"
    assert client.is_closed
