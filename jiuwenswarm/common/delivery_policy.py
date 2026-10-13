# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Pure delivery policy for duplex A2A / U2A messages.

The policy decides how an incoming message reaches a target agent.
It has no I/O and no runtime imports.

Flow for a caller::

    p = plan(env, state)
    if p.questions:
        answers = ask_classifier(p.questions)   # None on failure / timeout
    decision = resolve(env, p, answers, state)
    action = final_action(decision, target_idle_now=...)

Rules:

* Actions, strongest first: ABORT > STEER > FOLLOWUP. DELIVER_NOW wakes an
  idle target. VISIBLE_ONLY writes history without waking. REJECT drops.
* The classifier only downgrades, one level at a time (abort -> steer,
  steer -> followup). It never upgrades.
* Classifier failure (``answers is None`` or a missing answer) means
  DELIVER + APPEND.
* ABORT does not clear queued followups. STEER merges them
  (see :func:`merge_followups`).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

THETA_STEER = 0.9
THETA_ABORT = 0.97
THETA_SKIP = 0.8
VOICE_TIMEOUT_SECONDS = 0.8
BROADCAST_WINDOW_SECONDS = 10.0

Q_URGENCY = "urgency"
Q_RELEVANCE = "relevance"
INTERRUPT = "INTERRUPT"
APPEND = "APPEND"
DELIVER = "DELIVER"
SKIP = "SKIP"

SRC_USER_TEXT = "user_text"
SRC_USER_VOICE = "user_voice"
SRC_DM = "dm"
SRC_GROUP_MENTION = "group_mention"
SRC_GROUP_PLAIN = "group_plain"
SRC_BROADCAST = "broadcast"
SRC_VOICE_AMBIENT = "voice_ambient"
SRC_NON_TEXT = "non_text"

DIRECTED_SOURCES = frozenset({SRC_USER_VOICE, SRC_DM, SRC_GROUP_MENTION})
AMBIENT_SOURCES = frozenset({SRC_BROADCAST, SRC_VOICE_AMBIENT})
VOICE_SOURCES = frozenset({SRC_USER_VOICE, SRC_VOICE_AMBIENT})
USER_SOURCES = frozenset({SRC_USER_TEXT, SRC_USER_VOICE})
SOURCES = DIRECTED_SOURCES | AMBIENT_SOURCES | {SRC_USER_TEXT, SRC_GROUP_PLAIN, SRC_NON_TEXT}


class Action(str, Enum):
    ABORT = "abort"
    STEER = "steer"
    FOLLOWUP = "followup"
    DELIVER_NOW = "deliver_now"
    VISIBLE_ONLY = "visible_only"
    REJECT = "reject"


MODES = frozenset({Action.ABORT.value, Action.STEER.value, Action.FOLLOWUP.value})
_DOWNGRADE = {Action.ABORT: Action.STEER, Action.STEER: Action.FOLLOWUP}
_THETA = {Action.ABORT: THETA_ABORT, Action.STEER: THETA_STEER}


class AnswerLike(Protocol):
    """Structural match for ``choice_classifier.ChoiceAnswer``."""

    @property
    def choice(self) -> str: ...

    @property
    def probabilities(self) -> Mapping[str, float]: ...

    @property
    def confidence(self) -> float: ...


@dataclass(frozen=True)
class Envelope:
    source: str
    sender: str
    target: str
    content: str
    mode: str | None = None
    sender_can_abort: bool = False


@dataclass(frozen=True)
class TargetState:
    idle: bool


@dataclass(frozen=True)
class Plan:
    questions: tuple[str, ...]
    action: Action | None  # set when no question is needed


@dataclass(frozen=True)
class Decision:
    action: Action
    downgraded: bool
    reason: str


# ---------------------------------------------------------------- helpers

_BACKCHANNEL = frozenset({
    "嗯", "嗯嗯", "嗯哼", "哦", "噢", "啊", "好", "好的", "好滴", "好嘞", "行", "对", "对对",
    "是", "是的", "收到", "明白", "知道了",
    "ok", "okay", "yes", "yeah", "yep", "sure", "right", "mhm", "uhhuh",
})


def _strip_noise(text: str) -> str:
    return "".join(
        ch for ch in text
        if not unicodedata.category(ch).startswith(("P", "Z", "C", "S"))
    )


def is_backchannel(text: str) -> bool:
    """True for short acknowledgements that should not wake an agent."""
    core = _strip_noise(text or "").casefold()
    return len(core) < 2 or core in _BACKCHANNEL


def final_action(decision: Decision, target_idle_now: bool) -> Action:
    """Stale-state rule: a waking action on a now-idle target becomes DELIVER_NOW.

    VISIBLE_ONLY and REJECT never change. No re-judging happens.
    """
    if target_idle_now and decision.action in (Action.ABORT, Action.STEER, Action.FOLLOWUP):
        return Action.DELIVER_NOW
    return decision.action


def merge_followups(steer_text: str, queued: list[str]) -> str:
    """Append queued followups, in order, to a steer message.

    ABORT does not clear queued followups; only STEER merges them.
    """
    parts = [steer_text, *(q for q in queued if q)]
    return "\n\n".join(p for p in parts if p)


