"""Publication adapter against the installed SDK, SQLite and native ReAct hooks."""

import json
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.group_chat.conversation import GroupConversationLog
from openjiuwen.agent_teams.meeting import MeetingCoordinator, MeetingScope
from openjiuwen.agent_teams.paths import reset_task_openjiuwen_home, set_task_openjiuwen_home
from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase
from openjiuwen.agent_teams.tools.message_manager import TeamMessageManager
from openjiuwen.agent_teams.tools.tool_base import TeamTool
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder
from openjiuwen.harness.deep_agent import DeepAgent, DeepAgentConfig
from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.extensions.meeting.publication import create_meeting_publication_rail


@pytest.fixture
async def meeting(tmp_path):
    workspace = Path(f"\\\\?\\{tmp_path}") if sys.platform == "win32" else tmp_path
    home_token = set_task_openjiuwen_home(workspace)
    session_token = set_session_id("publication-session")
    database = TeamDatabase(DatabaseConfig(connection_string=":memory:"))
    await database.initialize()
    try:
        await database.team.create_team("publication-team", "Publication Team", "leader")
        for name, role in (("leader", "leader"), ("analyst", "teammate")):
            await database.member.create_member(name, "publication-team", name, "{}", "ready", role=role)
        manager = TeamMessageManager("publication-team", "leader", database, AsyncMock())
        conversation = GroupConversationLog("publication-team", "publication-session")
        coordinator = MeetingCoordinator(
            database, MeetingScope("publication-session", "publication-team", "meeting-one"),
            message_manager=manager, conversation=conversation,
        )
        await coordinator.start()
        yield SimpleNamespace(db=database, manager=manager, conversation=conversation, coordinator=coordinator)
        await coordinator.close()
    finally:
        await database.close()
        reset_session_id(session_token)
        reset_task_openjiuwen_home(home_token)


class _OrdinarySend(TeamTool):
    def __init__(self, name):
        super().__init__(ToolCard(id=f"test.{name}", name=name, description=name, input_params={
            "type": "object", "properties": {},
        }))
        self.sent = []

    async def invoke(self, inputs, **kwargs):
        self.sent.append(inputs)
        return ToolOutput(success=True, data={"sent": True})


async def _native(meeting, monkeypatch, member="leader", *, max_iterations=8, participant=False):
    agent = ReActAgent(card=AgentCard(id=uuid4().hex, name="meeting-native"))
    agent.configure(ReActAgentConfig(model_name="fake", max_iterations=max_iterations))
    agent.system_prompt_builder = SystemPromptBuilder(language="en")
    agent.prompt_builder = agent.system_prompt_builder
    agent.system_prompt_builder.add_section(PromptSection("existing", {"en": "Keep existing role guidance."}))
    ordinary = {name: _OrdinarySend(name) for name in ("send_message", "group_send_message", "ask_user_question")}
    for tool in ordinary.values():
        agent.ability_manager.add_ability(tool.card, tool)
    if participant:
        from openjiuwen.agent_teams.rails.team_context import inject_team_handles
        from jiuwenswarm.agents.swarm.context import SwarmBuildContext
        from jiuwenswarm.agents.swarm.providers.member_rails import build_meeting_participant

        context = SwarmBuildContext(
            session_id="publication-session", team_id="publication-team", member_name=member,
            role="leader" if member == "leader" else "teammate", language="en",
        )
        inject_team_handles(context.extras, team_backend=SimpleNamespace(
            team_name="publication-team", conversation_mode="group_chat",
        ))
        rail = build_meeting_participant({}, context)
    else:
        rail = await create_meeting_publication_rail(meeting.coordinator, member)
    await agent.register_rail(rail)
    messages = []
    context = MagicMock()
    context.get_messages.side_effect = lambda **_: list(messages)
    context.add_messages = AsyncMock(side_effect=messages.append)

    async def window(**kwargs):
        return MagicMock(get_messages=lambda: kwargs["system_messages"] + list(messages), get_tools=lambda: kwargs["tools"])

    context.get_context_window = AsyncMock(side_effect=window)
    engine = MagicMock()
    engine.create_context = AsyncMock(return_value=context)
    engine.save_contexts = AsyncMock()
    agent.context_engine = engine
    session = create_agent_session(session_id="publication-session", card=agent.card)
    await session.pre_run()
    monkeypatch.setattr(session, "write_stream", AsyncMock())
    model = MagicMock()
    model.invoke = AsyncMock()
    monkeypatch.setattr(agent, "_get_llm", lambda: model)
    return SimpleNamespace(agent=agent, rail=rail, model=model, session=session, messages=messages, ordinary=ordinary)


