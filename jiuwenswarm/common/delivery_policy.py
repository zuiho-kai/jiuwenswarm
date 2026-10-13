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

* User text is never judged: idle -> DELIVER_NOW, busy -> STEER.
* Directed messages (DM, group @) ask whether they must interrupt. STEER is
  kept only for INTERRUPT with p >= THETA_STEER; otherwise FOLLOWUP.
* Broadcasts ask relevance and urgency; SKIP with p >= THETA_SKIP records only.
* Group chat without @ records only. Self-sends are rejected.
* Classifier failure (``answers is None`` or a missing answer) means
  DELIVER + APPEND.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

THETA_STEER = 0.9
THETA_SKIP = 0.8
BROADCAST_WINDOW_SECONDS = 10.0

Q_URGENCY = "urgency"
Q_RELEVANCE = "relevance"
INTERRUPT = "INTERRUPT"
APPEND = "APPEND"
DELIVER = "DELIVER"
SKIP = "SKIP"

SRC_USER_TEXT = "user_text"
SRC_DM = "dm"
SRC_GROUP_MENTION = "group_mention"
SRC_GROUP_PLAIN = "group_plain"
SRC_BROADCAST = "broadcast"

DIRECTED_SOURCES = frozenset({SRC_DM, SRC_GROUP_MENTION})
SOURCES = DIRECTED_SOURCES | {SRC_USER_TEXT, SRC_GROUP_PLAIN, SRC_BROADCAST}


class Action(str, Enum):
    STEER = "steer"
    FOLLOWUP = "followup"
    DELIVER_NOW = "deliver_now"
    VISIBLE_ONLY = "visible_only"
    REJECT = "reject"


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

def final_action(decision: Decision, target_idle_now: bool) -> Action:
    """Stale-state rule: a waking action on a now-idle target becomes DELIVER_NOW.

    VISIBLE_ONLY and REJECT never change. No re-judging happens.
    """
    if target_idle_now and decision.action in (Action.STEER, Action.FOLLOWUP):
        return Action.DELIVER_NOW
    return decision.action


def broadcast_allowed(last_sent_at: float | None, now: float,
                      window: float = BROADCAST_WINDOW_SECONDS) -> bool:
    """Allow one broadcast per ``window`` seconds per sender."""
    return last_sent_at is None or now - last_sent_at >= window


# ---------------------------------------------------------------- plan

def _check(env: Envelope) -> None:
    if env.source not in SOURCES:
        raise ValueError(f"unknown envelope source: {env.source!r}")


def _fixed(env: Envelope, state: TargetState) -> Action | None:
    """Action decided without the classifier, or None when a question is needed."""
    src = env.source
    if env.sender == env.target:
        return Action.REJECT
    if src == SRC_GROUP_PLAIN:
        return Action.VISIBLE_ONLY
    if src == SRC_BROADCAST:
        return None  # ask even when idle
    if state.idle:
        return Action.DELIVER_NOW
    if src == SRC_USER_TEXT:
        return Action.STEER
    return None


def plan(env: Envelope, state: TargetState) -> Plan:
    _check(env)
    action = _fixed(env, state)
    if action is not None:
        return Plan(questions=(), action=action)
    if env.source == SRC_BROADCAST:
        return Plan(questions=(Q_RELEVANCE, Q_URGENCY), action=None)
    return Plan(questions=(Q_URGENCY,), action=None)


# ---------------------------------------------------------------- resolve

def _p(answer: AnswerLike | None, label: str) -> float | None:
    if answer is None:
        return None
    return float(answer.probabilities.get(label, 0.0))


def _reason(env: Envelope, action: Action, downgraded: bool, **extra: object) -> str:
    fields = [f"scene={env.source}"]
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
        why = "self_send" if env.sender == env.target else "fixed"
        return Decision(plan.action, False,
                        _reason(env, plan.action, False, idle=state.idle, rule=why))
    if env.source == SRC_BROADCAST:
        return _resolve_broadcast(env, answers, state)
    return _resolve_directed(env, answers, state)


def _resolve_directed(env: Envelope, answers: Mapping[str, AnswerLike] | None,
                      state: TargetState) -> Decision:
    urgency = _get(answers, Q_URGENCY)
    p_int = _p(urgency, INTERRUPT)
    keep = (urgency is not None and urgency.choice == INTERRUPT
            and p_int is not None and p_int >= THETA_STEER)
    action = Action.STEER if keep else Action.FOLLOWUP
    downgraded = not keep
    return Decision(action, downgraded, _reason(
        env, action, downgraded, idle=state.idle, failed=(urgency is None) or None,
        p_interrupt=p_int, theta=THETA_STEER))


def _resolve_broadcast(env: Envelope, answers: Mapping[str, AnswerLike] | None,
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
