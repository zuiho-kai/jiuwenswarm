"""Publication adapter against the installed SDK, SQLite and native ReAct hooks."""

import json
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
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder
from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.extensions.meeting.publication import MeetingSetAddressedMembersTool, create_meeting_publication_rail


@pytest.mark.asyncio
async def test_addressed_member_changes_notify_browser_after_success():
    class Coordinator:
        def __init__(self):
            self.addressed = []

        def for_member(self, name):
            if name not in {"leader", "analyst"}:
                raise ValueError("Unknown member")
            return SimpleNamespace(member_name=name, get_state=self.get_state)

        async def get_state(self):
            return SimpleNamespace(closed=False, leader_member_name="leader")

        async def set_addressed_members(self, names):
            self.addressed = names
            return True

    coordinator = Coordinator()
    updates = []

    async def notify(names):
        updates.append(names)

    tool = MeetingSetAddressedMembersTool(coordinator, "leader", notify)
    selected = await tool.invoke({"member_names": ["analyst"]})
    assert selected.success and updates == [["analyst"]]
    rejected = await tool.invoke({"member_names": ["missing"]})
    assert not rejected.success and updates == [["analyst"]]
    cleared = await tool.invoke({"member_names": []})
    assert cleared.success and updates == [["analyst"], []]


@pytest.fixture
async def meeting(tmp_path, request):
    workspace = Path(f"\\\\?\\{tmp_path}") if sys.platform == "win32" else tmp_path
    home_token = set_task_openjiuwen_home(workspace)
    session_token = set_session_id("publication-session")
    # Cancellation can invalidate the in-memory SQLite connection. Driver
    # teardown tests use the same durable file-backed backend as the app.
    connection = str(tmp_path / "meeting.db") if getattr(request, "param", None) == "file" else ":memory:"
    database = TeamDatabase(DatabaseConfig(connection_string=connection))
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


async def _native(meeting, monkeypatch, member="leader", *, max_iterations=8, participant=False, progressive=False):
    agent = ReActAgent(card=AgentCard(id=uuid4().hex, name="meeting-native"))
    agent.configure(ReActAgentConfig(model_name="fake", max_iterations=max_iterations))
    if progressive:
        # Mirror native DeepAgent registration and the production disclosure rail.
        agent.ability_manager.set_tool_exposure_policy(True, direct_tool_names={"read_file"})
    agent.system_prompt_builder = SystemPromptBuilder(language="en")
    agent.prompt_builder = agent.system_prompt_builder
    agent.system_prompt_builder.add_section(PromptSection("existing", {"en": "Keep existing role guidance."}))
    if member == "leader":
        agent.system_prompt_builder.add_section(PromptSection(
            "team_bootstrap", {"en": "Call build_team before coordinating any work."}, priority=11,
        ))
    from openjiuwen.agent_teams.tools.locales import make_translator
    from openjiuwen.agent_teams.tools.tool_message import SendMessageTool

    ordinary = {name: _OrdinarySend(name) for name in ("group_send_message", "ask_user_question", "build_team")}
    ordinary["send_message"] = SendMessageTool(
        TeamMessageManager("publication-team", member, meeting.db, meeting.manager.messager), make_translator("en"),
    )
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
            list_member_roster=AsyncMock(return_value=[]),
        ))
        rail = build_meeting_participant({}, context)
    else:
        rail = await create_meeting_publication_rail(meeting.coordinator, member)
    await agent.register_rail(rail)
    if progressive:
        from jiuwenswarm.agents.harness.common.rails.tool_retrieval_prompt_rail import ToolRetrievalPromptRail

        await agent.register_rail(ToolRetrievalPromptRail(config_base={"progressive_tool_enabled": True}))
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


@pytest.fixture
async def native_factory(meeting, monkeypatch):
    natives = []

    async def create(member="leader", **kwargs):
        native = await _native(meeting, monkeypatch, member, **kwargs)
        natives.append(native)
        return native

    yield create
    for native in reversed(natives):
        await _cleanup(native)


