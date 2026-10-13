"""Provider-independent answer contract and validation."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import InvalidResponseError


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float


Decision = dict[str, ChoiceAnswer]


def _probability(value: Any) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 <= value <= 1):
        raise InvalidResponseError("probabilities and confidence must be numbers in [0, 1]")
    return float(value)


def validate_answer(answer: ChoiceAnswer, labels: Mapping[str, Any]) -> ChoiceAnswer:
    """Copy and validate an answer against the question's exact criteria."""
    if not isinstance(answer, ChoiceAnswer):
        raise InvalidResponseError("provider must return ChoiceAnswer values")
    if (not labels or any(type(label) is not str or not label.strip()
                          or label != label.strip() for label in labels)):
        raise InvalidResponseError("each criterion label must be nonempty text")
    if type(answer.choice) is not str or answer.choice not in labels:
        raise InvalidResponseError("invalid choice")
    if not isinstance(answer.probabilities, Mapping) or set(answer.probabilities) != set(labels):
        raise InvalidResponseError("expected probabilities for every criterion")
    values = {label: _probability(answer.probabilities[label]) for label in labels}
    confidence = _probability(answer.confidence)
    if not math.isclose(sum(values.values()), 1.0, rel_tol=0, abs_tol=1e-5):
        raise InvalidResponseError("probabilities must sum to one")
    if values[answer.choice] < max(values.values()):
        raise InvalidResponseError("choice disagrees with probabilities")
    return ChoiceAnswer(answer.choice, values, confidence)
