# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit, runtime-only Feishu connections using saved meeting credentials."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from functools import partial
import os

from jiuwenswarm.gateway.routing.keys import ChannelKey

CONNECT_TIMEOUT_SECONDS = 20


class MeetingFeishuConnection:
    def __init__(self):
        self.channel = None
        self.lock = asyncio.Lock()

    def status(self, manager):
        registered = self.channel is not None and self.channel in manager.get_channels_by_id("feishu")
        return {"state": self.channel.connection_state if registered else "disconnected"}

    async def disconnect(self, manager):
        if self.channel is None:
            return self.status(manager)
        channel, self.channel = self.channel, None
        task = channel.start_task
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        await channel.stop()
        key = ChannelKey("feishu", channel.app_id)
        if manager.get_by_key(key) is channel:
            manager.unregister_channel(key)
        return {"state": "disconnected"}

    async def connect(self, manager):
        app_id = os.getenv("MEETING_FEISHU_APP_ID", "").strip()
        secret = os.getenv("MEETING_FEISHU_APP_SECRET", "").strip()
        if not app_id or not secret:
            raise ValueError("Configure the meeting Feishu App ID and App Secret first.")
        if (
            self.status(manager)["state"] == "connected"
            and self.channel.app_id == app_id
            and self.channel.config.app_secret == secret
        ):
            return self.status(manager)
        existing = manager.get_by_key(ChannelKey("feishu", app_id))
        if existing is not None and existing is not self.channel:
            raise ValueError("This app is already running in Channels > Feishu. Manage its connection there.")
        await self.disconnect(manager)

        from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
        from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import (
            FEISHU_AVAILABLE, FeishuChannel, FeishuConfig,
        )

        if not FEISHU_AVAILABLE:
            raise ValueError("The Feishu SDK is not installed on the Gateway.")
        channel = FeishuChannel(
            FeishuConfig(enabled=True, app_id=app_id, app_secret=secret, api_base="https://open.feishu.cn"),
            RobotMessageRouter(),
        )
        manager.register_channel(channel)
        self.channel = channel
        channel.start_task = asyncio.create_task(channel.start(), name="meeting-feishu")

        async def wait_connected():
            while self.status(manager)["state"] != "connected":
                if (
                    channel not in manager.get_channels_by_id("feishu")
                    or channel.start_task.done()
                    or channel.connection_state == "failed"
                ):
                    raise ValueError("Feishu connection failed. Check the app credentials and Gateway logs.")
                await asyncio.sleep(0.1)

        try:
            await asyncio.wait_for(wait_connected(), timeout=CONNECT_TIMEOUT_SECONDS)
        except BaseException:
            await self.disconnect(manager)
            raise
        return self.status(manager)


def bind_feishu_handlers(web, get_manager):
    connection = MeetingFeishuConnection()

    async def control(operation, ws, req_id, params, session_id):
        del params, session_id
        manager = get_manager()
        if manager is None:
            await web.send_response(
                ws, req_id, ok=False, error="Channel manager unavailable", code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            if operation == "status":
                result = connection.status(manager)
            else:
                async with connection.lock:
                    result = await getattr(connection, operation)(manager)
            await web.send_response(ws, req_id, ok=True, payload=result)
        except TimeoutError:
            await web.send_response(
                ws, req_id, ok=False, code="FEISHU_CONNECTION_ERROR",
                error="Feishu connection timed out. Check credentials and network access, then retry.",
            )
        except ValueError as exc:
            await web.send_response(
                ws, req_id, ok=False, error=str(exc), code="FEISHU_CONNECTION_ERROR",
            )
        except Exception:
            # SDK exceptions can include authenticated URLs; never return them to the browser.
            await web.send_response(
                ws, req_id, ok=False, code="FEISHU_CONNECTION_ERROR",
                error="Feishu connection failed. Check Gateway logs and retry.",
            )

    for operation in ("status", "connect", "disconnect"):
        web.register_method(f"meeting.feishu.{operation}", partial(control, operation), local_only=True)