def _post_call(**overrides):
    args = {"content": "Complete explanation", "speech_text": "Short spoken result", "mentions": ["analyst"]}
    args.update(overrides)
    return AssistantMessage(content="", tool_calls=[ToolCall(
        type="function", id=uuid4().hex, name="meeting_post_message", arguments=json.dumps(args),
    )])


async def _cleanup(native):
    await native.agent.unregister_rail(native.rail)
    await native.agent.agent_callback_manager.clear()
    native.agent.ability_manager.teardown_tools()


@pytest.mark.asyncio
async def test_real_native_corrects_plain_final_and_only_grant_publishes(meeting, monkeypatch):
    native = await _native(meeting, monkeypatch)
    native.model.invoke.side_effect = [
        AssistantMessage(content="Ungated draft must not become public"),
        _post_call(), AssistantMessage(content="Complete explanation"),
    ]
    try:
        result = await native.agent.invoke({"query": "Review the proposal"}, session=native.session, _streaming=True)
        assert result == {"output": "", "result_type": "answer", "meeting_queued": True}
        native.model.stream.assert_not_called()
        assert native.model.invoke.await_count == 3
        assert all(call.args[0].type not in {"llm_output", "llm_reasoning"} for call in native.session.write_stream.await_args_list)
        for call in native.model.invoke.await_args_list:
            assert not {tool.name for tool in call.kwargs["tools"]} & native.ordinary.keys()
            assert any("meeting_post_message" in str(message.content) for message in call.kwargs["messages"])
        assert len(await meeting.coordinator.list_pending()) == 1
        assert await meeting.db.message.get_team_messages("publication-team") == []
        assert await meeting.db.message.get_broadcast_messages("publication-team", "analyst") == []
        assert not meeting.conversation.history_path.exists()
        meeting.manager.messager.publish.assert_not_awaited()
        grant = await meeting.coordinator.grant_next()
        assert grant.content == "Complete explanation" and grant.speech_text == "Short spoken result"
        assert grant.delivery_error is None
        assert len(await meeting.db.message.get_team_messages("publication-team")) == 1
        history = meeting.conversation.history_path.read_text(encoding="utf-8")
        assert "Complete explanation" in history and "Ungated draft" not in history
        assert await meeting.coordinator.grant_next() is None
        assert await meeting.coordinator.complete_playback(grant.lease_token)
    finally:
        await _cleanup(native)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["send_message", "group_send_message", "ask_user_question"])
async def test_cached_unrestricted_tool_call_is_rejected_before_execution(meeting, monkeypatch, name):
    native = await _native(meeting, monkeypatch)
    native.model.invoke.side_effect = [
        AssistantMessage(content="", tool_calls=[ToolCall(type="function", id="bypass", name=name, arguments="{}")]),
        _post_call(), AssistantMessage(content=""),
    ]
    try:
        result = await native.agent.invoke({"query": "Discuss"}, session=native.session, _streaming=True)
        assert result["meeting_queued"]
        assert native.ordinary[name].sent == []
        assert len(await meeting.coordinator.list_pending()) == 1
        assert await meeting.db.message.get_team_messages("publication-team") == []
        assert any("meeting_post_message" in str(message.content) for message in native.messages)
    finally:
        await _cleanup(native)


@pytest.mark.asyncio
async def test_missing_summary_can_be_corrected_without_admitting_bad_message(meeting, monkeypatch):
    native = await _native(meeting, monkeypatch)
    native.model.invoke.side_effect = [
        _post_call(speech_text="  "), _post_call(), AssistantMessage(content=""),
    ]
    try:
        result = await native.agent.invoke({"query": "Discuss"}, session=native.session)
        assert result["meeting_queued"]
        assert len(await meeting.coordinator.list_pending()) == 1
        assert any("nonblank" in str(message.content) for message in native.messages)
    finally:
        await _cleanup(native)


