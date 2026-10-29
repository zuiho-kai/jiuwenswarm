"""Voice compatibility, stable identity and atomic assignment behavior."""

from collections import Counter
from dataclasses import FrozenInstanceError

import pytest

from jiuwenswarm.common.voice_matching import (
    MemberVoiceRegistry, load_voice_catalog,
)

MODEL = "qwen3.8-omni-flash-realtime"
COSYVOICE_MODEL = "FunAudioLLM/CosyVoice2-0.5B"


def registry(model=MODEL, **kwargs):
    return MemberVoiceRegistry("session", "team", model, **kwargs)


@pytest.mark.parametrize("model,preferred,voice_id,alternate,foreign", [
    (MODEL, "Serena", "serena", "Tina", f"{COSYVOICE_MODEL}:anna"),
    (COSYVOICE_MODEL, f"{COSYVOICE_MODEL}:claire", "claire", f"{COSYVOICE_MODEL}:anna", "Tina"),
])
def test_explicit_preference_is_stable_and_does_not_silently_change(model, preferred, voice_id, alternate, foreign):
    voices = registry(model)
    assignment = voices.assign_voice("researcher", preference=preferred)
    assert assignment.provider_voice_id == preferred and assignment.voice_id == voice_id
    assert voices.assign_voice("researcher") is assignment
    assert voices.assign_voice("researcher", preference=voice_id) is assignment
    assert voices.get_voice("researcher") is assignment
    with pytest.raises(ValueError, match="stable"):
        voices.assign_voice("researcher", preference=alternate)
    with pytest.raises(FrozenInstanceError):
        assignment.provider_voice_id = "Tina"
    assert voices.get_voice("researcher") is assignment
    with pytest.raises(ValueError, match="incompatible"):
        voices.assign_voice("new-member", preference=foreign)


@pytest.mark.parametrize("model", [MODEL, COSYVOICE_MODEL])
def test_unused_then_least_used_voices_with_deterministic_ties(model):
    voices = registry(model)
    catalog = load_voice_catalog(model).compatible(model)
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
