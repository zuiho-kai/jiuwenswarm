"""Verify the installed SDK provides the Team meeting API."""

import subprocess
import sys
import textwrap


def test_installed_sdk_initial_message_prepares_leader_only_meeting_without_a_model_turn():
    result = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", textwrap.dedent(r'''
            import asyncio
            import json
            import sys
            import tempfile
            from pathlib import Path
            from unittest.mock import AsyncMock

            from openjiuwen.agent_teams import DeepAgentSpec, StorageSpec, TeamAgentSpec
            from openjiuwen.agent_teams.meeting import MeetingCoordinator, MeetingScope, MeetingState
            from openjiuwen.agent_teams.paths import set_task_openjiuwen_home, reset_task_openjiuwen_home
            from openjiuwen.core.runner import Runner
            from openjiuwen.core.runner.runner import GLOBAL_RUNNER

            async def check(home):
                token = set_task_openjiuwen_home(home)
                spec = TeamAgentSpec(
                    team_name="meeting-init-test",
                    agents={"leader": DeepAgentSpec(workspace=None)},
                    storage=StorageSpec(type="sqlite", params={"connection_string": ":memory:"}),
                    lifecycle="persistent",
                    evolution_enabled=False,
                )
                stream = Runner.run_agent_team_streaming(
                    agent_team=spec,
                    inputs={"query": {
                        "type": "group_chat", "body": "Start meeting",
                        "client_message_id": "meeting-init", "mentions": [],
                    }},
                    session="meeting-session",
                )
                accepted = asyncio.Event()
                captured = {}

                async def consume():
                    async for chunk in stream:
                        payload = chunk.payload
                        if payload.get("event_type") == "team.runtime_ready":
                            entry = await GLOBAL_RUNNER._get_team_runtime_manager().pool.get(spec.team_name)
                            captured["agent"] = entry.agent
                            captured["enqueue"] = AsyncMock(wraps=entry.agent._coordination.enqueue_user_input)
                            entry.agent._coordination.enqueue_user_input = captured["enqueue"]
                        if payload.get("event_type") == "team.group_message.accepted":
                            accepted.set()

                await Runner.start()
                consumer = asyncio.create_task(consume())
                try:
                    async with asyncio.timeout(30):
                        while not accepted.is_set():
                            if consumer.done():
                                await consumer
                                raise AssertionError("Stream ended before initialization was accepted")
                            await asyncio.sleep(0.01)
                    agent = captured["agent"]
                    backend = agent.team_backend
                    assert backend.conversation_mode == "group_chat"
                    captured["enqueue"].assert_not_awaited()
                    members = await backend.db.member.get_team_members(spec.team_name)
                    assert [member.member_name for member in members] == [spec.leader.member_name]
                    conversation = await backend.group_conversation()
                    lines = conversation.history_path.read_text(encoding="utf-8").splitlines()
                    assert len(lines) == 1 and json.loads(lines[0])["content"] == "Start meeting"
                    coordinator = MeetingCoordinator(
                        backend.db,
                        MeetingScope("meeting-session", spec.team_name, "meeting"),
                        message_manager=backend.message_manager,
                        conversation=conversation,
                    )
                    state = await coordinator.start()
                    assert isinstance(state, MeetingState)
                    assert state.leader_member_name == spec.leader.member_name and not state.closed
                    await coordinator.close()
                finally:
                    await Runner.stop_agent_team(team_name=spec.team_name, session_id="meeting-session")
                    await asyncio.wait_for(consumer, 10)
                    await Runner.stop()
                    reset_task_openjiuwen_home(token)

            base = tempfile.gettempdir()
            if sys.platform == "win32":
                base = "\\\\?\\" + base
            with tempfile.TemporaryDirectory(dir=base) as directory:
                asyncio.run(check(Path(directory)))
        ''')],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr


def test_installed_sdk_mounts_meeting_provider_on_real_leader_and_spawned_member():
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", textwrap.dedent(r'''
            import asyncio
            import sys
            import tempfile
            from pathlib import Path
            from types import SimpleNamespace

            from openjiuwen.agent_teams import DeepAgentSpec, StorageSpec, TeamAgentSpec
            from openjiuwen.agent_teams.schema.deep_agent_spec import RailSpec
            from openjiuwen.agent_teams.paths import set_task_openjiuwen_home, reset_task_openjiuwen_home
            from openjiuwen.core.runner import Runner
            from openjiuwen.core.runner.runner import GLOBAL_RUNNER
            from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs
            from openjiuwen.core.single_agent.schema.agent_card import AgentCard

            from jiuwenswarm.agents.swarm.context import SwarmBuildContext
            from jiuwenswarm.agents.swarm.registry import register_swarm_providers
            from jiuwenswarm.extensions.meeting import meeting_manager as host_module
            from jiuwenswarm.extensions.meeting.participants import MeetingParticipantRail

            async def check(home):
                token = set_task_openjiuwen_home(home)
                register_swarm_providers()
                spec = TeamAgentSpec(
                    team_name="provider-meeting-test", spawn_mode="inprocess",
                    agents={role: DeepAgentSpec(workspace=None, rails=[RailSpec(type="swarm.meeting_participant")])
                            for role in ("leader", "teammate")},
                    build_context=SwarmBuildContext(session_id="provider-session", team_id="provider-meeting-test"),
                    storage=StorageSpec(type="sqlite", params={"connection_string": ":memory:"}),
                    lifecycle="persistent", evolution_enabled=False,
                )
                ready = asyncio.Event()
                captured = {}
                stream = Runner.run_agent_team_streaming(
                    agent_team=spec, session="provider-session",
                    inputs={"query": {"type": "group_chat", "body": "Start meeting",
                                      "client_message_id": "provider-init", "mentions": []}},
                )

                async def consume():
                    async for chunk in stream:
                        if chunk.payload.get("event_type") == "team.runtime_ready":
                            entry = await GLOBAL_RUNNER._get_team_runtime_manager().pool.get(spec.team_name)
                            captured["agent"] = entry.agent
                        if chunk.payload.get("event_type") == "team.group_message.accepted":
                            ready.set()

                await Runner.start()
                consumer = asyncio.create_task(consume())
                manager = None
                try:
                    async with asyncio.timeout(30):
                        while not ready.is_set():
                            if consumer.done():
                                await consumer
                                raise AssertionError("Startup ended before acceptance")
                            await asyncio.sleep(0.01)
                    agent = captured["agent"]
                    backend = agent.team_backend
                    teams = SimpleNamespace(
                        get_active_team_name=lambda sid: spec.team_name,
                        is_runtime_active=lambda sid: True,
                        get_team_agent=lambda sid: agent,
                    )
                    manager = host_module.MeetingManager(teams, enabled=lambda: True)
                    host_module._server_manager = manager
                    state = await manager.start("provider-session")
                    await manager.enable_publication("provider-session", state.scope.meeting_id, on_queued=asyncio.Event().set)
                    spawned = await backend.spawn_member(
                        "later-expert", "Later Expert", AgentCard(id="later-expert", name="later-expert"),
                    )
                    assert spawned.ok
                    assert await backend.startup_member("later-expert", on_created=agent._on_teammate_created)
                    handle = agent._spawn_manager.spawned_handles["later-expert"]
                    child = handle.agent_ref
                    for current, expected in ((agent, spec.leader.member_name), (child, "later-expert")):
                        native = current.resources.harness.get_deep_agent()
                        await native.ensure_initialized()
                        rails = current.resources.harness.find_rails(MeetingParticipantRail)
                        assert len(rails) == 1, (expected, rails)
                        ctx = AgentCallbackContext(agent=native, inputs=ModelCallInputs(tools=[]))
                        await rails[0].before_model_call(ctx)
                        assert not ctx.has_force_finish_request, (expected, ctx.consume_force_finish())
                        names = {tool.name for tool in ctx.inputs.tools}
                        assert {"meeting_post_message", "meeting_read_history", "meeting_get_speaker"} <= names
                        assert rails[0]._policy._client.member_name == expected
                    assert await manager.end("provider-session", state.scope.meeting_id)
                    for current in (agent, child):
                        native = current.resources.harness.get_deep_agent()
                        assert native.system_prompt_builder.get_section("meeting_publication") is None
                    conversation = await backend.group_conversation()
                    assert len(conversation.history_path.read_text(encoding="utf-8").splitlines()) == 1
                finally:
                    if manager is not None:
                        await manager.aclose()
                    host_module._server_manager = None
                    await Runner.stop_agent_team(team_name=spec.team_name, session_id="provider-session")
                    await asyncio.wait_for(consumer, 10)
                    await Runner.stop()
                    reset_task_openjiuwen_home(token)

            base = tempfile.gettempdir()
            if sys.platform == "win32":
                base = "\\\\?\\" + base
            with tempfile.TemporaryDirectory(dir=base) as directory:
                asyncio.run(check(Path(directory)))
        ''')],
        capture_output=True, text=True, encoding="utf-8", timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr
