"""Scenario tests: delivery policy wired into the duplex delivery hook."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from choice_classifier import ChoiceAnswer, ProviderError

from jiuwenswarm.agents.harness.team import duplex_delivery as dd
from jiuwenswarm.agents.harness.team import duplex_shadow as shadow
from jiuwenswarm.common.delivery_policy import Action, Decision, Envelope, Plan
from jiuwenswarm.common.duplex_classifier import RELEVANCE, URGENCY
from jiuwenswarm.common.duplex_router import ControlSnapshot, InboundMessage

CONFIG = {"mode": "active", "provider": "typed_choice"}


def answers(p_interrupt=0.1, p_skip=None):
    out = {}
    choice = "INTERRUPT" if p_interrupt >= 0.5 else "APPEND"
    probs = {"INTERRUPT": p_interrupt, "APPEND": 1 - p_interrupt}
    out[URGENCY] = ChoiceAnswer(choice, probs, probs[choice])
    if p_skip is not None:
        choice = "SKIP" if p_skip >= 0.5 else "DELIVER"
        probs = {"SKIP": p_skip, "DELIVER": 1 - p_skip}
        out[RELEVANCE] = ChoiceAnswer(choice, probs, probs[choice])
    return out


class FakeNative:
    def __init__(self, busy=True):
        self._duplex_received = set()
        self.active_round = object() if busy else None
        self.interrupt = AsyncMock(return_value="INTERRUPT")


@pytest.fixture
def route(monkeypatch):
    from jiuwenswarm.agents.harness.team import duplex_native

    monkeypatch.setattr(duplex_native, "DuplexNativeHarness", FakeNative)
    monkeypatch.setattr(shadow, "SCHEDULER", dd.DeliveryScheduler())
    monkeypatch.setattr(shadow, "snapshot_from_native", lambda native: (
        ControlSnapshot("v1", "r1", "c1", "model") if native.active_round is not None else None))
    emit = AsyncMock()
    monkeypatch.setattr(shadow, "_emit_duplex_error", emit)

    async def run(source, *, busy=True, result=None, sender="A1", content="hello there",
                  settings=CONFIG, use_steer=True, mid="m1", native=None):
        native = native or FakeNative(busy)
        monkeypatch.setattr(shadow, "native_from_runtime", lambda _: native)
        classify = AsyncMock(side_effect=result if isinstance(result, Exception) else None,
                             return_value=None if isinstance(result, Exception) else result)
        monkeypatch.setattr(shadow, "classify", classify)
        original = AsyncMock(return_value="sent")
        out = await shadow.deliver_routed(
            NS(harness=native, blueprint=NS(member_name="A2")),
            shadow.RoutedInput(content, InboundMessage(mid, sender, content), source),
            use_steer=use_steer, original=original, settings=settings)
        return NS(out=out, native=native, classify=classify, original=original, emit=emit)

    return run


def steer_of(r):
    return r.original.await_args.kwargs["use_steer"]


# ---------------------------------------------------------------- dm / group @

@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["dm", "group_mention"])
async def test_directed_idle_delivers_now_without_classifier(route, source):
    r = await route(source, busy=False)
    r.classify.assert_not_awaited()
    assert r.out == "sent" and steer_of(r) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["dm", "group_mention"])
async def test_directed_busy_interrupt_runs_safe_interrupt(route, source):
    r = await route(source, result=answers(0.95))
    assert r.out == "INTERRUPT"
    r.native.interrupt.assert_awaited_once()
    r.original.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["dm", "group_mention"])
async def test_directed_busy_append_queues_followup(route, source):
    r = await route(source, result=answers(0.2))
    r.native.interrupt.assert_not_awaited()
    assert steer_of(r) is False


@pytest.mark.asyncio
async def test_directed_failure_followup_and_notice(route):
    r = await route("dm", result=ProviderError("down"))
    assert steer_of(r) is False
    assert r.emit.await_args.kwargs["phase"] == "route"


@pytest.mark.asyncio
async def test_interrupt_unavailable_falls_back_to_plain_steer(route):
    native = FakeNative()
    native.interrupt = AsyncMock(return_value="UNAVAILABLE")
    r = await route("dm", result=answers(0.99), native=native)
    assert r.out == "sent" and steer_of(r) is True


@pytest.mark.asyncio
async def test_target_idle_by_execution_delivers_now(route, monkeypatch):
    native = FakeNative()

    async def classify_then_finish(*args):
        native.active_round = None  # the round ended while the classifier ran
        return answers(0.99)

    monkeypatch.setattr(shadow, "native_from_runtime", lambda _: native)
    original = AsyncMock(return_value="sent")
    monkeypatch.setattr(shadow, "classify", classify_then_finish)
    out = await shadow.deliver_routed(
        NS(harness=native, blueprint=NS(member_name="A2")),
        shadow.RoutedInput("x y", InboundMessage("m9", "A1", "x y"), "dm"),
        use_steer=True, original=original, settings=CONFIG)
    assert out == "sent"
    native.interrupt.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_send_is_rejected(route):
    r = await route("dm", sender="A2")
    assert r.out is None
    r.original.assert_not_awaited()
    r.classify.assert_not_awaited()


# ---------------------------------------------------------------- group plain

@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [True, False])
async def test_group_without_mention_is_visible_only(route, busy):
    r = await route("group_plain", busy=busy)
    assert r.out is None
    r.original.assert_not_awaited()
    r.classify.assert_not_awaited()
    assert "m1" in r.native._duplex_received


# ---------------------------------------------------------------- broadcast

@pytest.mark.asyncio
@pytest.mark.parametrize("busy,result,expected", [
    (False, answers(0.1, p_skip=0.9), "visible"),
    (True, answers(0.99, p_skip=0.8), "visible"),
    (False, answers(0.1, p_skip=0.1), "deliver"),
    (True, answers(0.95, p_skip=0.1), "steer"),
    (True, answers(0.5, p_skip=0.1), "followup"),
    (False, ProviderError("down"), "deliver"),
    (True, ProviderError("down"), "followup"),
])
async def test_broadcast_skip_deliver_steer(route, busy, result, expected):
    r = await route("broadcast", busy=busy, result=result)
    assert r.classify.await_args.args[4] == (RELEVANCE, URGENCY)
    r.native.interrupt.assert_not_awaited()  # a broadcast never triggers the safe interrupt
    if expected == "visible":
        assert r.out is None
        r.original.assert_not_awaited()
    else:
        assert r.out == "sent"
        assert steer_of(r) is (expected != "followup")


@pytest.mark.asyncio
async def test_broadcast_rate_limit_records_only(route):
    first = await route("broadcast", result=answers(0.1, p_skip=0.1), mid="b1")
    assert first.out == "sent"
    second = await route("broadcast", result=answers(0.1, p_skip=0.1), mid="b2")
    second.classify.assert_not_awaited()
    second.original.assert_not_awaited()
    assert second.out is None
    other = await route("broadcast", result=answers(0.1, p_skip=0.1), mid="b3", sender="A3")
    assert other.out == "sent"


def test_broadcast_window_is_per_target_and_sender():
    now = [100.0]
    s = dd.DeliveryScheduler(clock=lambda: now[0])
    assert s.allow_broadcast("A2", "A1")
    assert s.allow_broadcast("A3", "A1")
    assert not s.allow_broadcast("A2", "A1")
    now[0] = 110.0
    assert s.allow_broadcast("A2", "A1")


# ---------------------------------------------------------------- user text

@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [True, False])
async def test_user_text_never_asks_and_steers(route, busy):
    r = await route("user_text", sender="user", busy=busy, result=answers(0.99))
    r.classify.assert_not_awaited()
    r.native.interrupt.assert_not_awaited()
    assert r.out == "sent" and steer_of(r) is True


# ---------------------------------------------------------------- disabled / off

@pytest.mark.asyncio
@pytest.mark.parametrize("settings", [
    {"enabled": False, "mode": "active"}, {"mode": "off"}, {"enabled": "false"},
])
@pytest.mark.parametrize("source", ["dm", "broadcast", "group_plain", "user_text"])
async def test_disabled_keeps_original_behavior(route, settings, source):
    r = await route(source, settings=settings, result=answers(0.99, p_skip=0.99))
    r.classify.assert_not_awaited()
    r.native.interrupt.assert_not_awaited()
    assert r.out == "sent" and steer_of(r) is True


# ---------------------------------------------------------------- mapping

def _dec(action, downgraded=False):
    return Decision(action, downgraded, "")


ASKED = Plan(questions=(URGENCY,), action=None)


@pytest.mark.parametrize("source,plan,decision,action,expected", [
    ("dm", ASKED, _dec(Action.STEER), Action.STEER, dd.RUN_INTERRUPT),
    ("group_mention", ASKED, _dec(Action.STEER), Action.STEER, dd.RUN_INTERRUPT),
    ("broadcast", ASKED, _dec(Action.STEER), Action.STEER, dd.RUN_STEER),
    ("user_text", Plan((), Action.STEER), _dec(Action.STEER), Action.STEER, dd.RUN_STEER),
    ("dm", ASKED, _dec(Action.FOLLOWUP, True), Action.FOLLOWUP, dd.RUN_FOLLOWUP),
    ("dm", ASKED, _dec(Action.STEER), Action.DELIVER_NOW, dd.RUN_DELIVER),
    ("broadcast", ASKED, _dec(Action.VISIBLE_ONLY), Action.VISIBLE_ONLY, dd.RUN_VISIBLE),
    ("dm", Plan((), Action.REJECT), _dec(Action.REJECT), Action.REJECT, dd.RUN_REJECT),
])
def test_runtime_action_mapping(source, plan, decision, action, expected):
    env = Envelope(source, "A1", "A2", "text")
    assert dd.runtime_action(env, plan, decision, action) == expected


def test_envelope_from_sdk_signals():
    env = dd.envelope_for(InboundMessage("m", "A1", "x"), source="dm", target="A2")
    assert env == Envelope("dm", "A1", "A2", "x")


# ---------------------------------------------------------------- scheduler

@pytest.mark.asyncio
async def test_one_judgment_per_target_batches_later_messages():
    s = dd.DeliveryScheduler()
    gate = asyncio.Event()
    calls = []

    async def ask(questions, messages):
        calls.append([m.message_id for m in messages])
        if len(calls) == 1:
            await gate.wait()
        return {"ids": [m.message_id for m in messages]}

    msg = lambda mid: InboundMessage(mid, "A1", mid)  # noqa: E731
    first = asyncio.create_task(s.judge("A2", (URGENCY,), msg("a"), ask))
    await asyncio.sleep(0)
    later = [asyncio.create_task(s.judge("A2", (URGENCY,), msg(m), ask)) for m in ("b", "c")]
    other = asyncio.create_task(s.judge("A3", (URGENCY,), msg("d"), ask))
    await asyncio.sleep(0.01)
    assert calls == [["a"], ["d"]]  # A2 has one in flight; A3 is independent
    gate.set()
    results = await asyncio.gather(first, *later, other)
    assert calls == [["a"], ["d"], ["b", "c"]]
    assert results[1].answers == results[2].answers == {"ids": ["b", "c"]}
    assert results[1].failure is None and results[1].run is None


@pytest.mark.asyncio
async def test_scheduler_returns_failure_to_each_waiter():
    s = dd.DeliveryScheduler()

    async def ask(questions, messages):
        raise ProviderError("down")

    verdict = await s.judge("A2", (URGENCY,), InboundMessage("a", "A1", "a"), ask)
    assert verdict.answers is None and isinstance(verdict.failure, ProviderError)


def _batch_world(monkeypatch, native):
    """Message "a" holds the target's judgment; "b" and "c" queue and form one batch."""
    gate = asyncio.Event()
    calls = []

    async def classify(host, config, snapshot, messages, questions):
        calls.append([m.message_id for m in messages])
        if not gate.is_set():
            await gate.wait()
        return answers(0.99)

    monkeypatch.setattr(shadow, "native_from_runtime", lambda _: native)
    monkeypatch.setattr(shadow, "SCHEDULER", dd.DeliveryScheduler())
    monkeypatch.setattr(shadow, "classify", classify)
    original = AsyncMock(return_value="sent")

    async def send(mid):
        return await shadow.deliver_routed(
            NS(harness=native, blueprint=NS(member_name="A2")),
            shadow.RoutedInput(f"text {mid}", InboundMessage(mid, "A1", mid), "dm"),
            use_steer=True, original=original, settings=CONFIG)

    async def run():
        blocker = asyncio.create_task(send("a"))
        await asyncio.sleep(0.01)
        batch = [asyncio.create_task(send(m)) for m in ("b", "c")]
        await asyncio.sleep(0.01)
        gate.set()
        return await asyncio.gather(blocker, *batch, return_exceptions=True)

    return run, calls, original


