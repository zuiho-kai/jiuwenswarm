"""Exercise summary freshness through real Native state and fast-model HTTP input."""
import asyncio
import json

import pytest
from openjiuwen.harness.schema.task import TaskPlan, TodoItem

from tests.integration_tests.test_duplex_e2e import world, send_message  # noqa: F401


@pytest.mark.asyncio
async def test_fast_prompt_uses_new_committed_plan_even_with_old_published_summary(world):  # noqa: F811
    w = world
    w.endpoint.tool_first = False
    w.endpoint.fast_action = "APPEND"
    await w.harness.send("Implement the order system")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    w.native.commit_working_intent({"goal": "OLD_KAFKA_GOAL", "next_action": "OLD_KAFKA_ACTION",
                                   "current_hypothesis": "OLD_KAFKA_HYPOTHESIS",
                                   "constraints": ["OLD_KAFKA_CONSTRAINT"]})
    state = w.native.load_state(w.native._session)
    state.task_plan = TaskPlan(goal="NEW_SQL_GOAL", current_task_id="sql",
                               tasks=[TodoItem(id="sql", description="NEW_SQL_ACTION")])
    w.native.save_state(w.native._session, state)
    await send_message(w, "Please report current progress")
    await asyncio.wait_for(w.handler.on_poll_mailbox(None), 6)
    fast = next(call for call in w.endpoint.calls if call["model"] == "fast")
    prompt = json.dumps(fast["messages"])
    assert "NEW_SQL_GOAL" in prompt and "NEW_SQL_ACTION" in prompt
    assert "OLD_KAFKA" not in prompt
