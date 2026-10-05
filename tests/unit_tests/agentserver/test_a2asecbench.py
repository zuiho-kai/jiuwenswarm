"""A2ASecBench stays an external checkout; these tests only check the launch contract."""
import json
from pathlib import Path

import pytest

from jiuwenswarm.benchmarks.a2asecbench import FAMILIES, orchestration_command
from jiuwenswarm.benchmarks.duplex_experiment import Experiment


def _checkout(root: Path, *configs: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "orchestration.py").write_text("# pinned runner\n", encoding="utf-8")
    for config in configs:
        path = root / config
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("cases: []\n", encoding="utf-8")
    return root


def test_as_command_uses_the_offline_config(tmp_path):
    root = _checkout(tmp_path / "repo", FAMILIES["as"]["config"])
    command = orchestration_command(root, Path("python"), "as", tmp_path / "run.jsonl", 1)
    assert command[1] == "orchestration.py"
    assert command[command.index("--config") + 1] == "configs/offline/as.yaml"
    assert "--mode" not in command


def test_cc_whitebox_command_sets_mode(tmp_path):
    root = _checkout(tmp_path / "repo", FAMILIES["cc_whitebox"]["config"])
    command = orchestration_command(root, Path("python"), "cc_whitebox", tmp_path / "run.jsonl", 2)
    assert command[command.index("--mode") + 1] == "whitebox"
    assert command[command.index("--trials") + 1] == "2"


def test_unknown_family_and_bad_trials_are_rejected(tmp_path):
    root = _checkout(tmp_path / "repo", FAMILIES["as"]["config"])
    with pytest.raises(ValueError, match="unknown A2ASecBench family"):
        orchestration_command(root, Path("python"), "missing", tmp_path / "run.jsonl", 1)
    with pytest.raises(ValueError, match="positive integer"):
        orchestration_command(root, Path("python"), "as", tmp_path / "run.jsonl", 0)


def test_experiment_keeps_each_family_summary(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "repo", FAMILIES["as"]["config"], FAMILIES["cc_blackbox"]["config"])
    monkeypatch.setattr("jiuwenswarm.benchmarks.a2asecbench.prepare", lambda _root: "pinned")

    def fake_command(self, args, cwd, label):
        output = Path(args[args.index("--out") + 1])
        output.write_text("{}\n", encoding="utf-8")
        output.with_name("summary.json").write_text(
            json.dumps({"label": label}), encoding="utf-8")

    monkeypatch.setattr(Experiment, "command", fake_command)
    output = tmp_path / "results"
    Experiment({
        "benchmark": "a2asecbench", "repo": str(root),
        "families": ["as", "cc_blackbox"], "trials": 1,
    }, output).a2asecbench()
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["benchmark"] == "a2asecbench"
    assert report["revision"] == "pinned"
    assert [run["family"] for run in report["runs"]] == ["as", "cc_blackbox"]
    assert (output / "as" / "summary.json").is_file()
    assert (output / "cc_blackbox" / "summary.json").is_file()
