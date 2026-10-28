"""Stage 2a: a meeting attaches to one existing Team runtime."""

import asyncio
from types import SimpleNamespace

import pytest

from openjiuwen.agent_teams.meeting import MeetingState

from jiuwenswarm.extensions.meeting import meeting_manager as module


class _Backend:
    def __init__(self):
        self.team_name = "team-one"
        self.conversation_mode = "group_chat"
        self.group_session_id = "session-one"
        self.db = object()
        self.message_manager = object()
        self.conversation = SimpleNamespace(
            team_name=self.team_name, session_id=self.group_session_id
        )
        self.conversation_calls = 0

    async def group_conversation(self):
        self.conversation_calls += 1
        return self.conversation


class _TeamManager:
    def __init__(self):
        self.backend = _Backend()
        self.active = True

    def get_active_team_name(self, session_id):
        return (
            self.backend.team_name
            if self.active and session_id == "session-one"
            else None
        )

    def is_runtime_active(self, session_id):
        return self.active and session_id == "session-one"

    def get_team_agent(self, session_id):
        if not self.is_runtime_active(session_id):
            return None
        return SimpleNamespace(team_backend=self.backend)


class _Coordinator:
    instances = []

    def __init__(self, database, scope, *, message_manager, conversation):
        self.database = database
        self.scope = scope
        self.message_manager = message_manager
        self.conversation = conversation
        self.closed = False
        self.starts = 0
        self.instances.append(self)

    def _state(self):
        return MeetingState(
            scope=self.scope,
            leader_member_name="leader",
            addressed_members=(),
            closed=self.closed,
            owner_request_id=None,
            lease_token=None,
            interruption_id=None,
            revision=0,
        )

    async def start(self):
        self.starts += 1
        return self._state()

    async def get_state(self):
        return self._state()

    async def close(self):
        self.closed = True
        return True


@pytest.fixture
async def setup_manager(monkeypatch):
    _Coordinator.instances.clear()
    monkeypatch.setattr(module, "MeetingCoordinator", _Coordinator)
    team_manager = _TeamManager()
    manager = module.MeetingManager(team_manager, enabled=lambda: True)
    yield manager, team_manager
    await manager.aclose()


async def _wait_for_idle(manager):
    async with asyncio.timeout(1):
        while await manager.lifecycle_status("session-one") != {"state": "idle"}:
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_start_attaches_existing_team_resources_once(setup_manager):
    manager, teams = setup_manager

    first, second = await asyncio.gather(
        manager.start("session-one"), manager.start("session-one")
    )

    assert first.scope == second.scope
    assert first.scope.team_name == "team-one"
    assert len(_Coordinator.instances) == 1
    coordinator = _Coordinator.instances[0]
    assert coordinator.starts == 1
    assert coordinator.database is teams.backend.db
    assert coordinator.message_manager is teams.backend.message_manager
    assert coordinator.conversation is teams.backend.conversation
    assert teams.backend.conversation_calls == 1
    assert await manager.status("session-one") == first


@pytest.mark.asyncio
async def test_end_only_closes_matching_meeting_not_team(setup_manager):
    manager, teams = setup_manager
    first = await manager.start("session-one")

    assert not await manager.end("session-one", "stale-meeting")
    assert await manager.status("session-one") == first
    assert await manager.end("session-one", first.scope.meeting_id)
    assert not await manager.end("session-one", first.scope.meeting_id)
    assert await manager.status("session-one") is None
    assert teams.active
    assert _Coordinator.instances[0].closed

    second = await manager.start("session-one")
    assert second.scope.meeting_id != first.scope.meeting_id


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_at", ["conversation", "coordinator"])
async def test_starting_waits_for_sdk_initialization(
    setup_manager, monkeypatch, pause_at
):
    _, teams = setup_manager
    manager = module.MeetingManager(teams, enabled=lambda: True)
    entered, resume = asyncio.Event(), asyncio.Event()
    if pause_at == "conversation":

        async def slow_conversation():
            entered.set()
            await resume.wait()
            return teams.backend.conversation

        monkeypatch.setattr(teams.backend, "group_conversation", slow_conversation)
    else:

        class SlowCoordinator(_Coordinator):
            async def start(self):
                entered.set()
                await resume.wait()
                return await super().start()

        monkeypatch.setattr(module, "MeetingCoordinator", SlowCoordinator)
    await manager.request_start("session-one", "owner-one")
    task = manager._sessions["session-one"].task
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0.04)
        assert not task.done()
        assert await manager.lifecycle_status("session-one") == {"state": "starting"}
        resume.set()
        state = await asyncio.wait_for(task, 1)
        assert state is not None
        assert (await manager.lifecycle_status("session-one"))["state"] == "active"
    finally:
        resume.set()
        await manager.aclose()


@pytest.mark.asyncio
async def test_failed_cleanup_stays_tracked_and_can_be_retried(
    setup_manager, monkeypatch
):
    manager, _ = setup_manager
    state = await manager.start("session-one")
    coordinator = _Coordinator.instances[0]
    close = coordinator.close

    async def fail_close():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(coordinator, "close", fail_close)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await manager.end("session-one", state.scope.meeting_id)
    assert await manager.lifecycle_status("session-one") == {"state": "cleanup_failed"}
    assert manager._sessions["session-one"].coordinator is coordinator
    monkeypatch.setattr(coordinator, "close", close)
    with pytest.raises(RuntimeError, match="cleanup"):
        await manager.start("session-one")
    await _wait_for_idle(manager)
    assert coordinator.closed
    assert (await manager.start("session-one")).scope != state.scope
