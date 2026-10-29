# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Receive Feishu group voice messages for RFC #8260 PR4.

The host supplies its existing Feishu file service and passes the message ID and
file key from an audio message. Routing and backend integration belong to the host.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import TYPE_CHECKING

from jiuwenswarm.gateway.channel_manager.web.task_asr import transcribe_task_audio

if TYPE_CHECKING:
    from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_file_service import FeishuFileService


class FeishuMeetingReceiver:
    """Download voice notes and transcribe them with the existing ASR service."""

    def __init__(self, files: FeishuFileService) -> None:
        self._files = files

    async def receive_voice(self, message_id: str, file_key: str) -> str:
        """Return one voice note's transcript using the shared ASR configuration.

        Download and transcription failures propagate to the caller. Downloaded
        files follow the supplied file service's normal storage lifecycle.
        """
        info = await self._files.download_audio(file_key=file_key, message_id=message_id)
        if not info:
            raise RuntimeError("Feishu voice-message download failed")
        audio = await asyncio.to_thread(Path(info["path"]).read_bytes)
        # Native Feishu voice notes use Ogg/Opus; ASR expects the container MIME.
        mime_type = "audio/ogg" if audio.startswith(b"OggS") else info["mime_type"]
        return await transcribe_task_audio({
            "audio_base64": base64.b64encode(audio).decode("ascii"),
            "mime_type": mime_type,
        })
