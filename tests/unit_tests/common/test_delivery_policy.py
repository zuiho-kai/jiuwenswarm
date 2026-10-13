# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from jiuwenswarm.common.delivery_policy import (
    THETA_ABORT,
    THETA_SKIP,
    THETA_STEER,
    VOICE_TIMEOUT_SECONDS,
    Action,
    Decision,
    Envelope,
    TargetState,
    broadcast_allowed,
    final_action,
    is_backchannel,
    merge_followups,
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

# id, source, mode, can_abort, idle, content, self_send, questions, answers, action, downgraded
ROWS = [
    # --- user_text: never ask, execute the user's mode
    ("ut-idle", "user_text", "abort", False, True, TEXT, False, (), None, A.DELIVER_NOW, False),
    ("ut-busy-abort", "user_text", "abort", False, False, TEXT, False, (), None, A.ABORT, False),
    ("ut-busy-steer", "user_text", "steer", False, False, TEXT, False, (), None, A.STEER, False),
    ("ut-busy-followup", "user_text", "followup", False, False, TEXT, False, (), None, A.FOLLOWUP, False),
    ("ut-busy-default-steer", "user_text", None, False, False, TEXT, False, (), None, A.STEER, False),
    # --- dm
    ("dm-idle", "dm", "steer", False, True, TEXT, False, (), None, A.DELIVER_NOW, False),
    ("dm-idle-abort", "dm", "abort", True, True, TEXT, False, (), None, A.DELIVER_NOW, False),
    ("dm-followup", "dm", "followup", False, False, TEXT, False, (), None, A.FOLLOWUP, False),
    ("dm-default-steer-keep", "dm", None, False, False, TEXT, False, U, ua(0.95), A.STEER, False),
    ("dm-default-steer-below", "dm", None, False, False, TEXT, False, U, ua(0.5), A.FOLLOWUP, True),
    ("dm-default-fail", "dm", None, False, False, TEXT, False, U, FAIL, A.FOLLOWUP, True),
    ("dm-steer-keep", "dm", "steer", False, False, TEXT, False, U, ua(0.95), A.STEER, False),
    ("dm-steer-edge", "dm", "steer", False, False, TEXT, False, U, ua(THETA_STEER), A.STEER, False),
    ("dm-steer-below", "dm", "steer", False, False, TEXT, False, U, ua(0.89), A.FOLLOWUP, True),
    ("dm-steer-append", "dm", "steer", False, False, TEXT, False, U, ua(0.1), A.FOLLOWUP, True),
    ("dm-steer-fail", "dm", "steer", False, False, TEXT, False, U, FAIL, A.FOLLOWUP, True),
    ("dm-steer-missing-key", "dm", "steer", False, False, TEXT, False, U, {}, A.FOLLOWUP, True),
    ("dm-abort-keep", "dm", "abort", True, False, TEXT, False, U, ua(0.99), A.ABORT, False),
    ("dm-abort-edge", "dm", "abort", True, False, TEXT, False, U, ua(THETA_ABORT), A.ABORT, False),
    ("dm-abort-below", "dm", "abort", True, False, TEXT, False, U, ua(0.96), A.STEER, True),
    ("dm-abort-fail", "dm", "abort", True, False, TEXT, False, U, FAIL, A.STEER, True),
    ("dm-abort-unauth-keep", "dm", "abort", False, False, TEXT, False, U, ua(0.99), A.STEER, False),
    ("dm-abort-unauth-below", "dm", "abort", False, False, TEXT, False, U, ua(0.5), A.FOLLOWUP, True),
    ("dm-self", "dm", "steer", False, False, TEXT, True, (), None, A.REJECT, False),
    # --- group_mention: same as dm
    ("gm-idle", "group_mention", "steer", False, True, TEXT, False, (), None, A.DELIVER_NOW, False),
    ("gm-default-steer-keep", "group_mention", None, False, False, TEXT, False, U, ua(0.9), A.STEER, False),
    ("gm-default-steer-below", "group_mention", None, False, False, TEXT, False, U, ua(0.89), A.FOLLOWUP, True),
    ("gm-steer-keep", "group_mention", "steer", False, False, TEXT, False, U, ua(0.91), A.STEER, False),
    ("gm-steer-fail", "group_mention", "steer", False, False, TEXT, False, U, FAIL, A.FOLLOWUP, True),
    ("gm-abort-unauth", "group_mention", "abort", False, False, TEXT, False, U, ua(0.99), A.STEER, False),
    ("gm-abort-auth", "group_mention", "abort", True, False, TEXT, False, U, ua(0.99), A.ABORT, False),
    # --- group_plain: visible only
    ("gp-idle", "group_plain", "steer", False, True, TEXT, False, (), None, A.VISIBLE_ONLY, False),
    ("gp-busy", "group_plain", "abort", True, False, TEXT, False, (), None, A.VISIBLE_ONLY, False),
    # --- user_voice: directed, missing mode = steer, user may abort
    ("uv-idle", "user_voice", None, False, True, TEXT, False, (), None, A.DELIVER_NOW, False),
    ("uv-default-steer-keep", "user_voice", None, False, False, TEXT, False, U, ua(0.92), A.STEER, False),
    ("uv-default-steer-below", "user_voice", None, False, False, TEXT, False, U, ua(0.6), A.FOLLOWUP, True),
    ("uv-fail", "user_voice", None, False, False, TEXT, False, U, FAIL, A.FOLLOWUP, True),
    ("uv-stop-needs-theta", "user_voice", "abort", False, False, "stop", False, U, ua(0.95), A.STEER, True),
    ("uv-stop-edge", "user_voice", "abort", False, False, "stop", False, U, ua(THETA_ABORT), A.ABORT, False),
    ("uv-followup", "user_voice", "followup", False, False, TEXT, False, (), None, A.FOLLOWUP, False),
    ("uv-backchannel-busy", "user_voice", None, False, False, "好的。", False, (), None, A.VISIBLE_ONLY, False),
    ("uv-backchannel-idle", "user_voice", None, False, True, "嗯", False, (), None, A.VISIBLE_ONLY, False),
    ("uv-short", "user_voice", None, False, False, " 啊! ", False, (), None, A.VISIBLE_ONLY, False),
    # --- broadcast: ambient, mode ignored, never abort, ask even when idle
    ("bc-idle-deliver", "broadcast", None, False, True, TEXT, False, RU, rua(0.1, 0.1), A.DELIVER_NOW, False),
    ("bc-idle-skip", "broadcast", None, False, True, TEXT, False, RU, rua(0.9, 0.1), A.VISIBLE_ONLY, False),
    ("bc-skip-edge", "broadcast", None, False, False, TEXT, False, RU, rua(THETA_SKIP, 0.99), A.VISIBLE_ONLY, False),
    ("bc-skip-below", "broadcast", None, False, False, TEXT, False, RU, rua(0.79, 0.1), A.FOLLOWUP, False),
    ("bc-busy-steer", "broadcast", None, False, False, TEXT, False, RU, rua(0.1, THETA_STEER), A.STEER, False),
    ("bc-busy-below", "broadcast", None, False, False, TEXT, False, RU, rua(0.1, 0.89), A.FOLLOWUP, False),
    ("bc-abort-ignored", "broadcast", "abort", True, False, TEXT, False, RU, rua(0.0, 1.0), A.STEER, False),
    ("bc-fail-idle", "broadcast", None, False, True, TEXT, False, RU, FAIL, A.DELIVER_NOW, False),
    ("bc-fail-busy", "broadcast", None, False, False, TEXT, False, RU, FAIL, A.FOLLOWUP, False),
    ("bc-self", "broadcast", None, False, False, TEXT, True, (), None, A.REJECT, False),
    # --- voice_ambient
    ("va-idle-deliver", "voice_ambient", None, False, True, TEXT, False, RU, rua(0.2, 0.2), A.DELIVER_NOW, False),
    ("va-skip", "voice_ambient", None, False, False, TEXT, False, RU, rua(0.95, 0.99), A.VISIBLE_ONLY, False),
    ("va-busy-steer", "voice_ambient", None, False, False, TEXT, False, RU, rua(0.0, 0.95), A.STEER, False),
    ("va-busy-followup", "voice_ambient", None, False, False, TEXT, False, RU, rua(0.0, 0.3), A.FOLLOWUP, False),
    ("va-fail-idle", "voice_ambient", None, False, True, TEXT, False, RU, FAIL, A.DELIVER_NOW, False),
    ("va-fail-busy", "voice_ambient", None, False, False, TEXT, False, RU, FAIL, A.FOLLOWUP, False),
    ("va-backchannel", "voice_ambient", None, False, False, "ok", False, (), None, A.VISIBLE_ONLY, False),
    # --- non_text
    ("nt-idle", "non_text", None, False, True, "<image>", False, (), None, A.DELIVER_NOW, False),
    ("nt-busy", "non_text", "followup", False, False, "<image>", False, (), None, A.STEER, False),
]


