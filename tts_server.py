"""Local Indic TTS microservice — FastAPI on port 8002.

Multi-provider with smart fallback:
  1. Sarvam Bulbul v3 (best, requires SARVAM_API_KEY)
  2. Edge TTS Neural (fallback)
  3. MMS-TTS local (dev only — non-commercial license)

Endpoints:
  GET  /health       — liveness + active providers
  POST /synthesize   — {text, language, voice?, provider?, pace?, temperature?} → WAV (base64)
  POST /stream       — same body → streaming audio, format declared in X-TTS-Format:
                         pcm_s16le  raw PCM from Sarvam's WebSocket (normal path)
                         wav_chunks one WAV per sentence (fallback path)

Contract kept compatible with the existing frontend (`{audio: base64}`).
"""

from __future__ import annotations

from dotenv import load_dotenv

# Load SARVAM_API_KEY and friends from .env at process start.
load_dotenv()

import asyncio
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import numpy as np
import websockets
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from tts_engine import (
    DEFAULT_TEMPERATURE,
    DEFAULT_VOICES,
    chunk_text,
    detect_lang,
    normalize_text,
    synthesizer,
)

# Default speech rate. Bulbul v3 accepts 0.5–2.0 and is trained at 1.0, so 1.0
# is where it sounds human; this used to default to 1.4 (40% fast), which reads
# as a rushed machine however good the voice is. Override with TTS_PACE.
DEFAULT_PACE = float(os.environ.get("TTS_PACE", "1.0"))

# ─── Sarvam streaming WebSocket ──────────────────────────────────────────────
# /stream used to synthesize one HTTP request per sentence and concatenate the
# WAVs. That is what made the agent sound stitched together: Bulbul v3 infers
# emphasis, pauses and pacing across the WHOLE input (up to 2500 chars), so
# feeding it 180-char fragments threw that away and restarted pitch and energy
# from neutral at every sentence — with each fragment's leading and trailing
# silence concatenated into the gaps.
#
# One WebSocket per reply fixes both: continuous prosody, and audio starts as
# soon as Sarvam has buffered enough to begin. linear16 is verified to work here
# (the REST docs claim MP3-only, which is stale), so the audio arrives as raw
# PCM and no ffmpeg decode is needed anywhere in the call path.
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")
SARVAM_MODEL = os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3")
SARVAM_WS_URL = (
    f"wss://api.sarvam.ai/text-to-speech/ws?model={SARVAM_MODEL}"
    "&send_completion_event=true"
)
STREAM_SAMPLE_RATE = int(os.environ.get("TTS_SAMPLE_RATE", "24000"))

# Sarvam's own buffering, which replaces this module's chunk_text() on the
# streaming path. min_buffer_size trades time-to-first-audio against how much
# context the prosody model gets; max_chunk_length caps a single synthesis unit.
SARVAM_MIN_BUFFER = int(os.environ.get("TTS_MIN_BUFFER", "50"))
SARVAM_MAX_CHUNK = int(os.environ.get("TTS_MAX_CHUNK", "250"))

