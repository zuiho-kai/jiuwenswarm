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
        return self.backend.team_name if self.active and session_id == "session-one" else None

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
def setup_manager(monkeypatch):
    _Coordinator.instances.clear()
    monkeypatch.setattr(module, "MeetingCoordinator", _Coordinator)
    team_manager = _TeamManager()
    return module.MeetingManager(team_manager, enabled=lambda: True), team_manager


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
async def test_start_rejects_disabled_and_non_group_runtimes(setup_manager):
    manager, teams = setup_manager
    disabled = module.MeetingManager(teams, enabled=lambda: False)
    with pytest.raises(RuntimeError, match="disabled"):
        await disabled.start("session-one")

    teams.active = False
    with pytest.raises(RuntimeError, match="active group-chat Team"):
        await manager.start("session-one")

    teams.active = True
    teams.backend.conversation_mode = "team"
    with pytest.raises(RuntimeError, match="active group-chat Team"):
        await manager.start("session-one")
    assert not _Coordinator.instances


@pytest.mark.asyncio
async def test_failed_start_does_not_register_meeting(setup_manager, monkeypatch):
    manager, _ = setup_manager

    class _FailingCoordinator(_Coordinator):
        async def start(self):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(module, "MeetingCoordinator", _FailingCoordinator)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await manager.start("session-one")
    assert await manager.status("session-one") is None


@pytest.mark.asyncio
async def test_lifecycle_persists_in_existing_team_database(tmp_path):
    from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase

    database = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / "team.db")))
    await database.initialize()
    try:
        await database.team.create_team("team-one", "Team One", "leader")
        teams = _TeamManager()
        teams.backend.db = database
        teams.backend.message_manager = SimpleNamespace(db=database, team_name="team-one")
        manager = module.MeetingManager(teams, enabled=lambda: True)

        first = await manager.start("session-one")
        assert first == await manager.status("session-one")
        assert first.leader_member_name == "leader"
        assert await manager.end("session-one", first.scope.meeting_id)
        assert await manager.status("session-one") is None
        assert await database.team.get_team("team-one") is not None

        second = await manager.start("session-one")
        assert second.scope.meeting_id != first.scope.meeting_id
    finally:
        await database.close()
