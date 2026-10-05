# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Run a pinned A2ASecBench checkout. Attack cases stay in that repository."""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.common.duplex_public_benchmark import verify_checkout

FAMILIES = {
    "as": {"config": "configs/offline/as.yaml"},
    "cc_whitebox": {"config": "configs/offline/cc_whitebox.yaml", "mode": "whitebox"},
    "cc_blackbox": {"config": "configs/smoke/cc_blackbox.yaml", "mode": "blackbox"},
    "asrf": {"config": "configs/offline/asrf_eval.yaml"},
    "atsi": {"config": "configs/offline/atsi_eval.yaml"},
    "co": {"config": "configs/offline/co_eval.yaml"},
    "hotf": {"config": "configs/offline/hotf_eval.yaml"},
}


def prepare(root: Path) -> str:
    return verify_checkout(Path(root), "a2asecbench")


def orchestration_command(root: Path, python: Path, family: str, output: Path, trials: int) -> list[str]:
    """Build one upstream orchestration.py invocation. The caller sets cwd to the checkout."""
    try:
        spec = FAMILIES[family]
    except KeyError as exc:
        raise ValueError(f"unknown A2ASecBench family: {family}") from exc
    if isinstance(trials, bool) or not isinstance(trials, int) or trials < 1:
        raise ValueError("A2ASecBench trials must be a positive integer")
    checkout = Path(root)
    if not (checkout / "orchestration.py").is_file() or not (checkout / spec["config"]).is_file():
        raise ValueError("A2ASecBench checkout is missing orchestration.py or the selected config")
    command = [str(python), "orchestration.py", "--config", spec["config"],
               "--trials", str(trials), "--out", str(Path(output))]
    mode = spec.get("mode")
    if mode is not None:
        command.extend(["--mode", mode])
    return command
