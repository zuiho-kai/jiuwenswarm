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


async def _wait_for_active(manager, session_id="session-one"):
    for _ in range(100):
        status = await manager.lifecycle_status(session_id)
        if status["state"] == "active":
            return status
        await asyncio.sleep(0.01)
    pytest.fail("Meeting did not become active")


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
async def test_pending_start_uses_normal_team_activation_and_connection_owner(
    setup_manager,
):
    manager, teams = setup_manager
    teams.active = False

    assert await manager.request_start("session-one", "owner-one") is None
    assert await manager.lifecycle_status("session-one") == {"state": "starting"}
    with pytest.raises(RuntimeError, match="Another connection"):
        await manager.request_start("session-one", "owner-two")
    assert not _Coordinator.instances

    teams.active = True
    await _wait_for_active(manager)
    state = await manager.status("session-one")
    assert state is not None
    assert await manager.request_start("session-one", "owner-one") == state
    with pytest.raises(RuntimeError, match="Another connection"):
        await manager.request_start("session-one", "owner-two")
    assert (await manager.lifecycle_status("session-one"))[
        "meeting_id"
    ] == state.scope.meeting_id
    assert not await manager.end(
        "session-one", state.scope.meeting_id, owner_id="owner-two"
    )
    assert await manager.end(
        "session-one", state.scope.meeting_id, owner_id="owner-one"
    )
    assert await manager.lifecycle_status("session-one") == {"state": "idle"}


@pytest.mark.asyncio
async def test_owner_disconnect_closes_active_meeting_without_meeting_id(setup_manager):
    manager, teams = setup_manager
    state = await manager.request_start("session-one", "owner-one")
    assert state is None
    await _wait_for_active(manager)
    state = await manager.status("session-one")
    assert state is not None
    assert not await manager.release("session-one", "owner-two")
    assert not await manager.end("session-one", "", owner_id="owner-one")
    assert await manager.release("session-one", "owner-one")
    await _wait_for_idle(manager)
    assert teams.active
    assert await manager.lifecycle_status("session-one") == {"state": "idle"}


@pytest.mark.asyncio
async def test_pending_start_can_be_cancelled_before_team_activation(setup_manager):
    manager, teams = setup_manager
    teams.active = False
    await manager.request_start("session-one", "owner-one")
    assert not await manager.end("session-one", "", owner_id="owner-one")
    assert await manager.release("session-one", "owner-one")
    await _wait_for_idle(manager)
    teams.active = True
    assert await manager.lifecycle_status("session-one") == {"state": "idle"}
    assert not _Coordinator.instances


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_at", ["conversation", "coordinator"])
@pytest.mark.parametrize("activation", ["immediate", "pending"])
async def test_end_rejects_startup_but_disconnect_aborts_it(
    setup_manager, monkeypatch, pause_at, activation
):
    manager, teams = setup_manager
    entered = asyncio.Event()
    release = asyncio.Event()
    if pause_at == "conversation":

        async def slow_conversation():
            entered.set()
            await release.wait()
            return teams.backend.conversation

        teams.backend.group_conversation = slow_conversation
    else:

        class _SlowCoordinator(_Coordinator):
            async def start(self):
                entered.set()
                await release.wait()
                return await super().start()

        monkeypatch.setattr(module, "MeetingCoordinator", _SlowCoordinator)

    if activation == "pending":
        teams.active = False
    assert await manager.request_start("session-one", "owner-one") is None
    if activation == "pending":
        teams.active = True
        assert await manager.lifecycle_status("session-one") == {"state": "starting"}
    await asyncio.wait_for(entered.wait(), 1)
    start_task = manager._sessions["session-one"].task
    assert start_task is not None
    assert not await asyncio.wait_for(
        manager.end("session-one", "", owner_id="owner-one"), 0.2
    )
    assert await manager.release("session-one", "owner-one")
    assert await asyncio.wait_for(manager.lifecycle_status("another-session"), 0.2) == {
        "state": "idle"
    }
    assert await asyncio.wait_for(start_task, 1) is None
    assert await manager.lifecycle_status("session-one") == {"state": "idle"}
    assert await manager.status("session-one") is None
    if pause_at == "coordinator":
        assert len(_Coordinator.instances) == 1
        assert _Coordinator.instances[0].closed
    else:
        assert not _Coordinator.instances


