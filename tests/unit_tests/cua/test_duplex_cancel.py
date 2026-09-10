"""Queue cancellation through the Core task registry into a running CUA worker.

The RPC transport and desktop are simulated; the queue, adapter cancellation,
CUA tool and DeepAgent invocation use production code.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.cua import worker
from jiuwenswarm.agents.harness.cua.config import CuaConfig
from jiuwenswarm.extensions.video_duplex.backend import video_search
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from tests.unit_tests.cua.support import scripted_model
from tests.unit_tests.cua.test_integration import fake_session


@pytest.mark.asyncio
async def test_manual_queue_cancel_releases_cua_before_acknowledgement(
    monkeypatch, tmp_path
):
    desktop = fake_session(monkeypatch)
    model, model_client = scripted_model([])
    entered, interrupted = asyncio.Event(), asyncio.Event()

    async def thinking(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            interrupted.set()

    model_client.invoke = thinking
    cua = worker.CuaTaskTool(
        CuaConfig(enabled=True),
        lambda: model,
        workspace=str(tmp_path),
        agent_id="cancel-test",
    )
    adapter = SimpleNamespace(
        _session_agent_tasks={}, _resolve_interrupt_session_id=lambda sid: sid
    )
    core_tasks = []

    async def execute(_client, **kwargs):
        # A real deployment runs this on AgentServer, independently of Gateway.
        task = asyncio.create_task(cua.run(kwargs["question"]))
        core_tasks.append(task)
        adapter._session_agent_tasks[kwargs["core_session_id"]] = {task}
        return await asyncio.shield(task)

    async def send_request(env):
        stopped = await JiuWenSwarmDeepAdapter._cancel_session_agent_tasks(
            adapter, env.session_id
        )
        assert stopped == 1
        assert interrupted.is_set()
        desktop.call_tool.assert_awaited_with(
            "end_session",
            {"session": desktop.call_tool.await_args_list[0].args[1]["session"]},
        )
        return SimpleNamespace(ok=True, payload={"success": True})

    channel = SimpleNamespace(send_event=AsyncMock(), send_response=AsyncMock())
    monkeypatch.setattr(video_search, "execute_core_agent", execute)
    manager = video_search.VideoSearchManager(
        channel,
        SimpleNamespace(send_request=send_request),
        log_event=lambda _: None,
        qwen_active=lambda: True,
    )
    job = manager.start(
        None,
        question="Inspect desktop",
        query="Inspect desktop",
        search_session_id="scope",
    )
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            await manager.handle_control(
                None,
                "stop",
                {"search_session_id": "scope", "job_id": job["id"], "action": "cancel"},
                None,
            )
        assert channel.send_response.await_args.kwargs["ok"] is True
        assert manager._jobs[job["id"]]["status"] == "cancelled"
        assert not any(
            call.args[1] == "video.search.completed"
            for call in channel.send_event.await_args_list
        )
    finally:
        tasks = core_tasks + list(manager._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
