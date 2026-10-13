from __future__ import annotations

import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from choice_classifier import ChoiceAnswer, ClassifierTimeoutError, ProviderError

from jiuwenswarm.common.duplex_classifier import ClassifierConfigError, URGENCY
from jiuwenswarm.common.duplex_router import ControlSnapshot, InboundMessage, context_for
from jiuwenswarm.agents.harness.team import duplex_shadow as shadow


def snapshot():
    return ControlSnapshot("v1", "r1", "c1", "model", goal="Order event system",
                           next_action="Use Kafka")


def urgency(p_interrupt):
    choice = "INTERRUPT" if p_interrupt >= 0.5 else "APPEND"
    probabilities = {"INTERRUPT": p_interrupt, "APPEND": 1 - p_interrupt}
    return {URGENCY: ChoiceAnswer(choice, probabilities, probabilities[choice])}


MESSAGES = (InboundMessage("m204", "A1", "Customer forbids Kafka"),)


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
    assert "SECRET_REASONING" not in context_for(before)
    payload = json.loads(context_for(before))
    assert payload == {"goal": "Order system", "current_plan": "Implement Kafka", "last_action": "",
                       "phase": "model"}
    harness.active_round.iter_phase = "tool"
    assert before.context_version != shadow.snapshot_from_native(harness).context_version
    harness.active_round = None
    assert shadow.snapshot_from_native(harness) is None


class Host:
    def __init__(self):
        self.harness = native_state()
        self.blueprint = NS(member_name="A2")


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
    await handler._process_unread_messages("A2")
    assert [item.message.message_id for item in delivered] == ["direct", "broadcast"]
    assert len(delivered) == 2
    mm.mark_messages_read.assert_awaited_once_with(messages, "A2")
    mm.mark_message_read.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_sdk_failed_delivery_leaves_undelivered_message_unread(monkeypatch):
    messages = [message("direct"), message("broadcast", True)]
    handler, host, mm, delivered = make_handler(messages, fail_second=True)
    with pytest.raises(RuntimeError, match="delivery failed"):
        await handler._process_unread_messages("A2")
    mm.mark_messages_read.assert_awaited_once_with([messages[0]], "A2")
    # A lost wake-up is repaired by the same SDK mailbox polling path.
    handler._read_all_unread = AsyncMock(side_effect=[[messages[1]], []])
    host.deliver_input = AsyncMock()
    await handler.on_poll_mailbox(None)
    mm.mark_messages_read.assert_awaited_with([messages[1]], "A2")


@pytest.mark.asyncio
@pytest.mark.parametrize("sender,broadcast,source", [
    ("user", False, "user_text"),   # user @member DM (UserInbox.direct)
    ("user", True, "user_text"),    # user broadcast (UserInbox.broadcast)
    ("A1", False, "dm"),
    ("A1", True, "broadcast"),
])
async def test_mailbox_rows_from_user_are_user_text(sender, broadcast, source):
    shadow.install_shadow_observer()
    row = message("m1", broadcast)
    row.from_member_name = sender
    handler, host, mm, delivered = make_handler([row])
    await handler._process_unread_messages("A2")
    assert delivered[0].source == source
    assert delivered[0].message.sender == sender


def _fresh_sdk(monkeypatch):
    """Replace every patch target with a stand-in; monkeypatch restores the real ones."""
    from openjiuwen.agent_teams.agent.coordination.handlers.agent_lifecycle import AgentLifecycleHandler
    from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
    from openjiuwen.agent_teams.agent.team_agent import TeamAgent
    from openjiuwen.agent_teams.group_chat.message_handler import GroupMessageHandler
    from openjiuwen.agent_teams.harness import team_harness

    def fmt(self, msg, *, expanded, is_human_agent, now_ms):
        return "x"

    def group_fmt(self, msg, *, expanded, is_human_agent, now_ms):
        return "x"

    async def deliver(self, content, *, use_steer=True):
        return None

    async def on_user_input(self, event):
        return None

    class BaseNative:
        def __init__(self, *args, **kwargs):
            pass

    targets = [(MessageHandler, "_format_message", fmt), (GroupMessageHandler, "_format_message", group_fmt),
               (TeamAgent, "deliver_input", deliver), (AgentLifecycleHandler, "on_user_input", on_user_input),
               (team_harness, "NativeHarness", BaseNative)]
    for owner, name, value in targets:
        monkeypatch.setattr(owner, name, value)
    return targets


