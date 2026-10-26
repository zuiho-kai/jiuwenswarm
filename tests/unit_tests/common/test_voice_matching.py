"""Voice compatibility, stable identity and atomic assignment behavior."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace

import pytest

from jiuwenswarm.common.voice_matching import (
    MemberVoiceRegistry, VoiceCatalog, VoiceDefinition, load_qwen_voice_catalog,
)

MODEL = "qwen3.8-omni-flash-realtime"


def registry(**kwargs):
    return MemberVoiceRegistry("session", "team", MODEL, **kwargs)


def test_catalog_uses_exact_model_compatibility_and_normalized_languages():
    catalog = load_qwen_voice_catalog()
    assert {voice.provider_voice_id for voice in catalog.compatible(MODEL, "zh")} >= {"Tina", "Serena"}
    assert catalog.compatible(MODEL, "en-US") == catalog.compatible(MODEL, "en")
    legacy = catalog.compatible("qwen3-omni-flash-realtime-2025-09-15", "zh-CN")
    assert all(voice.provider_voice_id not in {"Tina", "Serena"} for voice in legacy)
    with pytest.raises(ValueError, match="Unsupported"):
        catalog.compatible("qwen3.8-omni-unknown-snapshot")
    with pytest.raises(ValueError, match="No compatible"):
        catalog.compatible(MODEL, "unsupported-language")


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


def test_preassigned_member_keeps_preference_when_existing_roster_is_initialized():
    voices = registry()
    explicit = voices.assign_voice("developer", "Ryan")
    voices.assign_roster(["developer", "leader", "analyst"], leader_id="leader")
    assert voices.get_voice("developer") is explicit
    assert voices.get_voice("leader").provider_voice_id == "Tina"


def test_unknown_or_incompatible_preferences_do_not_consume_an_assignment():
    voices = registry()
    for preference, language in (("JoyAI-only-voice", None), ("Tina", "unsupported")):
        with pytest.raises(ValueError):
            voices.assign_voice("invalid", preference, language=language)
        with pytest.raises(ValueError, match="Unknown member"):
            voices.get_voice("invalid")
    assert voices.assign_voice("leader").provider_voice_id == "Tina"
    legacy = MemberVoiceRegistry("session", "team", "qwen3-omni-flash-realtime-2025-09-15")
    with pytest.raises(ValueError, match="incompatible"):
        legacy.assign_voice("leader", "Tina")


def test_same_member_labels_and_other_scopes_do_not_share_usage_or_bindings():
    first = registry()
    first.assign_voice("member-slot-a", "Ryan")
    second = MemberVoiceRegistry("other-session", "team", MODEL)
    third = MemberVoiceRegistry("session", "other-team", MODEL)
    assert second.assign_voice("member-slot-a").provider_voice_id == "Tina"
    assert third.assign_voice("member-slot-a").provider_voice_id == "Tina"
    assert first.assign_voice("member-slot-b").provider_voice_id == "Tina"
    assert first.get_voice("member-slot-a").session_id == "session"
    assert third.get_voice("member-slot-a").team_name == "other-team"


def test_threaded_creation_balances_voices_and_repeated_creation_returns_same_object():
    voices = registry()
    members = [f"member-{i % 40}" for i in range(160)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assigned = list(pool.map(voices.assign_voice, members))
    for member, assignment in zip(members, assigned):
        assert voices.get_voice(member) is assignment
    counts = Counter(voices.get_voice(f"member-{i}").provider_voice_id for i in range(40))
    assert max(counts.values()) == min(counts.values())


def test_catalog_updates_only_affect_future_assignments_and_retain_queued_voices():
    voices = registry()
    previous = voices.assign_voice("departed-member", "Tina")
    catalog = load_qwen_voice_catalog()
    updated = replace(catalog, version="new", voices=tuple(voice for voice in catalog.voices if voice.id != "tina"))
    voices.update_catalog(updated)
    assert voices.get_voice("departed-member") is previous
    assert voices.assign_voice("departed-member", "Tina") is previous
    later = voices.assign_voice("new-member")
    assert later.provider_voice_id != "Tina" and later.catalog_version == "new"
    assert previous.catalog_version == catalog.version


def test_language_filter_applies_to_explicit_preferences_and_automatic_selection():
    catalog = VoiceCatalog("test", "test-source", (
        VoiceDefinition("english", "English", (MODEL,), ("en",)),
        VoiceDefinition("mandarin", "Mandarin", (MODEL,), ("zh-CN",)),
    ))
    voices = registry(catalog=catalog)
    assert voices.assign_voice("chinese", language="zh-Hans").provider_voice_id == "Mandarin"
    with pytest.raises(ValueError, match="incompatible"):
        voices.assign_voice("wrong", "English", language="zh")
    with pytest.raises(ValueError, match="support"):
        voices.assign_voice("chinese", language="en")


@pytest.mark.parametrize("data", ["{unclosed", "[]", "voices: not-a-list", "version: test\nsource: test\nvoices: []", "version: test\nsource: test\nvoices: [{id: x, provider_voice_id: X, models: [], languages: [en]}]"])
def test_invalid_catalog_data_is_rejected(tmp_path, data):
    path = tmp_path / "voices.yaml"
    path.write_text(data, encoding="utf-8")
    with pytest.raises(ValueError):
        load_qwen_voice_catalog(path)


def test_duplicate_catalog_ids_are_rejected():
    voice = VoiceDefinition("same", "Same", (MODEL,), ("en",))
    with pytest.raises(ValueError, match="Duplicate"):
        VoiceCatalog("test", "test", (voice, voice))
