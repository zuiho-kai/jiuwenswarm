import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from jiuwenswarm.gateway.channel_manager.web import video_live
from jiuwenswarm.gateway.channel_manager.web.omnimemory_live import (
    OmniMemoryLiveClient,
    normalize_memory_frames,
)


def _frame(source_id: str = "camera") -> dict[str, object]:
    return {
        "data_url": (
            "data:image/jpeg;base64,"
            + base64.b64encode(b"jpeg-bytes").decode()
        ),
        "captured_at": 1_785_464_910_000,
        "source_id": source_id,
        "source_label": "camera",
    }


def _task_frame(sequence: int = 0) -> dict[str, object]:
    return {
        **_frame(),
        "client_frame_id": f"client-frame-{sequence}",
        "frame_seq": sequence,
    }


def test_normalize_memory_frames_keeps_source_and_capture_time() -> None:
    frames = normalize_memory_frames({"frames": [_frame()]})

    assert frames == [
        {
            "content": b"jpeg-bytes",
            "mime_type": "image/jpeg",
            "observed_at": "2026-07-31T02:28:30+00:00",
            "source_id": "camera",
        }
    ]


def test_current_chunk_observation_restores_raw_frame(
    tmp_path: Path,
) -> None:
    frame_path = tmp_path / "frame.jpg"
    frame_path.write_bytes(b"current-frame")

    frames = video_live._current_chunk_frames(
        {
            "current_observations": [
                {
                    "modality": "image",
                    "data_ref": str(frame_path),
                    "source_id": "camera",
                    "timestamp": "2026-07-31T10:05:49+08:00",
                    "metadata": {"media_type": "image/jpeg"},
                }
            ]
        }
    )

    assert frames == [
        (
            "data:image/jpeg;base64,Y3VycmVudC1mcmFtZQ==",
            "当前 chunk：camera @ 2026-07-31T10:05:49+08:00",
        )
    ]


def test_grounding_verifies_same_entity_across_frames() -> None:
    result = video_live._parse_grounding(
        """{
          "primary_entity": "瑞幸咖啡",
          "candidates": ["瑞幸咖啡"],
          "verification_basis": "multi_frame_consistency",
          "per_frame": [
            {"frame_index": 0, "entity": "瑞幸咖啡", "visible_text": [], "visual_cues": ["蓝色鹿角"]},
            {"frame_index": 1, "entity": "瑞幸咖啡", "visible_text": [], "visual_cues": ["蓝色杯套"]}
          ]
        }"""
    )

    assert result["status"] == "VERIFIED"
    assert result["verification_basis"] == "multi_frame_consistency"
    assert result["primary_entity"] == "瑞幸咖啡"


def test_grounding_does_not_promote_unknown_from_model_confidence() -> None:
    result = video_live._parse_grounding(
        '{"primary_entity":null,"candidates":[],"confidence":0.99,'
        '"verification_basis":"none","per_frame":[]}'
    )

    assert result["status"] == "UNKNOWN"


def test_qwen_reasoning_content_tool_call_is_normalized() -> None:
    message = SimpleNamespace(
        tool_calls=None,
        reasoning_content=(
            '<tool_call>{"name":"deep_reasoning","arguments":'
            '{"problem":"比较方案"}}</tool_call>'
        ),
    )

    assert video_live._normalized_tool_calls(message, 1) == [
        {
            "id": "compat-call-1-0",
            "name": "deep_reasoning",
            "arguments": '{"problem": "比较方案"}',
        }
    ]


def test_grounding_answers_visual_identification_directly() -> None:
    result = video_live._parse_grounding(
        '{"primary_entity":"瑞幸咖啡","candidates":["瑞幸咖啡"],'
        '"direct_answer":"这是瑞幸咖啡。","verification_basis":"readable_brand_text",'
        '"per_frame":[{"frame_index":0,"entity":"瑞幸咖啡",'
        '"visible_text":["luckin coffee"],"visual_cues":[]}]}',
        "这是什么？",
    )

    assert result["direct_answer"] == "这是瑞幸咖啡。"
    assert result["needs_external_tools"] is False


def test_grounding_routes_brand_introduction_to_agent_tools() -> None:
    result = video_live._parse_grounding(
        '{"primary_entity":"瑞幸咖啡","candidates":["瑞幸咖啡"],'
        '"direct_answer":"这是瑞幸咖啡。","verification_basis":"readable_brand_text",'
        '"per_frame":[{"frame_index":0,"entity":"瑞幸咖啡",'
        '"visible_text":["luckin coffee"],"visual_cues":[]}]}',
        "介绍一下这个牌子",
    )

    assert result["needs_external_tools"] is True


