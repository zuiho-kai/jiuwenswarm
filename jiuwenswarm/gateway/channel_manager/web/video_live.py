"""Jiuwen Web RPC for Qwen3-Omni video Q&A with optional memory tools."""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_MAX_FRAMES = 8
_MAX_FRAME_CHARS = 1_500_000
_MAX_TOTAL_FRAME_CHARS = 6_500_000
_MAX_AUDIO_CHARS = 2_000_000
_MAX_AUDIO_INPUTS = 6
_MAX_TOTAL_REQUEST_CHARS = 7_500_000
_MAX_CURRENT_CHUNK_FRAMES = 100
_MAX_CURRENT_CHUNK_BYTES = 25 * 1024 * 1024
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
_memory_client = None
MemorySearch = Callable[
    [dict[str, object]],
    Awaitable[dict[str, object]],
]
_MEMORY_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "memory_search",
        "description": (
            "查询当前视频会话中没有完整出现在中期上下文里的历史事件。"
            "涉及之前、多久前、什么时候、物品去向或具体剧情时使用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "start_at": {
                    "type": "string",
                    "description": "可选，带时区的 ISO 时间",
                },
                "end_at": {
                    "type": "string",
                    "description": "可选，带时区的 ISO 时间",
                },
                "entities": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        },
    },
}


def _omnimemory_client():
    global _memory_client
    api_base = os.environ.get("OMNIMEMORY_API_BASE", "").strip().rstrip("/")
    if not api_base:
        return None
    from .omnimemory_live import OmniMemoryLiveClient

    if _memory_client is None or _memory_client.api_base != api_base:
        _memory_client = OmniMemoryLiveClient(api_base)
    return _memory_client


def _is_allowed_audio_data_url(value: str) -> bool:
    header, separator, _ = value.partition(",")
    if not separator or not header.lower().startswith("data:"):
        return False
    parts = header[5:].lower().split(";")
    return (
        parts[0] in _ALLOWED_AUDIO_MIME_TYPES
        and "base64" in parts[1:]
    )


def _current_chunk_frames(
    memory_context: dict[str, object] | None,
) -> list[tuple[str, str]]:
    if memory_context is None:
        return []
    observations = memory_context.get("current_observations")
    if not isinstance(observations, list):
        return []

    frames: list[tuple[str, str]] = []
    total_bytes = 0
    for observation in reversed(observations):
        if not isinstance(observation, dict):
            continue
        if observation.get("modality") != "image":
            continue
        data_ref = observation.get("data_ref")
        if not isinstance(data_ref, str):
            continue
        path = Path(data_ref)
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if not content or len(content) > _MAX_FRAME_CHARS:
            continue
        if total_bytes + len(content) > _MAX_CURRENT_CHUNK_BYTES:
            break
        metadata = observation.get("metadata")
        mime_type = (
            metadata.get("media_type")
            if isinstance(metadata, dict)
            else None
        )
        if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            continue
        total_bytes += len(content)
        source_id = str(observation.get("source_id") or "video")
        timestamp = str(observation.get("timestamp") or "")
        frames.append(
            (
                f"data:{mime_type};base64,"
                + base64.b64encode(content).decode("ascii"),
                f"当前 chunk：{source_id} @ {timestamp}",
            )
        )
        if len(frames) >= _MAX_CURRENT_CHUNK_FRAMES:
            break
    frames.reverse()
    return frames


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
    *,
    memory_context: dict[str, object] | None = None,
    memory_search: MemorySearch | None = None,
) -> AsyncIterator[str]:
    from openai import AsyncOpenAI

    api_base, api_key, model = _omni_model_config()
    if not api_base:
        raise RuntimeError(
            "请先在“更多 → 配置信息 → 视频模型”中配置 api_base、api_key 和 Qwen3-Omni 模型"
        )

    memory_available = memory_context is not None
    context_payload = memory_context or {
        "available": False,
        "reason": "OmniMemory is not configured or unavailable",
    }
    system_prompt = (
        "你是实时视频助手。优先根据当前画面和音频回答当前状态问题。"
        "Memory Context 分为 long_term_memory、mid_term_memories、"
        "current_chunk 和 qa_history。长期和中期内容可能是有损摘要。"
        "涉及之前、多久前、什么时候、物品去向、具体历史剧情，或者现有"
        "上下文不足时，必须调用一次 memory_search；不要凭空补全。"
        "工具失败时明确说历史记忆不可用。"
        f"\nMemory Context:\n{json.dumps(context_payload, ensure_ascii=False)}"
    )
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "下面是当前时刻从一个或多个实时视频源中抽取的连续画面。"
                "如果包含音频，请理解用户在音频中的问题，并结合画面直接回答。"
                "不要单独输出语音转写，无法确认时明确说明。"
            ),
        }
    ]
    for frame, source_label in _current_chunk_frames(memory_context):
        content.append({"type": "text", "text": source_label})
        content.append({"type": "image_url", "image_url": {"url": frame}})
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
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ]
        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": 256,
            "temperature": 0.2,
            "stream": False,
        }
        if memory_search is not None:
            request["tools"] = [_MEMORY_SEARCH_TOOL]
            request["tool_choice"] = "auto"
        response = await client.chat.completions.create(**request)
        if not response.choices:
            raise RuntimeError("Qwen3-Omni returned no choices")
        message = response.choices[0].message
        tool_calls = list(message.tool_calls or [])
        if tool_calls and memory_search is not None:
            tool_call = tool_calls[0]
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                tool_result = await memory_search(arguments)
            except Exception as exc:  # noqa: BLE001
                tool_result = {
                    "error": str(exc).strip() or "memory search failed"
                }
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [
                            {
                                "id": tool_call.id,
                                "type": "function",
                                "function": {
                                    "name": "memory_search",
                                    "arguments": tool_call.function.arguments,
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(
                            tool_result,
                            ensure_ascii=False,
                        ),
                    },
                ]
            )
            streamed = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=256,
                temperature=0.2,
                stream=True,
            )
            async for chunk in streamed:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if isinstance(delta, str) and delta:
                    yield delta
            return

        answer = message.content
        if isinstance(answer, str) and answer.strip():
            yield answer
            return
        if tool_calls and not memory_available:
            raise RuntimeError("historical memory is unavailable")
        raise RuntimeError("Qwen3-Omni returned an empty response")
    finally:
        await client.close()


