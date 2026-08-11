from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web import video_live
from jiuwenswarm.gateway.channel_manager.web.web_connect import (
    _LOCAL_ONLY_METHODS,
    WebChannel,
    WebChannelConfig,
)


def _frame() -> dict[str, object]:
    return {
        "data_url": "data:image/jpeg;base64,eA==",
        "captured_at": 1_785_464_910_000,
        "source_id": "camera",
        "source_label": "摄像头",
        "width": 768,
        "height": 432,
    }


class _Channel:
    def __init__(self) -> None:
        self.methods: dict[str, Any] = {}
        self.responses: list[dict[str, Any]] = []
        self.disconnect_hooks: list[Any] = []

    def register_method(self, name: str, handler: Any) -> None:
        self.methods[name] = handler

    def on_disconnect(self, callback: Any) -> None:
        self.disconnect_hooks.append(callback)

    async def send_response(self, ws, req_id, **response: Any) -> None:
        del ws
        self.responses.append({"id": req_id, **response})


@pytest.fixture(autouse=True)
def _disable_monitor_diagnostic_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        video_live,
        "_write_monitor_diagnostic",
        lambda _event, **_fields: None,
    )


def test_parse_monitor_decision_accepts_fenced_json() -> None:
    decision = video_live._parse_monitor_decision(
        """```json
        {
          "decision": "emit",
          "response": "Checkout 翻译为结账。",
          "working_memory": "最近确认的字幕是 Checkout"
        }
        ```"""
    )

    assert decision == {
        "decision": "emit",
        "response": "Checkout 翻译为结账。",
        "working_memory": "最近确认的字幕是 Checkout",
    }


def test_parse_monitor_intent_accepts_fenced_json() -> None:
    intent = video_live._parse_monitor_intent_response(
        """```json
        {
          "action": "start_monitor",
          "instruction": "有人进入画面时立即告诉我",
          "confidence": 0.96
        }
        ```""",
        "有人来了告诉我",
    )

    assert intent == {
        "action": "start_monitor",
        "instruction": "有人进入画面时立即告诉我",
        "confidence": 0.96,
    }


def test_parse_monitor_intent_keeps_chat_separate() -> None:
    assert video_live._parse_monitor_intent_response(
        '{"action":"chat","instruction":"不应保留","confidence":2}',
        "现在画面里有人吗？",
    ) == {
        "action": "chat",
        "instruction": "",
        "confidence": 1.0,
    }


@pytest.mark.asyncio
async def test_monitor_intent_handler_returns_model_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent_logs: list[dict[str, object]] = []

    async def classify(content, conversation_context=None):
        assert content == "有人来了告诉我"
        assert conversation_context == [
            {"role": "assistant", "content": "我会观察当前画面"}
        ]
        return {
            "action": "start_monitor",
            "instruction": "有人进入画面时立即告诉我",
            "confidence": 0.95,
            "model": "main-model",
        }

    monkeypatch.setattr(video_live, "_classify_monitor_intent", classify)
    monkeypatch.setattr(
        video_live,
        "_write_monitor_intent_log",
        lambda **fields: intent_logs.append(fields),
    )
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.monitor.intent"](
        object(),
        "intent-1",
        {
            "content": "有人来了告诉我",
            "recent_context": [
                {"role": "assistant", "content": "我会观察当前画面"}
            ],
        },
        "session-1",
    )

    assert channel.responses[-1] == {
        "id": "intent-1",
        "ok": True,
        "payload": {
            "action": "start_monitor",
            "instruction": "有人进入画面时立即告诉我",
            "confidence": 0.95,
            "model": "main-model",
        },
    }
    assert intent_logs == [
        {
            "request_id": "intent-1",
            "session_id": "session-1",
            "content": "有人来了告诉我",
            "recent_context_count": 1,
            "action": "start_monitor",
            "instruction": "有人进入画面时立即告诉我",
            "confidence": 0.95,
            "model": "main-model",
            "classifier_error": "",
        }
    ]


def test_write_monitor_intent_log_appends_jsonl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(video_live, "get_logs_dir", lambda: tmp_path)

    video_live._write_monitor_intent_log(
        request_id="intent-2",
        session_id="session-2",
        content="持续翻译画面",
        recent_context_count=2,
        action="start_monitor",
        instruction="持续翻译画面中的英文",
        confidence=0.91,
        model="intent-model",
        classifier_error="",
    )

    records = [
        json.loads(line)
        for line in (tmp_path / "video-monitor-intents.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(records) == 1
    assert records[0]["request_id"] == "intent-2"
    assert records[0]["content"] == "持续翻译画面"
    assert records[0]["action"] == "start_monitor"
    assert records[0]["timestamp"]


def test_video_stream_realtime_url_accepts_explicit_ws_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_REALTIME_PROTOCOL", "vllm_video_stream")
    monkeypatch.setenv(
        "VIDEO_REALTIME_URL",
        "ws://127.0.0.1:18001/v1/video/chat/stream",
    )

    assert video_live._video_stream_realtime_ws_url(
        "http://127.0.0.1:18001/v1",
        "openbmb/MiniCPM-o-4_5",
    ) == "ws://127.0.0.1:18001/v1/video/chat/stream"


def test_video_stream_realtime_url_derives_from_vllm_http_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_REALTIME_PROTOCOL", raising=False)
    monkeypatch.delenv("VIDEO_REALTIME_URL", raising=False)

    assert video_live._video_stream_realtime_ws_url(
        "http://127.0.0.1:18001/v1",
        "openbmb/MiniCPM-o-4_5",
    ) == "ws://127.0.0.1:18001/v1/video/chat/stream"
    assert (
        video_live._video_stream_realtime_ws_url(
            "https://example.com/v1",
            "text-only-model",
        )
        is None
    )


def test_strip_hidden_reasoning_keeps_only_final_answer() -> None:
    assert video_live._strip_hidden_reasoning(
        "<think>private reasoning</think>\n\nThe rectangle is red."
    ) == "The rectangle is red."


@pytest.mark.asyncio
async def test_video_transcribe_accepts_audio_without_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[tuple[str, str]]] = []

    async def transcribe(inputs: list[tuple[str, str]]) -> str:
        captured.append(inputs)
        return "show the current frame"

    monkeypatch.setattr(video_live, "_transcribe_audio_inputs", transcribe)
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.transcribe"](
        object(),
        "transcribe-1",
        {
            "audio_inputs": [
                {
                    "data_url": "data:audio/wav;base64,eA==",
                    "source_label": "\u7528\u6237\u9ea6\u514b\u98ce\u63d0\u95ee",
                }
            ]
        },
        "voice-session",
    )

    assert captured == [
        [
            (
                "data:audio/wav;base64,eA==",
                "\u7528\u6237\u9ea6\u514b\u98ce\u63d0\u95ee",
            )
        ]
    ]
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"] == {
        "transcript": "show the current frame",
        "accepted": True,
    }


