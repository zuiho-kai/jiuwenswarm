from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.harness.team.duplex_progress import ProgressDraft, recovery_note, MAX_DRAFT_CHARS
from jiuwenswarm.common.duplex_router import ControlSnapshot, observe, state_for, validate_decision
from jiuwenswarm.common.duplex_choice import choice_request, decision_from_body


def test_draft_keeps_text_and_unsent_messages_but_not_reasoning_or_executable_arguments():
    draft = ProgressDraft()
    draft.begin(1)
    draft.capture(NS(content="Found configuration issue", reasoning_content="PRIVATE THOUGHT",
                     tool_calls=[NS(index=0, name="shell", arguments="PRIVATE COMMAND")]))
    draft.capture(NS(content="", tool_calls=[NS(index=1, name="send_message", arguments='{"content":"Found ')]))
    draft.capture(NS(content="", tool_calls=[NS(index=1, name=None, arguments='configuration issue"}')]))
    assert "Found configuration issue" in draft.text
    assert "Unsent send_message" in draft.text
    assert 'configuration issue"}' in draft.text
    assert "PRIVATE" not in draft.text
    assert draft.for_round(2) == ""
    draft.capture(NS(content="a" * 10000))
    assert len(draft.text) <= MAX_DRAFT_CHARS
    draft.begin(2)
    assert draft.text == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["KEEP", "DISCARD"])
async def test_observation_carries_supervisor_progress_decision(policy):
    snapshot = ControlSnapshot("v", "1", "c", "model", progress_draft="unfinished draft")
    observation = await observe(snapshot, (), classify=AsyncMock(return_value={
        "action": "INTERRUPT", "progress_policy": policy}), current_snapshot=lambda: snapshot)
    assert observation.status == "ok"
    assert observation.progress_policy == policy
    assert state_for(snapshot, ())["snapshot"]["progress_draft"] == "unfinished draft"


@pytest.mark.asyncio
async def test_legacy_decision_defaults_to_discard():
    snapshot = ControlSnapshot("v", "1", "c", "model")
    observation = await observe(snapshot, (), classify=AsyncMock(return_value={"action": "INTERRUPT"}),
                                current_snapshot=lambda: snapshot)
    assert observation.progress_policy == "DISCARD"
    with pytest.raises(ValueError):
        validate_decision({"action": "INTERRUPT", "progress_policy": "execute"})


def test_typed_providers_support_progress_option_without_new_routing_actions():
    request = choice_request("jev", {}, "instructions")
    assert set(request["questions"]["action"]["criteria"]) == {"APPEND", "INTERRUPT"}
    assert set(request["questions"]["progress_policy"]["criteria"]) == {"KEEP", "DISCARD"}
    body = {"answers": {"action": {"type": "choice", "choice": "INTERRUPT", "confidence": 0.99,
            "probabilities": {"INTERRUPT": 0.99, "APPEND": 0.01}},
        "progress_policy": {"type": "choice", "choice": "KEEP", "confidence": 0.99,
            "probabilities": {"KEEP": 0.99, "DISCARD": 0.01}}}}
    assert decision_from_body(body, 0.9, provider="test")["progress_policy"] == "KEEP"
    body["answers"]["progress_policy"]["probabilities"] = {"KEEP": 0.6, "DISCARD": 0.4}
    assert decision_from_body(body, 0.9, provider="test")["progress_policy"] == "DISCARD"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["KEEP", "DISCARD"])