@pytest.mark.asyncio
async def test_client_creates_stream_then_sends_contiguous_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/v1/sessions":
            return httpx.Response(201, json={"status": "open"})
        if path.endswith("/streams"):
            return httpx.Response(201, json={"status": "open"})
        if path.endswith("/frames"):
            sequence = len(
                [item for item in requests if item.url.path.endswith("/frames")]
            ) - 1
            return httpx.Response(
                200,
                json={"observation_id": f"observation-{sequence}"},
            )
        if path.endswith("/context"):
            return httpx.Response(
                200,
                json={
                    "memories": [{"id": "memory-0"}],
                    "current_observations": [],
                },
            )
        if path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "memories": [{"id": "memory-0"}],
                    "evidence": ["frame-0.jpg"],
                },
            )
        if path.endswith("/interactions"):
            return httpx.Response(
                201,
                json={"id": "interaction-0", "status": "current"},
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    transport = httpx.MockTransport(handle)
    original_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    client = OmniMemoryLiveClient("http://memory")
    frames = normalize_memory_frames(
        {"frames": [_frame(), _frame()]}
    )

    observation_ids = await client.observe("session-a", frames)
    context = await client.context("session-a")
    search = await client.search(
        "session-a",
        {"query": "杯子放在哪里？"},
    )
    writeback = await client.write_interaction(
        "session-a",
        {
            "answer": "杯子在柜子里。",
            "asked_at": "2026-07-31T10:00:00+08:00",
        },
    )

    assert observation_ids == ["observation-0", "observation-1"]
    frame_requests = [
        request for request in requests if request.url.path.endswith("/frames")
    ]
    assert b'name="sequence_no"\r\n\r\n0' in frame_requests[0].content
    assert b'name="sequence_no"\r\n\r\n1' in frame_requests[1].content
    assert context["memories"] == [{"id": "memory-0"}]
    assert search["evidence"] == ["frame-0.jpg"]
    assert writeback["id"] == "interaction-0"


class _Channel:
    def __init__(self) -> None:
        self.methods: dict[str, Any] = {}
        self.responses: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    def register_method(self, name: str, handler: Any) -> None:
        self.methods[name] = handler

    async def send_response(self, ws, req_id, **response: Any) -> None:
        del ws
        self.responses.append({"id": req_id, **response})

    async def send_event(self, ws, name, payload, **metadata: Any) -> None:
        del ws
        self.events.append(
            {"name": name, "payload": payload, **metadata}
        )


def test_tts_config_reuses_omni_credentials_when_audio_is_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        video_live,
        "_configured_audio_model",
        lambda: ("", "your-api-key", "your-audio-model-name"),
    )
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: ("https://api.siliconflow.cn/v1", "secret", "omni"),
    )
    monkeypatch.delenv("TTS_MODEL_NAME", raising=False)
    monkeypatch.delenv("TTS_VOICE", raising=False)

    assert video_live._tts_model_config() == (
        "https://api.siliconflow.cn/v1",
        "secret",
        "FunAudioLLM/CosyVoice2-0.5B",
        "FunAudioLLM/CosyVoice2-0.5B:claire",
    )


def test_voice_transcript_rejects_assistant_speaker_echo() -> None:
    assistant_text = "这是一个瑞幸咖啡的纸杯，杯身上有品牌标志。"

    assert video_live._looks_like_assistant_echo(
        "这是一个瑞幸咖啡的纸杯",
        assistant_text,
    )
    assert not video_live._looks_like_assistant_echo(
        "那它多少钱？",
        assistant_text,
    )


@pytest.mark.asyncio
async def test_tts_handler_returns_generated_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def synthesize(text: str):
        assert text == "你好"
        return b"mp3-bytes", "audio/mpeg", "tts-model"

    monkeypatch.setattr(video_live, "_synthesize_speech", synthesize)
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["tts.synthesize"](
        object(), "tts-1", {"text": "你好"}, "session-a"
    )

    assert channel.responses == [
        {
            "id": "tts-1",
            "ok": True,
            "payload": {
                "success": True,
                "audio_base64": base64.b64encode(b"mp3-bytes").decode(),
                "audio_mime": "audio/mpeg",
                "model": "tts-model",
            },
        }
    ]


def test_current_visual_identification_drops_stale_entity_memory() -> None:
    memory_context = {
        "available": True,
        "qa_history": [
            {
                "question": "这是什么？",
                "answer": "这是一个瑞幸咖啡的纸杯。",
            }
        ],
        "mid_term_memories": [{"summary": "用户一直拿着瑞幸咖啡"}],
    }

    scoped = video_live._memory_context_for_question(
        memory_context,
        "这个是什么？",
    )

    assert scoped == {"available": True, "scope": "current_frames_only"}
    assert "瑞幸" not in json.dumps(scoped, ensure_ascii=False)


def test_historical_visual_question_keeps_memory() -> None:
    memory_context = {
        "available": True,
        "qa_history": [{"answer": "之前拿着瑞幸咖啡"}],
    }

    assert video_live._memory_context_for_question(
        memory_context,
        "我刚才手里拿的是什么？",
    ) is memory_context


