import asyncio
from typing import Any

import pytest

from jiuwenswarm.gateway.channel_manager.web.video_interaction import (
    VideoInteractionRuntime,
)


def _frame(sequence: int) -> dict[str, object]:
    return {
        "client_frame_id": f"frame-{sequence}",
        "frame_seq": sequence,
        "source_id": "screen",
        "source_label": "screen",
        "captured_at": 1_785_814_400_000 + sequence * 1_000,
        "data_url": "data:image/jpeg;base64,eA==",
    }


class _MemoryClient:
    def __init__(self) -> None:
        self.written: list[dict[str, object]] = []
        self.written_event = asyncio.Event()

    async def write_interaction(
        self, session_id: str, record: dict[str, object]
    ) -> dict[str, object]:
        assert session_id == "session-a"
        self.written.append(record)
        self.written_event.set()
        return {"id": "interaction-1"}

    async def context(self, session_id: str) -> dict[str, object]:
        return {"session_id": session_id, "context_version": 1}


@pytest.mark.asyncio
async def test_latest_frame_task_emits_then_writes_exact_observation() -> None:
    calls: list[list[dict[str, object]]] = []

    async def action_call(
        target_language: str,
        frames: list[dict[str, object]],
        context: dict[str, object],
        recent_outputs: list[str],
    ) -> dict[str, str]:
        del context, recent_outputs
        assert target_language == "中文"
        calls.append(frames)
        return {"action": "respond", "text": "你好"}

    emitted: list[dict[str, object]] = []
    async def emit(payload: dict[str, object]) -> None:
        emitted.append(payload)

    memory = _MemoryClient()
    runtime = VideoInteractionRuntime(action_call)
    await runtime.start(
        session_id="session-a",
        source_id="screen",
        target_language="中文",
        emit=emit,
        memory_client=memory,
        context={"context_version": 0, "mid_term_memories": []},
        model="qwen-omni",
    )
    assert runtime.offer_frame("session-a", _frame(0)) is True
    runtime.bind_observation(
        session_id="session-a",
        client_frame_id="frame-0",
        observation_id="observation-0",
        context_version=0,
    )

    await asyncio.wait_for(memory.written_event.wait(), timeout=1)
    assert emitted[0]["text"] == "你好"
    assert memory.written[0]["current_observation_ids"] == [
        "observation-0"
    ]
    assert memory.written[0]["task_type"] == "realtime_translation"
    assert len(calls[0]) == 1
    await runtime.stop("session-a")


@pytest.mark.asyncio
async def test_silent_action_does_not_write_memory() -> None:
    async def action_call(*args: Any) -> dict[str, str]:
        del args
        return {"action": "silent", "text": ""}

    memory = _MemoryClient()
    runtime = VideoInteractionRuntime(action_call)
    await runtime.start(
        session_id="session-a",
        source_id="screen",
        target_language="中文",
        emit=lambda payload: asyncio.sleep(0),
        memory_client=memory,
    )
    runtime.offer_frame("session-a", _frame(0))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert runtime.status("session-a")["silent"] == 1
    assert memory.written == []
    await runtime.stop("session-a")