@pytest.mark.asyncio
async def test_late_abandoned_start_blocks_replacement_until_cleaned(setup_manager):
    manager, teams = setup_manager
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Model a dependency that takes time to finish after cancellation.
                await release.wait()
        return teams.backend.conversation

    teams.backend.group_conversation = slow_once
    assert await manager.request_start("session-one", "owner-one") is None
    await asyncio.wait_for(entered.wait(), 1)
    old_task = manager._sessions["session-one"].task
    assert old_task is not None
    assert await manager.release("session-one", "owner-one")
    with pytest.raises(RuntimeError):
        await manager.request_start("session-one", "owner-two")
    release.set()
    assert await asyncio.wait_for(old_task, 1) is None
    assert len(_Coordinator.instances) == 1
    assert _Coordinator.instances[0].closed
    assert await manager.request_start("session-one", "owner-two") is None
    current = await _wait_for_active(manager)
    assert (await manager.lifecycle_status("session-one"))["meeting_id"] == current[
        "meeting_id"
    ]
    assert sum(not coordinator.closed for coordinator in _Coordinator.instances) == 1
    assert await manager.end("session-one", current["meeting_id"], owner_id="owner-two")


@pytest.mark.asyncio
async def test_lifecycle_persists_in_existing_team_database(tmp_path):
    from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase

    database = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / "team.db")))
    await database.initialize()
    try:
        await database.team.create_team("team-one", "Team One", "leader")
        teams = _TeamManager()
        teams.backend.db = database
        teams.backend.message_manager = SimpleNamespace(
            db=database, team_name="team-one"
        )
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
        await manager.aclose()
        await database.close()


@pytest.mark.asyncio
async def test_pending_start_remains_reserved_until_release(setup_manager):
    _, teams = setup_manager
    teams.active = False
    manager = module.MeetingManager(teams, enabled=lambda: True)
    await manager.request_start("session-one", "owner-one")
    reservation = manager._sessions["session-one"]
    await manager.request_start("session-one", "owner-one")
    assert manager._sessions["session-one"] is reservation
    await asyncio.sleep(0.04)
    assert not reservation.task.done()
    assert await manager.lifecycle_status("session-one") == {"state": "starting"}
    assert await manager.release("session-one", "owner-one")
    assert await asyncio.wait_for(reservation.task, 1) is None
    assert await manager.lifecycle_status("session-one") == {"state": "idle"}
    teams.active = True
    assert not _Coordinator.instances
    await manager.request_start("session-one", "owner-two")
    await _wait_for_active(manager)
    await manager.aclose()


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
@pytest.mark.parametrize("change", ["stop", "replace", "disable"])
async def test_invalidated_start_is_cleaned_and_can_be_retried(
    setup_manager, monkeypatch, change
):
    manager, teams = setup_manager
    entered, release = asyncio.Event(), asyncio.Event()
    original_backend = teams.backend

    async def slow_conversation():
        entered.set()
        await release.wait()
        return original_backend.conversation

    monkeypatch.setattr(original_backend, "group_conversation", slow_conversation)
    await manager.request_start("session-one", "owner-one")
    task = manager._sessions["session-one"].task
    await asyncio.wait_for(entered.wait(), 1)
    if change == "stop":
        teams.active = False
    elif change == "replace":
        teams.backend = _Backend()  # Same name, different runtime resources.
    else:
        manager._enabled = lambda: False
    release.set()
    assert await asyncio.wait_for(task, 1) is None
    assert _Coordinator.instances[0].closed
    assert await manager.lifecycle_status("session-one") == {"state": "idle"}
    teams.active = True
    manager._enabled = lambda: True
    await manager.request_start("session-one", "owner-two")
    await _wait_for_active(manager)


@pytest.mark.asyncio
async def test_cancelled_start_waiter_does_not_cancel_shared_start(
    setup_manager, monkeypatch
):
    manager, teams = setup_manager
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_conversation():
        entered.set()
        await release.wait()
        return teams.backend.conversation

    monkeypatch.setattr(teams.backend, "group_conversation", slow_conversation)
    first = asyncio.create_task(manager.start("session-one"))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(manager.start("session-one"))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    state = await asyncio.wait_for(second, 1)
    assert await manager.start("session-one") == state
    assert len(_Coordinator.instances) == 1


