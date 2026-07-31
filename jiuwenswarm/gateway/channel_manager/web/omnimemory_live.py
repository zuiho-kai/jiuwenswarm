"""OmniMemory adapter for Jiuwen's browser video streams."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx


_ALLOWED_IMAGE_PREFIXES = (
    "data:image/jpeg;base64,",
    "data:image/png;base64,",
    "data:image/webp;base64,",
)
_MAX_FRAMES = 8
_MAX_FRAME_CHARS = 1_500_000
_MAX_TOTAL_FRAME_CHARS = 6_500_000


def normalize_memory_frames(params: Any) -> list[dict[str, object]]:
    if not isinstance(params, dict):
        raise ValueError("params must be object")
    raw_frames = params.get("frames")
    if not isinstance(raw_frames, list) or not raw_frames:
        raise ValueError("frames are required")
    if len(raw_frames) > _MAX_FRAMES:
        raise ValueError(f"at most {_MAX_FRAMES} frames are allowed")

    frames: list[dict[str, object]] = []
    total_chars = 0
    for raw_frame in raw_frames:
        if not isinstance(raw_frame, dict):
            raise ValueError("each frame must be an object")
        data_url = raw_frame.get("data_url")
        if (
            not isinstance(data_url, str)
            or not data_url.startswith(_ALLOWED_IMAGE_PREFIXES)
        ):
            raise ValueError("frame must be a JPEG, PNG, or WebP data URL")
        if len(data_url) > _MAX_FRAME_CHARS:
            raise ValueError("a frame is too large")
        total_chars += len(data_url)
        if total_chars > _MAX_TOTAL_FRAME_CHARS:
            raise ValueError("total frame payload is too large")

        captured_at = raw_frame.get("captured_at")
        if (
            isinstance(captured_at, bool)
            or not isinstance(captured_at, (int, float))
            or captured_at <= 0
        ):
            raise ValueError(
                "captured_at must be a Unix timestamp in milliseconds"
            )
        source_id = str(raw_frame.get("source_id") or "").strip()
        if not source_id:
            raise ValueError("source_id is required")

        header, _, encoded = data_url.partition(",")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError("frame contains invalid base64 data") from exc
        frames.append(
            {
                "content": content,
                "mime_type": header[5:].split(";", 1)[0].lower(),
                "observed_at": datetime.fromtimestamp(
                    captured_at / 1000,
                    timezone.utc,
                ).isoformat(),
                "source_id": source_id,
            }
        )
    return frames


class OmniMemoryLiveClient:
    def __init__(self, api_base: str) -> None:
        self.api_base = api_base.rstrip("/")
        self._ingest_lock = asyncio.Lock()
        self._sessions: set[str] = set()
        self._streams: set[tuple[str, str]] = set()
        self._next_sequence: dict[tuple[str, str], int] = {}

    @staticmethod
    def _error(response: httpx.Response) -> RuntimeError:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        detail = payload.get("detail") if isinstance(payload, dict) else None
        return RuntimeError(
            str(
                detail
                or response.text
                or f"OmniMemory HTTP {response.status_code}"
            ).strip()
        )

    async def _ensure_session(
        self,
        client: httpx.AsyncClient,
        session_id: str,
    ) -> None:
        if session_id in self._sessions:
            return
        response = await client.post(
            f"{self.api_base}/v1/sessions",
            json={"session_id": session_id},
        )
        if response.status_code not in {201, 409}:
            raise self._error(response)
        self._sessions.add(session_id)

    async def _ensure_stream(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        source_id: str,
    ) -> None:
        key = (session_id, source_id)
        if key in self._streams:
            return
        await self._ensure_session(client, session_id)
        session_path = quote(session_id, safe="")
        source_path = quote(source_id, safe="")
        response = await client.post(
            f"{self.api_base}/v1/sessions/{session_path}/streams",
            json={"source_id": source_id},
        )
        if response.status_code == 201:
            self._next_sequence[key] = 0
        elif response.status_code == 409:
            status = await client.get(
                f"{self.api_base}/v1/sessions/{session_path}"
                f"/sources/{source_path}"
            )
            if status.status_code != 200:
                raise self._error(status)
            payload = status.json()
            if payload.get("kind") != "stream" or payload.get("status") != "open":
                raise RuntimeError("OmniMemory stream is not open")
            self._next_sequence[key] = int(payload["last_sequence_no"]) + 1
        else:
            raise self._error(response)
        self._streams.add(key)

    async def observe(
        self,
        session_id: str,
        frames: list[dict[str, object]],
    ) -> list[str]:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required")
        async with self._ingest_lock, httpx.AsyncClient(
            timeout=30.0
        ) as client:
            observation_ids: list[str] = []
            for frame in frames:
                source_id = str(frame["source_id"])
                await self._ensure_stream(client, session_id, source_id)
                key = (session_id, source_id)
                sequence_no = self._next_sequence[key]
                response = await client.post(
                    f"{self.api_base}/v1/sessions/"
                    f"{quote(session_id, safe='')}/streams/"
                    f"{quote(source_id, safe='')}/frames",
                    data={
                        "sequence_no": str(sequence_no),
                        "observed_at": str(frame["observed_at"]),
                    },
                    files={
                        "file": (
                            f"frame-{sequence_no}.jpg",
                            frame["content"],
                            frame["mime_type"],
                        )
                    },
                )
                if response.status_code != 200:
                    raise self._error(response)
                self._next_sequence[key] = sequence_no + 1
                observation_ids.append(str(response.json()["observation_id"]))
            return observation_ids

    async def context(
        self,
        session_id: str,
    ) -> dict[str, object]:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required")
        async with httpx.AsyncClient(timeout=3.0) as client:
            await self._ensure_session(client, session_id)
            response = await client.get(
                f"{self.api_base}/v1/sessions/"
                f"{quote(session_id, safe='')}/context",
            )
            if response.status_code != 200:
                raise self._error(response)
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("OmniMemory returned an invalid response")
            return payload

    async def search(
        self,
        session_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required")
        if not isinstance(arguments, dict):
            raise ValueError("memory search arguments must be an object")
        async with httpx.AsyncClient(timeout=10.0) as client:
            await self._ensure_session(client, session_id)
            response = await client.post(
                f"{self.api_base}/v1/sessions/"
                f"{quote(session_id, safe='')}/search",
                json=arguments,
            )
            if response.status_code != 200:
                raise self._error(response)
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("OmniMemory returned an invalid response")
            return payload

    async def write_interaction(
        self,
        session_id: str,
        record: dict[str, object],
    ) -> dict[str, object]:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required")
        if not isinstance(record, dict):
            raise ValueError("interaction record must be an object")
        async with httpx.AsyncClient(timeout=3.0) as client:
            await self._ensure_session(client, session_id)
            response = await client.post(
                f"{self.api_base}/v1/sessions/"
                f"{quote(session_id, safe='')}/interactions",
                json=record,
            )
            if response.status_code != 201:
                raise self._error(response)
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("OmniMemory returned an invalid response")
            return payload
