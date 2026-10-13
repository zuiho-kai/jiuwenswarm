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
RUN_STEER = "steer"          # original deliver_input(use_steer=True)
RUN_FOLLOWUP = "followup"    # original deliver_input(use_steer=False)
RUN_DELIVER = "deliver"      # original deliver_input; an idle target starts a round
RUN_VISIBLE = "visible"      # no wake-up; the SDK drain still marks the row read
RUN_REJECT = "reject"        # dropped and logged


def envelope_for(message: InboundMessage, *, source: str, target: str) -> Envelope:
    """Build the policy envelope from SDK signals only.

    Neither A2A senders nor user text carry a mode (the SDK has no such
    parameter), so the policy default applies: user text steers unjudged,
    directed messages ask whether they must interrupt.
    """
    return Envelope(source=source, sender=message.sender, target=target,
                    content=message.content, mode=None, sender_can_abort=False)


def runtime_action(env: Envelope, plan: Plan, decision: Decision, action: Action) -> str:
    """Map the final policy Action to one runtime operation.

    A directed STEER that the classifier kept (INTERRUPT with p >= THETA_STEER,
    not a downgrade or a fixed rule) runs the safe interrupt; every other STEER
    is a plain steer.
    """
    if action is Action.ABORT:
        return RUN_INTERRUPT
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
    return Decision(action, False, f"scene={env.source} mode=- idle={int(idle)} "
                                   f"rule=broadcast_rate_limited action={action.value} downgraded=0")


Ask = Callable[[tuple[str, ...], tuple[InboundMessage, ...]], Awaitable[Any]]


@dataclass
class _Pending:
    questions: tuple[str, ...]
    message: InboundMessage
    ask: Ask
    future: asyncio.Future = field(repr=False)


class DeliveryScheduler:
    """Per-target judgment queue and broadcast rate limit.

    One classifier request is in flight per target. Messages that arrive
    meanwhile wait and are judged together in the next request when they need
    the same questions; each waiter gets the shared answers.
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
                    ask: Ask) -> tuple[Any, BaseException | None]:
        """Return (answers, None) or (None, failure) for ``message``."""
        future = asyncio.get_running_loop().create_future()
        self._pending.setdefault(target, []).append(_Pending(questions, message, ask, future))
        runner = self._runners.get(target)
        if runner is None or runner.done():
            self._runners[target] = asyncio.create_task(self._run(target))
        return await future

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
                    result = (await batch[-1].ask(questions, tuple(i.message for i in batch)), None)
                except Exception as exc:  # CancelledError propagates.
                    result = (None, exc)
                for item in batch:
                    if not item.future.done():
                        item.future.set_result(result)
        finally:
            for item in (*batch, *queue):
                if not item.future.done():
                    item.future.set_result((None, RuntimeError("duplex judgment runner stopped")))
            queue.clear()
            self._runners.pop(target, None)


SCHEDULER = DeliveryScheduler()