@pytest.mark.asyncio
async def test_cancelled_end_waiter_does_not_abandon_close(setup_manager, monkeypatch):
    manager, _ = setup_manager
    state = await manager.start("session-one")
    coordinator = _Coordinator.instances[0]
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_close():
        entered.set()
        await release.wait()
        coordinator.closed = True
        return True

    monkeypatch.setattr(coordinator, "close", slow_close)
    first = asyncio.create_task(manager.end("session-one", state.scope.meeting_id))
    await asyncio.wait_for(entered.wait(), 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await manager.lifecycle_status("session-one") == {"state": "closing"}
    with pytest.raises(RuntimeError, match="closing"):
        await manager.start("session-one")
    second = asyncio.create_task(manager.end("session-one", state.scope.meeting_id))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.wait_for(second, 1)
    assert coordinator.closed
    assert await manager.lifecycle_status("session-one") == {"state": "idle"}


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


@pytest.mark.asyncio
async def test_shutdown_aborts_startup_before_runtime_teardown(
    setup_manager, monkeypatch
):
    manager, teams = setup_manager
    entered = asyncio.Event()

    async def slow_conversation():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(teams.backend, "group_conversation", slow_conversation)
    await manager.request_start("session-one", "owner-one")
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(manager.aclose(), 1)
    assert await manager.lifecycle_status("session-one") == {"state": "idle"}
    assert teams.active
    with pytest.raises(RuntimeError, match="shutting down"):
        await manager.request_start("session-one", "owner-one")


@pytest.mark.asyncio
async def test_late_page_release_cannot_abort_new_startup_on_same_connection(
    setup_manager,
):
    manager, teams = setup_manager
    teams.active = False
    await manager.request_start("session-one", "owner-one", "old-startup")
    await manager.release("session-one", "owner-one", "old-startup")
    await _wait_for_idle(manager)
    await manager.request_start("session-one", "owner-one", "new-startup")
    assert not await manager.release("session-one", "owner-one", "old-startup")
    assert await manager.lifecycle_status("session-one") == {"state": "starting"}
    teams.active = True
    await _wait_for_active(manager)
    assert not await manager.release("session-one", "owner-one", "old-startup")
    assert await manager.lifecycle_status("session-one", "old-startup") == {
        "state": "idle"
    }
    assert (await manager.lifecycle_status("session-one", "new-startup"))[
        "state"
    ] == "active"
    assert await manager.release("session-one", "owner-one", "new-startup")
    await _wait_for_idle(manager)


@pytest.mark.asyncio
async def test_release_reserves_session_while_dependency_finishes_cancellation(
    setup_manager, monkeypatch
):
    _, teams = setup_manager
    manager = module.MeetingManager(teams, enabled=lambda: True)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowCoordinator(_Coordinator):
        async def start(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return await super().start()

    monkeypatch.setattr(module, "MeetingCoordinator", SlowCoordinator)
    await manager.request_start("session-one", "owner-one")
    task = manager._sessions["session-one"].task
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await manager.release("session-one", "owner-one")
        await asyncio.wait_for(cancelled.wait(), 1)
        assert await manager.lifecycle_status("session-one") == {"state": "closing"}
        with pytest.raises(RuntimeError, match="closing"):
            await manager.request_start("session-one", "owner-two")
        assert len(_Coordinator.instances) == 1
    finally:
        release.set()
        assert await asyncio.wait_for(task, 1) is None
        await manager.aclose()
    assert _Coordinator.instances[0].closed


@pytest.mark.asyncio
async def test_real_partial_start_failure_closes_persisted_row(tmp_path, monkeypatch):
    from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase

    database = TeamDatabase(
        DatabaseConfig(connection_string=str(tmp_path / "partial.db"))
    )
    await database.initialize()
    teams = _TeamManager()
    teams.backend.db = database
    teams.backend.message_manager = SimpleNamespace(db=database, team_name="team-one")
    manager = module.MeetingManager(teams, enabled=lambda: True)
    instances = []
    real_coordinator = module.MeetingCoordinator

    class PartialCoordinator(real_coordinator):
        async def start(self):
            instances.append(self)
            await super().start()
            raise RuntimeError("failed after row commit")

    monkeypatch.setattr(module, "MeetingCoordinator", PartialCoordinator)
    try:
        await database.team.create_team("team-one", "Team One", "leader")
        with pytest.raises(RuntimeError, match="after row commit"):
            await manager.start("session-one")
        assert (await instances[0].get_state()).closed
        assert await manager.lifecycle_status("session-one") == {"state": "idle"}
        assert await database.team.get_team("team-one") is not None
    finally:
        await manager.aclose()
        await database.close()
