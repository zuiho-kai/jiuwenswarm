"""JEV typed-choice adapter using the existing repository's wire contract."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from .errors import InvalidInputError, InvalidResponseError, JevTimeoutError, ProviderError
from .prompts import CRITERIA, INSTRUCTIONS


@dataclass(frozen=True, slots=True)
class ChoiceQuestion:
    instructions: str
    criteria: Mapping[str, str]


def _choice_question(instructions: Any, criteria: Any) -> ChoiceQuestion:
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("instructions must be nonempty text")
    if isinstance(criteria, str) or not isinstance(criteria, Mapping) or len(criteria) < 2:
        raise ValueError("criteria must contain at least two labels")
    copied: dict[str, str] = {}
    for label, text in criteria.items():
        if type(label) is not str or not label.strip() or label != label.strip():
            raise ValueError("each criterion label must be nonempty text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"criteria[{label}] must be nonempty text")
        copied[label] = text
    return ChoiceQuestion(instructions, copied)


@dataclass(frozen=True, slots=True)
class JevHttpConfig:
    endpoint: str
    model: str
    api_key: str = field(repr=False)
    max_response_bytes: int = 65536
    questions: Mapping[str, ChoiceQuestion] = field(default_factory=dict)
    instructions: str | None = field(default=INSTRUCTIONS, repr=False)
    criteria: Mapping[str, str] | None = field(default_factory=lambda: dict(CRITERIA), repr=False)

    def __post_init__(self) -> None:
        for name in ("endpoint", "model", "api_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonempty text")
        instructions, criteria = self.instructions, self.criteria
        if (instructions is None) != (criteria is None):
            raise ValueError("instructions and criteria must be supplied together")
        questions: dict[str, ChoiceQuestion] = {}
        supplied = self.questions
        if isinstance(supplied, str) or not isinstance(supplied, Mapping):
            raise ValueError("questions must be a mapping")
        for name, question in supplied.items():
            if type(name) is not str or not name.strip() or name != name.strip():
                raise ValueError("each question name must be nonempty text")
            if not isinstance(question, ChoiceQuestion):
                raise ValueError("questions must contain choice questions")
            questions[name] = ChoiceQuestion(question.instructions, dict(question.criteria))
        if instructions is not None:
            if "action" in questions:
                raise ValueError("question action already exists")
            questions["action"] = _choice_question(instructions, criteria)
        object.__setattr__(self, "questions", questions)
        object.__setattr__(self, "instructions", None)
        object.__setattr__(self, "criteria", None)
        try:
            url = httpx.URL(self.endpoint)
        except (httpx.InvalidURL, ValueError):
            raise ValueError("invalid JEV endpoint") from None
        if (url.scheme not in ("http", "https") or not url.host
                or url.userinfo or url.query or url.fragment):
            raise ValueError("endpoint must be HTTP(S), without credentials, query or fragment")
        if type(self.max_response_bytes) is not int or self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")

    @classmethod
    def without_questions(
        cls, endpoint: str, model: str, api_key: str, max_response_bytes: int = 65536,
    ) -> JevHttpConfig:
        """Build a config with no questions. add_question() can install them later."""
        return cls(
            endpoint, model, api_key, max_response_bytes=max_response_bytes,
            instructions=None, criteria=None,
        )

    def add_question(
        self,
        name: str = "action",
        instructions: str | None = None,
        criteria: Mapping[str, str] | None = None,
    ) -> JevHttpConfig:
        """Add one choice question. No arguments installs the module's original prompt."""
        if type(name) is not str or not name.strip() or name != name.strip():
            raise ValueError("question name must be nonempty text")
        if name in self.questions:
            raise ValueError(f"question {name} already exists")
        if instructions is None and criteria is None:
            instructions, criteria = INSTRUCTIONS, CRITERIA
        question = _choice_question(instructions, criteria)
        questions = dict(self.questions)
        questions[name] = question
        return replace(self, questions=questions)


def _probability(value: Any) -> float:
    # The range test also rejects NaN/infinity without converting huge integers.
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 <= value <= 1):
        raise InvalidResponseError("probabilities and confidence must be numbers in [0, 1]")
    return float(value)


def parse_response(body: Any, questions: Mapping[str, ChoiceQuestion]) -> dict[str, str]:
    """Validate each answer against the criteria for the question that was asked."""
    if not questions:
        raise InvalidResponseError("no questions to validate")
    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        raise InvalidResponseError("missing answers object")
    answers = body["answers"]
    if set(answers) != set(questions):
        raise InvalidResponseError("answers must match the requested questions")
    choices: dict[str, str] = {}
    for name, question in questions.items():
        answer = answers[name]
        labels = set(question.criteria)
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise InvalidResponseError(f"missing {name} choice")
        choice = answer.get("choice")
        if type(choice) is not str or choice not in labels:
            raise InvalidResponseError(f"invalid {name} choice")
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != labels:
            raise InvalidResponseError(f"expected probabilities for every {name} criterion")
        values = {label: _probability(probabilities[label]) for label in labels}
        _probability(answer.get("confidence"))
        if not math.isclose(sum(values.values()), 1.0, rel_tol=0, abs_tol=1e-5):
            raise InvalidResponseError("probabilities must sum to one")
        if values[choice] < max(values.values()):
            raise InvalidResponseError("choice disagrees with probabilities")
        choices[name] = choice
    return choices


class JevHttpProvider:
    """One HTTP request, no redirects/retries; injected client stays caller-owned.

    Use through JevClassifier, which supplies the total deadline including body
    streaming. If no client is injected, a short-lived client is created per call.
    """

    def __init__(self, config: JevHttpConfig, *, client: httpx.AsyncClient | None = None):
        self.config = config
        self.client = client

    async def decide(self, *, context: str, messages: tuple[str, ...]) -> dict[str, str]:
        if not self.config.questions:
            raise InvalidInputError("at least one question is required")
        payload = {"model": self.config.model,
                   "state": {"context": context, "messages": list(messages)},
                   "questions": {
                       name: {"type": "choice", "instructions": question.instructions,
                              "criteria": dict(question.criteria)}
                       for name, question in self.config.questions.items()
                   }}
        client = self.client if self.client is not None else httpx.AsyncClient()
        try:
            async with client.stream(
                "POST", self.config.endpoint,
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json=payload, follow_redirects=False, timeout=None,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise ProviderError(f"JEV HTTP status {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self.config.max_response_bytes:
                        raise InvalidResponseError("JEV response exceeds max_response_bytes")
                    body.extend(chunk)
            try:
                parsed = json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                raise InvalidResponseError("JEV response is not valid JSON") from None
            return parse_response(parsed, self.config.questions)
        except httpx.TimeoutException:
            raise JevTimeoutError("JEV HTTP request timed out") from None
        except httpx.HTTPError:
            raise ProviderError("JEV HTTP request failed") from None
        finally:
            if self.client is None:
                await client.aclose()

