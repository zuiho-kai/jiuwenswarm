"""Voice compatibility, stable identity and atomic assignment behavior."""

from collections import Counter
from dataclasses import FrozenInstanceError

import pytest

from jiuwenswarm.common.voice_matching import (
    MemberVoiceRegistry, load_qwen_voice_catalog,
)

MODEL = "qwen3.8-omni-flash-realtime"


def registry(**kwargs):
    return MemberVoiceRegistry("session", "team", MODEL, **kwargs)


def test_explicit_preference_is_stable_and_does_not_silently_change():
    voices = registry()
    assignment = voices.assign_voice("researcher", preference="Serena")
    assert assignment.provider_voice_id == "Serena" and assignment.voice_id == "serena"
    assert voices.assign_voice("researcher") is assignment
    assert voices.assign_voice("researcher", preference="serena") is assignment
    assert voices.get_voice("researcher") is assignment
    with pytest.raises(ValueError, match="stable"):
        voices.assign_voice("researcher", preference="Tina")
    with pytest.raises(FrozenInstanceError):
        assignment.provider_voice_id = "Tina"
    assert voices.get_voice("researcher") is assignment


def test_unused_then_least_used_voices_with_deterministic_ties():
    voices = registry()
    catalog = load_qwen_voice_catalog().compatible(MODEL)
    assigned = [voices.assign_voice(f"member-{i}") for i in range(len(catalog) * 3 + 2)]
    assert [voice.provider_voice_id for voice in assigned[:len(catalog)]] == [voice.provider_voice_id for voice in catalog]
    counts = Counter(voice.provider_voice_id for voice in assigned)
    assert max(counts.values()) - min(counts.values()) <= 1
    assert assigned[len(catalog)].provider_voice_id == catalog[0].provider_voice_id


def test_roster_initialization_is_leader_first_and_independent_of_query_order():
    first, second = registry(), registry()
    first.assign_roster(["developer", "leader", "analyst"], leader_id="leader")
    second.assign_roster(["analyst", "developer", "leader"], leader_id="leader")
    for member in ("leader", "analyst", "developer"):
        assert first.get_voice(member) == second.get_voice(member)
    assert first.get_voice("leader").provider_voice_id == "Tina"
    with pytest.raises(ValueError, match="canonical roster"):
        registry().assign_roster(["teammate"], leader_id="leader")
