# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One bounded TypeSafe Choice request for the duplex router, without retries."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping
from typing import Any

import httpx

from jiuwenswarm.common.duplex_router import (
    ROUTING_INSTRUCTIONS,
    ControlSnapshot,
    InboundMessage,
    state_for,
)

DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_API_BASE = "https://api.typesafe.ai/v1"
DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_INTERRUPT_THRESHOLD = 0.9
logger = logging.getLogger(__name__)


def _probability(value: Any) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError("Jev probabilities and confidence must be finite numbers in [0, 1]")
    return float(value)


def _decision(payload: Any, threshold: float) -> dict[str, str]:
    """Reject malformed or contradictory answers before admitting an interrupt."""
    answer = payload["answers"]["action"]
    if answer["type"] != "choice" or answer["choice"] not in ("APPEND", "INTERRUPT"):
        raise ValueError("invalid Jev action choice")
    probabilities = answer["probabilities"]
    if not isinstance(probabilities, dict) or set(probabilities) != {"APPEND", "INTERRUPT"}:
        raise ValueError("invalid Jev action probabilities")
    append = _probability(probabilities["APPEND"])
    interrupt = _probability(probabilities["INTERRUPT"])
    _probability(answer["confidence"])
    if not math.isclose(append + interrupt, 1.0, rel_tol=0, abs_tol=1e-5):
        raise ValueError("Jev action probabilities must sum to one")
    chosen = probabilities[answer["choice"]]
    if chosen < max(append, interrupt):
        raise ValueError("Jev choice disagrees with its probabilities")
    action = "INTERRUPT" if answer["choice"] == "INTERRUPT" and interrupt >= threshold else "APPEND"
    logger.info("Jev decision choice=%s effective=%s p_interrupt=%.4f p_append=%.4f threshold=%.4f",
                answer["choice"], action, interrupt, append, threshold)
    return {"action": action}


async def classify_jev(
    snapshot: ControlSnapshot,
    messages: tuple[InboundMessage, ...],
    *,
    model_name: str = DEFAULT_MODEL,
    settings: Mapping[str, Any] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Map Jev's typed answer to the existing action-only router contract.

    The caller's observe() enforces the total deadline and snapshot freshness.
    HTTP failures and invalid responses propagate to its ordinary steer fallback.
    Credentials are read only from the selected environment variable.
    """
    if settings is None:
        settings = {}
    threshold = _probability(settings.get("interrupt_threshold", DEFAULT_INTERRUPT_THRESHOLD))
    if threshold <= 0.5:
        raise ValueError("Jev interrupt_threshold must be greater than 0.5 and at most 1")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Jev timeout_seconds must be finite and positive")
    key_env = settings.get("api_key_env", "TYPESAFE_API_KEY")
    api_key = os.environ.get(key_env, "").strip()
    if not api_key:
        raise ValueError("Jev API key environment variable is empty or missing")
    api_base = settings.get("api_base", DEFAULT_API_BASE).rstrip("/")
    endpoint_path = settings.get("endpoint_path", "systemone")
    if endpoint_path not in ("systemone", "decisions"):
        raise ValueError("invalid Jev endpoint_path")
    url = httpx.URL(api_base + "/" + endpoint_path)
    if (url.scheme not in ("http", "https") or not url.host
            or url.userinfo or url.query or url.fragment):
        raise ValueError("invalid Jev api_base")
    request = {
        "model": model_name,
        "state": state_for(snapshot, messages),
        "questions": {"action": {
            "type": "choice",
            "instructions": ROUTING_INSTRUCTIONS,
            "criteria": {
                "APPEND": "The message does not invalidate the current plan or next action, or evidence is insufficient.",
                "INTERRUPT": "A valid changed goal, hard constraint, or evidence of a mistake invalidates the current plan or next action.",
            },
        }},
    }
    try:
        # HTTPX has no automatic retries. Redirects must not forward credentials.
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=request)
            response.raise_for_status()
            return _decision(response.json(), threshold)
    except httpx.HTTPStatusError as exc:
        logger.warning("Jev request failed status_code=%s", exc.response.status_code)
        raise
    except httpx.TimeoutException as exc:
        logger.warning("Jev request timed out")
        raise TimeoutError("Jev request timed out") from exc
    except Exception as exc:
        logger.warning("Jev request or response failed error_type=%s", type(exc).__name__)
        raise