@pytest.mark.asyncio
async def test_video_transcribe_reports_empty_asr_exception_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyAsrError(Exception):
        pass

    async def transcribe(_inputs: list[tuple[str, str]]) -> str:
        raise EmptyAsrError

    monkeypatch.setattr(video_live, "_transcribe_audio_inputs", transcribe)
    monkeypatch.setattr(
        video_live,
        "_asr_model_config",
        lambda: ("http://127.0.0.1:18002/v1", "EMPTY", "whisper"),
    )
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.transcribe"](
        object(),
        "transcribe-error",
        {
            "audio_inputs": [
                {
                    "data_url": "data:audio/wav;base64,eA==",
                    "source_label": "\u7528\u6237\u9ea6\u514b\u98ce\u63d0\u95ee",
                }
            ]
        },
        "voice-session",
    )

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["error"] == "ASR \u8bf7\u6c42\u5931\u8d25 (EmptyAsrError)"


def test_video_question_still_requires_frames() -> None:
    with pytest.raises(ValueError, match="frames are required"):
        video_live._normalize_request({"question": "show the current frame"})


@pytest.mark.asyncio
async def test_vllm_video_stream_session_sends_frames_and_reads_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.events = asyncio.Queue()
            self.events.put_nowait(
                json.dumps(
                    {
                        "type": "video.frame.ack",
                        "frame_id": "frame-1",
                        "accepted": True,
                    }
                )
            )
            self.events.put_nowait(
                json.dumps({"type": "response.text.delta", "delta": "<think>x</think>"})
            )
            self.events.put_nowait(
                json.dumps(
                    {
                        "type": "response.text.done",
                        "text": "<think>x</think>\n当前画面是一只杯子。",
                    },
                    ensure_ascii=False,
                )
            )

        async def send(self, payload: str) -> None:
            self.sent.append(json.loads(payload))

        async def recv(self) -> str:
            return await self.events.get()

        async def close(self) -> None:
            return None

    websocket = _WebSocket()

    async def connect(*_args: object, **_kwargs: object) -> _WebSocket:
        return websocket

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    session = video_live._VllmVideoStreamSession(
        "ws://127.0.0.1:18001/v1/video/chat/stream",
        "EMPTY",
        "openbmb/MiniCPM-o-4_5",
    )
    try:
        answer = await session.ask(
            system_prompt="只根据画面回答。",
            question="这是什么？",
            frames=[("data:image/jpeg;base64,eA==", "摄像头")],
            audio_inputs=[],
        )
    finally:
        await session.close()

    assert answer == "当前画面是一只杯子。"
    assert [item["type"] for item in websocket.sent] == [
        "session.config",
        "video.frame",
        "video.query",
    ]
    assert websocket.sent[0]["model"] == "openbmb/MiniCPM-o-4_5"


@pytest.mark.asyncio
async def test_vllm_video_stream_session_updates_prompt_without_reconnecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_count = 0
    close_count = 0

    class _WebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.events: asyncio.Queue[str] = asyncio.Queue()

        async def send(self, payload: str) -> None:
            event = json.loads(payload)
            self.sent.append(event)
            if event["type"] == "video.query":
                await self.events.put(
                    json.dumps({"type": "response.text.done", "text": "ok"})
                )

        async def recv(self) -> str:
            return await self.events.get()

        async def close(self) -> None:
            nonlocal close_count
            close_count += 1

    websocket = _WebSocket()

    async def connect(*_args: object, **_kwargs: object) -> _WebSocket:
        nonlocal connect_count
        connect_count += 1
        return websocket

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    session = video_live._VllmVideoStreamSession(
        "ws://127.0.0.1:18001/v1/video/chat/stream",
        "EMPTY",
        "openbmb/MiniCPM-o-4_5",
    )

    await session.start("idle prompt")
    assert await session.ask(
        system_prompt="question prompt",
        question="What is visible?",
        frames=[("data:image/jpeg;base64,eA==", "camera")],
        audio_inputs=[],
    ) == "ok"
    assert await session.ask(
        system_prompt="monitor prompt",
        question="Return JSON.",
        frames=[("data:image/jpeg;base64,eA==", "camera")],
        audio_inputs=[],
    ) == "ok"

    assert connect_count == 1
    assert close_count == 0
    assert [event["type"] for event in websocket.sent].count("session.config") == 3
    assert session.turn_count == 2

    await session.close()
    assert close_count == 1


@pytest.mark.asyncio
async def test_video_realtime_start_preconnects_one_socket_per_web_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_count = 0
    close_count = 0

    class _WebSocket:
        async def send(self, _payload: str) -> None:
            return None

        async def close(self) -> None:
            nonlocal close_count
            close_count += 1

    async def connect(*_args: object, **_kwargs: object) -> _WebSocket:
        nonlocal connect_count
        connect_count += 1
        return _WebSocket()

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: (
            "http://127.0.0.1:18001/v1",
            "EMPTY",
            "openbmb/MiniCPM-o-4_5",
        ),
    )
    monkeypatch.setattr(
        video_live,
        "_video_stream_realtime_ws_url",
        lambda _base, _model: (
            "ws://127.0.0.1:18001/v1/video/chat/stream"
        ),
    )
    channel = _Channel()
    video_live.register_video_live_handler(channel)
    ws = object()

    await channel.methods["video.realtime.start"](ws, "start-1", {}, "session")
    await channel.methods["video.realtime.start"](ws, "start-2", {}, "session")

    assert connect_count == 1
    assert channel.responses[-1]["payload"] == {
        "enabled": True,
        "connected": True,
        "model": "openbmb/MiniCPM-o-4_5",
        "protocol": "vllm_video_stream",
    }

    await channel.methods["video.realtime.stop"](ws, "stop", {}, "session")
    assert close_count == 1


