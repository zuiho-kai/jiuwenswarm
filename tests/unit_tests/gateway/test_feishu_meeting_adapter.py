# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Focused checks for the Feishu meeting receiver and publisher."""

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.extensions.meeting import feishu_receiver
from jiuwenswarm.extensions.meeting.feishu_publisher import FeishuMeetingPublisher
from jiuwenswarm.extensions.meeting.gateway_feishu import MeetingFeishuConnection
from jiuwenswarm.gateway.routing.keys import ChannelKey
from jiuwenswarm.gateway.channel_manager.web.task_asr import TaskAsrError


OPUS = b"OggS" + b"\0" * 24 + b"OpusHead" + b"encoded speech"


@pytest.mark.asyncio
async def test_downloads_voice_note_and_uses_shared_asr(tmp_path, monkeypatch):
    path = tmp_path / "input.opus"
    path.write_bytes(OPUS)
    files = SimpleNamespace(download_audio=AsyncMock(return_value={
        "path": str(path), "mime_type": "audio/opus",
    }))
    transcribe = AsyncMock(return_value="Leader, investigate this.")
    monkeypatch.setattr(feishu_receiver, "transcribe_task_audio", transcribe)

    receiver = feishu_receiver.FeishuMeetingReceiver(files)
    assert await receiver.receive_voice("message-1", "file-1") == "Leader, investigate this."
    files.download_audio.assert_awaited_once_with(file_key="file-1", message_id="message-1")
    transcribe.assert_awaited_once_with({
        "audio_base64": base64.b64encode(OPUS).decode("ascii"), "mime_type": "audio/ogg",
    })


@pytest.mark.asyncio
async def test_download_failure_does_not_call_asr(monkeypatch):
    files = SimpleNamespace(download_audio=AsyncMock(return_value=None))
    transcribe = AsyncMock()
    monkeypatch.setattr(feishu_receiver, "transcribe_task_audio", transcribe)

    with pytest.raises(RuntimeError, match="download failed"):
        await feishu_receiver.FeishuMeetingReceiver(files).receive_voice("message-1", "file-1")

    transcribe.assert_not_awaited()


@pytest.mark.asyncio
async def test_asr_failure_reaches_caller(tmp_path, monkeypatch):
    path = tmp_path / "input.wav"
    path.write_bytes(b"RIFF-wave-audio")
    files = SimpleNamespace(download_audio=AsyncMock(return_value={
        "path": str(path), "mime_type": "audio/wav",
    }))
    failure = TaskAsrError("ASR unavailable", "ASR_CONNECTION_ERROR")
    transcribe = AsyncMock(side_effect=failure)
    monkeypatch.setattr(feishu_receiver, "transcribe_task_audio", transcribe)

    with pytest.raises(TaskAsrError) as raised:
        await feishu_receiver.FeishuMeetingReceiver(files).receive_voice("message-1", "file-1")

    assert raised.value is failure
    assert transcribe.call_args.args[0]["mime_type"] == "audio/wav"


def make_publisher():
    client = Mock()
    client.im.v1.message.create.return_value = SimpleNamespace(
        success=lambda: True, data=SimpleNamespace(message_id="sent-1"),
    )
    files = SimpleNamespace(upload_file_resource=AsyncMock())
    return FeishuMeetingPublisher(client, files), client, files


@pytest.mark.asyncio
async def test_sends_full_text_and_preserves_retry_uuid():
    publisher, client, _ = make_publisher()

    assert await publisher.send_text("oc_group", "Complete report: café", message_uuid="retry-1") == "sent-1"

    request = client.im.v1.message.create.call_args.args[0]
    assert request.receive_id_type == "chat_id"
    assert request.request_body.receive_id == "oc_group"
    assert request.request_body.msg_type == "text"
    assert request.request_body.uuid == "retry-1"
    assert json.loads(request.request_body.content) == {"text": "Complete report: café"}


@pytest.mark.asyncio
async def test_uploads_native_audio_and_cleans_up_temporary_file():
    publisher, client, files = make_publisher()
    uploaded_paths = []

    async def upload(path):
        uploaded_paths.append(Path(path))
        assert Path(path).suffix == ".opus"
        assert Path(path).read_bytes() == OPUS
        return {"file_key": "file-speech"}

    files.upload_file_resource.side_effect = upload

    assert await publisher.send_audio("oc_group", OPUS, message_uuid="retry-audio") == "sent-1"

    request = client.im.v1.message.create.call_args.args[0]
    assert request.request_body.receive_id == "oc_group"
    assert request.request_body.msg_type == "audio"
    assert request.request_body.uuid == "retry-audio"
    assert json.loads(request.request_body.content) == {"file_key": "file-speech"}
    assert len(uploaded_paths) == 1
    assert not uploaded_paths[0].exists()


