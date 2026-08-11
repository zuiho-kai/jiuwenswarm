from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from faster_whisper import WhisperModel


MODEL_ID = os.environ.get(
    "ASR_MODEL_ID", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
).strip()
API_KEY = os.environ.get("ASR_API_KEY", "EMPTY").strip()
DEVICE = os.environ.get("ASR_DEVICE", "cuda").strip()
COMPUTE_TYPE = os.environ.get("ASR_COMPUTE_TYPE", "float16").strip()
MAX_UPLOAD_BYTES = int(os.environ.get("ASR_MAX_UPLOAD_BYTES", str(32 * 1024 * 1024)))

app = FastAPI(title="JiuWenSwarm ASR", version="1.0.0")
_model: WhisperModel | None = None
_inference_lock = asyncio.Lock()


@app.on_event("startup")
def load_model() -> None:
    global _model
    _model = WhisperModel(MODEL_ID, device=DEVICE, compute_type=COMPUTE_TYPE)


def _require_auth(authorization: str | None) -> None:
    if API_KEY and authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Invalid API key")


def _run_transcription(
    path: Path,
    language: str | None,
    prompt: str | None,
) -> dict[str, Any]:
    if _model is None:
        raise RuntimeError("ASR model is not ready")
    segments, info = _model.transcribe(
        str(path),
        language=language or None,
        initial_prompt=prompt or None,
        beam_size=1,
        temperature=0.0,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
        condition_on_previous_text=False,
    )
    segment_list = list(segments)
    return {
        "text": "".join(segment.text for segment in segment_list).strip(),
        "language": info.language,
        "duration": info.duration,
        "segments": [
            {
                "id": index,
                "start": segment.start,
                "end": segment.end,
                "text": segment.text,
            }
            for index, segment in enumerate(segment_list)
        ],
    }


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok" if _model is not None else "loading",
        "model": MODEL_ID,
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
    }


@app.get("/v1/models")
def models(authorization: str | None = Header(default=None)) -> dict[str, object]:
    _require_auth(authorization)
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}],
    }


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str = Form(default=MODEL_ID),
    language: str | None = Form(default=None),
    prompt: str | None = Form(default=None),
    response_format: str = Form(default="json"),
    authorization: str | None = Header(default=None),
):
    _require_auth(authorization)
    if model not in {MODEL_ID, "whisper-1", "faster-whisper"}:
        raise HTTPException(status_code=404, detail=f"Model {model!r} is not available")

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="The audio file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The audio file is too large")

    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        async with _inference_lock:
            result = await asyncio.to_thread(
                _run_transcription,
                temporary_path,
                language,
                prompt,
            )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    if response_format == "text":
        return PlainTextResponse(result["text"])
    if response_format == "verbose_json":
        return JSONResponse(result)
    return {"text": result["text"]}
