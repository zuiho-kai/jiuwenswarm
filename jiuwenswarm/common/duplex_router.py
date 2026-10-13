# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Duplex supervisor data types, classifier context and settings migration."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ControlSnapshot:
    context_version: str
    round_id: str
    checkpoint_id: str
    phase: str
    goal: str = ""
    next_action: str = ""
    last_action: str = ""


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    sender: str
    content: str


def context_for(snapshot: ControlSnapshot) -> str:
    """Describe the recipient's current work; private reasoning and history stay out."""
    return json.dumps({"goal": snapshot.goal, "current_plan": snapshot.next_action,
                       "last_action": snapshot.last_action, "phase": snapshot.phase},
                      ensure_ascii=False)


def env_secret(name: str) -> str:
    """Read a router credential, decrypting it when a crypto provider is active."""
    value = os.environ.get(name, "")
    if not isinstance(value, str):
        value = ""
    value = value.strip()
    if not value:
        return ""
    lowered = name.lower()
    if "api_key" not in lowered and "token" not in lowered:
        return value
    try:
        from jiuwenswarm.common.security.base_crypto import get_crypto_provider

        crypto = get_crypto_provider()
        if crypto is not None:
            decrypted = crypto.decrypt(value)
            if isinstance(decrypted, str) and decrypted.strip():
                return decrypted.strip()
    except Exception:
        return value
    return value


# Keys of the pre-provider settings shape: backend: sdk | jev | mindshub | clef.
_LEGACY_KEYS = ("backend", "model_name", "jev", "mindshub", "clef")


def is_legacy_router_settings(section: Any) -> bool:
    return (isinstance(section, Mapping) and "provider" not in section
            and any(key in section for key in _LEGACY_KEYS))


def migrate_legacy_router_settings(section: dict[str, Any]) -> bool:
    """Rewrite a legacy duplex_router section in place to the provider shape.

    sdk -> logprob_llm/team_model, mindshub -> logprob_llm/mindshub,
    jev -> typed_choice/jev, clef -> typed_choice/clef. Per-backend
    interrupt_threshold values are dropped; thresholds belong to routing policy.
    Works on plain dicts and ruamel CommentedMaps. Returns whether it changed.
    """
    if not is_legacy_router_settings(section):
        return False
    backend = str(section.get("backend") or "sdk").strip().lower()
    model_name = str(section.get("model_name") or "").strip()
    old = {name: section.get(name) if isinstance(section.get(name), Mapping) else {}
           for name in ("jev", "mindshub", "clef")}
    for key in _LEGACY_KEYS:
        section.pop(key, None)
    if backend in ("jev", "clef"):
        settings = old[backend]
        typed: dict[str, Any] = {"preset": backend}
        if backend == "jev":
            base = str(settings.get("api_base") or "https://api.typesafe.ai/v1").rstrip("/")
            typed["endpoint"] = f"{base}/{settings.get('endpoint_path') or 'systemone'}"
            typed["model"] = model_name or "jev-1.13.0"
        else:
            typed["model"] = str(settings.get("model") or "clef")
            if settings.get("account_id_env"):
                typed["account_id_env"] = str(settings["account_id_env"])
        if settings.get("api_key_env"):
            typed["api_key_env"] = str(settings["api_key_env"])
        section["provider"] = "typed_choice"
        section["typed_choice"] = typed
    else:
        llm: dict[str, Any] = {"preset": "mindshub" if backend == "mindshub" else "team_model"}
        if backend == "mindshub":
            settings = old["mindshub"]
            llm["api_base"] = str(settings.get("api_base") or "https://api.mindshub.ai/v1")
            llm["model"] = model_name or "mindshub_air"
            if settings.get("api_key_env"):
                llm["api_key_env"] = str(settings["api_key_env"])
        else:
            llm["model_name"] = model_name
        section["provider"] = "logprob_llm"
        section["logprob_llm"] = llm
    return True
