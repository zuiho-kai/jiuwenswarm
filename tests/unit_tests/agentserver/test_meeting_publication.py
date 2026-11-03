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


async def _native(
    meeting, monkeypatch, member="leader", *, max_iterations=8, participant=False, progressive=False, language="en",
):
    agent = ReActAgent(card=AgentCard(id=uuid4().hex, name="meeting-native"))
    agent.configure(ReActAgentConfig(model_name="fake", max_iterations=max_iterations))
    if progressive:
        # Mirror native DeepAgent registration and the production disclosure rail.
        agent.ability_manager.set_tool_exposure_policy(True, direct_tool_names={"read_file"})
    agent.system_prompt_builder = SystemPromptBuilder(language=language)
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
        rail = await create_meeting_publication_rail(meeting.coordinator, member, language=language)
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
    assert meeting.conversation.history_path.exists()
    assert len(await meeting.db.message.get_team_messages("publication-team")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "cn"])
async def test_leader_prompt_and_send_schema_share_complete_user_forwarding_rule(native_factory, language):
    from jiuwenswarm.extensions.meeting.prompts import forwarding_contract

    native = await native_factory(language=language)
    native.model.invoke.return_value = AssistantMessage(content="")
    await native.agent.invoke({"query": "Assess the proposal"}, session=native.session)
    prompt = native.agent.system_prompt_builder.build()
    tools = {tool.name: tool for tool in native.model.invoke.call_args.kwargs["tools"]}
    contract = forwarding_contract(language)
    assert contract in prompt and tools["send_message"].description == contract
    if language == "en":
        assert "Copy the entire user message verbatim" in contract
        assert "Never use send_message for your own delegation" in contract
        assert "In every other case use meeting_post_message" in contract
        assert "For 'A, can you respond to me'" in prompt
        assert "add only the context" not in prompt
    else:
        assert "content 必须逐字保留用户的整条消息" in contract
        assert "其他任何情况都必须使用 meeting_post_message" in contract
        assert "只补充目标执行所必需的背景" not in prompt
    original_description = native.rail._send_settings[2]
    native.rail.uninit(native.agent)
    assert native.ordinary["send_message"].card.description == original_description


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


@pytest.fixture
async def responsive_meeting(meeting):
    from jiuwenswarm.extensions.meeting.coordination import ResponsiveMeetingCoordinator
    coordinator = ResponsiveMeetingCoordinator(
        meeting.db, MeetingScope("publication-session", "publication-team", "responsive"),
        message_manager=meeting.manager, conversation=meeting.conversation,
    )
    await coordinator.start()
    yield coordinator
    await coordinator.close()


@pytest.mark.asyncio
async def test_responsive_text_arrives_before_audio_and_is_not_sent_twice(responsive_meeting):
    coordinator = responsive_meeting
    result = await coordinator.for_member("analyst").post_message(
        "A new finding requiring attention", speech_text="New finding", mentions=["leader"],
    )
    assert result.message.published and result.message.message_id
    assert result.notified_members == ("leader",)
    history = await coordinator.for_member("leader").get_history()
    assert len(history) == 1
    granted = await coordinator.grant_next()
    assert granted.source_message_id == result.message.message_id
    assert len(await coordinator.for_member("leader").get_history()) == 1


@pytest.mark.asyncio
async def test_responsive_new_post_replaces_waiting_audio_without_losing_text(responsive_meeting):
    coordinator = responsive_meeting
    member = coordinator.for_member("analyst")
    first = await member.post_message("Old detail", speech_text="Old summary")
    second = await member.post_message("Updated detail", speech_text="Updated summary")
    pending = await coordinator.list_pending()
    assert [r.request_id for r in pending] == [second.speech_request.request_id]
    assert (await coordinator.get_request(first.speech_request.request_id)).status == "cancelled"
    assert len(await member.get_history()) == 2