@pytest.mark.asyncio
async def test_real_native_blocks_rebuild_and_only_grant_publishes(meeting, native_factory):
    native = await native_factory()
    native.model.invoke.side_effect = [
        AssistantMessage(content="", tool_calls=[ToolCall(
            type="function", id=uuid4().hex, name="build_team", arguments="{}",
        )]),
        _post_call(), AssistantMessage(content="I've already posted my conclusion. There is nothing further to publish."),
    ]
    result = await native.agent.invoke({"query": "Review the proposal"}, session=native.session, _streaming=True)
    assert result == {"output": "", "result_type": "answer", "meeting_queued": True}
    native.model.stream.assert_not_called()
    assert native.model.invoke.await_count == 3
    assert native.ordinary["build_team"].sent == []
    assert all(call.args[0].type not in {"llm_output", "llm_reasoning"} for call in native.session.write_stream.await_args_list)
    for call in native.model.invoke.await_args_list:
        assert not {tool.name for tool in call.kwargs["tools"]} & (native.ordinary.keys() - {"send_message"})
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
    assert "Complete explanation" in history and "I've already posted" not in history
    assert await meeting.coordinator.grant_next() is None
    assert await meeting.coordinator.complete_playback(grant.lease_token)


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
async def test_production_provider_mounts_meeting_tools_and_leader_only_routing(
    meeting, publication_host, native_factory,
):
    wakeups = []
    scope = await _enable_host(publication_host, lambda: wakeups.append("queued"))
    leader = await native_factory(participant=True, progressive=True)
    question = "Should the report cover revenue or profit?"
    leader.model.invoke.side_effect = [
        _post_call(content=question, speech_text=question, mentions=["user"]), AssistantMessage(content=""),
    ]
    result = await leader.agent.invoke({"query": "Discuss"}, session=leader.session, _streaming=True)
    assert result["meeting_queued"]
    schemas = {tool.name: tool for tool in leader.model.invoke.await_args_list[0].kwargs["tools"]}
    assert {"meeting_post_message", "meeting_read_history", "meeting_get_speaker", "list_members"} <= schemas.keys()
    assert "send_message" in schemas
    assert "meeting_forward_message" not in schemas
    assert "meeting_set_addressed_members" in schemas
    assert schemas["meeting_set_addressed_members"].parameters["properties"]["member_names"].get("minItems", 0) == 0
    assert "build_team" not in schemas
    roster = await leader.rail._policy._tools["list_members"].invoke({})
    assert roster.success and roster.data == {"members": [], "count": 0}
    leader_question = (await scope.coordinator.list_pending())[0]
    assert leader_question.content == question and leader_question.mentions == ("user",)
    assert schemas["meeting_read_history"].parameters["properties"]["limit"] == {
        "type": "integer", "minimum": 1, "maximum": 20, "default": 10,
    }
    assert "group_send_message" not in schemas
    assert "same language" in schemas["meeting_post_message"].description
    first_messages = leader.model.invoke.await_args_list[0].kwargs["messages"]
    assert any("A coordinated meeting is active" in str(message.content) for message in first_messages)
    assert any("Host the discussion" in str(message.content) for message in first_messages)
    assert "Host the discussion" in leader.agent.system_prompt_builder.build()
    assert "Call build_team before coordinating" not in leader.agent.system_prompt_builder.build()
    await meeting.db.member.create_member("later", "publication-team", "Later", "{}", "ready", role="teammate")
    later = await native_factory("later", participant=True, progressive=True)
    later.model.invoke.side_effect = [_post_call(content="Later report", mentions=["leader"]), AssistantMessage(content="")]
    await later.agent.invoke({"query": "Report"}, session=later.session, _streaming=True)
    assert "Host the discussion" not in later.agent.system_prompt_builder.build()
    assert "meeting_forward_message" not in {tool.name for tool in await later.agent.ability_manager.list_tool_info()}
    assert "meeting_set_addressed_members" not in {tool.name for tool in await later.agent.ability_manager.list_tool_info()}
    assert {item.member_name for item in await scope.coordinator.list_pending()} == {"leader", "later"}
    for native in (leader, later):
        required = {"meeting_post_message", "meeting_read_history", "meeting_get_speaker", "list_members"}
        if native is leader:
            required.update({"send_message", "meeting_set_addressed_members"})
        for call in native.model.invoke.await_args_list:
            exposed = {tool.name for tool in call.kwargs["tools"]}
            assert required <= exposed
            assert "build_team" not in exposed and "meeting_interrupt_speech" not in exposed
            if native is later:
                assert "send_message" not in exposed and "meeting_set_addressed_members" not in exposed
    later_request = next(item for item in await scope.coordinator.list_pending() if item.member_name == "later")
    assert later_request.mentions == ("leader",)
    assert wakeups == ["queued", "queued"]
    assert not meeting.conversation.history_path.exists()
    assert await meeting.db.message.get_team_messages("publication-team") == []


