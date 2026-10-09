from pathlib import Path

from jiuwenswarm.benchmarks.duplex_outcome import (
    coupling_record, fold_instructions, load_case, score_artifact,
)

CASE = Path(__file__).parents[2] / "fixtures/duplex/outcome/coupled-order-platform-migration.json"


def test_case_prompt_hash_and_high_coupling():
    case = load_case(CASE)
    assert "implementation-ready migration package" in case["initial_prompt"]
    assert "Set producer to kafka." not in case["initial_prompt"]
    record = coupling_record(case["members"])
    assert record["team_size"] == 3
    assert record["assignment_coupling"] == 1


def test_current_admission_leaves_component_on_kafka_and_oracle_replaces_it():
    case = load_case(CASE)
    forbidden = case["forbidden"]
    for member in case["members"]:
        current = fold_instructions(member["assignment"] + "\n" + member["correction"])
        oracle = fold_instructions(member["oracle"])
        current_score = score_artifact(member["expected"], forbidden, current)
        oracle_score = score_artifact(member["expected"], forbidden, oracle)
        assert current_score["stale_fields"] == [member["name"]]
        assert current_score["requirement_met"] is False
        assert oracle_score["requirement_met"] is True
        assert oracle_score["stale_fields"] == []
