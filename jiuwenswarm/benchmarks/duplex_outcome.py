"""Score whether admitted instructions change a coupled team's artifacts.

The causal executor applies explicit ``Set <field> to <value>`` instructions in
the order the runtime admitted them. It is a gate for admission, not a live
model and not a substitute for master-led team formation.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_SET = re.compile(
    r"Set ([a-z]+(?:, [a-z]+)*(?: and [a-z]+)?) to ([a-z]+)\.",
    re.IGNORECASE,
)
_MEMBER = re.compile(r"Your name: ([A-Za-z0-9_-]+)\.")


def load_prompt(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("initial prompt is empty")
    return text + "\n"


def prompt_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_case(path: Path) -> dict:
    case = json.loads(path.read_text(encoding="utf-8"))
    prompt_path = path.parent / case["initial_prompt_file"]
    prompt = load_prompt(prompt_path)
    digest = prompt_sha256(prompt)
    if case.get("prompt_sha256") != digest:
        raise ValueError("initial prompt hash does not match the case")
    case["initial_prompt"] = prompt
    return case


def instruction_fields(text: str) -> list[tuple[str, str]]:
    updates = []
    for match in _SET.finditer(text):
        value = match.group(2).lower()
        raw_fields = match.group(1).lower().replace(" and ", ", ")
        fields = [item.strip() for item in raw_fields.split(",") if item.strip()]
        updates.extend((field, value) for field in fields)
    return updates


def fold_instructions(text: str) -> dict:
    artifact = {}
    for field, value in instruction_fields(text):
        artifact[field] = value
    return artifact


def member_name(text: str) -> str | None:
    match = _MEMBER.search(text)
    return match.group(1) if match else None


def score_artifact(expected: dict, forbidden: list[str], artifact: dict) -> dict:
    blocked = {item.lower() for item in forbidden}
    stale = [field for field in expected if str(artifact.get(field, "")).lower() in blocked]
    requirement_met = all(artifact.get(field) == value for field, value in expected.items()) and not stale
    return {
        "requirement_met": requirement_met,
        "stale_fields": stale,
        "artifact": artifact,
    }


def coupling_record(members: list[dict]) -> dict:
    if not members:
        raise ValueError("team formation is empty")
    coupled = [member for member in members if member.get("depends_on")]
    return {
        "team_size": len(members),
        "coupled_members": len(coupled),
        "assignment_coupling": len(coupled) / len(members),
        "members": members,
    }
