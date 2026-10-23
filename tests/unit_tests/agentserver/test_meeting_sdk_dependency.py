"""Verify the installed SDK provides the Team meeting API."""

import subprocess
import sys
import textwrap


def test_installed_sdk_exposes_meeting_api_without_pythonpath():
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "from openjiuwen.agent_teams.meeting import "
            "MeetingCoordinator, MeetingScope, MeetingState",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    assert result.returncode == 0, result.stderr


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
            from openjiuwen.agent_teams.meeting import MeetingCoordinator, MeetingScope
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