@pytest.mark.asyncio
async def test_responsive_context_change_cancels_old_audio_and_preserves_current(responsive_meeting):
    coordinator = responsive_meeting
    first = await coordinator.for_member("analyst").post_message("Original plan", speech_text="Original plan")
    granted = await coordinator.grant_next()
    await coordinator.for_member("leader").post_message("Waiting plan", speech_text="Waiting plan")
    with pytest.raises(ValueError, match="Leader"):
        await coordinator.update_context("analyst", "Use the existing project")
    update = await coordinator.update_context("leader", "Use the existing Canvas project; do not change apps")
    assert update["cancelled_audio"] == 1
    assert not await coordinator.list_pending()
    assert (await coordinator.get_speaker()).lease_token == granted.lease_token
    assert (await coordinator.get_request(first.speech_request.request_id)).source_message_id
    assert len(await coordinator.for_member("leader").get_history()) == 3


@pytest.mark.asyncio
async def test_responsive_aged_member_precedes_fresh_leader(responsive_meeting, monkeypatch):
    import jiuwenswarm.extensions.meeting.coordination as policy
    coordinator = responsive_meeting
    older = await coordinator.for_member("analyst").post_message("Member result", speech_text="Result")
    # Move the policy clock forward, while the SDK records fresh admission at real time.
    monkeypatch.setattr(policy, "get_current_time", lambda: older.speech_request.created_at + 61_000)
    await coordinator.for_member("leader").post_message("Leader update", speech_text="Update")
    # Explicitly make only the first request aged in the durable queue.
    from sqlalchemy import update
    from openjiuwen.agent_teams.tools.models import _get_speech_request_model
    dao = await coordinator._dao()
    model = _get_speech_request_model(coordinator.scope.session_id)
    async with dao._locked(coordinator.scope) as (session, _):
        await session.execute(update(model).where(model.meeting_id == coordinator.scope.meeting_id,
            model.member_name == "leader").values(created_at=older.speech_request.created_at + 60_000))
    assert (await coordinator.list_pending())[0].member_name == "analyst"
    assert (await coordinator.grant_next()).member_name == "analyst"


@pytest.mark.asyncio
async def test_responsive_summary_budget_rejects_instead_of_truncating(responsive_meeting):
    coordinator = responsive_meeting
    with pytest.raises(ValueError, match="Rewrite"):
        await coordinator.for_member("analyst").post_message("Full text", speech_text="很长的口播"*40)
    assert not await coordinator.list_pending()
    assert not await coordinator.for_member("leader").get_history()
    from jiuwenswarm.extensions.meeting.coordination import speech_budget_error
    assert speech_budget_error("word "*66)
    assert speech_budget_error("结论清楚，保留风险，下一步先测试。") is None


@pytest.mark.asyncio
async def test_responsive_latest_user_input_is_shared_with_all_members(responsive_meeting):
    coordinator = responsive_meeting
    await coordinator.post_human_message("Use Canvas, do not switch to delivery apps", mentions=["leader"])
    await coordinator.refresh_user_context()
    version = coordinator.context_version
    assert "Canvas" in coordinator.latest_user_input
    await coordinator.refresh_user_context()
    assert coordinator.context_version == version


@pytest.mark.asyncio
async def test_responsive_browser_can_remove_waiting_audio_only(responsive_meeting):
    from jiuwenswarm.extensions.meeting.playback import BrowserPlaybackConsumer
    coordinator = responsive_meeting
    consumer = BrowserPlaybackConsumer(coordinator.scope, None, owner_id="owner", route_id="route",
                                       config_revision="revision", emit=AsyncMock(return_value=True))
    consumer.driver = SimpleNamespace(coordinator=coordinator, wake=MagicMock())
    post = await coordinator.for_member("analyst").post_message("Result", speech_text="Result")
    snapshot = await consumer.queue_control({"operation": "status"})
    assert len(snapshot["pending"]) == 1
    assert not (await consumer.queue_control({"operation": "cancel", "request_id": post.speech_request.request_id}))["pending"]
    assert len(await coordinator.for_member("leader").get_history()) == 1
    with pytest.raises(ValueError, match="no longer"):
        await consumer.queue_control({"operation": "cancel", "request_id": post.speech_request.request_id})