def _patched(targets):
    return [getattr(owner, name) is not value for owner, name, value in targets]


@pytest.mark.parametrize("module,name", [
    ("openjiuwen.agent_teams.harness.native_harness:NativeHarness", "_on_round_done"),
    ("openjiuwen.agent_teams.harness.native_harness:NativeHarness", "_rollback_to_snapshot"),
    ("openjiuwen.core.single_agent.agents.react_agent:ReActAgent", "_handle_context_abort"),
    ("openjiuwen.agent_teams.harness.team_harness:TeamHarness", "inner_agent"),
    ("openjiuwen.agent_teams.harness.control", "_CmdPause"),
])
def test_missing_sdk_symbol_leaves_all_originals_untouched(monkeypatch, module, name):
    import importlib

    targets = _fresh_sdk(monkeypatch)
    path, _, attr = module.partition(":")
    owner = importlib.import_module(path)
    owner = getattr(owner, attr) if attr else owner
    monkeypatch.delattr(owner, name)
    if not attr:  # a module-level name: force duplex_native to re-import it
        monkeypatch.delitem(__import__("sys").modules, "jiuwenswarm.agents.harness.team.duplex_native")
    warn = Mock()
    monkeypatch.setattr(shadow.logger, "warning", warn)
    assert shadow.install_shadow_observer() is False
    assert _patched(targets) == [False] * len(targets)
    warn.assert_called_once()


def test_install_patches_all_targets_together(monkeypatch):
    targets = _fresh_sdk(monkeypatch)
    assert shadow.install_shadow_observer() is True
    assert _patched(targets) == [True] * len(targets)


@pytest.mark.parametrize("settings,duplex", [
    ({"enabled": True, "mode": "active"}, True),
    ({"mode": "active"}, True),
    ({"enabled": "on", "mode": "active"}, True),
    ({"enabled": False, "mode": "active"}, False),
    ({"enabled": "false", "mode": "active"}, False),
    ({"enabled": True, "mode": "off"}, False),
    ({}, False),
])
def test_native_factory_needs_enabled_and_active(monkeypatch, settings, duplex):
    from openjiuwen.agent_teams.harness import team_harness
    from jiuwenswarm.agents.harness.team import duplex_native
    from jiuwenswarm.common import config

    targets = _fresh_sdk(monkeypatch)
    base = targets[-1][2]

    class FakeDuplex:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(duplex_native, "DuplexNativeHarness", FakeDuplex)
    monkeypatch.setattr(config, "get_config", lambda: {"duplex_router": settings})
    assert shadow.install_shadow_observer()
    built = team_harness.NativeHarness("spec", "ctx")
    assert isinstance(built, FakeDuplex if duplex else base)


def test_install_is_idempotent():
    from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
    from openjiuwen.agent_teams.group_chat.message_handler import GroupMessageHandler
    shadow.install_shadow_observer()
    installed = MessageHandler._format_message
    group_installed = GroupMessageHandler.__dict__["_format_message"]
    assert shadow.install_shadow_observer()
    assert MessageHandler._format_message is installed
    assert GroupMessageHandler.__dict__["_format_message"] is group_installed
    assert group_installed._duplex_shadow


GROUP_META = json.dumps({"type": "group_chat", "client_message_id": "c1", "mentions": ["A2"]})


def group_message(mid, sender="user", meta=GROUP_META):
    return NS(message_id=mid, from_member_name=sender, to_member_name=None, broadcast=True,
              protocol="text", content="@A2 Customer forbids Kafka", timestamp=0, meta=meta)


def make_group_handler(messages, monkeypatch):
    from openjiuwen.agent_teams.agent.team_agent import TeamAgent
    from openjiuwen.agent_teams.group_chat.message_handler import GroupMessageHandler
    from openjiuwen.agent_teams.schema.team import TeamRole

    shadow.install_shadow_observer()
    routed = []

    async def deliver_routed(host, content, **kwargs):
        routed.append(content)

    monkeypatch.setattr(shadow, "deliver_routed", deliver_routed)
    built = Mock(side_effect=InboundMessage)
    monkeypatch.setattr(shadow, "InboundMessage", built)
    host = Host()
    host.has_pending_interrupt = lambda: False
    host.deliver_input = lambda content, **kw: TeamAgent.deliver_input(host, content, **kw)
    mm = NS(mark_messages_read=AsyncMock(), mark_message_read=AsyncMock())
    handler = GroupMessageHandler(host, NS(role=TeamRole.LEADER, language="en", member_name="A2"),
                                  NS(message_manager=mm, team_backend=None), NS())
    handler._read_all_unread = AsyncMock(side_effect=[messages, []])
    handler._expand = AsyncMock(side_effect=lambda msg: NS(
        body=f"<excerpt>{msg.content}</excerpt>" if msg.meta else msg.content, is_template=False))
    return handler, mm, routed, built