async def test_native_resume_injects_only_selected_draft_and_preserves_committed_state(monkeypatch, policy):
    from jiuwenswarm.agents.harness.team.duplex_native import DuplexNativeHarness, _RouteInput
    from jiuwenswarm.agents.harness.team.duplex_thinking import ThinkingWindow
    from openjiuwen.agent_teams.harness.native_harness import NativeHarness
    import asyncio

    native = DuplexNativeHarness.__new__(DuplexNativeHarness)
    native._session = NS()
    native._st = NS(paused_query="")
    native._duplex_received = set()
    native._duplex_progress = ProgressDraft()
    native._duplex_thinking = ThinkingWindow()
    state = NS(task_plan={"old": "plan"}, completed_tools=["already executed"])
    native.load_state = Mock(return_value=state)
    native.save_state = Mock()
    send = AsyncMock()
    monkeypatch.setattr(NativeHarness, "_on_send", send)
    ack = asyncio.get_running_loop().create_future()
    native._duplex_pending = _RouteInput("new constraint", "v", "m", ack,
                                        policy, "UNSENT_DRAFT", "1")
    await native._finish_transaction()
    assert await ack == "INTERRUPT"
    restored = native._st.paused_query
    assert ("UNSENT_DRAFT" in restored) == (policy == "KEEP")
    assert "new constraint" in restored
    assert state.task_plan is None
    assert state.completed_tools == ["already executed"]
    if policy == "KEEP":
        assert "NOT been sent to teammates" in restored
        assert "complete self-contained message" in restored
    assert native._duplex_thinking.seconds == pytest.approx(0.6)
    send.assert_awaited_once()


def test_discard_note_does_not_leak_old_draft():
    assert "OLD_SECRET" not in recovery_note("DISCARD", "OLD_SECRET")


@pytest.mark.asyncio
async def test_sdk_stream_inspector_captures_draft_and_clears_completed_response():
    from jiuwenswarm.agents.harness.team.duplex_native import _ThinkingRail
    from jiuwenswarm.agents.harness.team.duplex_thinking import ThinkingWindow
    harness = NS(active_round=NS(round_id=1, pause_requested=False),
                 _duplex_progress=ProgressDraft(), _duplex_thinking=ThinkingWindow())
    ctx = NS(extra={}, inputs=NS(response=None))
    rail = _ThinkingRail(harness)
    await rail.before_model_call(ctx)
    inspector = ctx.extra["_stream_chunk_inspectors"]["duplex_progress"]
    inspector(ctx, NS(content="partial answer", reasoning_content="private", tool_calls=[]))
    assert harness._duplex_progress.text == "partial answer"
    ctx.inputs.response = NS(content="complete answer", tool_calls=[])
    await rail.after_model_call(ctx)
    assert harness._duplex_progress.text == ""


@pytest.mark.asyncio
async def test_router_passes_only_supervisor_reviewed_draft_to_resume(monkeypatch):
    from jiuwenswarm.agents.harness.team import duplex_shadow as shadow, duplex_native
    from jiuwenswarm.common.duplex_router import Observation, InboundMessage

    class Native:
        _duplex_received = set()
        _duplex_progress = ProgressDraft()
        interrupt = AsyncMock(return_value="INTERRUPT")
    native = Native()
    snapshot = ControlSnapshot("v", "1", "c", "model", progress_draft="REVIEWED")
    monkeypatch.setattr(duplex_native, "DuplexNativeHarness", Native)
    monkeypatch.setattr(shadow, "native_from_runtime", lambda _: native)
    monkeypatch.setattr(shadow, "snapshot_from_native", lambda _: snapshot)
    monkeypatch.setattr(shadow, "_classifier", lambda *args: AsyncMock())
    async def classify(*args, **kwargs):
        native._duplex_progress.text = "NEW_UNREVIEWED_TEXT"
        return Observation(("m",), "v", "1", "c", "INTERRUPT", "UNCHANGED", "ok", 1, 1, "KEEP")
    monkeypatch.setattr(shadow, "observe", classify)
    await shadow.deliver_routed(NS(harness=native),
        shadow.RoutedInput("constraint", InboundMessage("m", "member", "constraint")),
        use_steer=True, original=AsyncMock(), settings={"mode": "active", "backend": "jev", "timeout_seconds": 2})
    native.interrupt.assert_awaited_once_with("constraint", version="v", message_id="m",
        progress_policy="KEEP", progress_draft="REVIEWED", progress_round="1")