@pytest.mark.parametrize(
    "source,mode,can_abort,idle,content,self_send,questions,answers,action,downgraded",
    [r[1:] for r in ROWS], ids=[r[0] for r in ROWS])
def test_policy_table(source, mode, can_abort, idle, content, self_send,
                      questions, answers, action, downgraded):
    env = Envelope(source=source, sender="a" if self_send else "s", target="a",
                   content=content, mode=mode, sender_can_abort=can_abort)
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


def test_ambient_never_aborts():
    for source in ("broadcast", "voice_ambient"):
        env = Envelope(source, "s", "a", TEXT, mode="abort", sender_can_abort=True)
        d = resolve(env, plan(env, TargetState(False)), rua(0.0, 1.0), TargetState(False))
        assert d.action is not Action.ABORT


def test_reason_has_p_and_theta():
    env = Envelope("dm", "s", "a", TEXT, mode="steer")
    d = resolve(env, plan(env, TargetState(False)), ua(0.5), TargetState(False))
    assert "p_interrupt=0.500" in d.reason and "theta=0.9" in d.reason
    env = Envelope("dm", "s", "a", TEXT, mode="abort")
    d = resolve(env, plan(env, TargetState(False)), ua(0.99), TargetState(False))
    assert "abort_unauthorized=1" in d.reason
    d = resolve(env, plan(env, TargetState(False)), None, TargetState(False))
    assert "failed=1" in d.reason