@pytest.mark.asyncio
async def test_repeated_plain_answers_fail_with_bounded_correction(meeting, monkeypatch):
    native = await _native(meeting, monkeypatch)
    native.model.invoke.side_effect = [AssistantMessage(content="Not submitted") for _ in range(3)]
    try:
        result = await native.agent.invoke({"query": "Discuss"}, session=native.session, _streaming=True)
        assert result["result_type"] == "error" and result["meeting_error"] == "missing_meeting_post"
        assert native.model.invoke.await_count == 3
        assert await meeting.coordinator.list_pending() == ()
        assert not meeting.conversation.history_path.exists()
    finally:
        await _cleanup(native)


@pytest.mark.asyncio
async def test_removal_restores_normal_streaming_and_rejects_retained_tool(meeting, monkeypatch):
    native = await _native(meeting, monkeypatch)
    retained = native.rail._tools["meeting_post_message"]
    try:
        await native.agent.unregister_rail(native.rail)
        assert "Keep existing role guidance" in native.agent.system_prompt_builder.build()
        assert "meeting_post_message" not in native.agent.system_prompt_builder.build()
        names = {tool.name for tool in await native.agent.ability_manager.list_tool_info()}
        assert names == native.ordinary.keys()
        late = await retained.invoke({"content": "Late", "speech_text": "Late"})
        assert not late.success and "ended" in late.error

        async def stream(**kwargs):
            yield AssistantMessageChunk(content="Ordinary reply")

        native.model.stream.side_effect = stream
        result = await native.agent.invoke({"query": "Normal chat"}, session=native.session, _streaming=True)
        assert result["output"] == "Ordinary reply"
        assert any(call.args[0].type == "llm_output" for call in native.session.write_stream.await_args_list)
    finally:
        await native.agent.agent_callback_manager.clear()
        native.agent.ability_manager.teardown_tools()


@pytest.mark.asyncio
async def test_member_added_after_start_gets_bound_identity_and_no_interrupt_tool(meeting, monkeypatch):
    await meeting.db.member.create_member("new-member", "publication-team", "New", "{}", "ready", role="teammate")
    native = await _native(meeting, monkeypatch, "new-member")
    native.model.invoke.side_effect = [_post_call(mentions=["leader"]), AssistantMessage(content="")]
    try:
        await native.agent.invoke({"query": "Report"}, session=native.session)
        queued, = await meeting.coordinator.list_pending()
        assert queued.member_name == "new-member" and queued.mentions == ("leader",)
        names = {tool.name for tool in await native.agent.ability_manager.list_tool_info()}
        assert "meeting_interrupt_speech" not in names
        assert "Host the discussion" not in native.agent.system_prompt_builder.build()
    finally:
        await _cleanup(native)


@pytest.mark.asyncio
async def test_ended_coordinator_rejects_tools_and_factory(meeting):
    rail = await create_meeting_publication_rail(meeting.coordinator, "leader")
    assert "meeting_interrupt_speech" not in rail._tools
    await meeting.coordinator.close()
    result = await rail._tools["meeting_post_message"].invoke({"content": "Late", "speech_text": "Late"})
    assert not result.success
    with pytest.raises(ValueError, match="active meeting"):
        await create_meeting_publication_rail(meeting.coordinator, "leader")


@pytest.mark.asyncio
async def test_factory_rejects_unregistered_identity(meeting):
    with pytest.raises(ValueError, match="registered agent"):
        await create_meeting_publication_rail(meeting.coordinator, "invented-member")


@pytest.mark.asyncio
async def test_new_conclusion_after_an_earlier_post_is_corrected_not_discarded(meeting, monkeypatch):
    native = await _native(meeting, monkeypatch)
    native.model.invoke.side_effect = [
        _post_call(), AssistantMessage(content="A new conclusion"),
        _post_call(content="A new conclusion", speech_text="New spoken conclusion"), AssistantMessage(content=""),
    ]
    try:
        result = await native.agent.invoke({"query": "Discuss"}, session=native.session, _streaming=True)
        assert result["meeting_queued"]
        assert {request.content for request in await meeting.coordinator.list_pending()} == {
            "Complete explanation", "A new conclusion",
        }
        assert not meeting.conversation.history_path.exists()
    finally:
        await _cleanup(native)


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["closed", "storage_failure"])
async def test_unavailable_meeting_stops_before_model_or_tools_execute(meeting, monkeypatch, unavailable):
    native = await _native(meeting, monkeypatch)
    if unavailable == "closed":
        await meeting.coordinator.close()
    else:
        monkeypatch.setattr(meeting.coordinator, "get_state", AsyncMock(side_effect=OSError("unavailable")))
    try:
        result = await native.agent.invoke({"query": "Discuss"}, session=native.session, _streaming=True)
        assert result["result_type"] == "error" and result["meeting_error"] == "meeting_unavailable"
        native.model.invoke.assert_not_awaited()
        native.model.stream.assert_not_called()
        assert all(not tool.sent for tool in native.ordinary.values())
    finally:
        await _cleanup(native)