@pytest.mark.asyncio
@pytest.mark.parametrize("sender,source", [("user", "user_text"), ("A1", "group_mention")])
async def test_group_mention_is_routed_with_raw_content_and_sdk_delivery_text(monkeypatch, sender, source):
    messages = [group_message("g1", sender)]
    handler, mm, routed, built = make_group_handler(messages, monkeypatch)
    await handler._process_unread_messages("A2")
    assert len(routed) == 1 and isinstance(routed[0], shadow.RoutedInput)
    # Delivery text is exactly the SDK excerpt; the classifier gets only the new message.
    assert str(routed[0]) == "<excerpt>@A2 Customer forbids Kafka</excerpt>"
    assert routed[0].message == InboundMessage("g1", sender, "@A2 Customer forbids Kafka")
    assert routed[0].source == source  # a user post is user input, never judged
    assert built.call_count == 1
    mm.mark_messages_read.assert_awaited_once_with(messages, "A2")


@pytest.mark.asyncio
async def test_non_group_broadcast_through_group_handler_is_wrapped_once(monkeypatch):
    messages = [group_message("b1", "A1", meta=None)]
    handler, mm, routed, built = make_group_handler(messages, monkeypatch)
    await handler._process_unread_messages("A2")
    assert len(routed) == 1 and isinstance(routed[0], shadow.RoutedInput)
    assert routed[0].message.message_id == "b1"
    assert routed[0].message.content == "@A2 Customer forbids Kafka"
    assert routed[0].source == "broadcast"
    assert built.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision_result,expected,phase", [
    (urgency(0.95), "INTERRUPT", None),
    (urgency(0.9), "INTERRUPT", None),
    (urgency(0.89), "FOLLOWUP", None),  # INTERRUPT choice below the threshold queues.
    (urgency(0.1), "FOLLOWUP", None),
    (ClassifierTimeoutError("timed out"), "FOLLOWUP", "route"),
    (ProviderError("unavailable"), "FOLLOWUP", "route"),
    (ClassifierConfigError("missing key"), "FOLLOWUP", "config"),
    (RuntimeError("bug"), "FOLLOWUP", "route"),
])
async def test_urgency_routes_by_threshold_and_falls_back(monkeypatch, decision_result, expected, phase):
    from jiuwenswarm.agents.harness.team import duplex_native
    from jiuwenswarm.agents.harness.team.duplex_delivery import DeliveryScheduler

    class FakeNative:
        _duplex_received = set()
        active_round = object()  # busy

        def __init__(self):
            self.interrupt = AsyncMock(return_value="INTERRUPT")

    native = FakeNative()
    failure = isinstance(decision_result, Exception)
    classify = AsyncMock(side_effect=decision_result if failure else None,
                         return_value=None if failure else decision_result)
    emit = AsyncMock()
    monkeypatch.setattr(duplex_native, "DuplexNativeHarness", FakeNative)
    monkeypatch.setattr(shadow, "SCHEDULER", DeliveryScheduler())
    monkeypatch.setattr(shadow, "native_from_runtime", lambda _: native)
    monkeypatch.setattr(shadow, "snapshot_from_native", lambda _: snapshot())
    monkeypatch.setattr(shadow, "classify", classify)
    monkeypatch.setattr(shadow, "_emit_duplex_error", emit)
    original = AsyncMock(return_value="sent")
    config = {"mode": "active", "provider": "logprob_llm",
              "logprob_llm": {"preset": "openai", "api_key_env": "TEST_KEY"}}
    host = NS(harness=native, blueprint=NS(member_name="A2"))

    result = await shadow.deliver_routed(
        host, shadow.RoutedInput("Customer forbids Kafka", MESSAGES[0]),
        use_steer=True, original=original, settings=config)

    # The classifier sees who sent the message.
    judged = InboundMessage("m204", "A1", "from A1: Customer forbids Kafka")
    classify.assert_awaited_once_with(host, config, snapshot(), (judged,), (URGENCY,))
    if expected == "INTERRUPT":
        assert result == "INTERRUPT"
        assert native.interrupt.await_args.kwargs == {"version": "v1", "message_id": "m204"}
        original.assert_not_awaited()
    else:
        assert result == "sent"
        native.interrupt.assert_not_awaited()
        original.assert_awaited_once_with(host, "Customer forbids Kafka", use_steer=False)
    assert emit.await_count == int(phase is not None)
    if phase is not None:
        assert emit.await_args.kwargs["phase"] == phase
        assert emit.await_args.kwargs["backend"] == "openai"