def test_named_term_definition_requires_external_lookup() -> None:
    assert video_live._requires_external_definition_lookup(
        "烈焰升腾/钢铁雄心 是什么"
    )
    assert video_live._requires_external_definition_lookup(
        "烈焰升腾/钢铁雄心 是什么\n\n当前音频转写：背景音乐。"
    )
    assert not video_live._requires_external_definition_lookup("这个是什么？")
    assert not video_live._requires_external_definition_lookup(
        "我刚才看到的是什么？"
    )


@pytest.mark.asyncio
async def test_video_answer_falls_back_when_omni_has_no_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = ("http://model", "key", "Qwen/Qwen3-Omni-30B-A3B-Instruct")
    fallback = ("http://model", "key", "Qwen/Qwen3-VL-8B-Instruct")
    monkeypatch.setattr(video_live, "_omni_model_config", lambda: primary)
    monkeypatch.setattr(
        video_live,
        "_fallback_video_model_config",
        lambda: fallback,
    )
    calls: list[str] = []
    statuses: list[str] = []

    async def stream_model(question, frames, audio_inputs, **options):
        del question, frames, audio_inputs
        model = options["model_config"][2]
        calls.append(model)
        if model == primary[2]:
            raise RuntimeError("returned no action")
        yield "备用模型回答"

    async def status_sink(status: str) -> None:
        statuses.append(status)

    monkeypatch.setattr(video_live, "_stream_qwen_omni", stream_model)
    answer = "".join(
        [
            part
            async for part in video_live._stream_video_answer(
                "这是什么？",
                [("data:image/jpeg;base64,eA==", "camera")],
                [],
                fallback_status_sink=status_sink,
            )
        ]
    )

    assert answer == "备用模型回答"
    assert calls == [primary[2], fallback[2]]
    assert statuses and fallback[2] in statuses[0]


class _MemoryClient:
    api_base = "http://memory"

    def __init__(self) -> None:
        self.observed: list[tuple[str, list[dict[str, object]]]] = []
        self.interactions: list[tuple[str, dict[str, object]]] = []

    async def observe(self, session_id, frames):
        self.observed.append((session_id, frames))
        return ["observation-0"]

    async def context(self, session_id):
        return {
            "session_id": session_id,
            "long_term_memory": {"summary": "长期摘要", "batches": []},
            "mid_term_memories": [{"id": "memory-0"}],
            "current_chunk": {
                "observations": [{"id": "observation-0"}],
                "interactions": [],
            },
            "qa_history": [],
            "memories": [{"id": "memory-0"}],
            "current_observations": [{"id": "observation-0"}],
        }

    async def search(self, session_id, arguments):
        return {
            "query": arguments["query"],
            "memories": [{"id": f"{session_id}-memory"}],
            "evidence": ["frame-0.jpg"],
        }

    async def write_interaction(self, session_id, record):
        self.interactions.append((session_id, record))
        return {"id": "interaction-0", "status": "current"}


@pytest.mark.asyncio
async def test_video_ground_handler_returns_structured_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ground(question, frames):
        assert question == "这是什么？"
        assert len(frames) == 1
        return {
            "status": "VERIFIED",
            "primary_entity": "瑞幸咖啡",
            "model": "qwen-omni",
        }

    monkeypatch.setattr(video_live, "_ground_video_entities", ground)
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.ground"](
        object(),
        "ground-1",
        {"question": "这是什么？", "frames": [_frame()]},
        "session-camera",
    )

    assert channel.responses == [
        {
            "id": "ground-1",
            "ok": True,
            "payload": {
                "grounding": {
                    "status": "VERIFIED",
                    "primary_entity": "瑞幸咖啡",
                    "model": "qwen-omni",
                },
                "frame_count": 1,
                "session_id": "session-camera",
            },
        }
    ]


@pytest.mark.asyncio
async def test_external_answer_uses_one_search_and_streams_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stream_external(question, grounding):
        assert question == "介绍一下这个牌子"
        assert grounding["primary_entity"] == "瑞幸咖啡"

        async def deltas():
            yield "瑞幸咖啡是"
            yield "中国咖啡连锁品牌。"

        return "1. 瑞幸官网\nURL: https://www.lkcoffee.com/about", deltas()

    monkeypatch.setattr(video_live, "_stream_external_answer", stream_external)
    monkeypatch.setattr(
        video_live,
        "_video_tool_model_config",
        lambda: ("http://model", "key", "Qwen/Qwen3.5-9B"),
    )
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.external.ask"](
        object(),
        "external-1",
        {
            "question": "介绍一下这个牌子",
            "grounding": {
                "status": "VERIFIED",
                "primary_entity": "瑞幸咖啡",
            },
        },
        "session-camera",
    )

    assert [event["name"] for event in channel.events] == [
        "video.started",
        "video.tool_status",
        "video.tool_status",
        "video.delta",
        "video.delta",
    ]
    assert channel.responses[0]["payload"]["answer"] == (
        "瑞幸咖啡是中国咖啡连锁品牌。"
    )
    assert channel.responses[0]["payload"]["tool_calls"][0]["name"] == (
        "free_search"
    )