# How long to wait for Sarvam's first audio frame before giving up and falling
# back to the chunked HTTP path.
SARVAM_FIRST_FRAME_TIMEOUT = float(os.environ.get("TTS_FIRST_FRAME_TIMEOUT", "10"))
SARVAM_FRAME_TIMEOUT = float(os.environ.get("TTS_FRAME_TIMEOUT", "20"))

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
        description="Speech speed, 0.5–2.0. 1.0 is the rate Bulbul v3 is trained at.",
    )
    temperature: Optional[float] = Field(
        default=None,
        description="Sarvam v3 prosody variation, 0.01–2.0 (default 0.6). Ignored by other providers.",
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
        "default_temperature": DEFAULT_TEMPERATURE,
        "streaming": "sarvam-websocket" if SARVAM_API_KEY else "chunked-http",
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
    temperature = req.temperature if req.temperature is not None else DEFAULT_TEMPERATURE
    t0 = time.time()
    try:
        wav_bytes, used = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: synthesizer.synthesize(
                text, lang, preferred=req.provider, voice=req.voice,
                pace=pace, temperature=temperature,
            ),
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


# Silence gate. Sarvam prepends ~650 ms of digital silence to a reply and leaves
# ~300 ms on the end; on a call that is dead air added to every single turn, and
# it reads as the agent hesitating before it answers.
#
# The gate drops silence at the START and END of a reply only. Pauses BETWEEN
# sentences are held back and re-emitted intact the moment speech resumes —
# those are Bulbul v3's inferred prosodic pauses and are exactly what we spent
# the WebSocket work to get, so removing them would undo the point.
SILENCE_RMS_THRESHOLD = float(os.environ.get("TTS_SILENCE_RMS", "150"))
_GATE_WINDOW_SAMPLES = 240  # 10 ms at 24 kHz
_GATE_PREROLL_WINDOWS = 2   # keep 20 ms before the first sound, so onsets don't click


async def _gate_silence(frames: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Trim leading/trailing silence from a PCM stream, preserving inner pauses."""
    win_bytes = _GATE_WINDOW_SAMPLES * 2  # s16
    started = False
    held: list[bytes] = []   # silence seen since the last speech
    carry = b""              # partial window carried across frame boundaries

    async for frame in frames:
        buf = carry + frame
        n = len(buf) // win_bytes * win_bytes
        carry = buf[n:]
        if not n:
            continue
        # RMS per window in one vectorised pass. int16 squares overflow, so
        # widen before multiplying.
        windows = (
            np.frombuffer(buf[:n], dtype="<i2")
            .reshape(-1, _GATE_WINDOW_SAMPLES)
            .astype(np.float32)
        )
        loud = np.sqrt((windows ** 2).mean(axis=1)) > SILENCE_RMS_THRESHOLD

        out: list[bytes] = []
        for i, is_speech in enumerate(loud):
            window = buf[i * win_bytes : (i + 1) * win_bytes]
            if is_speech:
                if not started:
                    # Keep a short pre-roll so the first syllable isn't clipped.
                    held = held[-_GATE_PREROLL_WINDOWS:]
                    started = True
                out.extend(held)
                held = []
                out.append(window)
            else:
                held.append(window)
        if out:
            yield b"".join(out)

    # `held` and `carry` are whatever trailed the last sound — dropped.


async def _sarvam_ws_pcm(
    text: str, lang: str, voice: Optional[str], pace: float, temperature: float
) -> AsyncIterator[bytes]:
    """Yield raw s16le mono PCM for `text` from one Sarvam WebSocket session.

    Raises before the first frame if the connection or config is rejected, which
    is what lets the caller fall back to the chunked HTTP path while it still
    can — once a byte of the response body is out, the format is committed.
    """
    profile = DEFAULT_VOICES.get(lang) or DEFAULT_VOICES["hi"]
    config = {
        "target_language_code": profile.sarvam_lang,
        "speaker": voice or profile.sarvam_speaker,
        "model": SARVAM_MODEL,
        "pace": pace,
        "speech_sample_rate": STREAM_SAMPLE_RATE,
        "output_audio_codec": "linear16",
        "min_buffer_size": SARVAM_MIN_BUFFER,
        "max_chunk_length": SARVAM_MAX_CHUNK,
    }
    # temperature is v3-only; sending it to v2 is an error rather than a no-op.
    if SARVAM_MODEL.endswith("v3"):
        config["temperature"] = temperature

    async with websockets.connect(
        SARVAM_WS_URL, additional_headers={"api-subscription-key": SARVAM_API_KEY}
    ) as ws:
        await ws.send(json.dumps({"type": "config", "data": config}))
        # The whole reply in one go — Sarvam buffers it per min_buffer_size, and
        # sees the full text when inferring prosody.
        await ws.send(json.dumps({"type": "text", "data": {"text": text}}))
        await ws.send(json.dumps({"type": "flush"}))

        first = True
        while True:
            timeout = SARVAM_FIRST_FRAME_TIMEOUT if first else SARVAM_FRAME_TIMEOUT
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "audio":
                audio = base64.b64decode(msg["data"]["audio"])
                if audio:
                    first = False
                    yield audio
            elif kind == "error":
                raise RuntimeError(f"Sarvam stream error: {msg.get('data')}")
            elif kind == "event" and msg.get("data", {}).get("event_type") == "final":
                return


@app.post("/stream")
async def stream_synthesize(req: SynthesizeRequest) -> StreamingResponse:
    """Stream a spoken reply.

    Two response shapes, distinguished by the `X-TTS-Format` header so the
    caller never has to guess:
      pcm_s16le  — raw PCM from Sarvam's WebSocket (the normal path)
      wav_chunks — one self-contained WAV per sentence (the fallback path)
    """
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Empty text")
    lang = _resolve_lang(req.language, text)
    normalized = normalize_text(text, lang)
    pace = req.pace if req.pace is not None else DEFAULT_PACE
    temperature = req.temperature if req.temperature is not None else DEFAULT_TEMPERATURE

    # Primary: one continuous Sarvam WebSocket stream. Primed by pulling the
    # first frame here, so a failure still lands on the fallback below instead of
    # truncating a response we've already started.
    use_ws = SARVAM_API_KEY and req.provider in (None, "", "sarvam")
    if use_ws:
        t0 = time.time()
        try:
            agen = _gate_silence(
                _sarvam_ws_pcm(normalized, lang, req.voice, pace, temperature)
            )
            # Priming here doubles as the silence-gate warm-up: the first frame
            # out of the gate is the first frame that actually contains speech.
            first_frame = await agen.__anext__()
            logger.info(
                "stream provider=sarvam-ws lang=%s chars=%d first frame in %d ms",
                lang, len(normalized), int((time.time() - t0) * 1000),
            )

            async def pcm_gen() -> AsyncIterator[bytes]:
                total = len(first_frame)
                yield first_frame
                try:
                    async for frame in agen:
                        total += len(frame)
                        yield frame
                finally:
                    await agen.aclose()
                    logger.info(
                        "stream sarvam-ws done: %d PCM bytes (%d ms audio)",
                        total, int(total / (STREAM_SAMPLE_RATE * 2) * 1000),
                    )

            return StreamingResponse(
                pcm_gen(),
                media_type="audio/pcm",
                headers={
                    "X-TTS-Language": lang,
                    "X-TTS-Provider": "sarvam-ws",
                    "X-TTS-Format": "pcm_s16le",
                    "X-TTS-Sample-Rate": str(STREAM_SAMPLE_RATE),
                },
            )
        except StopAsyncIteration:
            logger.warning("Sarvam stream returned no audio — falling back to chunked HTTP")
        except Exception as e:
            logger.warning("Sarvam stream unavailable (%s) — falling back to chunked HTTP", e)

    # Fallback: the original per-sentence HTTP path, which also covers Edge TTS
    # and an explicitly forced provider. Prosody restarts per sentence here —
    # this is a degraded mode, not the intended one.
    chunks = chunk_text(normalized)

    async def wav_gen() -> AsyncIterator[bytes]:
        loop = asyncio.get_event_loop()
        for i, ck in enumerate(chunks):
            t0 = time.time()
            wav, used = await loop.run_in_executor(
                None,
                lambda c=ck: synthesizer.synthesize(
                    c, lang, preferred=req.provider, voice=req.voice,
                    pace=pace, temperature=temperature,
                ),
            )
            logger.info(
                "stream provider=%s chunk %d/%d (%d chars, %d ms audio) in %d ms",
                used, i + 1, len(chunks), len(ck),
                _wav_duration_ms(wav), int((time.time() - t0) * 1000),
            )
            yield wav
            await asyncio.sleep(0)

    return StreamingResponse(
        wav_gen(),
        media_type="audio/wav",
        headers={
            "X-TTS-Language": lang,
            "X-TTS-Chunks": str(len(chunks)),
            "X-TTS-Provider": synthesizer.primary or "none",
            "X-TTS-Format": "wav_chunks",
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