def test_error_text_names_provider_and_reason():
    assert shadow._duplex_error_text("team_model", "route", ClassifierTimeoutError()) == (
        "A2A 监工异常：团队模型请求超时，当前消息已按追加方式继续处理。")
    assert "Jev连接或响应解析失败" in shadow._duplex_error_text("jev", "route", ProviderError())
    assert "配置无效" in shadow._duplex_error_text("clef", "config", ClassifierConfigError())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,use_steer,has_snapshot,reason", [
    ("off", True, True, "mode_not_active"),
    ("active", False, True, "use_steer_false"),
])
async def test_route_bypass_logs_reason_without_content(monkeypatch, mode, use_steer, has_snapshot, reason):
    from jiuwenswarm.agents.harness.team import duplex_native

    class FakeNative:
        def __init__(self):
            self._duplex_received = set()

    native = FakeNative()
    monkeypatch.setattr(duplex_native, "DuplexNativeHarness", FakeNative)
    monkeypatch.setattr(shadow, "native_from_runtime", lambda _: native)
    monkeypatch.setattr(shadow, "snapshot_from_native", lambda _: snapshot() if has_snapshot else None)
    log = Mock()
    monkeypatch.setattr(shadow.logger, "info", log)
    original = AsyncMock(return_value="sent")
    content = shadow.RoutedInput("private-message-never-log", MESSAGES[0])
    result = await shadow.deliver_routed(NS(harness=native, blueprint=NS(member_name="A2")), content,
                                        use_steer=use_steer, original=original,
                                        settings={"mode": mode, "provider": "typed_choice"})
    assert result == "sent"
    original.assert_awaited_once()
    logged = "\n".join(call.args[0] % call.args[1:] for call in log.call_args_list)
    assert f"reason={reason}" in logged
    assert "recipient=A2" in logged
    assert "private-message-never-log" not in logged


@pytest.mark.asyncio
@pytest.mark.parametrize("sender,notified", [("user", True), ("A1", False)])
async def test_interrupt_failure_notifies_user_input_and_reraises(monkeypatch, sender, notified):
    from jiuwenswarm.agents.harness.team import duplex_native

    class FakeNative:
        active_round = object()  # busy

        def __init__(self):
            self._duplex_received = set()
            self.interrupt = AsyncMock(side_effect=RuntimeError("supervisor stopped"))

    native = FakeNative()
    monkeypatch.setattr(duplex_native, "DuplexNativeHarness", FakeNative)
    monkeypatch.setattr(shadow, "native_from_runtime", lambda _: native)
    monkeypatch.setattr(shadow, "snapshot_from_native", lambda _: snapshot())
    monkeypatch.setattr(shadow, "classify", AsyncMock(return_value=urgency(0.97)))
    emit = AsyncMock()
    monkeypatch.setattr(shadow, "_emit_duplex_error", emit)
    original = AsyncMock(return_value="sent")
    message = InboundMessage("m1", sender, "Stop using Kafka")

    # A group @ from the user reaches the safe interrupt; its failure is user-visible.
    with pytest.raises(RuntimeError, match="supervisor stopped"):
        await shadow.deliver_routed(NS(harness=native), shadow.RoutedInput(
                                        "Stop using Kafka", message, "group_mention"),
                                    use_steer=True, original=original,
                                    settings={"mode": "active", "provider": "typed_choice", "timeout_seconds": 1})

    original.assert_not_awaited()
    assert "m1" not in native._duplex_received
    assert emit.await_count == int(notified)
    if notified:
        assert emit.await_args.kwargs["phase"] == "interrupt"
    assert "未送达" in shadow._duplex_error_text("clef", "interrupt", RuntimeError())