def test_unknown_source_and_mode():
    with pytest.raises(ValueError):
        plan(Envelope("fax", "s", "a", TEXT), TargetState(False))
    with pytest.raises(ValueError):
        plan(Envelope("dm", "s", "a", TEXT, mode="nuke"), TargetState(False))


@pytest.mark.parametrize("text,expected", [
    ("嗯", True), ("好的", True), ("好的。", True), ("OK!", True), ("对", True),
    ("是的", True), ("好", True), ("", True), ("  ", True), ("a", True), ("…", True),
    ("uh-huh", True), ("好的，帮我把测试也跑一下", False), ("stop", False), ("停下", False),
])
def test_is_backchannel(text, expected):
    assert is_backchannel(text) is expected


@pytest.mark.parametrize("action,idle_now,expected", [
    (A.STEER, True, A.DELIVER_NOW), (A.FOLLOWUP, True, A.DELIVER_NOW),
    (A.ABORT, True, A.DELIVER_NOW), (A.STEER, False, A.STEER),
    (A.FOLLOWUP, False, A.FOLLOWUP), (A.VISIBLE_ONLY, True, A.VISIBLE_ONLY),
    (A.REJECT, True, A.REJECT), (A.DELIVER_NOW, False, A.DELIVER_NOW),
])
def test_final_action(action, idle_now, expected):
    assert final_action(Decision(action, False, ""), idle_now) is expected


def test_merge_followups():
    assert merge_followups("steer", []) == "steer"
    assert merge_followups("steer", ["f1", "f2"]) == "steer\n\nf1\n\nf2"


@pytest.mark.parametrize("last,now,expected", [
    (None, 0.0, True), (100.0, 105.0, False), (100.0, 110.0, True), (100.0, 120.0, True),
])
def test_broadcast_allowed(last, now, expected):
    assert broadcast_allowed(last, now) is expected


def test_constants():
    assert (THETA_STEER, THETA_ABORT, THETA_SKIP, VOICE_TIMEOUT_SECONDS) == (0.9, 0.97, 0.8, 0.8)