@pytest.mark.asyncio
async def test_iteration_limit_final_answer_cannot_escape_without_a_post(meeting, monkeypatch):
    native = await _native(meeting, monkeypatch, max_iterations=1)
    native.agent.config.on_max_iterations = "final_answer"
    native.model.invoke.side_effect = [AssistantMessage(content="Unqueued draft"), AssistantMessage(content="Final bypass")]
    try:
        result = await native.agent.invoke({"query": "Discuss"}, session=native.session, _streaming=True)
        assert result["meeting_error"] == "missing_meeting_post"
        assert await meeting.coordinator.list_pending() == ()
        assert not meeting.conversation.history_path.exists()
        assert not any(call.args[0].type == "llm_output" for call in native.session.write_stream.await_args_list)
    finally:
        await _cleanup(native)


@pytest.mark.asyncio
async def test_identity_arguments_cannot_impersonate_another_member(meeting):
    rail = await create_meeting_publication_rail(meeting.coordinator, "analyst")
    result = await rail._tools["meeting_post_message"].invoke({
        "content": "Impersonation", "speech_text": "Impersonation", "member_name": "leader",
    })
    assert not result.success
    assert await meeting.coordinator.list_pending() == ()


@pytest.mark.asyncio
async def test_deep_agent_registration_bridges_the_publication_hooks(meeting, monkeypatch):
    native = await _native(meeting, monkeypatch)
    await native.agent.unregister_rail(native.rail)
    outer = DeepAgent(AgentCard(id=uuid4().hex, name="meeting-deep"))
    outer._react_agent = native.agent
    outer._deep_config = DeepAgentConfig(enable_task_loop=False, workspace=None)
    outer.system_prompt_builder = native.agent.system_prompt_builder
    outer.ability_manager = native.agent.ability_manager
    rail = await create_meeting_publication_rail(meeting.coordinator, "leader")
    await outer.register_rail(rail)
    native.model.invoke.side_effect = [_post_call(), AssistantMessage(content="")]
    try:
        result = await native.agent.invoke({"query": "Discuss"}, session=native.session, _streaming=True)
        assert result["meeting_queued"]
        assert len(await meeting.coordinator.list_pending()) == 1
        assert "Host the discussion" in outer.system_prompt_builder.build()
    finally:
        await outer.unregister_rail(rail)
        await outer.agent_callback_manager.clear()
        await native.agent.agent_callback_manager.clear()
        native.agent.ability_manager.teardown_tools()


@pytest.mark.asyncio
async def test_second_scope_cannot_overwrite_a_members_tools(meeting, monkeypatch):
    native = await _native(meeting, monkeypatch)
    second = await create_meeting_publication_rail(meeting.coordinator, "leader")
    try:
        with pytest.raises(ValueError, match="already has"):
            await native.agent.register_rail(second)
        native.model.invoke.side_effect = [_post_call(), AssistantMessage(content="")]
        result = await native.agent.invoke({"query": "Discuss"}, session=native.session)
        assert result["meeting_queued"]
    finally:
        await _cleanup(native)