@pytest.mark.asyncio
async def test_camera_agent_answer_and_tools_write_back_to_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_client = _MemoryClient()
    monkeypatch.setenv("OMNIMEMORY_API_BASE", memory_client.api_base)
    monkeypatch.setattr(video_live, "_memory_client", memory_client)
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.interaction.write"](
        object(),
        "write-1",
        {
            "question": "介绍一下瑞幸",
            "answer": "瑞幸咖啡是一家中国咖啡连锁品牌。",
            "model": "Jiuwen Agent",
            "request_id": "agent-1",
            "source_ids": ["camera"],
            "tool_calls": [
                {"type": "camera_grounding", "status": "VERIFIED"},
                {"type": "tool_call", "name": "web_search"},
            ],
        },
        "session-camera",
    )

    record = memory_client.interactions[0][1]
    assert record["task_type"] == "camera_agent"
    assert record["current_observation_ids"] == ["observation-0"]
    assert record["tool_calls"][1]["name"] == "web_search"
    assert channel.responses[0]["payload"]["written"] is True


@pytest.mark.asyncio
async def test_video_handlers_give_context_and_search_tool_to_qwen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_client = _MemoryClient()
    monkeypatch.setenv("OMNIMEMORY_API_BASE", memory_client.api_base)
    monkeypatch.setattr(video_live, "_memory_client", memory_client)
    captured: dict[str, object] = {}

    async def stream_answer(
        question,
        frames,
        audio_inputs,
        *,
        memory_context,
        memory_search,
        **tools,
    ):
        del tools
        del frames, audio_inputs
        captured["context"] = memory_context
        result = await memory_search({"query": "杯子放在哪里？"})
        captured["search"] = result
        yield f"{question}：杯子在柜子里。"

    monkeypatch.setattr(video_live, "_stream_qwen_omni", stream_answer)
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.memory.observe"](
        object(),
        "observe-1",
        {"frames": [_frame()]},
        "session-a",
    )
    await channel.methods["video.ask"](
        object(),
        "ask-1",
        {"question": "我在干什么？", "frames": [_frame()]},
        "session-a",
    )

    assert memory_client.observed[0][0] == "session-a"
    assert channel.responses[0]["payload"]["accepted"] == 1
    assert channel.responses[1]["payload"]["answer"] == (
        "我在干什么？：杯子在柜子里。"
    )
    assert captured["context"] == {
        "session_id": "session-a",
        "long_term_memory": {"summary": "长期摘要", "batches": []},
        "mid_term_memories": [{"id": "memory-0"}],
        "current_chunk": {
            "observations": [{"id": "observation-0"}],
            "interactions": [],
        },
        "qa_history": [],
        "memories": [{"id": "memory-0"}],
        "current_observations": [{"id": "observation-0"}],
    }
    assert captured["search"] == {
        "query": "杯子放在哪里？",
        "memories": [{"id": "session-a-memory"}],
        "evidence": ["frame-0.jpg"],
    }
    assert memory_client.interactions[0][0] == "session-a"
    assert memory_client.interactions[0][1]["answer"] == (
        "我在干什么？：杯子在柜子里。"
    )
    assert memory_client.interactions[0][1][
        "current_observation_ids"
    ] == ["observation-0"]
    assert memory_client.interactions[0][1]["context_memory_ids"] == [
        "memory-0"
    ]
    assert channel.responses[1]["payload"]["memory_writeback"] == {
        "ok": True,
        "interaction_id": "interaction-0",
    }


@pytest.mark.asyncio
async def test_voice_history_question_reenters_memory_search_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_client = _MemoryClient()
    monkeypatch.setenv("OMNIMEMORY_API_BASE", memory_client.api_base)
    monkeypatch.setattr(video_live, "_memory_client", memory_client)
    calls: list[tuple[str, bool]] = []

    async def transcribe(audio_inputs):
        assert audio_inputs
        return "刚才杯子放在哪里？"

    monkeypatch.setattr(video_live, "_transcribe_audio_inputs", transcribe)

    async def stream_answer(
        question,
        frames,
        audio_inputs,
        **options,
    ):
        del frames
        calls.append((question, bool(audio_inputs)))
        assert not audio_inputs
        result = await options["memory_search"](
            {"query": "刚才杯子放在哪里？"}
        )
        assert result["evidence"] == ["frame-0.jpg"]
        yield "刚才杯子被放进柜子里。"

    monkeypatch.setattr(video_live, "_stream_qwen_omni", stream_answer)
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.ask"](
        object(),
        "ask-voice-memory",
        {
            "question": "",
            "frames": [_frame()],
            "audio_inputs": [
                {
                    "data_url": "data:audio/wav;base64,eA==",
                    "source_label": "用户麦克风提问",
                }
            ],
        },
        "session-voice-memory",
    )

    payload = channel.responses[-1]["payload"]
    assert payload["answer"] == "刚才杯子被放进柜子里。"
    assert payload["transcript"] == "刚才杯子放在哪里？"
    assert calls == [("刚才杯子放在哪里？", False)]
    assert memory_client.interactions[-1][1]["question"] == (
        "刚才杯子放在哪里？"
    )
    assert memory_client.interactions[-1][1]["tool_calls"][0]["name"] == (
        "memory_search"
    )


