"""Typed-choice adapter shared by JEV-compatible services."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Self

import httpx

from .answers import ChoiceAnswer, Decision, validate_answer
from .errors import (
    ClassifierError, ClassifierTimeoutError, InvalidInputError, InvalidResponseError, ProviderError,
)
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
class TypedChoiceConfig:
    endpoint: str
    model: str
    api_key: str = field(repr=False)
    max_response_bytes: int = 65536
    questions: Mapping[str, ChoiceQuestion] = field(default_factory=dict)
    instructions: str | None = field(default=INSTRUCTIONS, repr=False)
    criteria: Mapping[str, str] | None = field(default_factory=lambda: dict(CRITERIA), repr=False)
    # Extracts the answer body from a vendor envelope, e.g. a REST {"result": ...} wrapper.
    unwrap: Callable[[Any], Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.unwrap is not None and not callable(self.unwrap):
            raise ValueError("unwrap must be callable or None")
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
            questions[name] = _choice_question(question.instructions, question.criteria)
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
            raise ValueError("invalid provider endpoint") from None
        if (url.scheme not in ("http", "https") or not url.host
                or url.userinfo or url.query or url.fragment):
            raise ValueError("endpoint must be HTTP(S), without credentials, query or fragment")
        if type(self.max_response_bytes) is not int or self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")

    @classmethod
    def without_questions(
        cls, endpoint: str, model: str, api_key: str, max_response_bytes: int = 65536,
        unwrap: Callable[[Any], Any] | None = None,
    ) -> Self:
        """Build a config with no questions. add_question() can install them later."""
        return cls(
            endpoint, model, api_key, max_response_bytes=max_response_bytes,
            instructions=None, criteria=None, unwrap=unwrap,
        )

    def add_question(
        self,
        name: str = "action",
        instructions: str | None = None,
        criteria: Mapping[str, str] | None = None,
    ) -> Self:
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


def parse_response(body: Any, questions: Mapping[str, ChoiceQuestion]) -> Decision:
    """Validate each answer against the criteria for the question that was asked."""
    if not questions:
        raise InvalidResponseError("no questions to validate")
    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        raise InvalidResponseError("missing answers object")
    answers = body["answers"]
    if set(answers) != set(questions):
        raise InvalidResponseError("answers must match the requested questions")
    choices: Decision = {}
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise InvalidResponseError(f"missing {name} choice")
        choices[name] = validate_answer(ChoiceAnswer(
            answer.get("choice"), answer.get("probabilities"), answer.get("confidence"),
        ), question.criteria)
    return choices


class TypedChoiceProvider:
    """One HTTP request, no redirects/retries; injected client stays caller-owned.

    Use through ChoiceClassifier, which supplies the total deadline including body
    streaming. If no client is injected, a short-lived client is created per call.
    """

    def __init__(self, config: TypedChoiceConfig, *, client: httpx.AsyncClient | None = None):
        self.config = config
        self.client = client

    async def decide(self, *, context: str, messages: tuple[str, ...]) -> Decision:
        if not self.config.questions:
            raise InvalidInputError("at least one question is required")
        payload = {"model": self.config.model,
                   "state": {"context": context, "messages": list(messages)},
                   "questions": {
                       name: {"type": "choice", "instructions": question.instructions,
                              "criteria": dict(question.criteria)}
                       for name, question in self.config.questions.items()
                   }}
        parsed = await post_json(self.config, payload, client=self.client)
        return parse_response(unwrap_body(self.config, parsed), self.config.questions)


def unwrap_body(config: TypedChoiceConfig, body: Any) -> Any:
    """Apply the configured envelope unwrapper; its failures are invalid responses."""
    if config.unwrap is None:
        return body
    try:
        return config.unwrap(body)
    except ClassifierError:
        raise
    except Exception:
        # The unwrapper sees the raw body; do not chain it into the error.
        raise InvalidResponseError("provider response envelope is invalid") from None


async def post_json(
    config: TypedChoiceConfig, payload: dict[str, Any], *,
    client: httpx.AsyncClient | None = None, endpoint: str | None = None,
) -> Any:
    """Bounded, cancellation-aware transport shared by both HTTP providers."""
    owned = client is None
    client = client if client is not None else httpx.AsyncClient()
    try:
        async with client.stream(
            "POST", endpoint or config.endpoint,
            headers={"Authorization": f"Bearer {config.api_key}"},
            json=payload, follow_redirects=False, timeout=None,
        ) as response:
            if not 200 <= response.status_code < 300:
                raise ProviderError(f"provider HTTP status {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > config.max_response_bytes:
                    raise InvalidResponseError("provider response exceeds max_response_bytes")
                body.extend(chunk)
        try:
            return json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise InvalidResponseError("provider response is not valid JSON") from None
    except httpx.TimeoutException:
        raise ClassifierTimeoutError("provider HTTP request timed out") from None
    except httpx.HTTPError:
        raise ProviderError("provider HTTP request failed") from None
    finally:
        if owned:
            await client.aclose()