@pytest.fixture
async def publication_host(meeting, monkeypatch):
    from jiuwenswarm.extensions.meeting import meeting_manager as module

    flags = SimpleNamespace(enabled=True, active=True)
    backend = SimpleNamespace(
        team_name="publication-team", group_session_id="publication-session", conversation_mode="group_chat",
        db=meeting.db, message_manager=meeting.manager,
        group_conversation=AsyncMock(return_value=meeting.conversation),
    )
    agent = SimpleNamespace(team_backend=backend, spec=SimpleNamespace(spawn_mode="inprocess"))
    teams = SimpleNamespace(
        get_active_team_name=lambda sid: "publication-team" if flags.active and sid == "publication-session" else None,
        is_runtime_active=lambda sid: flags.active and sid == "publication-session",
        get_team_agent=lambda sid: agent if flags.active and sid == "publication-session" else None,
    )
    manager = module.MeetingManager(teams, enabled=lambda: flags.enabled)
    state = await manager.start("publication-session")
    monkeypatch.setattr(module, "_server_manager", manager)
    host = SimpleNamespace(manager=manager, state=state, flags=flags, agent=agent)
    yield host
    await manager.aclose()


async def _enable_host(host, on_queued):
    return await host.manager.enable_publication(
        "publication-session", host.state.scope.meeting_id, on_queued=on_queued,
    )


@pytest.mark.asyncio
async def test_production_provider_mounts_all_three_tools_for_leader_and_later_member(
    meeting, publication_host, monkeypatch,
):
    wakeups = []
    scope = await _enable_host(publication_host, lambda: wakeups.append("queued"))
    leader = await _native(meeting, monkeypatch, participant=True)
    leader.model.invoke.side_effect = [_post_call(), AssistantMessage(content="")]
    later = None
    try:
        result = await leader.agent.invoke({"query": "Discuss"}, session=leader.session, _streaming=True)
        assert result["meeting_queued"]
        schemas = {tool.name: tool for tool in leader.model.invoke.await_args_list[0].kwargs["tools"]}
        assert {"meeting_post_message", "meeting_read_history", "meeting_get_speaker"} <= schemas.keys()
        assert schemas["meeting_read_history"].parameters["properties"]["limit"]["maximum"] == 100
        assert "send_message" not in schemas and "group_send_message" not in schemas
        assert "same language" in schemas["meeting_post_message"].description
        first_messages = leader.model.invoke.await_args_list[0].kwargs["messages"]
        assert any("A coordinated meeting is active" in str(message.content) for message in first_messages)
        assert any("Host the discussion" in str(message.content) for message in first_messages)
        assert "Host the discussion" in leader.agent.system_prompt_builder.build()
        await meeting.db.member.create_member("later", "publication-team", "Later", "{}", "ready", role="teammate")
        later = await _native(meeting, monkeypatch, "later", participant=True)
        later.model.invoke.side_effect = [_post_call(content="Later report", mentions=["leader"]), AssistantMessage(content="")]
        await later.agent.invoke({"query": "Report"}, session=later.session, _streaming=True)
        assert "Host the discussion" not in later.agent.system_prompt_builder.build()
        assert {item.member_name for item in await scope.coordinator.list_pending()} == {"leader", "later"}
        assert wakeups == ["queued", "queued"]
        assert not meeting.conversation.history_path.exists()
        assert await meeting.db.message.get_team_messages("publication-team") == []
    finally:
        if later is not None:
            await _cleanup(later)
        await _cleanup(leader)


@pytest.mark.asyncio
async def test_history_tool_is_exposed_and_reads_without_acknowledging_mailbox(meeting, publication_host, monkeypatch):
    scope = await _enable_host(publication_host, lambda: None)
    await scope.coordinator.for_member("analyst").post_message(
        "Previously published context", speech_text="Prior context", mentions=["leader"],
    )
    grant = await scope.coordinator.grant_next()
    await scope.coordinator.complete_playback(grant.lease_token)
    before = await meeting.db.message.get_broadcast_messages("publication-team", "leader", unread_only=True)
    leader = await _native(meeting, monkeypatch, participant=True)
    leader.model.invoke.side_effect = [
        AssistantMessage(content="", tool_calls=[ToolCall(
            type="function", id="read-context", name="meeting_read_history", arguments='{"limit":5}',
        )]), _post_call(), AssistantMessage(content=""),
    ]
    try:
        await leader.agent.invoke({"query": "Recall"}, session=leader.session)
        assert any("Previously published context" in str(message.content) for message in leader.messages)
        after = await meeting.db.message.get_broadcast_messages("publication-team", "leader", unread_only=True)
        assert [message.message_id for message in after] == [message.message_id for message in before]
    finally:
        await _cleanup(leader)