@pytest.mark.asyncio
async def test_typed_question_transcribes_attached_audio_before_vl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIMEMORY_API_BASE", raising=False)
    monkeypatch.setattr(video_live, "_memory_client", None)
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: ("http://model", "key", "Qwen/Qwen3-VL-8B-Instruct"),
    )
    captured: dict[str, object] = {}

    async def transcribe(audio_inputs):
        assert audio_inputs
        return "背景正在播放产品发布会。"

    async def stream_answer(question, frames, audio_inputs, **options):
        del frames, options
        captured["question"] = question
        captured["audio_inputs"] = audio_inputs
        yield "画面里正在进行产品发布。"

    monkeypatch.setattr(video_live, "_transcribe_audio_inputs", transcribe)
    monkeypatch.setattr(video_live, "_stream_qwen_omni", stream_answer)
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.ask"](
        object(),
        "ask-text-with-audio",
        {
            "question": "画面里在做什么？",
            "frames": [_frame()],
            "audio_inputs": [
                {
                    "data_url": "data:audio/webm;base64,eA==",
                    "source_label": "共享屏幕音频",
                }
            ],
        },
        "session-text-audio",
    )

    assert captured["audio_inputs"] == []
    assert captured["question"] == (
        "画面里在做什么？\n\n当前音频转写：背景正在播放产品发布会。"
    )
    assert channel.responses[-1]["payload"]["answer"] == (
        "画面里正在进行产品发布。"
    )


@pytest.mark.asyncio
async def test_text_external_question_uses_same_video_ask_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_client = _MemoryClient()
    monkeypatch.setenv("OMNIMEMORY_API_BASE", memory_client.api_base)
    monkeypatch.setattr(video_live, "_memory_client", memory_client)

    async def search_invoke(arguments):
        assert arguments["query"] == "可口可乐公司"
        return "search evidence"

    monkeypatch.setattr(video_live.mcp_free_search, "invoke", search_invoke)

    async def choose_search(question, frames, audio_inputs, **options):
        del frames, audio_inputs
        assert question == "介绍一下可口可乐公司"
        result = await options["free_search"]({"query": "可口可乐公司"})
        assert result["results"] == "search evidence"
        yield "可口可乐公司成立于美国。"

    monkeypatch.setattr(video_live, "_stream_qwen_omni", choose_search)
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.ask"](
        object(),
        "ask-text-search",
        {"question": "介绍一下可口可乐公司", "frames": [_frame()]},
        "session-text-search",
    )

    assert channel.responses[-1]["payload"]["answer"] == (
        "可口可乐公司成立于美国。"
    )
    assert memory_client.interactions[-1][1]["tool_calls"][0]["name"] == (
        "free_search"
    )


@pytest.mark.asyncio
async def test_search_evidence_is_injected_into_deep_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def search_invoke(arguments):
        assert "可口可乐" in arguments["query"]
        return "最新财报：营收增长，估值数据见搜索结果。"

    captured_reasoning: dict[str, object] = {}

    async def run_reasoning(arguments, **options):
        del options
        captured_reasoning.update(arguments)
        return {
            "model": "deepseek-ai/DeepSeek-V3.2",
            "conclusion": "需要结合估值谨慎判断。",
        }

    async def choose_tools(question, frames, audio_inputs, **options):
        del frames, audio_inputs
        assert "股票" in question
        await options["free_search"]({"query": "可口可乐股票最新财报估值"})
        await options["deep_reasoning"]({"problem": question})
        yield "需要结合最新财报和估值谨慎判断。"

    monkeypatch.setattr(video_live.mcp_free_search, "invoke", search_invoke)
    monkeypatch.setattr(video_live, "_run_deep_reasoning", run_reasoning)
    monkeypatch.setattr(
        video_live,
        "_deep_reasoning_model_config",
        lambda: ("http://reasoning", "key", "deepseek-ai/DeepSeek-V3.2"),
    )
    monkeypatch.setattr(video_live, "_stream_qwen_omni", choose_tools)
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.ask"](
        object(),
        "ask-stock",
        {"question": "用推理模型看看可口可乐股票怎么样", "frames": [_frame()]},
        "session-stock",
    )

    assert "最新财报" in str(captured_reasoning["known_facts"])
    assert channel.responses[-1]["payload"]["answer"] == (
        "需要结合最新财报和估值谨慎判断。"
    )


