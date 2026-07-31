import base64
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
    ):
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
async def test_qwen_calls_memory_search_once_before_final_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    requests: list[dict[str, Any]] = []

    class _Chunks:
        def __aiter__(self):
            async def iterate():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="杯子在柜子里。")
                        )
                    ]
                )

            return iterate()

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
    assert requests[0]["tools"][0]["function"]["name"] == "memory_search"
    assert requests[1]["messages"][-1]["role"] == "tool"