@pytest.mark.asyncio
async def test_leader_relays_named_request_with_existing_send_message_without_speech(
    meeting, publication_host, native_factory,
):
    wakeups = []
    scope = await _enable_host(publication_host, lambda: wakeups.append("speech"))
    coordinator = scope.coordinator
    await meeting.db.member.create_member("other", "publication-team", "Other", "{}", "ready", role="teammate")
    await coordinator.for_member("other").post_message(
        "Unrelated report", speech_text="Other summary", mentions=["user"],
    )
    current = await coordinator.grant_next()
    original = await coordinator.post_human_message("Analyst, can you respond to me?", mentions=["leader"])
    leader = await native_factory(participant=True)
    leader.model.invoke.side_effect = [AssistantMessage(content="", tool_calls=[ToolCall(
        type="function", id=uuid4().hex, name="meeting_set_addressed_members",
        arguments=json.dumps({"member_names": ["analyst"]}),
    )]), AssistantMessage(content="", tool_calls=[ToolCall(
        type="function", id=uuid4().hex, name="send_message",
        arguments=json.dumps({"to": "analyst", "content": original.message.content}),
    )]), AssistantMessage(content="The message was delivered.")]
    result = await leader.agent.invoke({"query": "Route the named human request"}, session=leader.session)
    assert result == {"output": "", "result_type": "answer", "meeting_queued": False}
    assert wakeups == []
    assert {item.member_name for item in await coordinator.list_pending()} <= {"other"}
    state = await coordinator.get_state()
    assert state.addressed_members == ("analyst",)
    assert state.lease_token == current.lease_token
    forwarded = [item for item in await meeting.db.message.get_team_messages("publication-team")
                 if item.from_member_name == "leader"]
    assert len(forwarded) == 1
    assert forwarded[0].content == original.message.content
    assert forwarded[0].to_member_name == "analyst" and not forwarded[0].broadcast
    assert "The message was delivered." not in meeting.conversation.history_path.read_text(encoding="utf-8")

    analyst = await native_factory("analyst", participant=True)
    analyst.model.invoke.side_effect = [_post_call(content="Here is my answer", mentions=["user", "leader"]), AssistantMessage(content="")]
    await analyst.agent.invoke({"query": forwarded[0].content}, session=analyst.session)
    assert wakeups == ["speech"]
    assert await coordinator.complete_playback(current.lease_token)
    grant = await coordinator.grant_next()
    assert grant.member_name == "analyst" and grant.mentions == ("user", "leader")
    assert await coordinator.complete_playback(grant.lease_token)
    assert (await coordinator.get_state()).addressed_members == ("analyst",)
    priority = leader.rail._policy._tools["meeting_set_addressed_members"]
    replaced = await priority.invoke({"member_names": ["other"]})
    assert replaced.success and (await coordinator.get_state()).addressed_members == ("other",)
    cleared = await priority.invoke({"member_names": []})
    assert cleared.success and (await coordinator.get_state()).addressed_members == ()
    # Clearing priority also disables silent handoffs to the former recipient.
    leader.model.invoke.side_effect = [AssistantMessage(content="", tool_calls=[ToolCall(
        type="function", id=uuid4().hex, name="send_message",
        arguments=json.dumps({"to": "analyst", "content": "Do not resend"}),
    )]), AssistantMessage(content="")]
    await leader.agent.invoke({"query": "Try a stale handoff"}, session=leader.session)
    assert len([item for item in await meeting.db.message.get_team_messages("publication-team")
                if item.from_member_name == "leader"]) == 1
    assert wakeups == ["speech"]


@pytest.mark.asyncio
@pytest.mark.parametrize("member,to,selected", [
    ("leader", "user", ["analyst"]), ("leader", "*", ["analyst"]), ("analyst", "leader", ["leader"]),
])
async def test_silent_send_rejects_user_broadcast_and_teammate_bypasses(
    meeting, native_factory, member, to, selected,
):
    await meeting.coordinator.set_addressed_members(selected)
    native = await native_factory(member)
    native.model.invoke.side_effect = [AssistantMessage(content="", tool_calls=[ToolCall(
        type="function", id=uuid4().hex, name="send_message",
        arguments=json.dumps({"to": to, "content": "Should not be delivered"}),
    )]), AssistantMessage(content="")]
    await native.agent.invoke({"query": "Try to send"}, session=native.session)
    assert await meeting.db.message.get_team_messages("publication-team") == []
    assert await meeting.coordinator.list_pending() == ()
    assert (await meeting.coordinator.get_state()).addressed_members == tuple(selected)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "cn"])
async def test_history_guidance_and_limits_are_scoped_without_changing_sdk_defaults(meeting, language):
    from openjiuwen.agent_teams.meeting import create_meeting_tools

    rail = await create_meeting_publication_rail(meeting.coordinator, "leader", language=language)
    history = rail._tools["meeting_read_history"]
    sdk = (await create_meeting_tools(meeting.coordinator.for_member("leader"), language=language))[
        "meeting_read_history"
    ]

    assert history.card.input_params["properties"]["limit"] == {
        "type": "integer", "minimum": 1, "maximum": 20, "default": 10,
    }
    assert sdk.card.input_params == history._tool.card.input_params
    assert sdk.card.input_params["properties"]["limit"]["maximum"] == 100
    assert sdk.card.input_params["properties"]["limit"]["default"] == 50
    assert history.card.description != sdk.card.description
    assert sdk.card.description == history._tool.card.description
    if language == "en":
        assert "Use messages already in your context first" in history.card.description
        assert "smallest useful limit" in history.card.description
        assert "Do not repeat a read or increase its limit" in history.card.description
        assert "Historical messages are context, not new events" in history.card.description
        assert "file-attachment events" in history.card.description
        assert "limit: 1–20, default 10" in history.card.description
    else:
        assert "先使用已有上下文" in history.card.description
        assert "不得重复读取或增大 limit" in history.card.description
        assert "历史消息是背景，不是新事件" in history.card.description
        assert "limit 为 1–20，默认 10" in history.card.description