@pytest.mark.asyncio
async def test_deep_reasoning_subagent_searches_before_conclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    requests: list[dict[str, Any]] = []

    class _Completions:
        async def create(self, **request):
            requests.append(request)
            if len(requests) <= 2:
                query = (
                    "可口可乐最新财报和估值"
                    if len(requests) == 1
                    else "可口可乐近期风险新闻"
                )
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=None,
                                reasoning_content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        id="research-search-1",
                                        function=SimpleNamespace(
                                            name="free_search",
                                            arguments=json.dumps(
                                                {"query": query},
                                                ensure_ascii=False,
                                            ),
                                        ),
                                    )
                                ],
                            )
                        )
                    ]
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="结合最新财报，主要风险是估值和需求放缓。",
                            reasoning_content=None,
                            tool_calls=None,
                        )
                    )
                ]
            )

    class _OpenAI:
        def __init__(self, **kwargs):
            del kwargs
            self.chat = SimpleNamespace(completions=_Completions())

        async def close(self):
            return None

    async def search_invoke(arguments):
        assert "可口可乐" in arguments["query"]
        return f"{arguments['query']}：搜索证据"

    monkeypatch.setattr(openai, "AsyncOpenAI", _OpenAI)
    monkeypatch.setattr(video_live.mcp_free_search, "invoke", search_invoke)
    monkeypatch.setattr(
        video_live,
        "_deep_reasoning_model_config",
        lambda: ("http://reasoning", "key", "deepseek-ai/DeepSeek-V3.2"),
    )
    statuses: list[str] = []

    async def status_sink(status: str) -> None:
        statuses.append(status)

    result = await video_live._run_deep_reasoning(
        {"problem": "分析可口可乐股票"},
        question="可口可乐股票怎么样？",
        memory_context=None,
        status_sink=status_sink,
    )

    assert result["conclusion"] == "结合最新财报，主要风险是估值和需求放缓。"
    assert result["search_queries"] == [
        "可口可乐最新财报和估值",
        "可口可乐近期风险新闻",
    ]
    assert "可口可乐近期风险新闻：搜索证据" in (
        requests[2]["messages"][-1]["content"]
    )
    assert "tools" not in requests[2]
    assert statuses == [
        "DSV3.2 正在搜索：可口可乐最新财报和估值",
        "DSV3.2 正在搜索：可口可乐近期风险新闻",
        "DSV3.2 正在汇总结论",
    ]


@pytest.mark.asyncio
async def test_deep_reasoning_stops_after_search_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    request_count = 0

    class _Completions:
        async def create(self, **request):
            nonlocal request_count
            del request
            request_count += 1
            if request_count == 1:
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=None,
                                reasoning_content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        id="search-failed",
                                        function=SimpleNamespace(
                                            name="free_search",
                                            arguments='{"query":"可口可乐股票"}',
                                        ),
                                    )
                                ],
                            )
                        )
                    ]
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="搜索不可用，无法可靠判断。",
                            reasoning_content=None,
                            tool_calls=None,
                        )
                    )
                ]
            )

    class _OpenAI:
        def __init__(self, **kwargs):
            del kwargs
            self.chat = SimpleNamespace(completions=_Completions())

        async def close(self):
            return None

    async def search_invoke(arguments):
        del arguments
        return "[ERROR]: free search failed: 403 Forbidden"

    monkeypatch.setattr(openai, "AsyncOpenAI", _OpenAI)
    monkeypatch.setattr(video_live.mcp_free_search, "invoke", search_invoke)
    monkeypatch.setattr(
        video_live,
        "_deep_reasoning_model_config",
        lambda: ("http://reasoning", "key", "deepseek-ai/DeepSeek-V3.2"),
    )
    statuses: list[str] = []

    async def status_sink(status: str) -> None:
        statuses.append(status)

    result = await video_live._run_deep_reasoning(
        {"problem": "分析股票"},
        question="股票怎么样？",
        memory_context=None,
        status_sink=status_sink,
    )

    assert request_count == 2
    assert result["search_queries"] == ["可口可乐股票"]
    assert result["conclusion"] == "搜索不可用，无法可靠判断。"
    assert statuses == [
        "DSV3.2 正在搜索：可口可乐股票",
        "外部搜索不可用，正在根据现有证据总结",
        "DSV3.2 正在汇总结论",
    ]


