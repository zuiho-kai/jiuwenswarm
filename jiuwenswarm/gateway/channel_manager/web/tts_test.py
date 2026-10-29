"""Explicit settings test of the saved, dedicated TTS configuration."""

from __future__ import annotations

import asyncio
import base64

from jiuwenswarm.gateway.voice.tts import TtsSession


async def test_tts_handler(channel, ws, req_id, params, session_id):
    session = None
    try:
        async with asyncio.timeout(60):
            session = TtsSession()
            try:
                await session.start()
                result = await session.synthesize("this is working", session.config.voice)
            finally:
                await session.aclose()
        if not result.audio:
            raise ValueError("TTS returned no audio")
    except Exception as exc:  # noqa: BLE001
        message = str(exc).strip() or "TTS test failed or timed out"
        if session is not None and session.config.api_key:
            message = message.replace(session.config.api_key, "[redacted]")
        await channel.send_response(ws, req_id, ok=False, error=message, code="TTS_ERROR")
        return
    await channel.send_response(ws, req_id, ok=True, payload={
        "success": True,
        "audio_base64": base64.b64encode(result.audio).decode("ascii"),
        "audio_mime": result.audio_mime,
    })
