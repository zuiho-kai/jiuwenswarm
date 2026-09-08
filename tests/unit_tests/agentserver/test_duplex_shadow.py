from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.duplex_router import (
    ControlSnapshot, InboundMessage, observe, prompt_for,
)
from jiuwenswarm.agents.harness.team import duplex_shadow as shadow


def snapshot():
    return ControlSnapshot("v1", "r1", "c1", "model", goal="Order event system",
                           current_hypothesis="Use Kafka")


def decision(s, action="INTERRUPT"):
    return dict(action=action, context_version=s.context_version,
                round_id=s.round_id, checkpoint_id=s.checkpoint_id)


MESSAGES = (InboundMessage("m204", "A1", "Customer forbids Kafka"),)


@pytest.mark.asyncio
async def test_interrupt_is_observed_only():
    s = snapshot()
    result = await observe(s, MESSAGES, classify=AsyncMock(return_value=decision(s)),
                           current_snapshot=lambda: s)
    assert result.proposed_action == "INTERRUPT"
    assert result.effective_action == "UNCHANGED"
    assert result.message_ids == ("m204",)
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_stale_decision_is_recomputed_from_latest_snapshot():
    old = snapshot()
    new = replace(old, context_version="v2", round_id="r2", checkpoint_id="c2")
    classify = AsyncMock(side_effect=[decision(old), decision(new, "APPEND")])
    result = await observe(old, MESSAGES, classify=classify, current_snapshot=lambda: new)
    assert result.attempts == 2
    assert result.context_version == "v2"
    assert result.proposed_action == "APPEND"
    assert classify.await_args_list[1].args[0] == new


@pytest.mark.asyncio
async def test_continually_changing_context_is_discarded():
    s = snapshot()
    newer = replace(s, context_version="v2")
    newest = replace(s, context_version="v3")
    values = iter([newer, newest])
    classify = AsyncMock(side_effect=[decision(s), decision(newer)])
    result = await observe(s, MESSAGES, classify=classify,
                           current_snapshot=lambda: next(values))
    assert result.status == "stale"
    assert result.proposed_action == "APPEND"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, "INTERRUPT", {},
    dict(action="DELETE", context_version="v1", round_id="r1", checkpoint_id="c1"),
    dict(action="INTERRUPT", context_version="v0", round_id="r1", checkpoint_id="c1")])
async def test_invalid_response_falls_back(response):
    s = snapshot()
    result = await observe(s, MESSAGES, classify=AsyncMock(return_value=response),
                           current_snapshot=lambda: s)
    assert result.status == "error"
    assert result.proposed_action == "APPEND"