@pytest.mark.asyncio
async def test_qwen_calls_memory_search_once_before_final_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    requests: list[dict[str, Any]] = []

    class _Completions:
        async def create(self, **request):
            requests.append(request)
            if len(requests) == 1:
                tool_call = SimpleNamespace(
                    id="call-1",
                    function=SimpleNamespace(
                        name="memory_search",
                        arguments='{"query":"杯子放在哪里？"}',
                    ),
                )
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=None,
                                tool_calls=[tool_call],
                            )
                        )
                    ]
                )
            tool_call = SimpleNamespace(
                id="call-2",
                function=SimpleNamespace(
                    name="respond",
                    arguments='{"text":"杯子在柜子里。"}',
                ),
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=None,
                            tool_calls=[tool_call],
                        )
                    )
                ]
            )

    class _OpenAI:
        def __init__(self, **kwargs):
            del kwargs
            self.chat = SimpleNamespace(completions=_Completions())

        async def close(self):
            return None

    monkeypatch.setattr(openai, "AsyncOpenAI", _OpenAI)
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: ("http://model", "key", "qwen-omni"),
    )
    searches: list[dict[str, object]] = []

    async def search(arguments):
        searches.append(arguments)
        return {
            "memories": [{"summary": "杯子被放进柜子"}],
            "evidence": ["frame-120.jpg"],
        }

    answer = "".join(
        [
            part
            async for part in video_live._stream_qwen_omni(
                "杯子放在哪里？",
                [("data:image/jpeg;base64,eA==", "camera")],
                [],
                memory_context={"memories": []},
                memory_search=search,
            )
        ]
    )

    assert answer == "杯子在柜子里。"
    assert searches == [{"query": "杯子放在哪里？"}]
    assert [
        tool["function"]["name"] for tool in requests[0]["tools"]
    ] == ["respond", "silent", "memory_search"]
    assert requests[0]["tool_choice"] == "auto"
    assert requests[1]["messages"][-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_named_term_definition_searches_before_model_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    requests: list[dict[str, Any]] = []

    class _Completions:
        async def create(self, **request):
            requests.append(request)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            reasoning_content="检索后确认这是一个游戏模组。",
                            tool_calls=None,
                        )
                    )
                ]
            )

    class _OpenAI:
        def __init__(self, **kwargs):
            del kwargs
            self.chat = SimpleNamespace(completions=_Completions())

        async def close(self):
            return None

    monkeypatch.setattr(openai, "AsyncOpenAI", _OpenAI)
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: ("http://model", "key", "Qwen/Qwen3-VL-8B-Instruct"),
    )
    searches: list[dict[str, object]] = []

    async def search(arguments):
        searches.append(arguments)
        return {"results": "烈焰升腾是钢铁雄心IV的模组。"}

    answer = "".join(
        [
            part
            async for part in video_live._stream_qwen_omni(
                "烈焰升腾/钢铁雄心 是什么",
                [("data:image/jpeg;base64,eA==", "screen")],
                [],
                free_search=search,
            )
        ]
    )

    assert answer == "检索后确认这是一个游戏模组。"
    assert searches == [{"query": "烈焰升腾/钢铁雄心 是什么"}]
    assert "搜索结果" in requests[0]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_qwen_decides_to_call_deep_reasoning_then_responds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    requests: list[dict[str, Any]] = []

    class _Completions:
        async def create(self, **request):
            requests.append(request)
            if len(requests) == 1:
                name = "deep_reasoning"
                arguments = '{"problem":"比较两个方案的长期风险"}'
            else:
                name = "respond"
                arguments = '{"text":"方案 A 的长期风险更低。"}'
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    id=f"call-{len(requests)}",
                                    function=SimpleNamespace(
                                        name=name,
                                        arguments=arguments,
                                    ),
                                )
                            ],
                        )
                    )
                ]
            )

    class _OpenAI:
        def __init__(self, **kwargs):
            del kwargs
            self.chat = SimpleNamespace(completions=_Completions())

        async def close(self):
            return None

    monkeypatch.setattr(openai, "AsyncOpenAI", _OpenAI)
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: ("http://model", "key", "qwen-omni"),
    )
    reasoning_calls: list[dict[str, object]] = []

    async def reason(arguments):
        reasoning_calls.append(arguments)
        return {"conclusion": "方案 A 的长期风险更低"}

    answer = "".join(
        [
            part
            async for part in video_live._stream_qwen_omni(
                "哪个方案长期风险更低？",
                [],
                [],
                deep_reasoning=reason,
            )
        ]
    )

    assert answer == "方案 A 的长期风险更低。"
    assert reasoning_calls == [{"problem": "比较两个方案的长期风险"}]
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert requests[1]["messages"][-1]["content"] == (
        '{"conclusion": "方案 A 的长期风险更低"}'
    )


@pytest.mark.asyncio
async def test_siliconflow_plain_reasoning_content_after_search_is_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    call_count = 0

    class _Completions:
        async def create(self, **request):
            nonlocal call_count
            del request
            call_count += 1
            if call_count == 1:
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="",
                                tool_calls=None,
                                reasoning_content=(
                                    '<tool_call>{"name":"free_search",'
                                    '"arguments":{"query":"可口可乐公司介绍"}}'
                                    "</tool_call>"
                                ),
                            )
                        )
                    ]
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            tool_calls=None,
                            reasoning_content="可口可乐公司成立于1886年。",
                        )
                    )
                ]
            )

    class _OpenAI:
        def __init__(self, **kwargs):
            del kwargs
            self.chat = SimpleNamespace(completions=_Completions())

        async def close(self):
            return None

    monkeypatch.setattr(openai, "AsyncOpenAI", _OpenAI)
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: ("http://model", "key", "Qwen3-Omni-Instruct"),
    )

    async def search(arguments):
        assert arguments == {"query": "可口可乐公司介绍"}
        return {"results": "搜索结果"}

    answer = "".join(
        [
            part
            async for part in video_live._stream_qwen_omni(
                "介绍一下公司",
                [],
                [],
                free_search=search,
            )
        ]
    )

    assert answer == "可口可乐公司成立于1886年。"


