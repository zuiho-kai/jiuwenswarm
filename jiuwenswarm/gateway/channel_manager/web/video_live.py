"""Jiuwen Web RPC for short-window Qwen3-Omni audio-video Q&A."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from typing import Any


_MAX_FRAMES = 8
_MAX_FRAME_CHARS = 1_500_000
_MAX_TOTAL_FRAME_CHARS = 6_500_000
_MAX_AUDIO_CHARS = 2_000_000
_MAX_AUDIO_INPUTS = 6
_MAX_TOTAL_REQUEST_CHARS = 7_500_000
_ALLOWED_DATA_URL_PREFIXES = (
    "data:image/jpeg;base64,",
    "data:image/png;base64,",
    "data:image/webp;base64,",
)
_ALLOWED_AUDIO_MIME_TYPES = (
    "audio/webm",
    "audio/ogg",
    "audio/wav",
    "audio/mp4",
    "audio/mpeg",
)


def _is_allowed_audio_data_url(value: str) -> bool:
    header, separator, _ = value.partition(",")
    if not separator or not header.lower().startswith("data:"):
        return False
    parts = header[5:].lower().split(";")
    return (
        parts[0] in _ALLOWED_AUDIO_MIME_TYPES
        and "base64" in parts[1:]
    )


def _configured_video_model() -> tuple[str, str, str]:
    """Read Jiuwen's existing models.video configuration."""
    try:
        from jiuwenswarm.common.config import get_config

        config = get_config()
        models = config.get("models") if isinstance(config, dict) else None
        video = models.get("video") if isinstance(models, dict) else None
        client = (
            video.get("model_client_config")
            if isinstance(video, dict)
            else None
        )
        if isinstance(client, dict):
            return (
                str(client.get("api_base") or "").strip().rstrip("/"),
                str(client.get("api_key") or "").strip(),
                str(client.get("model_name") or "").strip(),
            )
    except Exception:
        pass
    return "", "", ""


def _omni_model_config() -> tuple[str, str, str]:
    configured_base, configured_key, configured_model = _configured_video_model()
    api_base = (
        configured_base
        or os.environ.get("VIDEO_API_BASE")
        or os.environ.get("VISION_API_BASE")
        or os.environ.get("API_BASE")
        or ""
    ).strip()
    api_key = (
        configured_key
        or os.environ.get("VIDEO_API_KEY")
        or os.environ.get("VISION_API_KEY")
        or os.environ.get("API_KEY")
        or "EMPTY"
    ).strip()
    model = (
        configured_model
        or os.environ.get("VIDEO_MODEL_NAME")
        or os.environ.get("OMNI_MODEL_NAME")
        or "Qwen/Qwen3-Omni-30B-A3B-Instruct"
    ).strip()
    return api_base.rstrip("/"), api_key, model


def _normalize_request(
    params: Any,
) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    if not isinstance(params, dict):
        raise ValueError("params must be object")

    question = str(params.get("question") or "").strip()
    if len(question) > 4_000:
        raise ValueError("question is too long")

    raw_audio_inputs = params.get("audio_inputs")
    if raw_audio_inputs is None:
        legacy_audio = params.get("audio_data_url")
        raw_audio_inputs = (
            [{"data_url": legacy_audio, "source_label": "用户麦克风"}]
            if legacy_audio is not None
            else []
        )
    if not isinstance(raw_audio_inputs, list):
        raise ValueError("audio_inputs must be an array")
    if len(raw_audio_inputs) > _MAX_AUDIO_INPUTS:
        raise ValueError(f"at most {_MAX_AUDIO_INPUTS} audio inputs are allowed")

    audio_inputs: list[tuple[str, str]] = []
    total_audio_chars = 0
    for audio_input in raw_audio_inputs:
        if not isinstance(audio_input, dict):
            raise ValueError("each audio input must be an object")
        data_url = audio_input.get("data_url")
        if not isinstance(data_url, str) or not _is_allowed_audio_data_url(data_url):
            raise ValueError("audio must be a WebM, OGG, WAV, MP4, or MPEG data URL")
        if len(data_url) > _MAX_AUDIO_CHARS:
            raise ValueError("an audio payload is too large")
        total_audio_chars += len(data_url)
        label = str(audio_input.get("source_label") or "视频音频").strip()[:120]
        audio_inputs.append((data_url, label or "视频音频"))
    if not question and not audio_inputs:
        raise ValueError("question or audio is required")

    raw_frames = params.get("frames")
    if not isinstance(raw_frames, list) or not raw_frames:
        raise ValueError("frames are required")
    if len(raw_frames) > _MAX_FRAMES:
        raise ValueError(f"at most {_MAX_FRAMES} frames are allowed")

    frames: list[tuple[str, str]] = []
    total_chars = 0
    for frame in raw_frames:
        if not isinstance(frame, dict):
            raise ValueError("each frame must be an object")
        data_url = frame.get("data_url")
        if not isinstance(data_url, str) or not data_url.startswith(_ALLOWED_DATA_URL_PREFIXES):
            raise ValueError("frame must be a JPEG, PNG, or WebP data URL")
        if len(data_url) > _MAX_FRAME_CHARS:
            raise ValueError("a frame is too large")
        total_chars += len(data_url)
        if total_chars > _MAX_TOTAL_FRAME_CHARS:
            raise ValueError("total frame payload is too large")
        source_label = str(frame.get("source_label") or "视频画面").strip()[:120]
        frames.append((data_url, source_label or "视频画面"))

    if total_chars + total_audio_chars > _MAX_TOTAL_REQUEST_CHARS:
        raise ValueError("combined audio and frame payload is too large")

    return question, frames, audio_inputs