@pytest.mark.asyncio
async def test_rejects_non_opus_audio_before_upload():
    publisher, client, files = make_publisher()

    with pytest.raises(ValueError, match="Ogg/Opus"):
        await publisher.send_audio("oc_group", b"RIFF-wave-audio")

    files.upload_file_resource.assert_not_awaited()
    client.im.v1.message.create.assert_not_called()


@pytest.mark.asyncio
async def test_upload_and_message_failures_reach_caller():
    publisher, client, files = make_publisher()
    files.upload_file_resource.return_value = None

    with pytest.raises(RuntimeError, match="upload failed"):
        await publisher.send_audio("oc_group", OPUS)

    client.im.v1.message.create.assert_not_called()
    client.im.v1.message.create.return_value = SimpleNamespace(
        success=lambda: False, code=230002, msg="Bot is not in group",
    )
    with pytest.raises(RuntimeError, match="230002"):
        await publisher.send_text("oc_group", "Complete report")


@pytest.fixture
def manual_connection(monkeypatch):
    from jiuwenswarm.gateway.channel_manager.im_platforms.feishu import feishu_connect
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager

    class Channel:
        def __init__(self, config, bus):
            self.config = config
            self.app_id = config.app_id
            self.channel_id = "feishu"
            self.connection_state = "connecting"
            self.ready = asyncio.Event()
            self.stop = AsyncMock()
            self.on_message = Mock()

        async def start(self):
            await self.ready.wait()
            self.connection_state = "connected"
            await asyncio.Event().wait()

    monkeypatch.setenv("MEETING_FEISHU_APP_ID", "meeting-app")
    monkeypatch.setenv("MEETING_FEISHU_APP_SECRET", "test-secret")
    monkeypatch.delenv("MEETING_FEISHU_API_BASE", raising=False)
    monkeypatch.setattr(feishu_connect, "FEISHU_AVAILABLE", True)
    monkeypatch.setattr(feishu_connect, "FeishuChannel", Channel)
    manager = ChannelManager(SimpleNamespace())
    manager.set_conf = AsyncMock()
    return MeetingFeishuConnection(), manager


@pytest.mark.asyncio
@pytest.mark.parametrize("api_base, expected", [
    ("", "https://open.feishu.cn"),
    (" https://open.larksuite.com/ ", "https://open.larksuite.com"),
])
async def test_manual_connection_uses_saved_api_url(manual_connection, monkeypatch, api_base, expected):
    connection, manager = manual_connection
    monkeypatch.setenv("MEETING_FEISHU_API_BASE", api_base)
    pending = asyncio.create_task(connection.connect(manager))
    await asyncio.sleep(0)
    assert connection.channel.config.api_base == expected
    connection.channel.ready.set()
    assert await pending == {"state": "connected"}
    await connection.disconnect(manager)


@pytest.mark.asyncio
async def test_manual_connection_replaces_socket_when_api_url_changes(manual_connection, monkeypatch):
    connection, manager = manual_connection
    pending = asyncio.create_task(connection.connect(manager))
    await asyncio.sleep(0)
    original = connection.channel
    original.ready.set()
    await pending
    monkeypatch.setenv("MEETING_FEISHU_API_BASE", "https://open.larksuite.com")
    pending = asyncio.create_task(connection.connect(manager))
    while connection.channel is original or connection.channel is None:
        await asyncio.sleep(0)
    original.stop.assert_awaited_once()
    connection.channel.ready.set()
    assert await pending == {"state": "connected"}
    await connection.disconnect(manager)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_base", [
    "open.larksuite.com", "https://open.larksuite.com/open-apis",
    "https://open.larksuite.com?token=test", "https://user:secret@open.larksuite.com",
])
async def test_manual_connection_rejects_invalid_api_url(manual_connection, monkeypatch, api_base):
    connection, manager = manual_connection
    monkeypatch.setenv("MEETING_FEISHU_API_BASE", api_base)
    with pytest.raises(ValueError, match="API base URL"):
        await connection.connect(manager)
    assert manager.get_channels_by_id("feishu") == []