def test_monitor_decision_parser_preserves_long_responses() -> None:
    response = "Long response. " * 300
    parsed = video_live._parse_monitor_decision(
        json.dumps(
            {
                "decision": "emit",
                "response": response,
                "working_memory": "",
            }
        )
    )
    assert len(response) > 2_000
    assert parsed["response"] == response.strip()


@pytest.mark.parametrize(
    "payload",
    [
        '{"decision":"emit","response":"","working_memory":""}',
        '{"decision":"unknown","response":"","working_memory":""}',
        '{"decision":"hold","response":""}',
        '{"decision":"hold","response":"","working_memory":"","event_key":"extra"}',
    ],
)
def test_parse_monitor_decision_rejects_invalid_contract(payload: str) -> None:
    with pytest.raises(ValueError):
        video_live._parse_monitor_decision(payload)


def test_monitor_event_identity_uses_normalized_response() -> None:
    welcome = video_live._monitor_event_identity("Welcome!")
    same_welcome = video_live._monitor_event_identity("  welcome  ")
    checkout = video_live._monitor_event_identity("Checkout")

    assert welcome == same_welcome
    assert welcome != checkout
    assert welcome.startswith("monitor_event:")
    assert len(welcome.rsplit(":", 1)[1]) == 16


def test_monitor_event_state_suppresses_active_duplicate() -> None:
    state: dict[str, object] = {"sequence": 0, "active": None}
    first, first_metadata = video_live._advance_monitor_event_state(
        state,
        {"decision": "emit", "response": "Welcome => 欢迎"},
        1_000,
        "run-1",
    )
    duplicate, duplicate_metadata = video_live._advance_monitor_event_state(
        state,
        {"decision": "emit", "response": " welcome => 欢迎。 "},
        20_000,
        "run-1",
    )

    assert first["display_action"] == "append"
    assert first["occurrence_id"] == "run-1:1"
    assert first_metadata["display_action"] == "append"
    assert duplicate == {"decision": "hold", "response": ""}
    assert duplicate_metadata["suppression_reason"] == "active_event_duplicate"
    assert duplicate_metadata["duplicate_event_age_ms"] == 19_000


def test_monitor_event_state_replaces_incremental_response() -> None:
    state: dict[str, object] = {"sequence": 0, "active": None}
    first, _ = video_live._advance_monitor_event_state(
        state,
        {"decision": "emit", "response": "你需要一个笔记本"},
        1_000,
        "run-1",
    )
    revised, metadata = video_live._advance_monitor_event_state(
        state,
        {"decision": "emit", "response": "你需要一个笔记本和一支笔"},
        2_000,
        "run-1",
    )

    assert revised["display_action"] == "replace"
    assert revised["occurrence_id"] == first["occurrence_id"]
    assert revised["event_key"] == first["event_key"]
    assert metadata["display_action"] == "replace"


def test_monitor_event_state_allows_similar_numeric_updates() -> None:
    state: dict[str, object] = {"sequence": 0, "active": None}
    responses = [
        "这个人进行了30次锻炼动作",
        "这个人进行了36次锻炼动作",
        "这个人进行了38次锻炼动作",
    ]
    results = [
        video_live._advance_monitor_event_state(
            state,
            {"decision": "emit", "response": response},
            1_000 + index * 1_000,
            "run-1",
        )[0]
        for index, response in enumerate(responses)
    ]

    assert [result["display_action"] for result in results] == [
        "append",
        "append",
        "append",
    ]
    assert [result["occurrence_id"] for result in results] == [
        "run-1:1",
        "run-1:2",
        "run-1:3",
    ]


def test_monitor_event_state_allows_similar_parallel_responses() -> None:
    state: dict[str, object] = {"sequence": 0, "active": None}
    first, _ = video_live._advance_monitor_event_state(
        state,
        {"decision": "emit", "response": "I need patience => 我需要耐心"},
        1_000,
        "run-1",
    )
    second, metadata = video_live._advance_monitor_event_state(
        state,
        {"decision": "emit", "response": "I need courage => 我需要勇气"},
        2_000,
        "run-1",
    )

    assert first["display_action"] == "append"
    assert second["display_action"] == "append"
    assert second["occurrence_id"] == "run-1:2"
    assert metadata["suppression_reason"] == ""


def test_monitor_event_state_allows_a_b_a_occurrences() -> None:
    state: dict[str, object] = {"sequence": 0, "active": None}
    results = [
        video_live._advance_monitor_event_state(
            state,
            {"decision": "emit", "response": response},
            timestamp,
            "run-1",
        )[0]
        for response, timestamp in (("A", 1_000), ("B", 2_000), ("A", 3_000))
    ]

    assert [result["display_action"] for result in results] == [
        "append",
        "append",
        "append",
    ]
    assert [result["occurrence_id"] for result in results] == [
        "run-1:1",
        "run-1:2",
        "run-1:3",
    ]


def test_monitor_runtime_context_tracks_elapsed_and_display_time() -> None:
    state: dict[str, object] = {
        "started_at_ms": 1_000,
        "turn_index": 4,
        "last_turn_completed_at_ms": 8_500,
        "last_displayed_at_ms": 5_000,
    }

    context = video_live._monitor_runtime_context(
        state,
        11_000,
        {
            "frame_span_ms": 2_000,
            "frames": [{"age_ms": 2_100}, {"age_ms": 100}],
        },
    )

    assert context == {
        "clock_unit": "milliseconds",
        "turn_index": 5,
        "monitor_elapsed_ms": 10_000,
        "since_previous_turn_ms": 2_500,
        "last_displayed_at_elapsed_ms": 4_000,
        "since_last_displayed_ms": 6_000,
        "observation_frame_span_ms": 2_000,
        "observation_frame_ages_ms": [2_100, 100],
    }