async def _stream_qwen_omni(
    question: str,
    frames: list[tuple[str, str]],
    audio_inputs: list[tuple[str, str]],
) -> AsyncIterator[str]:
    from openai import AsyncOpenAI

    api_base, api_key, model = _omni_model_config()
    if not api_base:
        raise RuntimeError(
            "请先在“更多 → 配置信息 → 视频模型”中配置 api_base、api_key 和 Qwen3-Omni 模型"
        )

    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "下面是从一个或多个实时视频源中抽取的连续画面。"
                "如果包含音频，请理解用户在音频中的问题，并结合画面直接回答。"
                "不要单独输出语音转写，无法确认时明确说明。"
            ),
        }
    ]
    for frame, source_label in frames:
        content.append({"type": "text", "text": f"画面来源：{source_label}"})
        content.append({"type": "image_url", "image_url": {"url": frame}})
    for audio_data_url, source_label in audio_inputs:
        content.append({"type": "text", "text": f"音频来源：{source_label}"})
        content.append(
            {
                "type": "audio_url",
                "audio_url": {"url": audio_data_url},
            }
        )
    content.append(
        {
            "type": "text",
            "text": question or "请回答音频中提出的问题。",
        }
    )

    client = AsyncOpenAI(
        api_key=api_key or "EMPTY",
        base_url=api_base,
        timeout=90.0,
    )
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            max_tokens=256,
            temperature=0.2,
            stream=True,
        )
        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if isinstance(delta, str) and delta:
                yield delta
    finally:
        await client.close()


def register_video_live_handler(channel: Any) -> None:
    async def _video_ask(ws, req_id, params, session_id):
        del session_id
        try:
            question, frames, audio_inputs = _normalize_request(params)
        except ValueError as exc:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code="BAD_REQUEST",
            )
            return

        started_at = time.perf_counter()
        first_token_ms: int | None = None
        answer_parts: list[str] = []
        sequence = 0
        _, _, model = _omni_model_config()
        try:
            await channel.send_event(
                ws,
                "video.started",
                {"request_id": req_id, "model": model},
                stream_id=req_id,
            )
            async for delta in _stream_qwen_omni(
                question,
                frames,
                audio_inputs,
            ):
                if first_token_ms is None:
                    first_token_ms = round(
                        (time.perf_counter() - started_at) * 1000
                    )
                answer_parts.append(delta)
                sequence += 1
                await channel.send_event(
                    ws,
                    "video.delta",
                    {"request_id": req_id, "content": delta},
                    seq=sequence,
                    stream_id=req_id,
                )
        except Exception as exc:  # noqa: BLE001
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc).strip() or "Qwen3-Omni request failed",
                code="VIDEO_MODEL_ERROR",
            )
            return

        answer = "".join(answer_parts).strip()
        if not answer:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="Qwen3-Omni returned an empty response",
                code="VIDEO_MODEL_ERROR",
            )
            return

        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                "answer": answer,
                "model": model,
                "latency_ms": round((time.perf_counter() - started_at) * 1000),
                "first_token_ms": first_token_ms,
                "frame_count": len(frames),
                "has_audio": bool(audio_inputs),
                "audio_count": len(audio_inputs),
            },
        )

    channel.register_method("video.ask", _video_ask)
