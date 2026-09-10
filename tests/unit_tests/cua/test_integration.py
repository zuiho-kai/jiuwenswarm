from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import types
from pydantic import ValidationError

from jiuwenswarm.agents.harness.cua import transport, worker
from jiuwenswarm.agents.harness.cua.config import CuaConfig
from tests.unit_tests.cua.support import (
    scripted_model,
    create_text_response,
    create_tool_call_response,
)


def test_disabled_and_invalid_configuration():
    assert worker.build_cua_task_tool({}, lambda: None) is None
    for raw in (
        {"enabled": "false"},
        {"capabilities": ["browser"]},
        {"timeout_s": 0},
        {"typo": True},
    ):
        with pytest.raises(ValidationError):
            CuaConfig.model_validate(raw)
    assert "click" not in CuaConfig().allowed_tools
    assert "click" in CuaConfig(capabilities=["input"]).allowed_tools
    assert "start_session" not in CuaConfig().allowed_tools


def test_structured_bounds_and_all_text_blocks_survive():
    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="first"),
            types.TextContent(type="text", text="second"),
        ],
        structuredContent={"bounds": {"x": 12}, "screenshot_png_b64": "BIG"},
    )
    output = transport.bridge_result(result, images=False, name="get_window_state")
    assert all(
        item in output.data["content"] for item in ("first", "second", '"x": 12')
    )
    assert "BIG" not in output.data["content"]
    assert result.structuredContent["screenshot_png_b64"] == "BIG"


def test_image_opt_in_and_driver_error():
    result = types.CallToolResult(
        content=[types.ImageContent(type="image", mimeType="image/png", data="YWJj")],
        isError=True,
    )
    output = transport.bridge_result(result, images=True, name="get_window_state")
    assert output.success is False
    assert output.data["multimodal"][0]["data_url"] == "data:image/png;base64,YWJj"
    assert (
        "multimodal" not in transport.bridge_result(result, images=False, name="x").data
    )


@pytest.mark.asyncio
async def test_desktop_lease_excludes_other_calls_and_releases_on_cancel(tmp_path):
    path = tmp_path / "desktop.lock"
    entered = asyncio.Event()

    async def hold():
        async with transport.desktop_lease(0, lock_path=path):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    with pytest.raises(transport.DesktopBusyError):
        async with transport.desktop_lease(0, lock_path=path):
            pytest.fail("two invocations acquired the desktop")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with transport.desktop_lease(0, lock_path=path):
        pass


def test_permissions_keep_explicit_denials_and_missing_host_rejects():
    parent = {"tools": {"mcp_cua-driver_click": "deny"}, "rules": [{"id": "deny"}]}
    result = worker.build_permissions(CuaConfig(capabilities=["input"]), parent)
    assert result["tools"]["mcp_cua-driver_click"] == "deny"
    assert "mcp_cua-driver_type_text" not in parent["tools"]
    assert result["rules"] == parent["rules"]


@pytest.mark.asyncio
async def test_no_nested_approval_is_never_auto_approved():
    assert not (await worker._reject_unhosted_confirmation(None)).approved


@pytest.mark.asyncio
async def test_execution_uses_latest_permission_snapshot(monkeypatch, tmp_path):
    session = fake_session(monkeypatch)
    model, _client = scripted_model(
        [
            create_tool_call_response("mcp_cua-driver_list_windows", "{}"),
            create_text_response("Desktop inspection was denied."),
        ]
    )
    permissions = {"tools": {"mcp_cua-driver_list_windows": "allow"}}
    tool = worker.CuaTaskTool(
        CuaConfig(enabled=True),
        lambda: model,
        workspace=str(tmp_path),
        agent_id="permission-reload",
        permissions_getter=lambda: permissions,
    )
    # A queued task must see a denial introduced after its tool was created.
    permissions["tools"]["mcp_cua-driver_list_windows"] = "deny"
    result = await tool.run("Read the desktop window titles")
    assert result["status"] == "finished", result
    calls = [call.args[0] for call in session.call_tool.await_args_list]
    assert calls == ["start_session", "end_session"]