@pytest.mark.parametrize("api_base, expected", [
    ("", "https://open.feishu.cn"),
    ("https://open.larksuite.com/", "https://open.larksuite.com"),
])
def test_rest_and_websocket_clients_use_same_api_url(tmp_path, monkeypatch, api_base, expected):
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from jiuwenswarm.gateway.channel_manager.im_platforms.feishu import feishu_connect

    channel = feishu_connect.FeishuChannel(
        feishu_connect.FeishuConfig(
            app_id="test-app", app_secret="test-secret", api_base=api_base, temp_file_dir=str(tmp_path),
        ),
        RobotMessageRouter(),
    )
    channel._initialize_api_client()
    assert channel._api_client._config.domain == expected
    thread = Mock()
    monkeypatch.setattr(feishu_connect.threading, "Thread", thread)
    monkeypatch.setattr(channel, "_wait_for_websocket_client_ready", Mock())
    channel._start_websocket_in_thread()
    config = thread.call_args.kwargs["args"][0]
    socket = Mock()
    monkeypatch.setattr(feishu_connect.lark.ws, "Client", socket)
    monkeypatch.setattr(channel, "_ensure_thread_local_ws_loop_proxy", Mock())
    monkeypatch.setattr(feishu_connect.asyncio, "set_event_loop", Mock())
    channel._run_websocket_client(config)
    assert socket.call_args.kwargs["domain"] == expected


@pytest.mark.asyncio
async def test_manual_connection_waits_for_socket_and_disconnects_only_its_app(manual_connection, monkeypatch):
    from jiuwenswarm.common.security import base_crypto

    monkeypatch.setenv("MEETING_FEISHU_APP_SECRET", "encoded:test-secret")
    monkeypatch.setattr(base_crypto, "get_crypto_provider", lambda: SimpleNamespace(
        decrypt=lambda value: value.removeprefix("encoded:"),
    ))
    connection, manager = manual_connection
    other = SimpleNamespace(channel_id="feishu", app_id="other-app", on_message=Mock())
    manager.register_channel(other)
    pending = asyncio.create_task(connection.connect(manager))
    await asyncio.sleep(0)
    channel = connection.channel
    assert channel.config.app_secret == "test-secret"
    assert connection.status(manager) == {"state": "connecting"}
    assert not pending.done()
    channel.ready.set()
    assert await pending == {"state": "connected"}
    assert await connection.connect(manager) == {"state": "connected"}
    assert connection.channel is channel
    assert await connection.disconnect(manager) == {"state": "disconnected"}
    channel.stop.assert_awaited_once()
    assert manager.get_by_key(ChannelKey("feishu", "other-app")) is other
    manager.set_conf.assert_not_called()  # No persisted channel/automatic startup change.


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["failed", "timeout"])
async def test_manual_connection_failure_cleans_up_and_can_retry(manual_connection, monkeypatch, failure):
    from jiuwenswarm.extensions.meeting import gateway_feishu

    if failure == "timeout":
        monkeypatch.setattr(gateway_feishu, "CONNECT_TIMEOUT_SECONDS", 0.01)
    connection, manager = manual_connection
    pending = asyncio.create_task(connection.connect(manager))
    await asyncio.sleep(0)
    channel = connection.channel
    if failure == "failed":
        channel.connection_state = "failed"
    with pytest.raises(ValueError if failure == "failed" else TimeoutError):
        await pending
    channel.stop.assert_awaited_once()
    assert manager.get_channels_by_id("feishu") == []
    assert channel.start_task.cancelled()
    monkeypatch.setattr(gateway_feishu, "CONNECT_TIMEOUT_SECONDS", 20)
    retry = asyncio.create_task(connection.connect(manager))
    await asyncio.sleep(0)
    connection.channel.ready.set()
    assert await retry == {"state": "connected"}
    await connection.disconnect(manager)


@pytest.mark.asyncio
async def test_manual_connection_refuses_missing_credentials_and_existing_channel(manual_connection, monkeypatch):
    connection, manager = manual_connection
    monkeypatch.delenv("MEETING_FEISHU_APP_SECRET")
    with pytest.raises(ValueError, match="Configure"):
        await connection.connect(manager)
    assert manager.get_channels_by_id("feishu") == []
    monkeypatch.setenv("MEETING_FEISHU_APP_SECRET", "test-secret")
    existing = SimpleNamespace(channel_id="feishu", app_id="meeting-app", on_message=Mock())
    manager.register_channel(existing)
    with pytest.raises(ValueError, match="already running"):
        await connection.connect(manager)
    await connection.disconnect(manager)
    assert manager.get_by_key(ChannelKey("feishu", "meeting-app")) is existing


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["cancelled", "replaced"])
async def test_manual_connection_interruption_stops_socket(manual_connection, reason):
    connection, manager = manual_connection
    pending = asyncio.create_task(connection.connect(manager))
    await asyncio.sleep(0)
    channel = connection.channel
    if reason == "cancelled":
        pending.cancel()
    else:
        replacement = SimpleNamespace(channel_id="feishu", app_id="meeting-app", on_message=Mock())
        manager.register_channel(replacement)
        assert connection.status(manager) == {"state": "disconnected"}
    with pytest.raises(asyncio.CancelledError if reason == "cancelled" else ValueError):
        await pending
    channel.stop.assert_awaited_once()
    assert manager.get_channels_by_id("feishu") == ([] if reason == "cancelled" else [replacement])