@pytest.mark.asyncio
async def test_joyai_chat_completion_uses_unified_json_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _RawResponse:
        content = b'{"choices":[]}'

        @staticmethod
        def parse() -> object:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        index=0,
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "decision": "emit",
                                    "response": "New line => 新的一行",
                                    "working_memory": "last subtitle: New line",
                                }
                            ),
                            reasoning_content=None,
                            audio=None,
                        ),
                    )
                ]
            )

    async def create(**kwargs: object) -> _RawResponse:
        captured.update(kwargs)
        return _RawResponse()

    class _Client:
        chat = SimpleNamespace(
            completions=SimpleNamespace(
                with_raw_response=SimpleNamespace(create=create)
            )
        )

        @staticmethod
        async def close() -> None:
            return None

    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **_kwargs: _Client())
    monkeypatch.setattr(video_live, "_write_raw_monitor_response", lambda _body: None)

    result = await video_live._evaluate_qwen_monitor(
        "出现新的英文时翻译成中文",
        frames=[
            (f"data:image/jpeg;base64,frame-{index}", f"frame {index}")
            for index in range(4)
        ],
        audio_inputs=[],
        recent_events=[
            {
                "event_key": "translation:welcome",
                "observed_text": ["Welcome"],
                "response": "Welcome => 欢迎",
            }
        ],
        model_config=(
            "http://model/v1",
            "key",
            "jdopensource/JoyAI-VL-Interaction",
        ),
    )

    assert result["decision"] == "emit"
    assert result["response"] == "New line => 新的一行"
    assert result["working_memory"] == "last subtitle: New line"
    assert captured["max_tokens"] == 768
    response_format = captured["response_format"]
    assert response_format["type"] == "json_schema"
    decision_schema = response_format["json_schema"]["schema"]["properties"]["decision"]
    assert decision_schema["enum"] == ["hold", "emit", "uncertain"]
    properties = response_format["json_schema"]["schema"]["properties"]
    assert set(properties) == {"decision", "response", "working_memory"}
    assert properties["response"]["maxLength"] == 8_000
    assert properties["working_memory"]["maxLength"] == 2_000
    assert response_format["json_schema"]["schema"]["required"] == [
        "decision",
        "response",
        "working_memory",
    ]
    system_prompt = captured["messages"][0]["content"]
    assert (
        '{"decision":"hold|emit|uncertain","response":"",'
        '"working_memory":""}' in system_prompt
    )
    assert "EMIT|" not in system_prompt
    assert "event_key" not in system_prompt
    user_content = captured["messages"][1]["content"]
    image_urls = [
        item["image_url"]["url"] for item in user_content if item["type"] == "image_url"
    ]
    assert image_urls == [f"data:image/jpeg;base64,frame-{index}" for index in range(4)]
    history_prompt = "\n".join(
        item["text"] for item in user_content if item["type"] == "text"
    )
    assert "Welcome => 欢迎" in history_prompt
    assert "translation:welcome" not in history_prompt


def test_bailian_realtime_ws_url_maps_compatible_base() -> None:
    assert video_live._bailian_realtime_ws_url(
        "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "qwen3.5-omni-flash-realtime",
    ) == (
        "wss://workspace.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
        "?model=qwen3.5-omni-flash-realtime"
    )
    assert (
        video_live._bailian_realtime_ws_url(
            "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "qwen3-vl-plus",
        )
        is None
    )


@pytest.mark.asyncio
async def test_bailian_realtime_monitor_uses_buffer_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []
    connect_kwargs: dict[str, object] = {}
    raw_events: list[str] = []
    response_text = (
        '{"decision":"hold","response":"","working_memory":"tracking speaker 1"}'
    )
    incoming = iter(
        [
            json.dumps({"type": "response.text.delta", "delta": response_text}),
            json.dumps({"type": "response.text.done", "text": response_text}),
            json.dumps({"type": "response.done", "response": {"status": "completed"}}),
        ]
    )

    class _WebSocket:
        closed = False

        async def send(self, payload: str) -> None:
            sent.append(json.loads(payload))

        async def recv(self) -> str:
            return next(incoming)

        async def close(self) -> None:
            self.closed = True

    async def connect(url: str, **kwargs: object) -> _WebSocket:
        connect_kwargs.update({"url": url, **kwargs})
        return _WebSocket()

    from websockets.asyncio import client as websocket_client

    monkeypatch.setattr(websocket_client, "connect", connect)
    monkeypatch.setattr(
        video_live,
        "_write_raw_realtime_events",
        lambda events: raw_events.extend(events),
    )

    decision = await video_live._evaluate_bailian_realtime_monitor(
        ws_url=(
            "wss://workspace.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
            "?model=qwen3.5-omni-flash-realtime"
        ),
        api_key="test-key",
        system_prompt="Return JSON.",
        instruction="Translate new English text.",
        frames=[("data:image/jpeg;base64,eA==", "screen")],
        audio_inputs=[],
    )

    assert decision["decision"] == "hold"
    assert [event["type"] for event in sent] == [
        "session.update",
        "input_audio_buffer.append",
        "input_image_buffer.append",
        "input_audio_buffer.commit",
        "response.create",
    ]
    assert len(base64.b64decode(str(sent[1]["audio"]))) == 3_200
    assert sent[2]["image"] == "eA=="
    assert connect_kwargs["additional_headers"] == {"Authorization": "Bearer test-key"}
    assert len(raw_events) == 3


@pytest.mark.asyncio
async def test_bailian_realtime_session_reuses_connection_between_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_count = 0
    close_count = 0
    sent_types: list[str] = []
    response_text = '{"decision":"hold","response":"","working_memory":""}'

    class _WebSocket:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[str] = asyncio.Queue()

        async def send(self, payload: str) -> None:
            event = json.loads(payload)
            sent_types.append(event["type"])
            if event["type"] != "response.create":
                return
            await self.incoming.put(
                json.dumps({"type": "response.text.done", "text": response_text})
            )
            await self.incoming.put(
                json.dumps(
                    {
                        "type": "response.done",
                        "response": {"status": "completed"},
                    }
                )
            )

        async def recv(self) -> str:
            return await self.incoming.get()

        async def close(self) -> None:
            nonlocal close_count
            close_count += 1

    async def connect(_url: str, **_kwargs: object) -> _WebSocket:
        nonlocal connect_count
        connect_count += 1
        return _WebSocket()

    from websockets.asyncio import client as websocket_client

    monkeypatch.setattr(websocket_client, "connect", connect)
    monkeypatch.setattr(
        video_live,
        "_write_raw_realtime_events",
        lambda _events: None,
    )
    session = video_live._BailianRealtimeMonitorSession(
        "wss://example.test/api-ws/v1/realtime?model=realtime",
        "test-key",
    )
    request = {
        "system_prompt": "Return JSON.",
        "instruction": "Translate new English text.",
        "frames": [("data:image/jpeg;base64,eA==", "screen")],
        "audio_inputs": [],
    }

    first = await session.evaluate(**request)
    assert session.last_connection_reused is False
    second = await session.evaluate(**request)

    assert first["decision"] == "hold"
    assert second["decision"] == "hold"
    assert connect_count == 1
    assert session.turn_count == 2
    assert session.last_connection_reused is True
    assert session.last_connect_ms == 0
    assert sent_types.count("session.update") == 1
    assert sent_types.count("response.create") == 2

    await session.close()
    assert close_count == 1


