"""Per-harness interrupt backoff; delivery acknowledgements remain awaitable."""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class ThinkingWindow:
    def __init__(self, *, initial=0.3, minimum=0.1, maximum=8.0, decrease=0.1,
                 clock=time.monotonic):
        self.minimum, self.maximum, self.decrease = minimum, maximum, decrease
        self.seconds = initial
        self.clock = clock
        self.started = None
        self.round = None
        self.output_seen = False
        self.lock = asyncio.Lock()
        self.pending = []

    def begin(self, round_id):
        if round_id != self.round:
            self.round = round_id
            self.started = self.clock()
            self.output_seen = False
            logger.info("duplex thinking begin round_id=%s window_seconds=%.3f", round_id, self.seconds)

    def remaining(self):
        return max(0.0, self.seconds - (self.clock() - self.started)) if self.started is not None else 0.0

    def interrupted(self):
        self.seconds = min(self.maximum, self.seconds * 2)
        self.started = self.clock()
        self.output_seen = False
        logger.info("duplex thinking backoff window_seconds=%.3f", self.seconds)

    def output(self):
        if not self.output_seen:
            self.output_seen = True
            self.seconds = max(self.minimum, self.seconds - self.decrease)
            logger.info("duplex thinking output window_seconds=%.3f", self.seconds)

    async def submit(self, item, deliver):
        """Batch concurrent deliveries; never acknowledge before actual delivery.

        Each caller retains ownership of its delivery. No detached task is used,
        so cancellation/stop cannot leave an unowned timer delivering later.
        """
        future = asyncio.get_running_loop().create_future()
        entry = (item, future)
        self.pending.append(entry)
        try:
            async with self.lock:
                if future.done():
                    ok, result = future.result()
                    if not ok:
                        raise result
                    return result
                while self.remaining() > 0:
                    await asyncio.sleep(self.remaining())
                batch, self.pending = self.pending, []
                batch = [(item, ack) for item, ack in batch if not ack.cancelled()]
                try:
                    result = await deliver([item for item, _ in batch])
                except BaseException as exc:
                    for _, ack in batch:
                        if not ack.done():
                            # Avoid unobserved exceptions on cancelled callers.
                            ack.set_result((False, exc))
                    raise
                else:
                    for _, ack in batch:
                        if not ack.done():
                            ack.set_result((True, result))
            ok, result = await future
            if not ok:
                raise result
            return result
        finally:
            if entry in self.pending:
                self.pending.remove(entry)
            if not future.done():
                future.cancel()
