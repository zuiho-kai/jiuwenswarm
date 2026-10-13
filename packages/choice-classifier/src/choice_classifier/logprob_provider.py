"""Choice probabilities from one-token OpenAI-compatible LLM responses."""

import asyncio
import json
import math
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

import httpx

from .answers import ChoiceAnswer, Decision, validate_answer
from .errors import InvalidInputError, InvalidResponseError
from .typed_choice_provider import ChoiceQuestion, TypedChoiceConfig, post_json


# Request fields the provider owns; extra_body cannot replace them.
_RESERVED_FIELDS = frozenset({
    "model", "messages", "logprobs", "top_logprobs", "temperature",
    "max_tokens", "max_completion_tokens",
})


@dataclass(frozen=True, slots=True)
class LogprobLLMConfig(TypedChoiceConfig):
    """endpoint is the API base URL, e.g. https://example.com/v1.

    The default request uses only standard chat completion fields. Vendor
    differences live here: max_tokens_field names the one-token limit, and
    extra_body adds vendor fields such as {"thinking": {"type": "disabled"}}.
    """

    max_tokens_field: str = "max_tokens"
    extra_body: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super(LogprobLLMConfig, self).__post_init__()
        if self.max_tokens_field not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("max_tokens_field must be max_tokens or max_completion_tokens")
        if isinstance(self.extra_body, str) or not isinstance(self.extra_body, Mapping):
            raise ValueError("extra_body must be a mapping")
        extra = dict(self.extra_body)
        if any(type(key) is not str for key in extra):
            raise ValueError("extra_body keys must be text")
        if reserved := sorted(_RESERVED_FIELDS & set(extra)):
            raise ValueError(f"extra_body cannot set {', '.join(reserved)}")
        try:
            json.dumps(extra)
        except (TypeError, ValueError):
            raise ValueError("extra_body must be JSON-serializable") from None
        object.__setattr__(self, "extra_body", extra)
        if any(len(question.criteria) > 26 for question in self.questions.values()):
            raise ValueError("logprob questions support at most 26 labels (A-Z)")

    @classmethod
    def openai(
        cls, model: str, api_key: str, *, endpoint: str = "https://api.openai.com/v1",
        reasoning_effort: str | None = None, **kwargs: Any,
    ) -> Self:
        """OpenAI Chat Completions: max_completion_tokens and no vendor fields.

        OpenAI rejects unknown fields and supports logprobs only on models that
        do not reason. Pass reasoning_effort="none" for reasoning-capable models
        that accept it; leave it None for models such as gpt-4.1.
        """
        extra = dict(kwargs.pop("extra_body", {}))
        if reasoning_effort is not None:
            extra["reasoning_effort"] = reasoning_effort
        return cls(endpoint, model, api_key, max_tokens_field="max_completion_tokens",
                   extra_body=extra, **kwargs)


def parse_logprobs(body: Any, question: ChoiceQuestion) -> ChoiceAnswer:
    try:
        entries = body["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
    except (KeyError, IndexError, TypeError):
        raise InvalidResponseError("missing label logprobs") from None
    if not isinstance(entries, list):
        raise InvalidResponseError("invalid top_logprobs")
    letters = dict(zip(string.ascii_uppercase, question.criteria))
    found: list[tuple[str, float]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("token"), str):
            raise InvalidResponseError("invalid logprob token")
        letter = entry["token"].strip()
        if letter not in letters:
            continue
        value = entry.get("logprob")
        try:
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and math.isfinite(value) and value <= 0)
        except OverflowError:
            valid = False
        if not valid:
            raise InvalidResponseError("invalid label logprob")
        found.append((letters[letter], float(value)))
    if not found:
        raise InvalidResponseError("no label appears in top_logprobs")
    # Subtracting the maximum preserves normalization without underflow.
    peak = max(value for _, value in found)
    weights = dict.fromkeys(question.criteria, 0.0)
    for label, value in found:
        weights[label] += math.exp(value - peak)
    total = sum(weights.values())
    probabilities = {label: weight / total for label, weight in weights.items()}
    choice = max(probabilities, key=probabilities.__getitem__)
    return validate_answer(ChoiceAnswer(choice, probabilities, probabilities[choice]), question.criteria)


class LogprobLLMProvider:
    """One concurrent request per question; use through ChoiceClassifier for deadlines."""

    def __init__(self, config: LogprobLLMConfig, *, client: httpx.AsyncClient | None = None):
        self.config = config
        self.client = client

    async def _answer(
        self, question: ChoiceQuestion, context: str, messages: tuple[str, ...],
        client: httpx.AsyncClient,
    ) -> ChoiceAnswer:
        options = "\n".join(
            f"{letter} = {label}: {criterion}"
            for letter, (label, criterion) in zip(string.ascii_uppercase, question.criteria.items())
        )
        payload = {
            **self.config.extra_body,
            "model": self.config.model, self.config.max_tokens_field: 1, "logprobs": True,
            "top_logprobs": 20, "temperature": 0,
            "messages": [
                {"role": "system", "content": f"{question.instructions}\n{options}\nOutput only one letter."},
                {"role": "user", "content": json.dumps(
                    {"context": context, "messages": list(messages)}, ensure_ascii=False,
                )},
            ],
        }
        body = await post_json(
            self.config, payload, client=client,
            endpoint=self.config.endpoint.rstrip("/") + "/chat/completions",
        )
        return parse_logprobs(body, question)

    async def decide(self, *, context: str, messages: tuple[str, ...]) -> Decision:
        if not self.config.questions:
            raise InvalidInputError("at least one question is required")
        client = self.client if self.client is not None else httpx.AsyncClient()
        tasks = []
        try:
            tasks = [asyncio.create_task(self._answer(question, context, messages, client))
                     for question in self.config.questions.values()]
            answers = await asyncio.gather(*tasks)
            return dict(zip(self.config.questions, answers))
        finally:
            # gather alone leaves sibling requests running when one fails.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.client is None:
                await client.aclose()
