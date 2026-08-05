"""Single-source, latest-frame realtime video task runtime."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


ActionCall = Callable[
    [str, list[dict[str, object]], dict[str, object], list[str]],
    Awaitable[dict[str, str]],
]
EmitCall = Callable[[dict[str, object]], Awaitable[None]]


@dataclass(slots=True)
class _PendingWriteback:
    turn_id: str
    text: str
    frames: list[dict[str, object]]


@dataclass(slots=True)
class _TaskState:
    session_id: str
    source_id: str
    target_language: str
    emit: EmitCall
    memory_client: Any
    context: dict[str, object]
    context_version: int
    model: str
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    slot: dict[str, object] | None = None
    previous_frame: dict[str, object] | None = None
    last_frame_seq: int = -1
    recent_outputs: list[str] = field(default_factory=list)
    observation_map: OrderedDict[str, tuple[str, float]] = field(
        default_factory=OrderedDict
    )
    pending_writebacks: list[_PendingWriteback] = field(default_factory=list)
    worker: asyncio.Task[None] | None = None
    refresh_task: asyncio.Task[None] | None = None
    processed: int = 0
    responded: int = 0
    silent: int = 0
    dropped: int = 0
    last_error: str | None = None


class VideoInteractionRuntime:
    """Runs one persistent realtime task per Jiuwen session."""

    def __init__(self, action_call: ActionCall) -> None:
        self.action_call = action_call
        self._tasks: dict[str, _TaskState] = {}

    async def start(
        self,
        *,
        session_id: str,
        source_id: str,
        target_language: str,
        emit: EmitCall,
        memory_client: Any = None,
        context: dict[str, object] | None = None,
        model: str = "",
    ) -> dict[str, object]:
        await self.stop(session_id)
        state = _TaskState(
            session_id=session_id,
            source_id=source_id,
            target_language=target_language,
            emit=emit,
            memory_client=memory_client,
            context=context or {},
            context_version=int((context or {}).get("context_version", 0)),
            model=model,
        )
        self._tasks[session_id] = state
        state.worker = asyncio.create_task(
            self._run(state),
            name=f"video-translation-{session_id}",
        )
        return self.status(session_id)

    async def stop(self, session_id: str) -> dict[str, object]:
        state = self._tasks.pop(session_id, None)
        if state is None:
            return {"running": False, "session_id": session_id}
        for task in (state.worker, state.refresh_task):
            if task is not None and not task.done():
                task.cancel()
        if state.worker is not None:
            await asyncio.gather(state.worker, return_exceptions=True)
        return {"running": False, "session_id": session_id}

    def status(self, session_id: str) -> dict[str, object]:
        state = self._tasks.get(session_id)
        if state is None:
            return {"running": False, "session_id": session_id}
        return {
            "running": True,
            "session_id": session_id,
            "source_id": state.source_id,
            "target_language": state.target_language,
            "last_frame_seq": state.last_frame_seq,
            "context_version": state.context_version,
            "processed": state.processed,
            "responded": state.responded,
            "silent": state.silent,
            "dropped": state.dropped,
            "last_error": state.last_error,
            "model": state.model,
        }

    def offer_frame(
        self,
        session_id: str,
        frame: dict[str, object],
    ) -> bool:
        state = self._tasks.get(session_id)
        if state is None or frame.get("source_id") != state.source_id:
            return False
        frame_seq = frame.get("frame_seq")
        if (
            isinstance(frame_seq, bool)
            or not isinstance(frame_seq, int)
            or frame_seq <= state.last_frame_seq
        ):
            return False
        state.last_frame_seq = frame_seq
        if state.slot is not None:
            state.dropped += 1
        state.slot = frame.copy()
        state.wake.set()
        return True

    def bind_observation(
        self,
        *,
        session_id: str,
        client_frame_id: str,
        observation_id: str,
        context_version: int,
    ) -> None:
        state = self._tasks.get(session_id)
        if state is None:
            return
        self._cleanup_observation_map(state)
        state.observation_map[client_frame_id] = (
            observation_id,
            time.monotonic(),
        )
        state.observation_map.move_to_end(client_frame_id)
        while len(state.observation_map) > 512:
            state.observation_map.popitem(last=False)
        loaded_context_version = int(
            state.context.get("context_version", 0)
        )
        if context_version > loaded_context_version:
            state.context_version = max(
                state.context_version, context_version
            )
            self._schedule_context_refresh(state)
        self._flush_writebacks(state)

    async def _run(self, state: _TaskState) -> None:
        while True:
            await state.wake.wait()
            state.wake.clear()
            frame = state.slot
            state.slot = None
            if frame is None:
                continue
            frames = [
                item
                for item in (state.previous_frame, frame)
                if item is not None
            ]
            try:
                action = await self.action_call(
                    state.target_language,
                    frames,
                    state.context.copy(),
                    list(state.recent_outputs),
                )
                state.processed += 1
                if action.get("action") == "respond":
                    text = str(action.get("text") or "").strip()
                    if text:
                        state.responded += 1
                        state.recent_outputs.append(text)
                        del state.recent_outputs[:-10]
                        turn_id = str(uuid4())
                        await state.emit(
                            {
                                "task_turn_id": turn_id,
                                "task_type": "realtime_translation",
                                "text": text,
                                "source_id": state.source_id,
                                "frame_seq": frame["frame_seq"],
                                "captured_at": frame["captured_at"],
                            }
                        )
                        if state.memory_client is not None:
                            state.pending_writebacks.append(
                                _PendingWriteback(turn_id, text, [frame])
                            )
                            self._flush_writebacks(state)
                    else:
                        state.silent += 1
                else:
                    state.silent += 1
                state.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                state.last_error = str(exc).strip() or type(exc).__name__
                try:
                    await state.emit(
                        {
                            "task_type": "realtime_translation",
                            "error": state.last_error,
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass
            finally:
                state.previous_frame = frame

    def _flush_writebacks(self, state: _TaskState) -> None:
        if state.memory_client is None or not state.pending_writebacks:
            return
        ready: list[tuple[_PendingWriteback, list[str]]] = []
        waiting: list[_PendingWriteback] = []
        for pending in state.pending_writebacks:
            observation_ids: list[str] = []
            for frame in pending.frames:
                mapped = state.observation_map.get(
                    str(frame["client_frame_id"])
                )
                if mapped is None:
                    break
                observation_ids.append(mapped[0])
            else:
                ready.append((pending, observation_ids))
                continue
            waiting.append(pending)
        state.pending_writebacks = waiting[-32:]
        for pending, observation_ids in ready:
            asyncio.create_task(
                self._writeback(state, pending, observation_ids)
            )

    async def _writeback(
        self,
        state: _TaskState,
        pending: _PendingWriteback,
        observation_ids: list[str],
    ) -> None:
        mid_term = state.context.get("mid_term_memories")
        record = {
            "question": (
                f"持续把画面中的新内容翻译成{state.target_language}"
            ),
            "answer": pending.text,
            "asked_at": datetime.now(timezone.utc).isoformat(),
            "observed_at": datetime.fromtimestamp(
                float(pending.frames[-1]["captured_at"]) / 1000,
                timezone.utc,
            ).isoformat(),
            "model": state.model or "Qwen3-Omni",
            "task_turn_id": pending.turn_id,
            "task_type": "realtime_translation",
            "source_ids": [state.source_id],
            "current_observation_ids": list(dict.fromkeys(observation_ids)),
            "context_memory_ids": [
                item["id"]
                for item in mid_term or []
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ],
            "tool_calls": [],
        }
        try:
            await state.memory_client.write_interaction(
                state.session_id,
                record,
            )
            for frame in pending.frames:
                state.observation_map.pop(
                    str(frame["client_frame_id"]), None
                )
        except Exception as exc:  # noqa: BLE001
            state.last_error = (
                str(exc).strip() or "interaction writeback failed"
            )

    def _schedule_context_refresh(self, state: _TaskState) -> None:
        if state.memory_client is None:
            return
        if state.refresh_task is not None and not state.refresh_task.done():
            return

        async def refresh() -> None:
            loaded_version = -1
            try:
                state.context = await state.memory_client.context(
                    state.session_id
                )
                loaded_version = int(
                    state.context.get(
                        "context_version", state.context_version
                    )
                )
                state.context_version = max(
                    state.context_version, loaded_version
                )
            except Exception as exc:  # noqa: BLE001
                state.last_error = str(exc).strip() or "context refresh failed"
            finally:
                state.refresh_task = None

        state.refresh_task = asyncio.create_task(refresh())

    @staticmethod
    def _cleanup_observation_map(state: _TaskState) -> None:
        cutoff = time.monotonic() - 600
        expired = [
            key
            for key, (_, created_at) in state.observation_map.items()
            if created_at < cutoff
        ]
        for key in expired:
            state.observation_map.pop(key, None)
