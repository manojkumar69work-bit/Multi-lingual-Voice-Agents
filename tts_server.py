"""Local Indic TTS microservice — FastAPI on port 8002.

Multi-provider with smart fallback:
  1. Sarvam Bulbul v3 (best, requires SARVAM_API_KEY)
  2. Edge TTS Neural (fallback)
  3. MMS-TTS local (dev only — non-commercial license)

Endpoints:
  GET  /health              — liveness + active providers
  POST /synthesize          — body {text, language, voice?, provider?, pace?} → WAV (base64)
  POST /stream              — body {text, language, voice?, provider?, pace?} → streaming WAV

Contract kept compatible with the existing frontend (`{audio: base64}`).
"""

from __future__ import annotations

from dotenv import load_dotenv

# Load SARVAM_API_KEY and friends from .env at process start.
load_dotenv()

import asyncio
import base64
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from tts_engine import (
    DEFAULT_VOICES,
    chunk_text,
    detect_lang,
    normalize_text,
    synthesizer,
)

# Default speech rate. 1.0 = normal, 1.4 ≈ natural fast conversational,
# 1.5+ starts sounding rushed. Override with TTS_PACE env var.
DEFAULT_PACE = float(os.environ.get("TTS_PACE", "1.4"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("tts-service")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    active = list(synthesizer.providers)
    logger.info("TTS service starting. Active providers (priority): %s", active)
    if "sarvam" not in synthesizer.providers:
        logger.warning(
            "SARVAM_API_KEY not set — falling back to Edge TTS. "
            "Get a free key at https://dashboard.sarvam.ai for best quality."
        )
    if "mms" in synthesizer.providers:
        logger.warning(
            "MMS-TTS provider enabled. CC-BY-NC-4.0 license — NOT for production/commercial use."
        )
    yield
    logger.info("TTS service shutting down")


app = FastAPI(
    title="Local Indic TTS",
    version="2.0.0",
    lifespan=lifespan,
)


class SynthesizeRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    language: str = Field(default="auto", description="hi | te | en | auto")
    voice: Optional[str] = Field(
        default=None,
        description="Voice override (Sarvam speaker name or Edge voice ID)",
    )
    provider: Optional[str] = Field(
        default=None,
        description="Force a specific provider: 'sarvam' | 'edge' | 'mms'",
    )
    pace: Optional[float] = Field(
        default=None,
        description="Speech speed: 1.0 = normal, 1.4 ≈ natural fast conversational",
    )


@app.get("/")
def root() -> dict:
    """Service info. The TTS Studio UI has been retired; this is now an API-only service."""
    return {
        "service": "Local Indic TTS",
        "version": "2.0.0",
        "ok": True,
        "endpoints": ["/health", "/synthesize", "/stream"],
    }


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "default_pace": DEFAULT_PACE,
        "providers": {
            name: {"available": True, "type": name}
            for name in synthesizer.providers
        },
        "primary": synthesizer.primary,
        "voices": {
            lang: {
                "sarvam_speaker": v.sarvam_speaker,
                "sarvam_lang": v.sarvam_lang,
                "edge_voice": v.edge_voice,
            }
            for lang, v in DEFAULT_VOICES.items()
        },
    }


@app.post("/synthesize")
async def synthesize(req: SynthesizeRequest) -> JSONResponse:
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Empty text")

    lang = _resolve_lang(req.language, text)

    pace = req.pace if req.pace is not None else DEFAULT_PACE
    t0 = time.time()
    try:
        wav_bytes, used = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: synthesizer.synthesize(text, lang, preferred=req.provider, voice=req.voice, pace=pace),
        )
    except Exception as e:
        logger.exception("Synthesis failed")
        raise HTTPException(500, f"All TTS providers failed: {e}") from e

    elapsed_ms = int((time.time() - t0) * 1000)
    duration_ms = _wav_duration_ms(wav_bytes)
    audio_b64 = base64.b64encode(wav_bytes).decode("ascii")

    logger.info(
        "synth provider=%s lang=%s chars=%d audio=%d KB duration=%d ms in %d ms",
        used, lang, len(text), len(wav_bytes) // 1024, duration_ms, elapsed_ms,
    )

    return JSONResponse(
        {
            "audio": audio_b64,
            "language": lang,
            "provider": used,
            "voice": (req.voice or _default_voice(lang, used)),
            "duration_ms": duration_ms,
            "synthesis_ms": elapsed_ms,
            "format": "wav_pcm16_mono",
        }
    )


@app.post("/stream")
async def stream_synthesize(req: SynthesizeRequest) -> StreamingResponse:
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Empty text")
    lang = _resolve_lang(req.language, text)
    normalized = normalize_text(text, lang)
    chunks = chunk_text(normalized)

    async def gen() -> AsyncIterator[bytes]:
        loop = asyncio.get_event_loop()
        pace = req.pace if req.pace is not None else DEFAULT_PACE
        for i, ck in enumerate(chunks):
            t0 = time.time()
            wav, used = await loop.run_in_executor(
                None,
                lambda c=ck: synthesizer.synthesize(c, lang, preferred=req.provider, voice=req.voice, pace=pace),
            )
            logger.info(
                "stream provider=%s chunk %d/%d (%d chars, %d ms audio) in %d ms",
                used, i + 1, len(chunks), len(ck),
                _wav_duration_ms(wav), int((time.time() - t0) * 1000),
            )
            yield wav
            await asyncio.sleep(0)

    return StreamingResponse(
        gen(),
        media_type="audio/wav",
        headers={
            "X-TTS-Language": lang,
            "X-TTS-Chunks": str(len(chunks)),
            "X-TTS-Provider": synthesizer.primary or "none",
            "Transfer-Encoding": "chunked",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_lang(requested: str, text: str) -> str:
    if requested and requested != "auto":
        if requested in DEFAULT_VOICES:
            return requested
        raise HTTPException(400, f"Unsupported language: {requested}")
    detected = detect_lang(text)
    if detected not in DEFAULT_VOICES:
        raise HTTPException(400, f"Auto-detected language '{detected}' not supported")
    return detected


def _default_voice(lang: str, provider: str) -> str:
    v = DEFAULT_VOICES.get(lang, DEFAULT_VOICES["en"])
    return v.sarvam_speaker if provider == "sarvam" else v.edge_voice


def _wav_duration_ms(wav_bytes: bytes) -> int:
    """Calculate WAV duration from the actual header (sample_rate, bits, channels)."""
    if len(wav_bytes) < 44 or wav_bytes[:4] != b"RIFF":
        return 0
    try:
        import struct
        channels = struct.unpack_from("<H", wav_bytes, 22)[0] or 1
        sample_rate = struct.unpack_from("<I", wav_bytes, 24)[0] or 24000
        bps = struct.unpack_from("<H", wav_bytes, 34)[0] or 16
        bytes_per_sec = sample_rate * channels * (bps // 8)
        pcm_bytes = max(0, len(wav_bytes) - 44)
        return int(pcm_bytes / bytes_per_sec * 1000) if bytes_per_sec else 0
    except Exception:
        pcm_bytes = max(0, len(wav_bytes) - 44)
        return int(pcm_bytes / 48_000 * 1000)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002, log_level="info")