@pytest.mark.asyncio
async def test_batched_interrupt_runs_once_with_all_messages_in_order(route, monkeypatch):
    """A batch judged INTERRUPT performs ONE interrupt carrying every message; none re-triggers."""
    native = FakeNative()
    run, calls, original = _batch_world(monkeypatch, native)
    results = await run()
    assert calls == [["a"], ["b", "c"]]
    assert results == ["INTERRUPT", "INTERRUPT", "INTERRUPT"]
    # "a" alone, then a single interrupt for the b+c batch, merged in arrival order.
    assert [c.args[0] for c in native.interrupt.await_args_list] == ["text a", "text b\n\ntext c"]
    assert native.interrupt.await_args.kwargs["message_id"] == "b"
    assert {"a", "b", "c"} <= native._duplex_received
    original.assert_not_awaited()


@pytest.mark.asyncio
async def test_batched_interrupt_unavailable_steers_merged_text_once(route, monkeypatch):
    native = FakeNative()
    native.interrupt = AsyncMock(return_value="UNAVAILABLE")
    run, _, original = _batch_world(monkeypatch, native)
    results = await run()
    assert results == ["sent", "sent", "sent"]
    assert [c.args[1] for c in original.await_args_list] == ["text a", "text b\n\ntext c"]
    assert all(c.kwargs["use_steer"] is True for c in original.await_args_list)


