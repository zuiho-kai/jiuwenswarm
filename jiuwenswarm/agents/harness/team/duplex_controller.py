"""One owned, interruptible input controller per Native runtime."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from jiuwenswarm.common.duplex_router import Observation, observe


@dataclass
class PendingInput:
    content: object
    future: asyncio.Future
    arrived: float


class InputController:
    """Batch arrivals without cancelling an already applying Native transaction.

    Futures resolve only after delivery. The SDK remains responsible for DB ACK.
    The oldest arrival owns the deadline, so a busy sender cannot starve delivery.
    """
    def __init__(self, *, snapshot, classify, apply, timeout=2.0, capacity=32):
        self.snapshot, self.classify, self.apply = snapshot, classify, apply
        self.timeout, self.capacity = timeout, capacity
        self.pending = {}
        self.changed = asyncio.Event()
        self.task = None
        self.closed = False

    def submit(self, content):
        if self.closed:
            raise RuntimeError("input controller closed")
        key = content.message.message_id
        if key in self.pending:
            previous = self.pending[key]
            if previous.content.message != content.message:
                raise ValueError("message_id reused with different content")
            return previous.future
        if len(self.pending) >= self.capacity:
            raise OverflowError("input controller full; message must remain unread")
        future = asyncio.get_running_loop().create_future()
        # A mailbox prefetch may fail before the original drain awaits it.
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        self.pending[key] = PendingInput(content, future, time.monotonic())
        self.changed.set()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="duplex-input-controller")
        return future

    async def _decide(self, deadline):
        attempts = 0
        started = time.monotonic()
        while True:
            self.changed.clear()
            batch = tuple(self.pending.values())
            snapshot = self.snapshot()
            if snapshot is None:
                return batch, None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return batch, Observation(tuple(p.content.message.message_id for p in batch),
                    snapshot.context_version, snapshot.round_id, snapshot.checkpoint_id,
                    "APPEND", "UNCHANGED", "timeout", (time.monotonic() - started) * 1000, attempts)
            classification = asyncio.create_task(observe(snapshot,
                tuple(p.content.message for p in batch), classify=self.classify,
                current_snapshot=self.snapshot, timeout_seconds=remaining))
            arrival = asyncio.create_task(self.changed.wait())
            try:
                await asyncio.wait((classification, arrival), return_when=asyncio.FIRST_COMPLETED)
                if self.changed.is_set():
                    # Never apply a decision that omitted a newly arrived message.
                    attempts += 1
                    continue
                decision = await classification
                from dataclasses import replace
                return batch, replace(decision, attempts=attempts + decision.attempts,
                                      latency_ms=(time.monotonic() - started) * 1000)
            finally:
                for task in (classification, arrival):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(classification, arrival, return_exceptions=True)

    async def _run(self):
        try:
            while self.pending:
                first = next(iter(self.pending.values()))
                batch, decision = await self._decide(first.arrived + self.timeout)
                await self.apply(tuple(p.content for p in batch), decision)
                for item in batch:
                    self.pending.pop(item.content.message.message_id, None)
                    if not item.future.done():
                        item.future.set_result(None)
        except BaseException as error:
            for item in self.pending.values():
                if not item.future.done():
                    if isinstance(error, asyncio.CancelledError):
                        item.future.cancel()
                    else:
                        item.future.set_exception(error)
            self.pending.clear()
            if isinstance(error, asyncio.CancelledError):
                raise

    async def aclose(self):
        self.closed = True
        if self.task is not None and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
