"""Stateless context/messages classification, independent of any agent runtime."""

import asyncio
import json
import math
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import (
    InputTooLargeError,
    InsufficientContextError,
    InvalidInputError,
    InvalidResponseError,
    JevError,
    JevTimeoutError,
    ProviderError,
)

Decision = dict[str, str]


class DecisionProvider(Protocol):
    """An HTTP, SDK or test adapter. Implementations must honor cancellation."""

    async def decide(self, *, context: str, messages: tuple[str, ...]) -> Decision:
        """Return one choice for each configured question, or raise."""
        ...


@dataclass(frozen=True, slots=True)
class ClassifierConfig:
    timeout_seconds: float = 2.0
    max_input_bytes: int = 32768
    max_messages: int = 64

    def __post_init__(self) -> None:
        try:
            valid_timeout = (
                not isinstance(self.timeout_seconds, bool)
                and isinstance(self.timeout_seconds, (int, float))
                and math.isfinite(self.timeout_seconds)
                and self.timeout_seconds > 0
            )
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("timeout_seconds must be finite and positive")
        for name in ("max_input_bytes", "max_messages"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


def _validate_input(
    context: str, messages: list[str] | tuple[str, ...], config: ClassifierConfig,
) -> tuple[str, ...]:
    if not isinstance(context, str):
        raise InvalidInputError("context must be text")
    if not context.strip():
        raise InsufficientContextError("context must describe the recipient's current work")
    if not isinstance(messages, (list, tuple)) or not messages:
        raise InvalidInputError("messages must be a nonempty list or tuple of text")
    if len(messages) > config.max_messages:
        raise InputTooLargeError("message count exceeds max_messages")
    batch = tuple(messages)
    for message in batch:
        if not isinstance(message, str) or not message.strip():
            raise InvalidInputError("each new message must be nonempty text")
    # Reject obviously excessive inputs before constructing the JSON copy.
    if len(context) + sum(map(len, batch)) > config.max_input_bytes:
        raise InputTooLargeError("context and messages exceed max_input_bytes")
    try:
        encoded = json.dumps(
            {"context": context, "messages": batch}, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidInputError("input contains invalid Unicode") from None
    if len(encoded) > config.max_input_bytes:
        raise InputTooLargeError("context and messages exceed max_input_bytes")
    return batch


def _validate_decision(decision: Any) -> Decision:
    """Accept the provider's choices. Allowed labels come from each question's criteria."""
    if not isinstance(decision, dict) or not decision:
        raise InvalidResponseError("provider must return a choice for each question")
    choices: Decision = {}
    for name, choice in decision.items():
        if type(name) is not str or not name.strip() or name != name.strip():
            raise InvalidResponseError("each question name must be nonempty text")
        if type(choice) is not str or not choice.strip() or choice != choice.strip():
            raise InvalidResponseError("each question choice must be nonempty text")
        choices[name] = choice
    return choices


class JevClassifier:
    """One provider call per batch; no routing, state lookup or message delivery."""

    def __init__(self, provider: DecisionProvider, config: ClassifierConfig | None = None):
        self.provider = provider
        self.config = config if config is not None else ClassifierConfig()

    async def classify(self, *, context: str, messages: list[str] | tuple[str, ...]) -> Decision:
        """Return one choice per question; failures are exceptions.

        Messages are ordered and classified jointly against one recipient's
        context. The caller owns correlation and the applicability of a result.
        """
        batch = _validate_input(context, messages, self.config)
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                decision = await self.provider.decide(context=context, messages=batch)
        except TimeoutError:
            raise JevTimeoutError("classification timed out") from None
        except JevError:
            raise
        except Exception:
            # Raw provider errors may include credentials or input content.
            raise ProviderError("decision provider failed") from None
        # CancelledError is a BaseException and propagates to the caller.
        return _validate_decision(decision)