def register_video_live_handler(channel: Any) -> None:
    async def _video_memory_observe(ws, req_id, params, session_id):
        client = _omnimemory_client()
        if client is None:
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload={"enabled": False, "accepted": 0},
            )
            return
        try:
            from .omnimemory_live import normalize_memory_frames

            frames = normalize_memory_frames(params)
            observation_ids = await client.observe(session_id, frames)
        except ValueError as exc:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code="BAD_REQUEST",
            )
            return
        except Exception as exc:  # noqa: BLE001
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc).strip() or "OmniMemory ingestion failed",
                code="OMNIMEMORY_ERROR",
            )
            return
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                "enabled": True,
                "accepted": len(observation_ids),
                "observation_ids": observation_ids,
            },
        )

    async def _video_ask(ws, req_id, params, session_id):
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
        memory_client = _omnimemory_client()
        memory_context: dict[str, object] | None = None
        memory_search: MemorySearch | None = None
        memory_trace: dict[str, object] = {}
        if memory_client is not None:
            try:
                memory_context = await memory_client.context(session_id)
            except Exception as exc:  # noqa: BLE001
                memory_context = {
                    "available": False,
                    "error": str(exc).strip() or "memory context failed",
                }

            async def _search(arguments: dict[str, object]):
                tool_record: dict[str, object] = {
                    "name": "memory_search",
                    "arguments": arguments,
                    "result_memory_ids": [],
                    "result_summaries": [],
                    "evidence": [],
                    "error": None,
                }
                memory_trace.setdefault("tool_calls", []).append(
                    tool_record
                )
                try:
                    result = await memory_client.search(
                        session_id,
                        arguments,
                    )
                except Exception as exc:  # noqa: BLE001
                    tool_record["error"] = (
                        str(exc).strip() or "memory search failed"
                    )
                    raise
                memories = result.get("memories")
                if isinstance(memories, list):
                    tool_record["result_memory_ids"] = [
                        memory.get("id")
                        for memory in memories
                        if isinstance(memory, dict)
                        and isinstance(memory.get("id"), str)
                    ]
                    tool_record["result_summaries"] = [
                        memory.get("summary")
                        for memory in memories
                        if isinstance(memory, dict)
                        and isinstance(memory.get("summary"), str)
                    ]
                evidence = result.get("evidence")
                if isinstance(evidence, list):
                    tool_record["evidence"] = evidence
                memory_trace["search_result"] = result
                return result

            memory_search = _search

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
                memory_context=memory_context,
                memory_search=memory_search,
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

        writeback: dict[str, object] | None = None
        if memory_client is not None:
            raw_frames = (
                params.get("frames")
                if isinstance(params, dict)
                else None
            )
            source_ids = list(
                dict.fromkeys(
                    str(frame.get("source_id"))
                    for frame in raw_frames or []
                    if isinstance(frame, dict)
                    and isinstance(frame.get("source_id"), str)
                    and str(frame.get("source_id")).strip()
                )
            )
            current_chunk = (
                memory_context.get("current_chunk")
                if isinstance(memory_context, dict)
                else None
            )
            current_observations = (
                current_chunk.get("observations")
                if isinstance(current_chunk, dict)
                else []
            )
            mid_term_memories = (
                memory_context.get("mid_term_memories")
                if isinstance(memory_context, dict)
                else []
            )
            record = {
                "question": question,
                "answer": answer,
                "asked_at": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "request_id": req_id,
                "source_ids": source_ids,
                "current_observation_ids": [
                    observation.get("id")
                    for observation in current_observations or []
                    if isinstance(observation, dict)
                    and isinstance(observation.get("id"), str)
                ],
                "context_memory_ids": [
                    memory.get("id")
                    for memory in mid_term_memories or []
                    if isinstance(memory, dict)
                    and isinstance(memory.get("id"), str)
                ],
                "tool_calls": memory_trace.get("tool_calls", []),
            }
            try:
                written = await memory_client.write_interaction(
                    session_id,
                    record,
                )
                writeback = {
                    "ok": True,
                    "interaction_id": written.get("id"),
                }
            except Exception as exc:  # noqa: BLE001
                writeback = {
                    "ok": False,
                    "error": str(exc).strip()
                    or "interaction writeback failed",
                }

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
                "memory_context_loaded": (
                    memory_context is not None
                    and memory_context.get("available") is not False
                ),
                "memory_search_result": memory_trace.get("search_result"),
                "memory_writeback": writeback,
            },
        )

    channel.register_method("video.memory.observe", _video_memory_observe)
    channel.register_method("video.ask", _video_ask)