def broadcast_allowed(last_sent_at: float | None, now: float,
                      window: float = BROADCAST_WINDOW_SECONDS) -> bool:
    """Allow one broadcast per ``window`` seconds per sender."""
    return last_sent_at is None or now - last_sent_at >= window


# ---------------------------------------------------------------- plan

def _check(env: Envelope) -> None:
    if env.source not in SOURCES:
        raise ValueError(f"unknown envelope source: {env.source!r}")
    if env.mode is not None and env.mode not in MODES:
        raise ValueError(f"unknown delivery mode: {env.mode!r}")


def _directed_mode(env: Envelope) -> tuple[Action, bool]:
    """Return (effective mode, abort_was_unauthorized)."""
    if env.mode is None:
        # A directed message without a mode asks whether it must interrupt.
        return Action.STEER, False
    mode = Action(env.mode)
    can_abort = env.source in USER_SOURCES or env.sender_can_abort
    if mode is Action.ABORT and not can_abort:
        return Action.STEER, True
    return mode, False


def _fixed(env: Envelope, state: TargetState) -> Action | None:
    """Action decided without the classifier, or None when a question is needed."""
    src = env.source
    if env.sender == env.target:
        return Action.REJECT
    if src in VOICE_SOURCES and is_backchannel(env.content):
        return Action.VISIBLE_ONLY
    if src == SRC_GROUP_PLAIN:
        return Action.VISIBLE_ONLY
    if src in AMBIENT_SOURCES:
        return None  # ask even when idle
    if state.idle:
        return Action.DELIVER_NOW
    if src == SRC_NON_TEXT:
        return Action.STEER
    if src == SRC_USER_TEXT:
        return Action(env.mode) if env.mode else Action.STEER
    mode, _ = _directed_mode(env)
    return Action.FOLLOWUP if mode is Action.FOLLOWUP else None


def plan(env: Envelope, state: TargetState) -> Plan:
    _check(env)
    action = _fixed(env, state)
    if action is not None:
        return Plan(questions=(), action=action)
    if env.source in AMBIENT_SOURCES:
        return Plan(questions=(Q_RELEVANCE, Q_URGENCY), action=None)
    return Plan(questions=(Q_URGENCY,), action=None)


# ---------------------------------------------------------------- resolve

def _p(answer: AnswerLike | None, label: str) -> float | None:
    if answer is None:
        return None
    return float(answer.probabilities.get(label, 0.0))


def _reason(env: Envelope, action: Action, downgraded: bool, **extra: object) -> str:
    fields = [f"scene={env.source}", f"mode={env.mode or '-'}"]
    for key, value in extra.items():
        if value is None:
            continue
        if isinstance(value, float):
            value = f"{value:.3f}"
        elif isinstance(value, bool):
            value = int(value)
        fields.append(f"{key}={value}")
    fields += [f"action={action.value}", f"downgraded={int(downgraded)}"]
    return " ".join(fields)


def _get(answers: Mapping[str, AnswerLike] | None, question: str) -> AnswerLike | None:
    return None if answers is None else answers.get(question)


def resolve(env: Envelope, plan: Plan, answers: Mapping[str, AnswerLike] | None,
            state: TargetState) -> Decision:
    _check(env)
    if plan.action is not None:
        why = "fixed"
        if env.sender == env.target:
            why = "self_send"
        elif env.source in VOICE_SOURCES and is_backchannel(env.content):
            why = "backchannel"
        return Decision(plan.action, False,
                        _reason(env, plan.action, False, idle=state.idle, rule=why))
    if env.source in AMBIENT_SOURCES:
        return _resolve_ambient(env, answers, state)
    return _resolve_directed(env, answers, state)


def _resolve_directed(env: Envelope, answers: Mapping[str, AnswerLike] | None,
                      state: TargetState) -> Decision:
    mode, unauthorized = _directed_mode(env)
    theta = _THETA[mode]
    urgency = _get(answers, Q_URGENCY)
    p_int = _p(urgency, INTERRUPT)
    keep = (urgency is not None and urgency.choice == INTERRUPT
            and p_int is not None and p_int >= theta)
    action = mode if keep else _DOWNGRADE[mode]
    downgraded = not keep
    return Decision(action, downgraded, _reason(
        env, action, downgraded, idle=state.idle, effective=mode.value,
        abort_unauthorized=unauthorized or None, failed=(urgency is None) or None,
        p_interrupt=p_int, theta=theta))


def _resolve_ambient(env: Envelope, answers: Mapping[str, AnswerLike] | None,
                     state: TargetState) -> Decision:
    relevance = _get(answers, Q_RELEVANCE)
    urgency = _get(answers, Q_URGENCY)
    p_skip = _p(relevance, SKIP)
    p_int = _p(urgency, INTERRUPT)
    failed = (relevance is None or urgency is None) or None
    if p_skip is not None and p_skip >= THETA_SKIP:
        action = Action.VISIBLE_ONLY
        theta = THETA_SKIP
    elif state.idle:
        action, theta = Action.DELIVER_NOW, None
    elif (urgency is not None and urgency.choice == INTERRUPT
          and p_int is not None and p_int >= THETA_STEER):
        action, theta = Action.STEER, THETA_STEER
    else:
        action, theta = Action.FOLLOWUP, THETA_STEER
    return Decision(action, False, _reason(
        env, action, False, idle=state.idle, failed=failed,
        p_skip=p_skip, p_interrupt=p_int, theta=theta))
