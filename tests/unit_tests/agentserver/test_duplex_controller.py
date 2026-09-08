import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.team.duplex_controller import InputController
from jiuwenswarm.agents.harness.team.duplex_ledger import ToolLedger, UncertainToolOutcome
from jiuwenswarm.agents.harness.team.duplex_shadow import RoutedInput
from jiuwenswarm.common.duplex_router import ControlSnapshot, InboundMessage


def message(key):
    return RoutedInput(key, InboundMessage(key, "sender", key))


@pytest.mark.asyncio
async def test_new_arrival_cancels_old_decision_and_applies_one_ordered_batch():
    snapshot = ControlSnapshot("v", "r", "c", "model")
    entered, cancelled = asyncio.Event(), asyncio.Event()
    applied = []

    async def classify(state, messages):
        if len(messages) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return dict(action="INTERRUPT", context_version="v", round_id="r", checkpoint_id="c")

    async def apply(batch, decision):
        applied.append(([str(item) for item in batch], decision.proposed_action))

    controller = InputController(snapshot=lambda: snapshot, classify=classify, apply=apply)
    try:
        first = controller.submit(message("m1"))
        await asyncio.wait_for(entered.wait(), 1)
        second = controller.submit(message("m2"))
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert cancelled.is_set()
        assert applied == [(["m1", "m2"], "INTERRUPT")]
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_continuous_arrivals_cannot_extend_oldest_message_deadline():
    snapshot = ControlSnapshot("v", "r", "c", "model")
    decisions = []

    async def classify(*args):
        await asyncio.Event().wait()

    async def apply(batch, decision):
        decisions.append(decision)

    controller = InputController(snapshot=lambda: snapshot, classify=classify, apply=apply, timeout=.08)
    try:
        first = controller.submit(message("m0"))
        for index in range(1, 6):
            await asyncio.sleep(.01)
            controller.submit(message(f"m{index}"))
        await asyncio.wait_for(first, .15)
        assert decisions[0].status == "timeout"
        assert decisions[0].proposed_action == "APPEND"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_committed_tool_receipt_survives_new_ledger_instance(tmp_path):
    from openjiuwen.core.foundation.llm import ToolMessage
    path = tmp_path / "ledger.sqlite3"
    call = SimpleNamespace(id="operation-1", name="send", arguments='{"body":"hello"}')
    effects = []

    async def execute():
        effects.append("sent")
        return "sent", ToolMessage(content="sent", tool_call_id=call.id)

    for ledger in (ToolLedger(path), ToolLedger(path)):
        value, _ = await ledger.execute(scope="session", call=call, idempotent=False, invoke=execute)
        assert value == "sent"
    assert effects == ["sent"]


@pytest.mark.asyncio
async def test_uncertain_side_effect_is_not_retried_with_a_new_call_id(tmp_path):
    ledger = ToolLedger(tmp_path / "ledger.sqlite3")
    effects = []

    async def execute():
        effects.append("sent")
        raise TimeoutError("response lost after send")

    call = SimpleNamespace(id="first", name="send", arguments={"body": "hello"})
    with pytest.raises(TimeoutError):
        await ledger.execute(scope="session", call=call, idempotent=False, invoke=execute)
    call.id = "retry"
    with pytest.raises(UncertainToolOutcome):
        await ledger.execute(scope="session", call=call, idempotent=False, invoke=execute)
    assert effects == ["sent"]