@pytest.mark.asyncio
async def test_qwen_silent_action_emits_no_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    class _Completions:
        async def create(self, **request):
            del request
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    id="call-silent",
                                    function=SimpleNamespace(
                                        name="silent",
                                        arguments="{}",
                                    ),
                                )
                            ],
                        )
                    )
                ]
            )

    class _OpenAI:
        def __init__(self, **kwargs):
            del kwargs
            self.chat = SimpleNamespace(completions=_Completions())

        async def close(self):
            return None

    monkeypatch.setattr(openai, "AsyncOpenAI", _OpenAI)
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: ("http://model", "key", "qwen-omni"),
    )

    answer = "".join(
        [
            part
            async for part in video_live._stream_qwen_omni(
                "",
                [],
                [],
            )
        ]
    )

    assert answer == ""


@pytest.mark.asyncio
async def test_audio_protocol_stream_hides_split_closing_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    class _Chunks:
        def __aiter__(self):
            async def iterate():
                for content in (
                    "<transcript>介绍一下可口可乐</transcript>"
                    "<route>free_search</route><entity>可口可乐</entity>"
                    "<answer>这是",
                    "可口可乐汽水罐。</answer",
                ):
                    yield SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content=content)
                            )
                        ]
                    )

            return iterate()

    class _Completions:
        async def create(self, **request):
            del request
            return _Chunks()

    class _OpenAI:
        def __init__(self, **kwargs):
            del kwargs
            self.chat = SimpleNamespace(completions=_Completions())

        async def close(self):
            return None

    monkeypatch.setattr(openai, "AsyncOpenAI", _OpenAI)
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: ("http://model", "key", "qwen-omni"),
    )
    transcripts: list[str] = []
    decisions: list[tuple[str, str]] = []

    async def accept_transcript(value: str) -> bool:
        transcripts.append(value)
        return True

    async def accept_decision(route: str, entity: str) -> None:
        decisions.append((route, entity))

    answer = "".join(
        [
            part
            async for part in video_live._stream_qwen_omni(
                "",
                [("data:image/jpeg;base64,eA==", "camera")],
                [("data:audio/wav;base64,eA==", "用户麦克风提问")],
                transcript_sink=accept_transcript,
                voice_decision_sink=accept_decision,
            )
        ]
    )

    assert transcripts == ["介绍一下可口可乐"]
    assert decisions == [("free_search", "可口可乐")]
    assert answer == "这是可口可乐汽水罐。"
    assert "answer" not in answer


@pytest.mark.asyncio
async def test_realtime_translation_handler_emits_and_writes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_client = _MemoryClient()
    monkeypatch.setenv("OMNIMEMORY_API_BASE", memory_client.api_base)
    monkeypatch.setattr(video_live, "_memory_client", memory_client)

    async def translate(target_language, frames, context, recent_outputs):
        del frames, context, recent_outputs
        assert target_language == "中文"
        return {"action": "respond", "text": "欢迎"}

    monkeypatch.setattr(video_live, "_run_translation_action", translate)
    channel = _Channel()
    video_live.register_video_live_handler(channel)

    await channel.methods["video.task.start"](
        object(),
        "start-1",
        {"source_id": "camera", "target_language": "中文"},
        "session-a",
    )
    await channel.methods["video.observe"](
        object(),
        "observe-1",
        {"frames": [_task_frame()]},
        "session-a",
    )
    for _ in range(10):
        if memory_client.interactions:
            break
        await asyncio.sleep(0)

    task_events = [
        event
        for event in channel.events
        if event["name"] == "video.task.response"
    ]
    assert task_events[0]["payload"]["text"] == "欢迎"
    assert memory_client.interactions[0][1][
        "current_observation_ids"
    ] == ["observation-0"]
    await channel.methods["video.task.stop"](
        object(), "stop-1", {}, "session-a"
    )


@pytest.mark.asyncio
async def test_realtime_translation_prefers_native_action_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    requests: list[dict[str, Any]] = []

    class _Completions:
        async def create(self, **request):
            requests.append(request)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    function=SimpleNamespace(
                                        name="respond",
                                        arguments='{"text":"欢迎"}',
                                    )
                                )
                            ],
                        )
                    )
                ]
            )

    class _OpenAI:
        def __init__(self, **kwargs):
            del kwargs
            self.chat = SimpleNamespace(completions=_Completions())

        async def close(self):
            return None

    monkeypatch.setattr(openai, "AsyncOpenAI", _OpenAI)
    monkeypatch.setattr(
        video_live,
        "_omni_model_config",
        lambda: ("http://model", "key", "qwen-omni-native"),
    )
    video_live._action_protocol_cache.clear()

    action = await video_live._run_translation_action(
        "中文",
        [_task_frame()],
        {},
        [],
    )

    assert action == {"action": "respond", "text": "欢迎"}
    assert requests[0]["tool_choice"] == "required"
    assert [
        tool["function"]["name"] for tool in requests[0]["tools"]
    ] == ["respond", "silent"]
