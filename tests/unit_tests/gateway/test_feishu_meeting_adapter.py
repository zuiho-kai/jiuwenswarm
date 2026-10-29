# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Focused checks for the Feishu meeting receiver and publisher."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.extensions.meeting import feishu_receiver
from jiuwenswarm.extensions.meeting.feishu_publisher import FeishuMeetingPublisher
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