def fake_session(monkeypatch, *, fail_start=False):
    names = [
        "list_windows",
        "get_window_state",
        "start_session",
        "end_session",
        "click",
        "browser_click",
    ]
    schemas = [
        types.Tool(
            name=name,
            inputSchema={
                "type": "object",
                "properties": {"session": {"type": "string"}},
            },
        )
        for name in names
    ]
    session = MagicMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=types.ListToolsResult(tools=schemas))

    async def call(name, args):
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="ok")],
            isError=fail_start and name == "start_session",
        )

    session.call_tool = AsyncMock(side_effect=call)

    @asynccontextmanager
    async def stdio(params):
        assert params.args == ["mcp"]
        yield None, None

    @asynccontextmanager
    async def client(*args):
        yield session

    monkeypatch.setattr(transport, "stdio_client", stdio)
    monkeypatch.setattr(transport, "ClientSession", client)
    monkeypatch.setattr(CuaConfig, "resolve_command", lambda self: "fake-driver")
    return session


@pytest.mark.asyncio
async def test_transport_enforces_catalog_session_and_cleanup(monkeypatch):
    session = fake_session(monkeypatch)
    with pytest.raises(ValueError, match="simulated"):
        async with transport.connect_desktop(CuaConfig(), "run-a") as tools:
            names = [t.card.name for t in tools]
            assert names == [
                "mcp_cua-driver_list_windows",
                "mcp_cua-driver_get_window_state",
            ]
            await tools[0].invoke({"session": "spoofed"})
            session.call_tool.assert_awaited_with("list_windows", {"session": "run-a"})
            raise ValueError("simulated")
    session.call_tool.assert_awaited_with("end_session", {"session": "run-a"})


@pytest.mark.asyncio
async def test_driver_rejection_prevents_model_run(monkeypatch):
    fake_session(monkeypatch, fail_start=True)
    with pytest.raises(RuntimeError, match="session failed"):
        async with transport.connect_desktop(CuaConfig(), "run-a"):
            pytest.fail("driver rejection ignored")


@pytest.mark.asyncio
async def test_task_reports_driver_failure_without_calling_model(monkeypatch, tmp_path):
    monkeypatch.setattr(
        CuaConfig,
        "resolve_command",
        lambda self: (_ for _ in ()).throw(FileNotFoundError("missing driver")),
    )
    tool = worker.CuaTaskTool(
        CuaConfig(enabled=True),
        lambda: object(),
        workspace=str(tmp_path),
        agent_id="test",
    )
    result = await tool.invoke({"task": "look at Calculator"})
    assert result["status"] == "error"
    assert "missing driver" in result["error"]
    assert result["completion_verified"] is False


@pytest.mark.asyncio
async def test_real_worker_dispatch_reads_structured_result(monkeypatch, tmp_path):
    session = fake_session(monkeypatch)
    model, client = scripted_model(
        [
            create_tool_call_response("mcp_cua-driver_list_windows", "{}"),
            create_text_response("Calculator is visible."),
        ]
    )
    tool = worker.CuaTaskTool(
        CuaConfig(enabled=True),
        lambda: model,
        workspace=str(tmp_path),
        agent_id="integration",
    )
    result = await tool.invoke({"task": "Which desktop app is open?"})
    assert result["status"] == "finished", result
    assert "Calculator is visible" in result["answer"]
    assert len(client.call_history) == 2
    assert any(c.args[0] == "list_windows" for c in session.call_tool.await_args_list)
    assert session.call_tool.await_args_list[-1].args[0] == "end_session"


@pytest.mark.asyncio
async def test_worker_cancellation_cleans_up(monkeypatch, tmp_path):
    fake_session(monkeypatch)
    entered = asyncio.Event()
    child = SimpleNamespace(
        cleanup_task_resources=AsyncMock(), ability_manager=MagicMock()
    )

    async def invoke(inputs):
        entered.set()
        await asyncio.Event().wait()

    child.invoke = invoke
    monkeypatch.setattr(worker, "create_worker", lambda *args: child)
    tool = worker.CuaTaskTool(
        CuaConfig(enabled=True),
        lambda: object(),
        workspace=str(tmp_path),
        agent_id="test",
    )
    job = asyncio.create_task(tool.run("test"))
    await entered.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    child.cleanup_task_resources.assert_awaited_once()
    child.ability_manager.teardown_tools.assert_called_once()