@pytest.mark.asyncio
async def test_bailian_realtime_session_reconnects_after_failed_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_count = 0
    response_text = '{"decision":"hold","response":"","working_memory":""}'

    class _WebSocket:
        def __init__(self, should_fail: bool) -> None:
            self.should_fail = should_fail
            self.incoming: asyncio.Queue[str] = asyncio.Queue()

        async def send(self, payload: str) -> None:
            event = json.loads(payload)
            if event["type"] != "response.create":
                return
            if self.should_fail:
                await self.incoming.put(
                    json.dumps(
                        {
                            "type": "error",
                            "error": {"message": "temporary failure"},
                        }
                    )
                )
                return
            await self.incoming.put(
                json.dumps({"type": "response.text.done", "text": response_text})
            )
            await self.incoming.put(
                json.dumps(
                    {
                        "type": "response.done",
                        "response": {"status": "completed"},
                    }
                )
            )

        async def recv(self) -> str:
            return await self.incoming.get()

        async def close(self) -> None:
            return None

    async def connect(_url: str, **_kwargs: object) -> _WebSocket:
        nonlocal connect_count
        connect_count += 1
        return _WebSocket(should_fail=connect_count == 1)

    from websockets.asyncio import client as websocket_client

    monkeypatch.setattr(websocket_client, "connect", connect)
    monkeypatch.setattr(
        video_live,
        "_write_raw_realtime_events",
        lambda _events: None,
    )
    session = video_live._BailianRealtimeMonitorSession(
        "wss://example.test/api-ws/v1/realtime?model=realtime",
        "test-key",
    )
    request = {
        "system_prompt": "Return JSON.",
        "instruction": "Translate new English text.",
        "frames": [("data:image/jpeg;base64,eA==", "screen")],
        "audio_inputs": [],
    }

    with pytest.raises(RuntimeError, match="temporary failure"):
        await session.evaluate(**request)
    decision = await session.evaluate(**request)

    assert decision["decision"] == "hold"
    assert connect_count == 2
    assert session.turn_count == 2
    assert session.last_connection_reused is False
    await session.close()


@pytest.mark.parametrize(
    ("message", "expected_text", "expected_source"),
    [
        (
            SimpleNamespace(content='{"decision":"hold"}'),
            '{"decision":"hold"}',
            "content",
        ),
        (
            SimpleNamespace(
                content="",
                reasoning_content='{"decision":"emit"}',
            ),
            '{"decision":"emit"}',
            "reasoning_content",
        ),
        (
            SimpleNamespace(
                content=[
                    {"type": "text", "text": '{"decision":'},
                    SimpleNamespace(type="text", text='"uncertain"}'),
                ]
            ),
            '{"decision":\n"uncertain"}',
            "content",
        ),
        (
            SimpleNamespace(
                content=None,
                reasoning_content=None,
                audio={"transcript": '{"decision":"hold"}'},
            ),
            '{"decision":"hold"}',
            "audio.transcript",
        ),
        (
            SimpleNamespace(content=None, reasoning_content="", audio=None),
            "",
            "empty",
        ),
    ],
)
def test_extract_model_message_text_supports_compatible_response_shapes(
    message: object,
    expected_text: str,
    expected_source: str,
) -> None:
    assert video_live._extract_model_message_text(message) == (
        expected_text,
        expected_source,
    )


def test_select_model_text_choice_skips_audio_only_choice() -> None:
    audio_choice = SimpleNamespace(
        index=0,
        message=SimpleNamespace(
            content=None,
            reasoning_content=None,
            audio=SimpleNamespace(transcript=None, data="AAEC"),
        ),
    )
    text_choice = SimpleNamespace(
        index=0,
        message=SimpleNamespace(
            content='{"decision":"emit"}',
            reasoning_content=None,
            audio=None,
        ),
    )

    selected, text, source, position = video_live._select_model_text_choice(
        [audio_choice, text_choice]
    )

    assert selected is text_choice
    assert text == '{"decision":"emit"}'
    assert source == "content"
    assert position == 1


@pytest.mark.asyncio
async def test_evaluate_monitor_requests_text_only_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _RawResponse:
        content = b'{"choices":[]}'

        @staticmethod
        def parse() -> object:
            return SimpleNamespace(
                model="Qwen3-Omni",
                choices=[
                    SimpleNamespace(
                        index=0,
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content=(
                                '{"decision":"hold","response":"","working_memory":""}'
                            ),
                            reasoning_content=None,
                            audio=None,
                        ),
                    )
                ],
            )

    async def create(**kwargs: object) -> _RawResponse:
        captured.update(kwargs)
        return _RawResponse()

    class _Client:
        chat = SimpleNamespace(
            completions=SimpleNamespace(
                with_raw_response=SimpleNamespace(create=create)
            )
        )

        @staticmethod
        async def close() -> None:
            return None

    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **_kwargs: _Client())
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: ("http://model", "key", "Qwen3-Omni"),
    )
    monkeypatch.setattr(
        video_live,
        "_write_raw_monitor_response",
        lambda _raw_body: None,
    )

    decision = await video_live._evaluate_qwen_monitor(
        "画面中出现新的英文内容时，立即翻译成中文。",
        [("data:image/jpeg;base64,eA==", "屏幕")],
        [],
        [],
    )

    assert captured["modalities"] == ["text"]
    assert decision["decision"] == "hold"


