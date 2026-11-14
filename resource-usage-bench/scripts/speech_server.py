"""Free speech server for the voice bench: faster-whisper (STT) + Piper (TTS) behind OpenAI-style endpoints,
so a standard LiveKit Agents worker can use them through `livekit-plugins-openai` (base_url = this server).

  POST /v1/audio/transcriptions   multipart: file, model, language?   -> {"text": "..."}
  POST /v1/audio/speech           json: input, voice (en|ar|hi|zh), response_format (pcm|wav) -> audio

Env: MODELS (default /models), STT_DEVICE cpu|cuda, STT_COMPUTE int8|float16, STT_THREADS, PORT (8100).
Piper voices are 22.05 kHz; PCM output is resampled to 24 kHz mono 16-bit (what the OpenAI TTS plugin expects).
"""

from __future__ import annotations

import asyncio
import io
import os
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

MODELS = Path(os.environ.get("MODELS", "/models"))
VOICES = {"en": "en/en_US/lessac/medium/en_US-lessac-medium.onnx", "ar": "ar/ar_JO/kareem/medium/ar_JO-kareem-medium.onnx",
          "hi": "hi/hi_IN/pratham/medium/hi_IN-pratham-medium.onnx", "zh": "zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx"}
OUT_RATE = 24_000

app = FastAPI()
STATE: dict[str, Any] = {"stt_calls": 0, "stt_audio_s": 0.0, "stt_busy_s": 0.0, "tts_calls": 0, "tts_audio_s": 0.0,
                         "tts_busy_s": 0.0}


@app.on_event("startup")
def load() -> None:
    from faster_whisper import WhisperModel
    from piper import PiperVoice

    t0 = time.perf_counter()
    app.state.stt = WhisperModel(str(MODELS / "faster-whisper-large-v3-turbo"), device=os.environ.get("STT_DEVICE", "cpu"),
                                 compute_type=os.environ.get("STT_COMPUTE", "int8"),
                                 cpu_threads=int(os.environ.get("STT_THREADS", "4")), num_workers=4)
    app.state.voices = {k: PiperVoice.load(str(MODELS / "piper-voices" / v)) for k, v in VOICES.items()}
    STATE["load_s"] = round(time.perf_counter() - t0, 1)


@app.get("/stats")
def stats() -> dict[str, Any]:
    """Work counters, plus this process's RAM and the card's VRAM (when the server runs outside Docker,
    `docker stats` cannot see it)."""
    out: dict[str, Any] = dict(STATE)
    try:
        import psutil

        out["rss_mb"] = round(psutil.Process().memory_info().rss / 1e6, 1)
    except ImportError:
        pass
    if os.environ.get("STT_DEVICE") == "cuda":
        import subprocess

        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, check=False).stdout.strip().split(",")
        if len(r) == 2:
            out["gpu_mem_used_mb"], out["gpu_util_pct"] = float(r[0]), float(r[1])
    return out


def wav_to_16k(src: Any) -> np.ndarray:
    """WAV (path or file object) -> 16 kHz mono float32, the input faster-whisper wants. Decoding here avoids
    faster-whisper's PyAV path: faster-whisper 1.2.1 passes `metadata_errors`, which PyAV 19 no longer accepts."""
    with wave.open(src) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != 16_000 and x.size:
        x = np.interp(np.linspace(0, x.size - 1, int(x.size * 16_000 / rate)), np.arange(x.size), x)
    return x.astype(np.float32)


def _transcribe(data: bytes, language: str | None) -> tuple[str, float]:
    segs, info = app.state.stt.transcribe(wav_to_16k(io.BytesIO(data)), language=language or None, beam_size=1,
                                          vad_filter=False)
    return " ".join(s.text.strip() for s in segs), float(info.duration)


@app.post("/v1/audio/transcriptions")
async def transcriptions(file: UploadFile = File(...), model: str = Form("whisper"),  # noqa: B008
                         language: str | None = Form(None)) -> JSONResponse:
    data = await file.read()
    t0 = time.perf_counter()
    text, dur = await asyncio.to_thread(_transcribe, data, language)
    STATE["stt_calls"] += 1
    STATE["stt_audio_s"] += dur
    STATE["stt_busy_s"] += time.perf_counter() - t0
    return JSONResponse({"text": text})


def _resample(pcm: bytes, rate: int) -> bytes:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if rate == OUT_RATE or x.size == 0:
        return pcm
    n = int(x.size * OUT_RATE / rate)
    y = np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x)
    return y.astype(np.int16).tobytes()


@app.post("/v1/audio/speech")
async def speech(request: Request) -> Response:
    req = await request.json()
    voice = app.state.voices.get(str(req.get("voice", "en"))[:2], app.state.voices["en"])
    text = str(req.get("input", ""))
    fmt = req.get("response_format", "pcm")
    rate = voice.config.sample_rate

    def chunks() -> list[bytes]:
        return [_resample(c.audio_int16_bytes, rate) for c in voice.synthesize(text)]

    t0 = time.perf_counter()
    parts = await asyncio.to_thread(chunks)
    pcm = b"".join(parts)
    STATE["tts_calls"] += 1
    STATE["tts_audio_s"] += len(pcm) / 2 / OUT_RATE
    STATE["tts_busy_s"] += time.perf_counter() - t0
    if fmt == "wav":
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(OUT_RATE)
            w.writeframes(pcm)
        return Response(buf.getvalue(), media_type="audio/wav")
    return StreamingResponse(iter(parts), media_type="audio/pcm")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8100")), log_level="warning")  # noqa: S104
