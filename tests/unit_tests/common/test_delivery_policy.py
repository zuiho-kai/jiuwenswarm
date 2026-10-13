# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from jiuwenswarm.common.delivery_policy import (
    THETA_SKIP,
    THETA_STEER,
    Action,
    Decision,
    Envelope,
    TargetState,
    broadcast_allowed,
    final_action,
    plan,
    resolve,
)

A = Action
U = ("urgency",)
RU = ("relevance", "urgency")
FAIL = None  # classifier failure / timeout


@dataclass(frozen=True)
class Ans:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float = 0.9


def urg(p_interrupt: float) -> Ans:
    choice = "INTERRUPT" if p_interrupt >= 0.5 else "APPEND"
    return Ans(choice, {"INTERRUPT": p_interrupt, "APPEND": 1 - p_interrupt})


def rel(p_skip: float) -> Ans:
    choice = "SKIP" if p_skip >= 0.5 else "DELIVER"
    return Ans(choice, {"SKIP": p_skip, "DELIVER": 1 - p_skip})


def ua(p):
    return {"urgency": urg(p)}


def rua(skip, interrupt):
    return {"relevance": rel(skip), "urgency": urg(interrupt)}


TEXT = "please also check the failing test"

# Rows follow the spec's scenario table (README "各场景怎么处理").
# id, source, idle, self_send, questions, answers, action, downgraded
ROWS = [
    # --- user input: never ask; idle -> deliver now, busy -> steer (original behavior)
    ("ut-idle", "user_text", True, False, (), None, A.DELIVER_NOW, False),
    ("ut-busy", "user_text", False, False, (), None, A.STEER, False),
    # --- dm: ask whether to interrupt
    ("dm-idle", "dm", True, False, (), None, A.DELIVER_NOW, False),
    ("dm-keep", "dm", False, False, U, ua(0.95), A.STEER, False),
    ("dm-edge", "dm", False, False, U, ua(THETA_STEER), A.STEER, False),
    ("dm-below", "dm", False, False, U, ua(0.89), A.FOLLOWUP, True),
    ("dm-append", "dm", False, False, U, ua(0.1), A.FOLLOWUP, True),
    ("dm-fail", "dm", False, False, U, FAIL, A.FOLLOWUP, True),
    ("dm-missing-key", "dm", False, False, U, {}, A.FOLLOWUP, True),
    ("dm-self", "dm", False, True, (), None, A.REJECT, False),
    # --- group_mention: same as dm
    ("gm-idle", "group_mention", True, False, (), None, A.DELIVER_NOW, False),
    ("gm-keep", "group_mention", False, False, U, ua(0.9), A.STEER, False),
    ("gm-below", "group_mention", False, False, U, ua(0.89), A.FOLLOWUP, True),
    ("gm-fail", "group_mention", False, False, U, FAIL, A.FOLLOWUP, True),
    # --- group_plain: visible only
    ("gp-idle", "group_plain", True, False, (), None, A.VISIBLE_ONLY, False),
    ("gp-busy", "group_plain", False, False, (), None, A.VISIBLE_ONLY, False),
    # --- broadcast: relevance + urgency, ask even when idle
    ("bc-idle-deliver", "broadcast", True, False, RU, rua(0.1, 0.1), A.DELIVER_NOW, False),
    ("bc-idle-skip", "broadcast", True, False, RU, rua(0.9, 0.1), A.VISIBLE_ONLY, False),
    ("bc-skip-edge", "broadcast", False, False, RU, rua(THETA_SKIP, 0.99), A.VISIBLE_ONLY, False),
    ("bc-skip-below", "broadcast", False, False, RU, rua(0.79, 0.1), A.FOLLOWUP, False),
    ("bc-busy-steer", "broadcast", False, False, RU, rua(0.1, THETA_STEER), A.STEER, False),
    ("bc-busy-below", "broadcast", False, False, RU, rua(0.1, 0.89), A.FOLLOWUP, False),
    ("bc-fail-idle", "broadcast", True, False, RU, FAIL, A.DELIVER_NOW, False),
    ("bc-fail-busy", "broadcast", False, False, RU, FAIL, A.FOLLOWUP, False),
    ("bc-self", "broadcast", False, True, (), None, A.REJECT, False),
]


@pytest.mark.parametrize(
    "source,idle,self_send,questions,answers,action,downgraded",
    [r[1:] for r in ROWS], ids=[r[0] for r in ROWS])
def test_policy_table(source, idle, self_send, questions, answers, action, downgraded):
    env = Envelope(source=source, sender="a" if self_send else "s", target="a", content=TEXT)
    state = TargetState(idle=idle)
    p = plan(env, state)
    assert p.questions == questions
    assert (p.action is None) == bool(questions)
    decision = resolve(env, p, answers, state)
    assert decision.action is action
    assert decision.downgraded is downgraded
    assert f"scene={source}" in decision.reason
    assert f"action={action.value}" in decision.reason
    assert f"downgraded={int(downgraded)}" in decision.reason


def test_reason_has_p_and_theta():
    env = Envelope("dm", "s", "a", TEXT)
    d = resolve(env, plan(env, TargetState(False)), ua(0.5), TargetState(False))
    assert "p_interrupt=0.500" in d.reason and "theta=0.9" in d.reason
    d = resolve(env, plan(env, TargetState(False)), None, TargetState(False))
    assert "failed=1" in d.reason


def test_unknown_source():
    with pytest.raises(ValueError):
        plan(Envelope("fax", "s", "a", TEXT), TargetState(False))


@pytest.mark.parametrize("action,idle_now,expected", [
    (A.STEER, True, A.DELIVER_NOW), (A.FOLLOWUP, True, A.DELIVER_NOW),
    (A.STEER, False, A.STEER),
    (A.FOLLOWUP, False, A.FOLLOWUP), (A.VISIBLE_ONLY, True, A.VISIBLE_ONLY),
    (A.REJECT, True, A.REJECT), (A.DELIVER_NOW, False, A.DELIVER_NOW),
])
def test_final_action(action, idle_now, expected):
    assert final_action(Decision(action, False, ""), idle_now) is expected


@pytest.mark.parametrize("last,now,expected", [
    (None, 0.0, True), (100.0, 105.0, False), (100.0, 110.0, True), (100.0, 120.0, True),
])
def test_broadcast_allowed(last, now, expected):
    assert broadcast_allowed(last, now) is expected


def test_constants():
    assert (THETA_STEER, THETA_SKIP) == (0.9, 0.8)
