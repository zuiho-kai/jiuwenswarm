# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Runtime glue between the pure delivery policy and the SDK delivery hook.

The policy (``jiuwenswarm.common.delivery_policy``) decides an Action. This
module holds the small amount of state the policy must not hold: one
in-flight classifier judgment per target with batching, and the per-sender
broadcast rate limit. It also maps an Action to one runtime operation.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.common.delivery_policy import (
    DIRECTED_SOURCES,
    Action,
    Decision,
    Envelope,
    Plan,
    broadcast_allowed,
)
from jiuwenswarm.common.duplex_router import InboundMessage

RUN_INTERRUPT = "interrupt"  # NativeHarness safe pause + invalidate plan + replan
RUN_JOINED = "joined"        # carried by the batch leader's single interrupt
RUN_STEER = "steer"          # original deliver_input(use_steer=True)
RUN_FOLLOWUP = "followup"    # original deliver_input(use_steer=False)
RUN_DELIVER = "deliver"      # original deliver_input; an idle target starts a round
RUN_VISIBLE = "visible"      # no wake-up; the SDK drain still marks the row read
RUN_REJECT = "reject"        # dropped and logged


def envelope_for(message: InboundMessage, *, source: str, target: str) -> Envelope:
    """Build the policy envelope from SDK signals only."""
    return Envelope(source=source, sender=message.sender, target=target, content=message.content)


def runtime_action(env: Envelope, plan: Plan, decision: Decision, action: Action) -> str:
    """Map the final policy Action to one runtime operation.

    A directed STEER that the classifier kept (INTERRUPT with p >= THETA_STEER,
    not a downgrade or a fixed rule) runs the safe interrupt; every other STEER
    is a plain steer.
    """
    if action is Action.STEER:
        classifier_kept = plan.action is None and not decision.downgraded
        return RUN_INTERRUPT if classifier_kept and env.source in DIRECTED_SOURCES else RUN_STEER
    return {
        Action.FOLLOWUP: RUN_FOLLOWUP,
        Action.DELIVER_NOW: RUN_DELIVER,
        Action.VISIBLE_ONLY: RUN_VISIBLE,
        Action.REJECT: RUN_REJECT,
    }[action]


def rate_limited_decision(env: Envelope, idle: bool) -> Decision:
    """Over the per-sender broadcast limit: record only, without asking."""
    action = Action.VISIBLE_ONLY
    return Decision(action, False, f"scene={env.source} idle={int(idle)} "
                                   f"rule=broadcast_rate_limited action={action.value} downgraded=0")


Ask = Callable[[tuple[str, ...], tuple[InboundMessage, ...]], Awaitable[Any]]
Decide = Callable[[Any, "BaseException | None"], str]


@dataclass(frozen=True)
class Verdict:
    """One waiter's share of a batched judgment.

    ``run`` is the waiter's own runtime action (from its ``decide``). When
    several waiters of one batch decide RUN_INTERRUPT, the first is the leader:
    it gets RUN_INTERRUPT with ``merged`` = every interrupting (message_id,
    content) in arrival order, and performs the single interrupt. The others
    get RUN_JOINED and await ``joined`` for the leader's outcome.
    """

    answers: Any
    failure: BaseException | None
    run: str | None = None
    merged: tuple[tuple[str, str], ...] = ()
    joined: asyncio.Future | None = field(default=None, repr=False, compare=False)


@dataclass
class _Pending:
    questions: tuple[str, ...]
    message: InboundMessage
    ask: Ask
    future: asyncio.Future = field(repr=False)
    content: str = ""
    decide: Decide | None = None


class DeliveryScheduler:
    """Per-target judgment queue and broadcast rate limit.

    One classifier request is in flight per target. Messages that arrive
    meanwhile wait and are judged together in the next request when they need
    the same questions; each waiter gets the shared answers. Interrupt
    decisions of one batch collapse into a single interrupt.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._pending: dict[str, list[_Pending]] = {}
        self._runners: dict[str, asyncio.Task] = {}
        self._broadcast_at: dict[tuple[str, str], float] = {}

    def allow_broadcast(self, target: str, sender: str) -> bool:
        key = (target, sender)
        now = self._clock()
        if not broadcast_allowed(self._broadcast_at.get(key), now):
            return False
        self._broadcast_at[key] = now
        return True

    async def judge(self, target: str, questions: tuple[str, ...], message: InboundMessage,
                    ask: Ask, *, content: str = "", decide: Decide | None = None) -> Verdict:
        """Judge ``message``; ``decide(answers, failure)`` maps the shared answers to its run."""
        future = asyncio.get_running_loop().create_future()
        self._pending.setdefault(target, []).append(
            _Pending(questions, message, ask, future, content, decide))
        runner = self._runners.get(target)
        if runner is None or runner.done():
            self._runners[target] = asyncio.create_task(self._run(target))
        try:
            return await future
        except asyncio.CancelledError:
            # A cancelled leader must not leave joined waiters hanging.
            if future.done() and not future.cancelled():
                verdict = future.result()
                if verdict.run == RUN_INTERRUPT and verdict.joined is not None:
                    settle_joined(verdict.joined, error=RuntimeError("duplex interrupt leader cancelled"))
            raise

    async def _run(self, target: str) -> None:
        queue = self._pending[target]
        batch: list[_Pending] = []
        try:
            while queue:
                questions = queue[0].questions
                batch = [item for item in queue if item.questions == questions]
                for item in batch:
                    queue.remove(item)
                try:
                    # The newest waiter carries the freshest snapshot.
                    answers, failure = await batch[-1].ask(questions, tuple(i.message for i in batch)), None
                except Exception as exc:  # CancelledError propagates.
                    answers, failure = None, exc
                self._settle(batch, answers, failure)
        finally:
            for item in (*batch, *queue):
                if not item.future.done():
                    item.future.set_result(Verdict(None, RuntimeError("duplex judgment runner stopped")))
            queue.clear()
            self._runners.pop(target, None)

    @staticmethod
    def _settle(batch: list[_Pending], answers: Any, failure: BaseException | None) -> None:
        runs: list[tuple[_Pending, str | None]] = []
        for item in batch:
            if item.future.done():
                continue
            try:
                run = item.decide(answers, failure) if item.decide is not None else None
            except Exception as exc:
                item.future.set_exception(exc)
                continue
            runs.append((item, run))
        interrupting = [item for item, run in runs if run == RUN_INTERRUPT]
        joined = None
        if len(interrupting) > 1:
            joined = asyncio.get_running_loop().create_future()
            # Mark a failure retrieved even when every joined waiter was cancelled.
            joined.add_done_callback(lambda f: f.cancelled() or f.exception())
        merged = tuple((i.message.message_id, i.content) for i in interrupting)
        for item, run in runs:
            if run == RUN_INTERRUPT and item is not interrupting[0]:
                item.future.set_result(Verdict(answers, failure, RUN_JOINED, joined=joined))
            elif run == RUN_INTERRUPT:
                item.future.set_result(Verdict(answers, failure, run, merged, joined))
            else:
                item.future.set_result(Verdict(answers, failure, run))


def settle_joined(joined: asyncio.Future | None, *, result: Any = None,
                  error: BaseException | None = None) -> None:
    """Publish the leader's interrupt outcome to the joined batch waiters."""
    if joined is None or joined.done():
        return
    if error is not None:
        joined.set_exception(error)
    else:
        joined.set_result(result)


SCHEDULER = DeliveryScheduler()