def test_monitor_message_diagnostics_contains_metadata_only() -> None:
    diagnostics = video_live._monitor_message_diagnostics(
        SimpleNamespace(
            content=[],
            reasoning_content='{"decision":"hold"}',
            audio={"transcript": "sample"},
        )
    )

    assert diagnostics == {
        "content_type": "list",
        "content_text_chars": 0,
        "reasoning_content_type": "str",
        "reasoning_text_chars": 19,
        "audio_type": "dict",
        "audio_transcript_chars": 6,
    }


def test_write_raw_monitor_response_preserves_exact_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw_body = (
        b'{"id":"response-1","choices":[{"message":'
        b'{"content":null,"audio":{"data":"AAEC"}}}]}'
    )
    monkeypatch.setattr(video_live, "get_logs_dir", lambda: tmp_path)

    video_live._write_raw_monitor_response(raw_body)

    output = tmp_path / "video-monitor-last-raw-response.json"
    assert output.read_bytes() == raw_body
    assert not (tmp_path / "video-monitor-last-raw-response.json.tmp").exists()


def test_monitor_methods_are_local_only() -> None:
    assert "video.ask" in _LOCAL_ONLY_METHODS
    assert "video.realtime.start" in _LOCAL_ONLY_METHODS
    assert "video.realtime.stop" in _LOCAL_ONLY_METHODS
    assert "video.monitor.intent" in _LOCAL_ONLY_METHODS
    assert "video.monitor.evaluate" in _LOCAL_ONLY_METHODS
    assert "video.monitor.cancel" in _LOCAL_ONLY_METHODS


@pytest.mark.asyncio
async def test_monitor_evaluate_runs_in_background_and_can_be_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = type("MonitorWebSocket", (), {"remote_address": ("127.0.0.1", 1)})()
    started = asyncio.Event()
    stopped = asyncio.Event()
    cleaned = asyncio.Event()
    responses: list[dict[str, object]] = []

    async def evaluate(_ws, _req_id, _params, _session_id) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def send_response(_ws, req_id, **response: object) -> None:
        responses.append({"id": req_id, **response})

    async def cancel(_ws, _req_id, _params, _session_id) -> None:
        cleaned.set()

    channel.register_method("video.monitor.evaluate", evaluate)
    channel.register_method("video.monitor.cancel", cancel)
    monkeypatch.setattr(channel, "send_response", send_response)

    await asyncio.wait_for(
        channel._handle_raw_message(
            ws,
            json.dumps(
                {
                    "type": "req",
                    "id": "monitor-1",
                    "method": "video.monitor.evaluate",
                    "params": {"monitor_run_id": "run-1"},
                }
            ),
            {},
        ),
        timeout=0.2,
    )
    await asyncio.wait_for(started.wait(), timeout=0.2)

    await asyncio.wait_for(
        channel._handle_raw_message(
            ws,
            json.dumps(
                {
                    "type": "req",
                    "id": "cancel-1",
                    "method": "video.monitor.cancel",
                    "params": {"monitor_run_id": "run-1"},
                }
            ),
            {},
        ),
        timeout=0.2,
    )
    await asyncio.wait_for(stopped.wait(), timeout=0.2)
    await asyncio.wait_for(cleaned.wait(), timeout=0.2)
    await asyncio.sleep(0)

    assert responses == [{"id": "cancel-1", "ok": True, "payload": {"cancelled": True}}]
    assert id(ws) not in channel._video_monitor_tasks


@pytest.mark.asyncio
async def test_video_live_reuses_session_across_monitor_runs_until_realtime_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[video_live._BailianRealtimeMonitorSession | None] = []

    async def evaluate(
        _instruction,
        _frames,
        _audio_inputs,
        _recent_events,
        *,
        realtime_session=None,
        model_config=None,
        working_memory="",
        runtime_context=None,
    ):
        del model_config, working_memory, runtime_context
        sessions.append(realtime_session)
        return {
            "decision": "hold",
            "response": "",
            "working_memory": "",
        }

    monkeypatch.setattr(video_live, "_evaluate_qwen_monitor", evaluate)
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: (
            "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "key",
            "qwen3.5-omni-flash-realtime",
        ),
    )
    channel = _Channel()
    video_live.register_video_live_handler(channel)
    ws = object()

    async def run(monitor_run_id: str, request_id: str) -> None:
        await channel.methods["video.monitor.evaluate"](
            ws,
            request_id,
            {
                "instruction": "Translate new English text.",
                "monitor_run_id": monitor_run_id,
                "frames": [_frame()],
            },
            "session",
        )

    await run("run-1", "request-1")
    await run("run-1", "request-2")
    await run("run-2", "request-3")

    assert sessions[0] is not None
    assert sessions[1] is sessions[0]
    assert sessions[2] is sessions[0]
    assert sessions[0]._closed is False

    await channel.methods["video.monitor.cancel"](
        ws,
        "cancel",
        {"monitor_run_id": "run-2"},
        "session",
    )
    assert sessions[0]._closed is False

    await channel.methods["video.realtime.stop"](
        ws,
        "realtime-stop",
        {},
        "session",
    )
    assert sessions[0]._closed is True


def test_monitor_request_diagnostics_reports_frame_freshness() -> None:
    params = {
        "monitor_run_id": "monitor-run-1",
        "instruction": "翻译新出现的英文",
        "client_started_at": 1_785_464_910_100,
        "skipped_intervals": 3,
        "buffered_frame_count": 17,
        "sampled_frame_count": 8,
        "coalesced_frame_count": 9,
        "buffer_overflow_dropped": 0,
        "frames": [_frame()],
        "audio_inputs": [{"data_url": "data:audio/wav;base64,eA=="}],
        "recent_events": [],
    }

    metrics = video_live._monitor_request_diagnostics(params, 1_785_464_910_250)

    assert metrics["client_to_server_ms"] == 150
    assert metrics["monitor_run_id"] == "monitor-run-1"
    assert metrics["skipped_intervals"] == 3
    assert metrics["buffered_frame_count"] == 17
    assert metrics["sampled_frame_count"] == 8
    assert metrics["coalesced_frame_count"] == 9
    assert metrics["buffer_overflow_dropped"] == 0
    assert metrics["newest_frame_age_ms"] == 250
    assert metrics["frame_span_ms"] == 0
    assert metrics["frames"][0]["width"] == 768
    assert metrics["frames"][0]["height"] == 432
    assert metrics["audio_count"] == 1