@pytest.mark.asyncio
async def test_normal_meeting_start_without_consumer_keeps_ordinary_behavior(meeting, publication_host, monkeypatch):
    native = await _native(meeting, monkeypatch, participant=True)

    async def stream(**kwargs):
        assert not any(tool.name.startswith("meeting_") for tool in kwargs["tools"])
        yield AssistantMessageChunk(content="Ordinary streaming reply")

    native.model.stream.side_effect = stream
    try:
        result = await native.agent.invoke({"query": "Discuss"}, session=native.session, _streaming=True)
        assert result["output"] == "Ordinary streaming reply"
        assert native.agent.system_prompt_builder.get_section("meeting_publication") is None
        assert publication_host.manager.publication_for_session("publication-session", "publication-team") is None
        with pytest.raises(ValueError, match="consumer"):
            await _enable_host(publication_host, None)
    finally:
        await _cleanup(native)


@pytest.mark.asyncio
@pytest.mark.parametrize("closure", ["end", "feature_off", "runtime_replaced"])
async def test_closure_removes_tools_and_guidance_from_existing_native(meeting, publication_host, monkeypatch, closure):
    scope = await _enable_host(publication_host, lambda: None)
    native = await _native(meeting, monkeypatch, participant=True)
    native.model.invoke.side_effect = [_post_call(), AssistantMessage(content="")]
    try:
        await native.agent.invoke({"query": "Discuss"}, session=native.session)
        retained = native.rail._policy._tools["meeting_post_message"]
        if closure == "end":
            assert await publication_host.manager.end("publication-session", publication_host.state.scope.meeting_id)
        else:
            if closure == "feature_off":
                publication_host.flags.enabled = False
            else:
                publication_host.flags.active = False
            assert publication_host.manager.publication_for_session("publication-session", "publication-team") is None
        assert scope.closed
        assert native.agent.system_prompt_builder.get_section("meeting_publication") is None
        assert not any(tool.name.startswith("meeting_") for tool in await native.agent.ability_manager.list_tool_info())
        assert not (await retained.invoke({"content": "Late", "speech_text": "Late"})).success

        async def stream(**kwargs):
            yield AssistantMessageChunk(content="Normal after meeting")

        native.model.stream.side_effect = stream
        result = await native.agent.invoke({"query": "Continue"}, session=native.session, _streaming=True)
        assert result["output"] == "Normal after meeting"
    finally:
        await _cleanup(native)


@pytest.mark.asyncio
async def test_end_during_model_call_cannot_publish_or_fall_back(meeting, publication_host, monkeypatch):
    await _enable_host(publication_host, lambda: None)
    native = await _native(meeting, monkeypatch, participant=True)

    async def end_while_thinking(**kwargs):
        await publication_host.manager.end("publication-session", publication_host.state.scope.meeting_id)
        return AssistantMessage(content="Late final must not escape")

    native.model.invoke.side_effect = end_while_thinking
    try:
        result = await native.agent.invoke({"query": "Discuss"}, session=native.session, _streaming=True)
        assert result["result_type"] == "error"
        assert "Late final" not in result["output"]
        assert not meeting.conversation.history_path.exists()
        native.model.stream.assert_not_called()
    finally:
        await _cleanup(native)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    "send_file_to_user", "spawn_external_cli", "spawn_bridge_agent", "spawn_human_agent",
    "swarmflow", "org_send_leader_message", "org_delegate_task",
    "org_create_task", "session_new", "session_send_message",
])
async def test_unsupported_publication_and_runtime_side_effects_are_blocked(meeting, publication_host, monkeypatch, name):
    await _enable_host(publication_host, lambda: None)
    native = await _native(meeting, monkeypatch, participant=True)
    side_effect = _OrdinarySend(name)
    native.agent.ability_manager.add_ability(side_effect.card, side_effect)
    native.model.invoke.side_effect = [
        AssistantMessage(content="", tool_calls=[ToolCall(type="function", id="unsupported", name=name, arguments="{}")]),
        _post_call(), AssistantMessage(content=""),
    ]
    try:
        result = await native.agent.invoke({"query": "Discuss"}, session=native.session, _streaming=True)
        assert result["meeting_queued"]
        assert side_effect.sent == []
        assert any("not supported" in str(message.content) for message in native.messages)
    finally:
        await _cleanup(native)


