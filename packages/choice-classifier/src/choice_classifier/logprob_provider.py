"""Choice probabilities from one-token OpenAI-compatible LLM responses."""

import asyncio
import json
import math
import string
from dataclasses import dataclass
from typing import Any

import httpx

from .answers import ChoiceAnswer, Decision, validate_answer
from .errors import InvalidInputError, InvalidResponseError
from .typed_choice_provider import ChoiceQuestion, TypedChoiceConfig, post_json


@dataclass(frozen=True, slots=True)
class LogprobLLMConfig(TypedChoiceConfig):
    """endpoint is the API base URL, e.g. https://example.com/v1.

    disable_thinking asks reasoning models to spend the single token on the
    answer letter. Otherwise that token is reasoning text and content logprobs
    are missing.
    """

    disable_thinking: bool = True

    def __post_init__(self) -> None:
        super(LogprobLLMConfig, self).__post_init__()
        if type(self.disable_thinking) is not bool:
            raise ValueError("disable_thinking must be a boolean")
        if any(len(question.criteria) > 26 for question in self.questions.values()):
            raise ValueError("logprob questions support at most 26 labels (A-Z)")


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
            "model": self.config.model, "max_tokens": 1, "logprobs": True,
            "top_logprobs": 20, "temperature": 0,
            "messages": [
                {"role": "system", "content": f"{question.instructions}\n{options}\nOutput only one letter."},
                {"role": "user", "content": json.dumps(
                    {"context": context, "messages": list(messages)}, ensure_ascii=False,
                )},
            ],
        }
        if self.config.disable_thinking:
            payload["thinking"] = {"type": "disabled"}
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