def test_connection_state_requires_live_socket():
    from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import FeishuChannel

    channel = SimpleNamespace(_running=True, _websocket_client=None, _websocket_thread=None)
    assert FeishuChannel.connection_state.fget(channel) == "connecting"
    channel._websocket_client = SimpleNamespace(_conn=SimpleNamespace(closed=False))
    assert FeishuChannel.connection_state.fget(channel) == "connected"
    channel._websocket_client._conn.closed = True
    channel._websocket_thread = SimpleNamespace(is_alive=lambda: False)
    assert FeishuChannel.connection_state.fget(channel) == "failed"
    channel._running = False
    assert FeishuChannel.connection_state.fget(channel) == "disconnected"


@pytest.mark.asyncio
async def test_manual_rpc_does_not_auto_connect_or_expose_sdk_errors(manual_connection, monkeypatch):
    from jiuwenswarm.extensions.meeting.gateway_feishu import bind_feishu_handlers

    _, manager = manual_connection
    methods = {}
    web = SimpleNamespace(
        register_method=lambda name, handler, **kwargs: methods.update({name: handler}),
        send_response=AsyncMock(),
    )
    bind_feishu_handlers(web, lambda: manager)
    await methods["meeting.feishu.status"](None, "status", {}, None)
    assert web.send_response.call_args.kwargs["payload"] == {"state": "disconnected"}
    assert manager.get_channels_by_id("feishu") == []
    monkeypatch.setattr(MeetingFeishuConnection, "connect", AsyncMock(side_effect=RuntimeError("test-secret")))
    await methods["meeting.feishu.connect"](None, "connect", {}, None)
    response = web.send_response.call_args.kwargs
    assert response["ok"] is False
    assert response["code"] == "FEISHU_CONNECTION_ERROR"
    assert "test-secret" not in str(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted", [False, True])
async def test_saved_secret_is_returned_for_editing_after_reload(monkeypatch, tmp_path, encrypted):
    from dotenv import dotenv_values
    from jiuwenswarm.common.config_panel import config_set_handlers as handlers
    from jiuwenswarm.common.security import base_crypto
    from jiuwenswarm.server.control import config_service

    env_file = tmp_path / ".env"
    monkeypatch.setattr(handlers, "ENV_FILE", env_file)
    monkeypatch.setattr(handlers, "get_config_raw", lambda: {})
    monkeypatch.setattr(handlers, "get_config", lambda: {})
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: {})
    crypto = SimpleNamespace(
        encrypt=lambda value: "encoded:" + value,
        decrypt=lambda value: value.removeprefix("encoded:"),
    ) if encrypted else None
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: crypto)
    monkeypatch.setattr(base_crypto, "get_crypto_provider", lambda: crypto)
    for key in ("MEETING_FEISHU_APP_ID", "MEETING_FEISHU_APP_SECRET"):
        monkeypatch.setenv(key, "")
    for secret in ("first-secret", "replacement-secret", ""):
        handlers.apply_config_payload({
            "meeting_feishu_app_id": "meeting-app" if secret else "",
            "meeting_feishu_app_secret": secret,
            "meeting_feishu_api_base": "https://open.larksuite.com" if secret else "",
        })
        # Read persisted values as a restarted process would.
        stored = dotenv_values(env_file)
        for key, value in stored.items():
            monkeypatch.setenv(key, value or "")
        if encrypted and secret:
            assert stored["MEETING_FEISHU_APP_SECRET"] == "encoded:" + secret
        web = SimpleNamespace(send_response=AsyncMock())
        await handlers.config_get_handler(web, None, "read", {}, None)
        for panel in (web.send_response.call_args.kwargs["payload"], config_service.get_panel()):
            assert panel["meeting_feishu_app_secret"] == secret
            assert panel["meeting_feishu_api_base"] == ("https://open.larksuite.com" if secret else "")
            assert panel["meeting_feishu_configured"] == ("true" if secret else "false")