@pytest.mark.asyncio
async def test_timeout_and_external_cancellation():
    s = snapshot()
    cancelled = asyncio.Event()

    async def blocked(*_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    result = await observe(s, MESSAGES, classify=blocked,
                           current_snapshot=lambda: s, timeout_seconds=0.01)
    assert result.status == "timeout"
    assert cancelled.is_set()
    task = asyncio.create_task(observe(s, MESSAGES, classify=blocked,
                                      current_snapshot=lambda: s))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def native_state():
    checkpoint = NS(iteration_index=2, context_messages=["SECRET_REASONING"],
                    deep_agent_state={"task_plan": {"goal": "Order system",
                        "current_task_id": "t1", "tasks": [
                            {"id": "t1", "content": "Implement Kafka"}]}})
    active = NS(round_id=3, original_query="Original task", last_iter_snapshot=checkpoint,
                pre_round_snapshot=None, iter_phase="model", model_call_in_flight=True,
                tool_started=False, pause_requested=False)
    return NS(active_round=active, session_id="session1")


def test_native_snapshot_excludes_history_and_tracks_phase_and_checkpoint():
    harness = native_state()
    before = shadow.snapshot_from_native(harness)
    assert before.next_action == "Implement Kafka"
    assert "SECRET_REASONING" not in prompt_for(before, MESSAGES)
    assert before.current_hypothesis == ""
    assert before.tool_has_side_effects is None
    harness.active_round.iter_phase = "tool"
    assert before.context_version != shadow.snapshot_from_native(harness).context_version
    harness.active_round = None
    assert shadow.snapshot_from_native(harness) is None


class Host:
    def __init__(self):
        self.harness = native_state()
        self.blueprint = NS(member_name="A2")


@pytest.mark.asyncio
async def test_observer_coalesces_bounds_queue_and_closes_without_leak(monkeypatch):
    info = Mock()
    monkeypatch.setattr(shadow.logger, "info", info)
    host = Host()
    observer = shadow.ShadowObserver(host, "small", 2)
    observer._snapshot = snapshot
    entered = asyncio.Event()

    async def blocked(*_):
        entered.set()
        await asyncio.Event().wait()

    observer._classify = blocked
    observer.submit(MESSAGES[0])
    observer.submit(MESSAGES[0])
    await entered.wait()
    for index in range(40):
        observer.submit(InboundMessage(str(index), "A1", "new evidence"))
    assert len(observer._pending) == 32
    assert observer._inflight == {"m204"}
    assert info.call_count == 8
    assert json.loads(info.call_args.args[1])["status"] == "overloaded"
    await observer.aclose()
    assert observer._task.done()
    assert not observer._pending


def make_handler(messages, *, fail_second=False):
    from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
    from openjiuwen.agent_teams.schema.team import TeamRole

    host = Host()
    host.has_pending_interrupt = lambda: False
    delivered = []

    async def deliver(text, **kwargs):
        if fail_second and len(delivered) == 1:
            raise RuntimeError("delivery failed")
        delivered.append(text)

    host.deliver_input = deliver
    mm = NS(mark_messages_read=AsyncMock(), mark_message_read=AsyncMock())
    handler = MessageHandler(host, NS(role=TeamRole.LEADER, language="en", member_name="A2"),
                             NS(message_manager=mm, team_backend=None), NS())
    handler._read_all_unread = AsyncMock(side_effect=[messages, []])
    handler._expand = AsyncMock(side_effect=lambda msg: NS(body=msg.content, is_template=False))
    return handler, host, mm, delivered


def message(mid, broadcast=False):
    return NS(message_id=mid, from_member_name="A1", to_member_name="A2",
              broadcast=broadcast, protocol="text", content="Customer forbids Kafka", timestamp=0)


@pytest.mark.asyncio
async def test_real_sdk_drain_preserves_delivery_ack_and_broadcast_objects(monkeypatch):
    shadow.install_shadow_observer()
    messages = [message("direct"), message("broadcast", True)]
    handler, host, mm, delivered = make_handler(messages)
    seen = []
    monkeypatch.setattr(shadow, "_submit", lambda h, m, *_: seen.append(m.message_id))
    await handler._process_unread_messages("A2")
    assert seen == ["direct", "broadcast"]
    assert len(delivered) == 2
    mm.mark_messages_read.assert_awaited_once_with(messages, "A2")
    mm.mark_message_read.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_sdk_failed_delivery_leaves_undelivered_message_unread(monkeypatch):
    messages = [message("direct"), message("broadcast", True)]
    handler, host, mm, delivered = make_handler(messages, fail_second=True)
    monkeypatch.setattr(shadow, "_submit", lambda *_: None)
    with pytest.raises(RuntimeError, match="delivery failed"):
        await handler._process_unread_messages("A2")
    mm.mark_messages_read.assert_awaited_once_with([messages[0]], "A2")
    # A lost wake-up is repaired by the same SDK mailbox polling path.
    handler._read_all_unread = AsyncMock(side_effect=[[messages[1]], []])
    host.deliver_input = AsyncMock()
    await handler.on_poll_mailbox(None)
    mm.mark_messages_read.assert_awaited_with([messages[1]], "A2")


@pytest.mark.asyncio
async def test_router_setup_failure_cannot_block_delivery(monkeypatch):
    handler, host, mm, delivered = make_handler([message("m1")])

    def broken(*_):
        raise ValueError("invalid config")

    monkeypatch.setattr(shadow, "_submit", broken)
    await handler._process_unread_messages("A2")
    assert len(delivered) == 1
    mm.mark_messages_read.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_gates_and_fast_model_never_delays_real_drain(monkeypatch):
    from openjiuwen.agent_teams.harness import native_harness
    from jiuwenswarm.common import config

    monkeypatch.setattr(native_harness, "NativeHarness", type(native_state()))
    cfg = {"duplex_router": {"mode": "off", "model_name": "small"}}
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    handler, host, mm, delivered = make_handler([message("m1")])
    await handler._process_unread_messages("A2")
    assert not hasattr(handler, "_duplex_shadow_observer")
    cfg["duplex_router"]["mode"] = "shadow"
    async def wait_forever(*_):
        await asyncio.Event().wait()

    monkeypatch.setattr(shadow.ShadowObserver, "_classify", wait_forever)
    handler._read_all_unread = AsyncMock(side_effect=[[message("m2")], []])
    await asyncio.wait_for(handler._process_unread_messages("A2"), timeout=0.2)
    assert len(delivered) == 2
    await handler._duplex_shadow_observer.aclose()


def test_install_is_idempotent():
    from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
    shadow.install_shadow_observer()
    installed = MessageHandler._format_message
    assert shadow.install_shadow_observer()
    assert MessageHandler._format_message is installed


@pytest.mark.asyncio
async def test_team_disposal_cancels_shadow_worker():
    from openjiuwen.agent_teams.agent.team_agent import TeamAgent
    host = Host()
    observer = shadow.ShadowObserver(host, "small", 2)
    observer._classify = AsyncMock(side_effect=RuntimeError("test"))
    observer.submit(MESSAGES[0])
    owner = NS(coordination=NS(dispatcher=NS(message=NS(_duplex_shadow_observer=observer))),
               infra=NS(tiny_agents={}))
    await TeamAgent._dispose_tiny_agents(owner)
    assert observer._task.done()
    assert not observer._pending


@pytest.mark.asyncio
async def test_replay_counts_failed_append_as_failure_and_never_sends_label():
    from pathlib import Path
    from jiuwenswarm.common.duplex_benchmark import load_cases, replay
    cases = load_cases(Path(__file__).parents[2] / "fixtures/duplex/routing_cases.jsonl")
    calls = 0

    async def classify(s, messages):
        nonlocal calls
        calls += 1
        assert "expected_action" not in prompt_for(s, messages)
        if calls == 2:
            raise RuntimeError("provider failed")
        return decision(s, cases[calls - 1]["expected_action"])

    report = await replay(cases, classify)
    assert report["runs"] == 6
    assert report["correct"] == 5
    assert report["failures"] == 1
    assert report["accuracy_including_failures"] == 5 / 6


def test_prompt_budget_retains_ids_and_marks_truncation():
    messages = tuple(InboundMessage(str(i), "A1", "x" * 4000) for i in range(32))
    payload = json.loads(prompt_for(snapshot(), messages))
    assert len(payload["messages"]) == 32
    assert sum(len(m["content"]) for m in payload["messages"]) <= 12000
    assert all(m["truncated"] for m in payload["messages"])


@pytest.mark.parametrize("mode,human,template,protocol,native", [
    ("off", False, False, "text", True),
    ("active", False, False, "text", True),
    ("shadow", True, False, "text", True),
    ("shadow", False, True, "text", True),
    ("shadow", False, False, "json", True),
    ("shadow", False, False, "text", False),
    ("shadow", False, False, "approval", True),
])
def test_control_and_unsupported_inputs_never_enter_router(monkeypatch, mode, human, template,
                                                          protocol, native):
    from openjiuwen.agent_teams.harness import native_harness
    from jiuwenswarm.common import config
    monkeypatch.setattr(native_harness, "NativeHarness", type(native_state()) if native else str)
    monkeypatch.setattr(config, "get_config", lambda: {
        "duplex_router": {"mode": mode, "model_name": "small"}})
    host = Host()
    handler = NS(_round=host)
    msg = message("control")
    msg.protocol = protocol
    shadow._submit(handler, msg, NS(body="control", is_template=template), human)
    assert not hasattr(handler, "_duplex_shadow_observer")
