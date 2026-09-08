"""Regression: explicit summaries are scoped to the committed plan they describe."""
import copy
from types import SimpleNamespace as NS

from jiuwenswarm.agents.harness.team.duplex_native import DuplexNativeHarness
from jiuwenswarm.agents.harness.team.duplex_shadow import snapshot_from_native
from jiuwenswarm.agents.harness.team.duplex_state import publish_state


class Session:
    def __init__(self):
        self.state = {}

    def get_state(self, key):
        return self.state.get(key)

    def update_state(self, value):
        self.state.update(copy.deepcopy(value))

    def get_session_id(self):
        return "test"


def native():
    plan = {"goal": "Build Kafka system", "current_task_id": "one", "tasks": [
        {"id": "one", "description": "Implement Kafka"},
        {"id": "two", "description": "Test the implementation"}]}
    session = Session()
    checkpoint = NS(iteration_index=1, deep_agent_state={"task_plan": copy.deepcopy(plan)})
    result = NS(_session=session, _duplex_version=1, _duplex_intent={}, _duplex_control_state={},
                _duplex_tools={}, plan=plan,
                active_round=NS(original_query="Build a system", round_id=1,
                                last_iter_snapshot=checkpoint, pre_round_snapshot=None,
                                iter_phase="model", model_call_in_flight=True,
                                tool_started=False, pause_requested=False),
                react_agent=NS(context_engine=NS(get_context=lambda **_: None)))
    result.load_state = lambda _: NS(to_session_dict=lambda: {"task_plan": result.plan})
    DuplexNativeHarness.commit_working_intent(result, {
        "goal": "Kafka detail", "next_action": "Create Kafka topics",
        "current_hypothesis": "Kafka is required", "constraints": ["Use approved infrastructure"]})
    return result


def test_new_plan_replaces_old_summary_before_and_after_publication():
    harness = native()
    publish_state(harness, NS(session=harness._session), "before_model_call")
    old = snapshot_from_native(harness)
    harness.plan = {"goal": "Use PostgreSQL instead", "constraints": ["No Kafka"],
                    "current_task_id": "new", "tasks": [{"id": "new", "description": "Build SQL queue"}]}
    # The checkpoint and published summary are deliberately still old.
    for _ in range(2):
        new = snapshot_from_native(harness)
        assert new.goal == "Use PostgreSQL instead"
        assert new.next_action == "Build SQL queue"
        assert new.current_hypothesis == ""
        assert new.constraints == ("No Kafka",)
        assert new.context_version != old.context_version
        publish_state(harness, NS(session=harness._session), "before_model_call")


def test_task_progress_preserves_valid_goal_and_constraints_only():
    harness = native()
    harness.plan["current_task_id"] = "two"
    state = snapshot_from_native(harness)
    assert state.goal == "Kafka detail"
    assert state.constraints == ("Use approved infrastructure",)
    assert state.next_action == "Test the implementation"
    assert state.current_hypothesis == ""
    assert state.intent_source == "mixed"
    assert harness._duplex_intent["next_action"] == "Create Kafka topics"


def test_adopted_goal_invalidates_old_scope_and_new_explicit_summary_is_accepted():
    harness = native()
    harness.active_round.original_query = "New user goal"
    harness.plan = {}
    state = snapshot_from_native(harness)
    assert state.goal == "New user goal"
    assert state.constraints == ()
    DuplexNativeHarness.commit_working_intent(harness, {
        "goal": "Refined new goal", "next_action": "Implement SQL", "current_hypothesis": "SQL",
        "constraints": ["No Kafka"]})
    assert snapshot_from_native(harness).current_hypothesis == "SQL"


def test_persisted_sources_survive_reload_and_unversioned_legacy_summary_is_ignored():
    harness = native()
    restored = Session()
    restored.state = copy.deepcopy(harness._session.state)
    harness._session = restored
    assert snapshot_from_native(harness).current_hypothesis == "Kafka is required"
    restored.state.pop("duplex_intent_sources")
    assert snapshot_from_native(harness).goal == "Build Kafka system"
    assert snapshot_from_native(harness).current_hypothesis == ""