@pytest.mark.asyncio
async def test_consumer_and_scope_identity_are_validated(publication_host):
    wake = asyncio.Event().set
    scope = await _enable_host(publication_host, wake)
    assert await _enable_host(publication_host, wake) is scope
    with pytest.raises(ValueError, match="already has"):
        await _enable_host(publication_host, lambda: None)
    with pytest.raises(ValueError, match="matching active"):
        await publication_host.manager.enable_publication("publication-session", "stale", on_queued=wake)
    assert publication_host.manager.publication_for_session("other-session", "publication-team") is None
    assert publication_host.manager.publication_for_session("publication-session", "other-team") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["external_cli", "bridge_agent", "human_agent"])
async def test_host_rejects_an_existing_unsupported_roster(meeting, publication_host, role):
    await meeting.db.member.create_member("unsupported", "publication-team", "Unsupported", "{}", "ready", role=role)
    with pytest.raises(ValueError, match="native in-process"):
        await _enable_host(publication_host, lambda: None)


@pytest.mark.parametrize("mode", ["team", "code.team"])
@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_capability_assembly_includes_the_same_member_rail_for_both_profiles(mode, role):
    from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs

    rails, _ = build_member_capability_specs({}, mode, role)
    assert [rail.type for rail in rails].count("swarm.meeting_participant") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported_mode", ["process", "team_plan"])
async def test_host_rejects_execution_modes_without_the_native_meeting_contract(publication_host, unsupported_mode):
    if unsupported_mode == "process":
        publication_host.agent.spec.spawn_mode = "process"
    else:
        publication_host.agent.spec.enable_team_plan = True
    with pytest.raises(ValueError, match="in-process|plan mode"):
        await _enable_host(publication_host, lambda: None)


@pytest.mark.asyncio
async def test_installation_does_not_restore_tools_hidden_by_another_rail(meeting, publication_host, monkeypatch):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent

    await _enable_host(publication_host, lambda: None)
    native = await _native(meeting, monkeypatch, participant=True)
    hidden = _OrdinarySend("unrelated_hidden_tool")
    native.agent.ability_manager.add_ability(hidden.card, hidden)

    async def filter_other_capability(ctx):
        ctx.inputs.tools = [tool for tool in ctx.inputs.tools if tool.name != hidden.card.name]

    await native.agent.register_callback(AgentCallbackEvent.BEFORE_MODEL_CALL, filter_other_capability, priority=1)
    native.model.invoke.side_effect = [_post_call(), AssistantMessage(content="")]
    try:
        await native.agent.invoke({"query": "Discuss"}, session=native.session)
        for call in native.model.invoke.await_args_list:
            names = {tool.name for tool in call.kwargs["tools"]}
            assert hidden.card.name not in names
            assert "meeting_post_message" in names
    finally:
        await _cleanup(native)


@pytest.mark.asyncio
async def test_task_completion_control_event_does_not_need_speaking_ownership(meeting, publication_host, monkeypatch):
    from openjiuwen.agent_teams.tools.task_manager import TeamTaskManager
    from openjiuwen.agent_teams.tools.tool_task import MemberCompleteTaskTool
    from openjiuwen.agent_teams.tools.locales import make_translator

    scope = await _enable_host(publication_host, lambda: None)
    tasks = TeamTaskManager("publication-team", "analyst", meeting.db, meeting.manager.messager)
    created = await tasks.add("Review", "Review the proposal")
    assert created.ok
    assert (await tasks.claim(created.task.task_id)).ok
    native = await _native(meeting, monkeypatch, "analyst", participant=True)
    native.agent.ability_manager.add_ability(
        (tool := MemberCompleteTaskTool(tasks, make_translator("en"))).card, tool,
    )
    native.model.invoke.side_effect = [
        AssistantMessage(content="", tool_calls=[ToolCall(
            type="function", id="complete-work", name="member_complete_task",
            arguments=json.dumps({"task_id": created.task.task_id, "note": "Control metadata"}),
        )]), _post_call(mentions=["leader"]), AssistantMessage(content=""),
    ]
    try:
        result = await native.agent.invoke({"query": "Finish and report"}, session=native.session)
        assert result["meeting_queued"]
        assert (await tasks.get(created.task.task_id)).status == "completed"
        assert len(await scope.coordinator.list_pending()) == 1
        assert not meeting.conversation.history_path.exists()
    finally:
        await _cleanup(native)
