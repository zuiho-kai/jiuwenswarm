"""Run-scoped MCP connection and structured-result bridge.

The pinned SDK already accepts multimodal tool results but does not read MCP
structuredContent. Adapt only CUA results here; do not patch global MCP clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import portalocker
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.foundation.tool.mcp.base import (
    McpToolResult,
    extract_mcp_tool_result_content,
    mcp_model_tool_name,
)

from .config import CuaConfig

logger = logging.getLogger(__name__)
SERVER_NAME = "cua-driver"


class DesktopBusyError(RuntimeError):
    """Another Jiuwen CUA invocation owns this user's desktop."""


@asynccontextmanager
async def desktop_lease(wait_s: int, *, lock_path: Path | None = None):
    # One file per OS user, shared by processes, sessions and checkouts. Kernel
    # locks are reclaimed on process exit; never unlink a possibly locked file.
    path = lock_path or Path.home() / ".jiuwenswarm/runtime/cua-desktop.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                break
            except portalocker.exceptions.LockException:
                if time.monotonic() >= deadline:
                    raise DesktopBusyError(
                        "Another CUA task is using this desktop; retry after it finishes."
                    )
                await asyncio.sleep(0.1)
        try:
            yield
        finally:
            portalocker.unlock(handle)


def _without_image_bytes(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[image supplied separately]"
            if key in {"screenshot_png_b64", "image_base64"}
            else _without_image_bytes(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_image_bytes(item) for item in value]
    return value


def bridge_result(
    result: types.CallToolResult, *, images: bool, name: str
) -> McpToolResult:
    blocks = list(result.content)
    if result.structuredContent is not None:
        blocks.append(
            types.TextContent(
                type="text",
                text=json.dumps(
                    _without_image_bytes(result.structuredContent), ensure_ascii=False
                ),
            )
        )
    copied = result.model_copy(update={"content": blocks})
    extracted = extract_mcp_tool_result_content(
        copied, include_image_content=images, tool_name=name
    )
    output = (
        extracted
        if isinstance(extracted, McpToolResult)
        else McpToolResult(
            data={
                "content": extracted
                if extracted is not None
                else "[empty driver result]"
            }
        )
    )
    output.success = not result.isError
    if result.isError:
        output.error = str(output.data.get("content", "CUA driver error"))
    return output


class DesktopTool(Tool):
    def __init__(
        self, schema: types.Tool, session: ClientSession, run_id: str, config: CuaConfig
    ):
        parameters = dict(schema.inputSchema)
        parameters["properties"] = dict(parameters.get("properties", {}))
        self._accepts_session = "session" in parameters["properties"]
        parameters["properties"].pop("session", None)
        parameters["required"] = [
            key for key in parameters.get("required", []) if key != "session"
        ]
        super().__init__(
            ToolCard(
                id=f"{run_id}.{schema.name}",
                parallel_safe=False,
                idempotent=False,
                name=mcp_model_tool_name(SERVER_NAME, schema.name),
                description=schema.description or schema.name,
                input_params=parameters,
            )
        )
        self._name, self._session, self._run_id, self._config = (
            schema.name,
            session,
            run_id,
            config,
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> McpToolResult:
        arguments = dict(inputs)
        arguments.pop("session", None)
        if self._accepts_session:
            arguments["session"] = self._run_id
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(self._name, arguments),
                self._config.tool_timeout_s,
            )
            return bridge_result(
                result, images=self._config.screenshot_multimodal, name=self._name
            )
        finally:
            logger.info(
                "[CUA] run=%s tool=%s elapsed_ms=%.0f",
                self._run_id,
                self._name,
                (time.monotonic() - started) * 1000,
            )

    async def stream(self, inputs: dict[str, Any], **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)


@asynccontextmanager
async def connect_desktop(config: CuaConfig, run_id: str):
    params = StdioServerParameters(
        command=config.resolve_command(), args=["mcp"], env=dict(os.environ)
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            schemas = []
            cursor = None
            while True:
                page = await session.list_tools(cursor=cursor)
                schemas.extend(page.tools)
                cursor = page.nextCursor
                if not cursor:
                    break
            names = {schema.name for schema in schemas}
            if (
                not {"start_session", "end_session", "get_window_state", "list_windows"}
                <= names
            ):
                raise RuntimeError(
                    "CUA driver has an incompatible tool catalog (requires cua-driver 0.10)."
                )
            started = await session.call_tool("start_session", {"session": run_id})
            if started.isError:
                raise RuntimeError(
                    f"CUA session failed: {bridge_result(started, images=False, name='start_session').data}"
                )
            try:
                allowed = set(config.allowed_tools)
                yield [
                    DesktopTool(schema, session, run_id, config)
                    for schema in schemas
                    if schema.name in allowed
                ]
            finally:
                try:
                    await asyncio.wait_for(
                        session.call_tool("end_session", {"session": run_id}), 5
                    )
                except Exception:
                    logger.warning(
                        "[CUA] run=%s end_session failed; driver TTL will reclaim it",
                        run_id,
                    )
