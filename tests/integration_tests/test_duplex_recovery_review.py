"""Cross-review regression: an explicit stop must not act like a process crash."""
import asyncio

import pytest
from tests.integration_tests.test_duplex_e2e import world  # noqa: F401


@pytest.mark.asyncio
async def test_explicit_stop_durably_suspends_automatic_recovery(world):  # noqa: F811
    w = world
    w.endpoint.tool_first = False
    await w.harness.send("Do not restart after I explicitly stop")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    checkpoint, _ = w.native._duplex_inbox.load(w.native.durable_scope)
    assert checkpoint["running"] is True
    await w.harness.stop()
    checkpoint, _ = w.native._duplex_inbox.load(w.native.durable_scope)
    assert checkpoint.get("suspended") is True