@pytest.mark.asyncio
async def test_native_discards_inflight_old_plan_after_shared_context_changes(
    meeting, publication_host, native_factory,
):
    scope = await _enable_host(publication_host, lambda: None)
    coordinator = scope.coordinator
    native = await native_factory("analyst", participant=True)
    calls = 0

    async def model_call(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await coordinator.update_context("leader", "Use the existing Canvas project; no delivery app")
            return _post_call(content="Outdated delivery app plan", mentions=["leader"])
        if calls == 2:
            assert any("Use the existing Canvas project" in str(m.content) for m in kwargs["messages"])
            return _post_call(content="Current Canvas improvement", speech_text="Improve the existing Canvas project", mentions=["leader"])
        return AssistantMessage(content="")

    native.model.invoke.side_effect = model_call
    await native.agent.invoke({"query": "Discuss the project"}, session=native.session)
    messages = await meeting.db.message.get_team_messages("publication-team")
    assert any(message.content == "Current Canvas improvement" for message in messages)
    assert not any(message.content == "Outdated delivery app plan" for message in messages)
    assert len(await coordinator.list_pending()) == 1
    schemas = {tool.name for tool in native.model.invoke.await_args_list[0].kwargs["tools"]}
    assert "meeting_update_context" not in schemas


@pytest.mark.asyncio
async def test_native_rewrites_overlong_speech_without_publishing_duplicate_text(
    meeting, publication_host, native_factory,
):
    scope = await _enable_host(publication_host, lambda: None)
    native = await native_factory(participant=True)
    native.model.invoke.side_effect = [
        _post_call(content="Important complete findings", speech_text="很长的详细口播"*40),
        _post_call(content="Important complete findings", speech_text="建议先完成测试，再根据结果修改。"),
        AssistantMessage(content=""),
    ]
    await native.agent.invoke({"query": "Report"}, session=native.session)
    messages = await meeting.db.message.get_team_messages("publication-team")
    assert len(messages) == 1 and messages[0].content == "Important complete findings"
    assert (await scope.coordinator.list_pending())[0].speech_text == "建议先完成测试，再根据结果修改。"
    schemas = {tool.name for tool in native.model.invoke.await_args_list[0].kwargs["tools"]}
    assert "meeting_update_context" in schemas


@pytest.mark.asyncio
async def test_queue_status_failure_does_not_undo_committed_message(responsive_meeting):
    coordinator = responsive_meeting
    coordinator.on_queue_changed = AsyncMock(side_effect=OSError("Browser unavailable"))
    posted = await coordinator.for_member("analyst").post_message("Preserved finding", speech_text="Finding")
    assert posted.message.published and len(await coordinator.list_pending()) == 1
    assert len(await coordinator.for_member("leader").get_history()) == 1


@pytest.mark.asyncio
async def test_meeting_actions_execute_review_and_reopen_without_changing_cluster_tasks(responsive_meeting):
    c = responsive_meeting
    assign = dict(operation="assign", action_id="prototype", owner="analyst", reviewer="leader",
                  goal="Make prototype navigable", acceptance="Open start screen and complete T1")
    before = c.context_version
    await c.update_action("leader", assign)
    assert c.context_version == before  # routine work must not invalidate concurrent speech
    await c.update_action("analyst", dict(operation="start", action_id="prototype", revision=1))
    with pytest.raises(ValueError, match="assigned/running/submitted"):
        await c.update_action("leader", {"operation": "finish"})
    with pytest.raises(ValueError, match="artifact paths"):
        await c.update_action("analyst", dict(operation="submit", action_id="prototype", revision=2, note="done"))
    await c.update_action("analyst", dict(operation="submit", action_id="prototype", revision=2,
                                         evidence=["outputs/prototype.html", "T1 navigation test passed"], note="Actual implementation"))
    with pytest.raises(ValueError, match="designated reviewer"):
        await c.update_action("analyst", dict(operation="accept", action_id="prototype", revision=3, note="I checked"))
    with pytest.raises(ValueError, match="Stale"):
        await c.update_action("leader", dict(operation="accept", action_id="prototype", revision=2, note="Old review"))
    await c.update_action("leader", dict(operation="accept", action_id="prototype", revision=3, note="Opened artifact and verified T1"))
    done = await c.update_action("leader", {"operation": "finish"})
    assert done["phase"] == "finished"
    await c.update_action("leader", dict(operation="rework", action_id="prototype", revision=4, note="New navigation defect"))
    snapshot = await c.queue_snapshot()
    assert snapshot["phase"] == "execution" and snapshot["actions"][0]["status"] == "assigned"
    # These operations only send meeting messages; no cluster task is created.
    from sqlalchemy import text
    async with c._database.engine.connect() as conn:
        tables = (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'team_task_%'"))).scalars().all()
        for table in tables:
            assert (await conn.execute(text(f'SELECT COUNT(*) FROM "{table}"'))).scalar() == 0


@pytest.mark.asyncio
async def test_meeting_action_blocking_scope_and_notification_failure(responsive_meeting):
    c = responsive_meeting
    action = dict(operation="assign", action_id="figma", owner="analyst", reviewer="leader",
                  goal="Connect Figma", acceptance="Anonymous T1 works")
    with pytest.raises(ValueError, match="Only the Leader"):
        await c.update_action("analyst", action)
    with pytest.raises(ValueError, match="distinct"):
        await c.update_action("leader", {**action, "reviewer": "analyst"})
    original = c._send_message
    c._send_message = AsyncMock(side_effect=RuntimeError("delivery failed"))
    with pytest.raises(RuntimeError):
        await c.update_action("leader", action)
    assert not c.actions.items
    c._send_message = original
    await c.update_action("leader", action)
    await c.update_action("analyst", dict(operation="block", action_id="figma", revision=1,
                                         note="No editor permission; owner must grant edit access"))
    result = await c.update_action("leader", {"operation": "finish"})
    assert result["phase"] == "waiting" and result["actions"][0]["status"] == "blocked"
    assert result["actions"][0]["acceptance"] == "Anonymous T1 works"
    await c.update_action("analyst", dict(operation="start", action_id="figma", revision=2))
    assert c.actions.phase == "execution"
    rail = await create_meeting_publication_rail(c, "analyst")
    assert "meeting_action" in rail._tools
    await c.close()
    assert not (await rail._tools["meeting_action"].invoke({"operation": "status"})).success


@pytest.mark.parametrize("speech", [
    "N3 rev4 已 submit，等待 accept。", "N7-demo-10task-audit 已完成。", "F1-A 已通过。",
    "报告哈希 3b94a8b28ea1a099 已固定。", "SHA256 已一致。",
    r"文件在 C:\Users\tester\report.pdf。", "请看 outputs/report.pdf。",
    "请打开 https://example.com/report。", "已经执行 meeting_post_message。",
    "请运行 `npm test`。", "修改节点3:32后通过。",
])
def test_meeting_speech_rejects_opaque_identifiers_and_technical_details(speech):
    from jiuwenswarm.extensions.meeting.coordination import speech_budget_error
    assert "Rewrite speech_text only" in speech_budget_error(speech)


@pytest.mark.parametrize("speech", [
    "报告已重新生成，另一位成员还需要检查，暂时不能算验收完成。",
    "原型修复说明已检查通过，但修改尚未应用到 Figma，页面能否点击仍待验证。",
    "十项任务已检查八项，其中两项无法完成。我们接下来补齐功能，再重新测试。",
    "测试还未开始，需要你提供 Maze 账号后才能继续。",
    "Canvas 的五项检查通过，下一步核对报告。", "我们将在12:30继续检查，任务截止时间为23:59。",
    "The report was submitted for another member to check. It has not passed review yet.",
])
def test_meeting_speech_preserves_meaningful_names_counts_times_and_uncertainty(speech):
    from jiuwenswarm.extensions.meeting.coordination import speech_budget_error
    assert speech_budget_error(speech) is None


@pytest.mark.asyncio
async def test_meeting_rewrites_speech_without_losing_full_technical_content(responsive_meeting):
    member = responsive_meeting.for_member("analyst")
    content = "N3 rev4 submitted. outputs/report.pdf SHA256 3b94a8b28ea1a099; pending independent review."
    with pytest.raises(ValueError, match="Rewrite speech_text only"):
        await member.post_message(content, speech_text="N3 rev4 已 submit。", mentions=["leader"])
    assert not await responsive_meeting.list_pending()
    assert not await responsive_meeting.for_member("leader").get_history()
    await member.post_message(content, speech_text="报告已重新生成，等待另一位成员检查，暂时不能算验收完成。", mentions=["leader"])
    history = await responsive_meeting.for_member("leader").get_history()
    assert len(history) == 1 and history[0].content == content


@pytest.mark.asyncio
async def test_scope_pause_is_atomic_blocks_old_acceptance_and_preserves_unrelated_work(responsive_meeting):
    c = responsive_meeting
    await c.update_action("leader", dict(operation="assign", action_id="old", owner="analyst", reviewer="leader", goal="Old project", acceptance="Old test"))
    await c.update_action("leader", dict(operation="assign", action_id="unrelated", owner="leader", reviewer="analyst", goal="Independent audit", acceptance="Audit evidence"))
    await c.for_member("analyst").post_message("Old progress", speech_text="Old progress")
    with pytest.raises(ValueError, match="Leader"):
        await c.pause_actions("analyst", ["old"], "Switch project")
    with pytest.raises(ValueError, match="existing"):
        await c.pause_actions("leader", ["old", "missing"], "Switch project")
    assert c.actions.items["old"]["status"] == "assigned"
    pause = await c.pause_actions("leader", ["old"], "User selected a different project; stop old work before investigating")
    assert pause["cancelled_audio"] == 1 and c.actions.phase == "adjusting"
    assert pause["paused_actions"] == [{"action_id": "old", "status": "paused", "revision": 2}]
    assert c.actions.items["unrelated"]["status"] == "assigned"
    assert not c.actions.execution_paused("leader") and c.actions.execution_paused("analyst")
    with pytest.raises(ValueError, match="paused"):
        await c.update_action("analyst", dict(operation="submit", action_id="old", revision=2, evidence=["old.html"], note="Done"))
    with pytest.raises(ValueError, match="paused"):
        await c.update_action("leader", dict(operation="accept", action_id="old", revision=2, note="Old result"))
    with pytest.raises(ValueError, match="paused"):
        await c.for_member("analyst").post_message("Old result", speech_text="Old result")
    with pytest.raises(ValueError, match="still has"):
        await c.update_action("leader", {"operation": "finish"})
    await c.update_action("leader", dict(operation="rework", action_id="old", revision=2, note="Investigated: retain this audit but use the new project"))
    assert not c.actions.execution_paused("analyst")
    await c.update_action("analyst", dict(operation="start", action_id="old", revision=3))


@pytest.mark.asyncio
async def test_pause_gate_survives_notification_failure_and_ordinary_questions_do_not_pause(responsive_meeting):
    c = responsive_meeting
    await c.update_action("leader", dict(operation="assign", action_id="report", owner="analyst", reviewer="leader", goal="Report", acceptance="Reviewed report"))
    await c.post_human_message("How is the report going?", mentions=["leader"])
    await c.refresh_user_context()
    assert not c.actions.execution_paused("analyst") and c.actions.work_versions == {}
    c._send_message = AsyncMock(side_effect=OSError("offline"))
    result = await c.pause_actions("leader", ["report"], "The user cancelled this report")
    assert result["notification_error"] == "pause_committed_notification_failed"
    assert c.actions.execution_paused("analyst")
    assert (await c.queue_snapshot())["actions"][0]["status"] == "paused"


@pytest.mark.asyncio
async def test_native_pause_prevents_inflight_old_file_write(meeting, publication_host, native_factory):
    scope = await _enable_host(publication_host, lambda: None)
    c = scope.coordinator
    await c.update_action("leader", dict(operation="assign", action_id="old", owner="analyst", reviewer="leader", goal="Old project", acceptance="Old test"))
    native = await native_factory("analyst", participant=True)
    writer = _OrdinarySend("write_file")
    native.agent.ability_manager.add_ability(writer.card, writer)
    calls = 0
    async def model_call(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await c.pause_actions("leader", ["old"], "User changed project")
            return AssistantMessage(content="", tool_calls=[ToolCall(type="function", id=uuid4().hex, name="write_file", arguments="{}")])
        return AssistantMessage(content="")
    native.model.invoke.side_effect = model_call
    await native.agent.invoke({"query": "Continue work"}, session=native.session)
    assert writer.sent == []
    assert c.actions.items["old"]["status"] == "paused"


@pytest.mark.asyncio
async def test_pause_stops_work_before_audio_ack_and_audio_failure_does_not_undo_it(responsive_meeting):
    from jiuwenswarm.extensions.meeting.publication import MeetingActionTool
    c = responsive_meeting
    await c.update_action("leader", dict(operation="assign", action_id="old", owner="analyst", reviewer="leader", goal="Old project", acceptance="Old test"))
    await c.for_member("analyst").post_message("Old progress", speech_text="Old progress")
    speaker = await c.grant_next()
    async def stop(member, token):
        assert member == "leader" and token == speaker.lease_token
        assert c.actions.execution_paused("analyst")
        raise RuntimeError("browser did not acknowledge")
    tool = MeetingActionTool(c, "leader", stop)
    result = await tool.invoke(dict(operation="pause", action_ids=["old"], note="User changed project"))
    assert result.success and result.data["audio_error"] == "work_paused_audio_stop_failed"
    assert c.actions.items["old"]["status"] == "paused"


@pytest.mark.asyncio
async def test_assignment_speech_is_required_before_work_is_committed(responsive_meeting):
    from jiuwenswarm.extensions.meeting.publication import MeetingActionTool
    c = responsive_meeting
    tool = MeetingActionTool(c, "leader")
    assignment = dict(operation="assign", action_id="layout", owner="analyst", reviewer="leader",
                      goal="Improve the layout", acceptance="The layout is clear")
    result = await tool.invoke(assignment)
    assert not result.success and not c.actions.items
    result = await tool.invoke({**assignment, "speech_text": "Open outputs/report.html"})
    assert not result.success and not c.actions.items


@pytest.mark.asyncio
async def test_assignment_announcements_keep_other_speech_and_wake_the_consumer(responsive_meeting):
    c = responsive_meeting
    wake = MagicMock()
    rail = await create_meeting_publication_rail(c, "leader", on_queued=wake)
    tool = rail._tools["meeting_action"]
    await c.for_member("leader").post_message("An existing answer", speech_text="Here is the answer to your question.")
    first = await tool.invoke(dict(operation="assign", action_id="layout", owner="analyst", reviewer="leader",
                                  goal="Improve the layout", acceptance="Clear navigation",
                                  speech_text="Analyst will improve the layout."))
    second = await tool.invoke(dict(operation="assign", action_id="checks", owner="analyst", reviewer="leader",
                                   goal="Check the controls", acceptance="Controls work",
                                   speech_text="Analyst will also check the controls."))
    assert first.success and second.success and wake.call_count == 2
    assert len(c.actions.items) == 2
    assert first.data["speech_queued"] and second.data["speech_queued"]
    pending = await c.list_pending()
    assert len(pending) == 3  # No assignment or existing answer was overwritten.
    assert {r.speech_text for r in pending} == {
        "Here is the answer to your question.", "Analyst will improve the layout.",
        "Analyst will also check the controls.",
    }
    for request in pending:
        assert request.source_message_id
    await c.for_member("leader").post_message("A later answer", speech_text="The review is still in progress.")
    pending = await c.list_pending()
    assert len(pending) == 3
    assert {first.data["speech_request_id"], second.data["speech_request_id"]} <= {r.request_id for r in pending}


@pytest.mark.asyncio
async def test_assignment_speech_failure_does_not_undo_delivered_work(responsive_meeting):
    from jiuwenswarm.extensions.meeting.publication import MeetingActionTool
    c = responsive_meeting
    c.announce_assignment = AsyncMock(side_effect=OSError("Speech queue unavailable"))
    result = await MeetingActionTool(c, "leader").invoke(dict(
        operation="assign", action_id="layout", owner="analyst", reviewer="leader",
        goal="Improve the layout", acceptance="Clear navigation", speech_text="Analyst will improve the layout.",
    ))
    assert result.success and result.data["audio_error"] == "assignment_committed_announcement_failed"
    assert c.actions.items["layout"]["owner"] == "analyst"
    assert c.actions.items["layout"]["status"] == "assigned"
