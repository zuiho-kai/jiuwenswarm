import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.team.duplex_thinking import ThinkingWindow


def test_exponential_backoff_and_once_per_round_recovery():
    now = [10.0]
    window = ThinkingWindow(clock=lambda: now[0])
    window.begin(1)
    assert window.remaining() == pytest.approx(0.3)
    now[0] += 0.2
    window.begin(1)  # repeated model calls must not restart protection
    assert window.remaining() == pytest.approx(0.1)
    window.interrupted()
    assert window.seconds == pytest.approx(0.6)
    window.begin(2)
    window.interrupted()
    assert window.seconds == pytest.approx(1.2)
    window.output()
    assert window.seconds == pytest.approx(1.1)
    window.output()
    assert window.seconds == pytest.approx(1.1)
    for _ in range(20):
        window.interrupted()
    assert window.seconds == 8
    for i in range(100):
        window.begin(i + 3)
        window.output()
    assert window.seconds == 0.1


@pytest.mark.asyncio
async def test_sdk_hooks_only_decay_on_valid_first_model_response():
    from jiuwenswarm.agents.harness.team.duplex_native import _ThinkingRail
    harness = NS(active_round=NS(round_id=1, pause_requested=False),
                 _duplex_thinking=ThinkingWindow())
    rail = _ThinkingRail(harness)
    await rail.before_model_call(NS())
    assert harness._duplex_thinking.remaining() > 0
    await rail.after_model_call(NS(inputs=NS(response=None)))
    assert not harness._duplex_thinking.output_seen
    await rail.after_model_call(NS(inputs=NS(response=NS(content="", tool_calls=[]))))
    assert not harness._duplex_thinking.output_seen
    await rail.after_model_call(NS(inputs=NS(response=NS(content="answer"))))
    assert harness._duplex_thinking.seconds == pytest.approx(0.2)
    await rail.after_model_call(NS(inputs=NS(response=NS(content="another answer"))))
    assert harness._duplex_thinking.seconds == pytest.approx(0.2)
    harness.active_round = NS(round_id=2, pause_requested=True)
    await rail.before_model_call(NS())
    await rail.after_model_call(NS(inputs=NS(response=NS(content="cancelled answer"))))
    assert not harness._duplex_thinking.output_seen


@pytest.mark.asyncio
async def test_slow_model_protected_and_concurrent_messages_batched():
    window = ThinkingWindow(initial=0.05)
    window.begin(1)
    deliver = AsyncMock(return_value="INTERRUPT")
    first = asyncio.create_task(window.submit("a", deliver))
    second = asyncio.create_task(window.submit("b", deliver))
    await asyncio.sleep(0.01)
    deliver.assert_not_awaited()
    assert not first.done()
    assert await first == "INTERRUPT"
    assert await second == "INTERRUPT"
    deliver.assert_awaited_once_with(["a", "b"])


@pytest.mark.asyncio
async def test_cancellation_does_not_deliver_later():
    window = ThinkingWindow(initial=0.05)
    window.begin(1)
    deliver = AsyncMock()
    task = asyncio.create_task(window.submit("a", deliver))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.06)
    deliver.assert_not_awaited()
    assert window.pending == []


@pytest.mark.asyncio
async def test_failed_delivery_is_not_acknowledged_as_success():
    window = ThinkingWindow(initial=0.01)
    window.begin(1)
    deliver = AsyncMock(side_effect=RuntimeError("stopped"))
    results = await asyncio.gather(window.submit("a", deliver), window.submit("b", deliver),
                                   return_exceptions=True)
    assert all(isinstance(result, RuntimeError) for result in results)


@pytest.mark.asyncio
async def test_old_decision_cannot_interrupt_new_protected_round(monkeypatch):
    from jiuwenswarm.agents.harness.team import duplex_shadow as shadow
    from jiuwenswarm.agents.harness.team.duplex_native import DuplexNativeHarness
    window = ThinkingWindow()
    window.begin(2)
    harness = NS(_duplex_received=set(), _duplex_thinking=window,
                 _ack=lambda ack, result: ack.set_result(result))
    monkeypatch.setattr(shadow, "snapshot_from_native", lambda _: NS(context_version="new"))
    ack = asyncio.get_running_loop().create_future()
    await DuplexNativeHarness._route(harness, NS(message_id="a", version="old", ack=ack))
    assert await ack == "STALE"


@pytest.mark.asyncio
async def test_route_uses_fresh_snapshot_and_all_batch_messages(monkeypatch):
    from jiuwenswarm.agents.harness.team import duplex_native, duplex_shadow as shadow
    from jiuwenswarm.common.duplex_router import ControlSnapshot, InboundMessage, Observation

    class FakeNative:
        def __init__(self):
            self._duplex_received = set()
            self._duplex_thinking = ThinkingWindow(initial=0.03)
            self._duplex_thinking.begin(1)
            self.interrupt = AsyncMock(return_value="INTERRUPT")

        def _require_alive(self):
            pass

    native = FakeNative()
    version = ["old"]
    def snapshot(_):
        return ControlSnapshot(version[0], "1", "c", "model")
    observation = Observation(("a", "b"), "new", "1", "c", "INTERRUPT", "UNCHANGED", "ok", 1, 1)
    observe = AsyncMock(return_value=observation)
    monkeypatch.setattr(duplex_native, "DuplexNativeHarness", FakeNative)
    monkeypatch.setattr(shadow, "native_from_runtime", lambda _: native)
    monkeypatch.setattr(shadow, "snapshot_from_native", snapshot)
    monkeypatch.setattr(shadow, "observe", observe)
    monkeypatch.setattr(shadow, "_classifier", lambda *args: AsyncMock())
    original = AsyncMock()
    config = {"mode": "active", "backend": "jev", "timeout_seconds": 2}
    tasks = [asyncio.create_task(shadow.deliver_routed(NS(harness=native),
        shadow.RoutedInput(text, InboundMessage(text, "member", text)), use_steer=True,
        original=original, settings=config)) for text in ("a", "b")]
    await asyncio.sleep(0.005)
    observe.assert_not_awaited()
    version[0] = "new"
    assert await asyncio.gather(*tasks) == ["INTERRUPT", "INTERRUPT"]
    observe.assert_awaited_once()
    assert observe.await_args.args[0].context_version == "new"
    assert [message.message_id for message in observe.await_args.args[1]] == ["a", "b"]
    native.interrupt.assert_awaited_once_with("a\n\nb", version="new", message_id="a")
    assert native._duplex_received == {"a", "b"}
    original.assert_not_awaited()