@pytest.mark.asyncio
async def test_monitor_handler_returns_structured_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    diagnostics: list[dict[str, object]] = []
    config_reads = 0

    def record_diagnostic(event: str, **fields: object) -> None:
        diagnostics.append({"event": event, **fields})

    monkeypatch.setattr(video_live, "_write_monitor_diagnostic", record_diagnostic)

    async def evaluate(
        instruction,
        frames,
        audio_inputs,
        recent_events,
        *,
        realtime_session=None,
        model_config=None,
        working_memory="",
        runtime_context=None,
    ):
        captured.update(
            {
                "instruction": instruction,
                "frames": frames,
                "audio_inputs": audio_inputs,
                "recent_events": recent_events,
                "realtime_session": realtime_session,
                "model_config": model_config,
                "working_memory": working_memory,
                "runtime_context": runtime_context,
            }
        )
        return {
            "decision": "emit",
            "response": "Welcome 翻译为欢迎。",
            "working_memory": "last subtitle: Welcome",
        }

    monkeypatch.setattr(video_live, "_evaluate_qwen_monitor", evaluate)

    def model_config() -> tuple[str, str, str]:
        nonlocal config_reads
        config_reads += 1
        return "http://model", "key", "qwen-omni"

    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        model_config,
    )
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.monitor.evaluate"](
        object(),
        "monitor-request-1",
        {
            "monitor_trace_id": "monitor-trace-1",
            "monitor_run_id": "monitor-run-1",
            "client_started_at": 1_785_464_910_100,
            "skipped_intervals": 2,
            "instruction": "出现新的英文时翻译",
            "frames": [_frame()],
            "recent_events": [
                {
                    "event_key": "english:checkout",
                    "response": "Checkout 翻译为结账。",
                    "observed_text": ["Checkout"],
                    "emitted_at": 1_785_464_900_000,
                }
            ],
        },
        "video-monitor-session",
    )

    assert captured["instruction"] == "出现新的英文时翻译"
    assert captured["frames"] == [
        ("data:image/jpeg;base64,eA==", "摄像头（本批最新画面）")
    ]
    assert captured["recent_events"] == [
        {
            "event_key": "english:checkout",
            "response": "Checkout 翻译为结账。",
            "observed_text": ["Checkout"],
            "emitted_at": 1_785_464_900_000,
        }
    ]
    response = channel.responses[0]
    expected_event_key = video_live._monitor_event_identity("Welcome 翻译为欢迎。")
    assert response["id"] == "monitor-request-1"
    assert response["ok"] is True
    assert response["payload"] == {
        "decision": "emit",
        "event_key": expected_event_key,
        "occurrence_id": "monitor-run-1:1",
        "display_action": "append",
        "response": "Welcome 翻译为欢迎。",
        "trace_id": "monitor-trace-1",
        "monitor_run_id": "monitor-run-1",
        "model": "qwen-omni",
        "latency_ms": response["payload"]["latency_ms"],
        "frame_count": 1,
        "audio_count": 0,
    }
    assert response["payload"]["latency_ms"] >= 0
    assert config_reads == 1
    assert captured["model_config"] == (
        "http://model",
        "key",
        "qwen-omni",
    )
    assert [entry["event"] for entry in diagnostics] == [
        "request_received",
        "model_decision",
    ]
    assert diagnostics[0]["trace_id"] == "monitor-trace-1"
    assert diagnostics[0]["skipped_intervals"] == 2
    assert diagnostics[1]["event_key"] == expected_event_key
    assert captured["working_memory"] == ""
    assert captured["runtime_context"]["turn_index"] == 1
    assert captured["runtime_context"]["monitor_elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_monitor_handler_suppresses_repeated_model_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_histories: list[list[dict[str, object]]] = []
    diagnostics: list[dict[str, object]] = []

    monkeypatch.setattr(
        video_live,
        "_write_monitor_diagnostic",
        lambda event, **fields: diagnostics.append({"event": event, **fields}),
    )

    async def evaluate(
        _instruction,
        _frames,
        _audio_inputs,
        recent_events,
        *,
        realtime_session=None,
        model_config=None,
        working_memory="",
        runtime_context=None,
    ):
        del realtime_session, model_config, working_memory, runtime_context
        captured_histories.append(recent_events)
        return {
            "decision": "emit",
            "response": "同一个持续事件",
            "working_memory": "same event remains visible",
        }

    monkeypatch.setattr(video_live, "_evaluate_qwen_monitor", evaluate)
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: ("http://model", "key", "joyai"),
    )
    channel = _Channel()
    video_live.register_video_live_handler(channel)
    ws = object()
    params = {
        "instruction": "报告新事件",
        "monitor_run_id": "run-dedup",
        "frames": [_frame()],
        "recent_events": [],
    }

    await channel.methods["video.monitor.evaluate"](ws, "request-1", params, "session")
    await channel.methods["video.monitor.evaluate"](ws, "request-2", params, "session")

    assert channel.responses[0]["payload"]["decision"] == "emit"
    assert channel.responses[0]["payload"]["display_action"] == "append"
    assert channel.responses[1]["payload"] == {
        "decision": "hold",
        "response": "",
        "trace_id": "request-2",
        "monitor_run_id": "run-dedup",
        "model": "joyai",
        "latency_ms": channel.responses[1]["payload"]["latency_ms"],
        "frame_count": 1,
        "audio_count": 0,
    }
    assert captured_histories[1][-1]["response"] == "同一个持续事件"
    final_decision = [
        item for item in diagnostics if item["event"] == "model_decision"
    ][-1]
    assert final_decision["decision"] == "hold"
    assert final_decision["suppression_reason"] == "active_event_duplicate"
    assert final_decision["original_model_output"] == {
        "decision": "emit",
        "response": "同一个持续事件",
        "working_memory": "same event remains visible",
    }