@pytest.mark.asyncio
async def test_core_agent_delegates_and_receives_desktop_answer(monkeypatch, tmp_path):
    from openjiuwen.harness.factory import create_deep_agent

    session = fake_session(monkeypatch)
    model, client = scripted_model(
        [
            create_tool_call_response(
                "cua_task", '{"task":"Read the Calculator window title"}'
            ),
            create_tool_call_response("mcp_cua-driver_list_windows", "{}"),
            create_text_response("The title is Calculator."),
            create_text_response("Your Calculator window is open."),
        ]
    )
    desktop_tool = worker.CuaTaskTool(
        CuaConfig(enabled=True),
        lambda: model,
        workspace=str(tmp_path),
        agent_id="core-test",
    )
    parent = create_deep_agent(
        model,
        tools=[desktop_tool],
        workspace=str(tmp_path / "parent"),
        max_iterations=4,
        add_general_purpose_agent=False,
        enable_read_image_multimodal=False,
    )
    try:
        answer = await parent.invoke(
            {
                "query": "Read the Calculator window title",
                "conversation_id": "core-cua-test",
            }
        )
        assert "Your Calculator" in str(answer), answer
        assert len(client.call_history) == 4
        messages = client.call_history[-1]
        assert any(
            "The title is Calculator" in str(getattr(msg, "content", ""))
            for msg in messages
        )
        assert any(
            call.args[0] == "list_windows" for call in session.call_tool.await_args_list
        ), [
            [
                (getattr(msg, "role", ""), str(getattr(msg, "content", msg))[:300])
                for msg in history
            ]
            for history in client.call_history
        ]
    finally:
        await parent.cleanup_task_resources()
        parent.ability_manager.teardown_tools()


def test_code_and_agent_builders_expose_cua_only_when_enabled(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep, interface_code

    configuration = {"cua": {"enabled": True}}
    monkeypatch.setattr(interface_deep, "get_config", lambda: configuration)
    adapter = SimpleNamespace(
        _model=object(),
        _workspace_dir=str(tmp_path),
        _resolve_runtime_language=lambda: "cn",
    )
    adapter._build_cua_task_tool = lambda agent_id: (
        interface_deep.JiuWenSwarmDeepAdapter._build_cua_task_tool(adapter, agent_id)
    )
    direct = adapter._build_cua_task_tool("agent-a")
    code = interface_code.JiuwenSwarmCodeAdapter._get_tool_build_func(
        adapter, "cua_task", "agent-b"
    )
    assert direct.card.name == code.card.name == "cua_task"
    assert direct.card.id != code.card.id
    assert direct.card.parallel_safe is False
    configuration["cua"]["enabled"] = False
    assert adapter._build_cua_task_tool("agent-a") is None


def test_adapter_reads_user_and_session_permissions_at_execution(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers

    config = {"cua": {"enabled": True}, "permissions": {"enabled": True}}
    user = {"tools": {"mcp_cua-driver_click": "deny"}}
    session = {"tools": {"mcp_cua-driver_get_window_state": "allow"}}
    monkeypatch.setattr(interface_deep, "get_config", lambda: config)
    monkeypatch.setattr(permissions_layers, "load_user_permissions", lambda: user)

    def load_session(session_id):
        assert session_id == "cua-parent-session"
        return session

    monkeypatch.setattr(permissions_layers, "load_session_permissions", load_session)
    adapter = SimpleNamespace(
        _model=object(),
        _workspace_dir=str(tmp_path),
        _parent_session_id="cua-parent-session",
        _resolve_runtime_language=lambda: "cn",
    )
    tool = interface_deep.JiuWenSwarmDeepAdapter._build_cua_task_tool(adapter, "parent")
    first = tool._permissions_getter()
    assert first["tools"]["mcp_cua-driver_click"] == "deny"
    assert first["tools"]["mcp_cua-driver_get_window_state"] == "allow"
    user["tools"]["mcp_cua-driver_type_text"] = "deny"
    second = tool._permissions_getter()
    assert second["tools"]["mcp_cua-driver_type_text"] == "deny"
    assert "mcp_cua-driver_type_text" not in first["tools"]


@pytest.mark.asyncio
async def test_real_stdio_mcp_handshake_and_structured_result(monkeypatch):
    import sys
    from pathlib import Path

    original_stdio = transport.stdio_client

    def fake_driver_stdio(params):
        assert params.args == ["mcp"]
        return original_stdio(
            params.model_copy(
                update={
                    "command": sys.executable,
                    "args": [str(Path(__file__).with_name("fake_driver.py"))],
                }
            )
        )

    monkeypatch.setattr(transport, "stdio_client", fake_driver_stdio)
    monkeypatch.setattr(CuaConfig, "resolve_command", lambda _: sys.executable)
    async with asyncio.timeout(20):
        async with transport.connect_desktop(CuaConfig(), "protocol-test") as tools:
            read = next(
                tool for tool in tools if tool.card.name.endswith("get_window_state")
            )
            result = await read.invoke({})
            assert result.success
            assert '"element_index": 0' in result.data["content"]
            assert "391" in result.data["content"]