@pytest.mark.asyncio
async def test_batched_interrupt_failure_reaches_every_joined_message(route, monkeypatch):
    native = FakeNative()
    native.interrupt = AsyncMock(side_effect=RuntimeError("supervisor stopped"))
    run, _, original = _batch_world(monkeypatch, native)
    results = await run()
    assert all(isinstance(r, RuntimeError) for r in results)
    assert native.interrupt.await_count == 2  # "a" alone, then one for the b+c batch
    assert not {"a", "b", "c"} & native._duplex_received
    original.assert_not_awaited()


# ---------------------------------------------------------------- real SDK drain

@pytest.mark.asyncio
async def test_visible_only_is_marked_read_by_real_drain_without_wakeup(monkeypatch):
    """Group chat without @ is consumed: DB read state advances, the agent is not fed."""
    from openjiuwen.agent_teams.agent.team_agent import TeamAgent
    from openjiuwen.agent_teams.group_chat.message_handler import GroupMessageHandler
    from openjiuwen.agent_teams.schema.team import TeamRole
    from jiuwenswarm.agents.harness.team import duplex_native
    from jiuwenswarm.common import config

    shadow.install_shadow_observer()
    monkeypatch.setattr(duplex_native, "DuplexNativeHarness", FakeNative)
    monkeypatch.setattr(config, "get_config", lambda: {"duplex_router": CONFIG})
    native = FakeNative()
    native.send = AsyncMock()
    monkeypatch.setattr(shadow, "native_from_runtime", lambda _: native)
    host = NS(harness=native, blueprint=NS(member_name="A2"), has_pending_interrupt=lambda: False)
    host.deliver_input = lambda content, **kw: TeamAgent.deliver_input(host, content, **kw)
    mm = NS(mark_messages_read=AsyncMock(), mark_message_read=AsyncMock())
    handler = GroupMessageHandler(host, NS(role=TeamRole.LEADER, language="en", member_name="A2"),
                                  NS(message_manager=mm, team_backend=None), NS())
    meta = json.dumps({"type": "group_chat", "client_message_id": "c1", "mentions": ["A3"]})
    row = NS(message_id="g1", from_member_name="A1", to_member_name=None, broadcast=True,
             protocol="text", content="for A3 only", timestamp=0, meta=meta)
    handler._read_all_unread = AsyncMock(side_effect=[[row], []])
    handler._expand = AsyncMock(return_value=NS(body="for A3 only", is_template=False))
    await handler._process_unread_messages("A2")
    native.send.assert_not_awaited()
    mm.mark_messages_read.assert_awaited_once_with([row], "A2")