@pytest.mark.asyncio
async def test_monitor_handler_carries_working_memory_to_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received_memories: list[str] = []
    model_results = iter(
        [
            {
                "decision": "hold",
                "response": "",
                "working_memory": "person_1 is in the down phase; count=7",
            },
            {
                "decision": "emit",
                "response": "已完成第 8 个俯卧撑。",
                "working_memory": "person_1 returned to up; count=8",
            },
        ]
    )

    async def evaluate(
        _instruction,
        _frames,
        _audio_inputs,
        _recent_events,
        *,
        realtime_session=None,
        model_config=None,
        working_memory="",
        runtime_context=None,
    ):
        del realtime_session, model_config, runtime_context
        received_memories.append(working_memory)
        return next(model_results)

    monkeypatch.setattr(video_live, "_evaluate_qwen_monitor", evaluate)
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: ("http://model", "key", "joyai"),
    )
    channel = _Channel()
    video_live.register_video_live_handler(channel)
    ws = object()
    params = {
        "instruction": "记录这个人做了多少个俯卧撑",
        "monitor_run_id": "run-working-memory",
        "frames": [_frame()],
        "recent_events": [],
    }

    await channel.methods["video.monitor.evaluate"](ws, "request-1", params, "session")
    await channel.methods["video.monitor.evaluate"](ws, "request-2", params, "session")

    assert received_memories == [
        "",
        "person_1 is in the down phase; count=7",
    ]
    assert channel.responses[0]["payload"]["decision"] == "hold"
    assert channel.responses[1]["payload"]["decision"] == "emit"
    assert channel.responses[1]["payload"]["response"] == "已完成第 8 个俯卧撑。"
    assert all(
        "working_memory" not in response["payload"] for response in channel.responses
    )


@pytest.mark.asyncio
async def test_monitor_handler_rejects_empty_instruction() -> None:
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.monitor.evaluate"](
        object(),
        "monitor-request-2",
        {"instruction": "", "frames": [_frame()]},
        "video-monitor-session",
    )

    assert channel.responses[0]["ok"] is False
    assert channel.responses[0]["code"] == "BAD_REQUEST"
    assert channel.responses[0]["error"] == "instruction is required"


def test_monitor_memory_history_only_restores_matching_instruction() -> None:
    context = {
        "current_chunk": {
            "interactions": [
                {
                    "task_type": "continuous_monitor",
                    "monitor_instruction": "Translate new English text",
                    "event_key": "english:checkout",
                    "answer": "Checkout => 结账",
                    "observed_text": ["Checkout"],
                },
                {
                    "task_type": "continuous_monitor",
                    "monitor_instruction": "Warn when a person enters",
                    "event_key": "person:entered",
                    "answer": "A person entered",
                },
            ]
        },
        "qa_history": [],
    }

    events = video_live._monitor_events_from_memory_context(
        context,
        "  translate   NEW english text  ",
    )

    assert events == [
        {
            "event_key": "english:checkout",
            "response": "Checkout => 结账",
            "observed_text": ["Checkout"],
        }
    ]


def test_compact_monitor_memory_context_bounds_durable_state() -> None:
    context = {
        "long_term_memory": {"summary": "stable room state"},
        "mid_term_memories": [
            {
                "summary": f"event {index}",
                "started_at": index,
                "ended_at": index + 1,
            }
            for index in range(10)
        ],
    }

    compact = video_live._compact_monitor_memory_context(context)

    assert compact["long_term_summary"] == "stable room state"
    assert len(compact["recent_mid_term_memories"]) == 8
    assert compact["recent_mid_term_memories"][0]["summary"] == "event 2"


@pytest.mark.asyncio
async def test_monitor_handler_restores_and_writes_omnimemory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MemoryClient:
        api_base = "http://memory"

        def __init__(self) -> None:
            self.interactions: list[tuple[str, dict[str, object]]] = []

        async def context(self, session_id: str) -> dict[str, object]:
            assert session_id == "monitor-memory-session"
            return {
                "long_term_memory": {"summary": "The room was empty."},
                "current_chunk": {
                    "interactions": [
                        {
                            "task_type": "continuous_monitor",
                            "monitor_instruction": "Translate new English text",
                            "event_key": "english:checkout",
                            "answer": "Checkout => 结账",
                            "observed_text": ["Checkout"],
                        }
                    ]
                },
                "mid_term_memories": [{"id": "memory-1"}],
                "qa_history": [],
            }

        async def write_interaction(
            self,
            session_id: str,
            record: dict[str, object],
        ) -> dict[str, object]:
            self.interactions.append((session_id, record))
            return {"id": "interaction-1"}

    memory_client = _MemoryClient()
    monkeypatch.setenv("OMNIMEMORY_API_BASE", memory_client.api_base)
    monkeypatch.setattr(video_live, "_memory_client", memory_client)
    captured_events: list[dict[str, object]] = []
    captured_memory: dict[str, object] = {}

    async def evaluate(
        _instruction,
        _frames,
        _audio_inputs,
        recent_events,
        *,
        realtime_session=None,
        model_config=None,
        memory_context=None,
        working_memory="",
        runtime_context=None,
    ):
        del realtime_session, model_config, working_memory, runtime_context
        captured_events.extend(recent_events)
        captured_memory.update(memory_context or {})
        return {
            "decision": "emit",
            "response": "Welcome => 欢迎",
            "working_memory": "last subtitle: Welcome",
        }

    monkeypatch.setattr(video_live, "_evaluate_qwen_monitor", evaluate)
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: ("http://model", "key", "joyai"),
    )
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.monitor.evaluate"](
        object(),
        "monitor-memory-request",
        {
            "instruction": "Translate new English text",
            "monitor_run_id": "new-run",
            "frames": [_frame()],
            "recent_events": [],
        },
        "monitor-memory-session",
    )
    for _ in range(10):
        if memory_client.interactions:
            break
        await asyncio.sleep(0)

    assert captured_events == [
        {
            "event_key": "english:checkout",
            "response": "Checkout => 结账",
            "observed_text": ["Checkout"],
        }
    ]
    assert captured_memory["long_term_summary"] == "The room was empty."
    assert len(memory_client.interactions) == 1
    session_id, record = memory_client.interactions[0]
    assert session_id == "monitor-memory-session"
    assert record["task_type"] == "continuous_monitor"
    assert record["monitor_instruction"] == "Translate new English text"
    assert record["answer"] == "Welcome => 欢迎"
    assert record["event_key"] == video_live._monitor_event_identity("Welcome => 欢迎")
    assert "observed_text" not in record
    assert "evidence" not in record
    assert "confidence" not in record
    assert record["context_memory_ids"] == ["memory-1"]
