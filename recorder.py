from __future__ import annotations
"""Free, infra-less call recording.

Instead of running LiveKit Egress (a separate container + object storage), we tap
the two audio sources we already have inside the agent process and mix them into a
single mono WAV:

  - caller audio  → read from the subscribed remote track via rtc.AudioStream
  - agent speech  → the PCM we already synthesize in VoiceTTSChunkedStream

Each chunk is timestamped against the call start, resampled to a common 24 kHz
mono, and overlaid onto one timeline at finalize. The result is one playable
recordings/<session_id>.wav, served by the client portal.

Scale-up path (documented in COSTS.md): swap this for LiveKit Egress → S3/R2 when
call volume or fidelity demands it. This stays entirely local and free.
"""
import logging
import os
import time
import wave

import numpy as np

logger = logging.getLogger("recorder")

SAMPLE_RATE = 24000  # canonical mono rate for the mixed output
RECORDINGS_DIR = os.environ.get(
    "RECORDINGS_DIR", os.path.join(os.path.dirname(__file__), "recordings")
)

# Safety cap so a stuck/forgotten call can't exhaust memory (≈ this many minutes
# per source at 24 kHz int16). Beyond it we stop accumulating but still finalize.
MAX_MINUTES = float(os.environ.get("RECORDING_MAX_MINUTES", "30"))
_MAX_SAMPLES = int(MAX_MINUTES * 60 * SAMPLE_RATE)


def _to_mono_24k(samples: np.ndarray, sample_rate: int, channels: int) -> np.ndarray:
    """Downmix to mono and resample to SAMPLE_RATE, returning float32 in [-32768,32767]."""
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    samples = samples.astype(np.float32)
    if sample_rate != SAMPLE_RATE and len(samples) > 1:
        n_out = int(round(len(samples) * SAMPLE_RATE / sample_rate))
        if n_out > 0:
            x_old = np.linspace(0.0, 1.0, len(samples), endpoint=False)
            x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
            samples = np.interp(x_new, x_old, samples).astype(np.float32)
    return samples


class CallRecorder:
    """Accumulates timestamped audio chunks from both directions and mixes on finalize.

    All methods run on the single agent asyncio loop, so no locking is needed.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._start = time.monotonic()
        # Each entry: (offset_samples:int, samples:np.ndarray float32)
        self._chunks: list[tuple[int, np.ndarray]] = []
        self._total_caller = 0
        self._total_agent = 0
        self._closed = False

    def _offset_samples(self) -> int:
        return int((time.monotonic() - self._start) * SAMPLE_RATE)

    def add_caller_frame(self, data: bytes, sample_rate: int, channels: int) -> None:
        if self._closed or self._total_caller > _MAX_SAMPLES:
            return
        try:
            arr = np.frombuffer(data, dtype=np.int16)
            if arr.size == 0:
                return
            mono = _to_mono_24k(arr, sample_rate, channels)
            self._chunks.append((self._offset_samples(), mono))
            self._total_caller += len(mono)
        except Exception as e:
            logger.debug("caller frame skipped: %s", e)

    def add_agent_pcm(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> None:
        """Agent TTS PCM (already 24 kHz mono int16 from VoiceTTS)."""
        if self._closed or not pcm or self._total_agent > _MAX_SAMPLES:
            return
        try:
            arr = np.frombuffer(pcm, dtype=np.int16)
            if arr.size == 0:
                return
            mono = _to_mono_24k(arr, sample_rate, 1)
            self._chunks.append((self._offset_samples(), mono))
            self._total_agent += len(mono)
        except Exception as e:
            logger.debug("agent pcm skipped: %s", e)

    def finalize(self) -> str | None:
        """Mix all chunks onto one timeline and write a WAV. Returns the path or None."""
        self._closed = True
        if not self._chunks:
            return None
        try:
            total = max(off + len(s) for off, s in self._chunks)
            if total <= 0:
                return None
            mix = np.zeros(total, dtype=np.float32)
            for off, s in self._chunks:
                end = off + len(s)
                if end > total:
                    s = s[: total - off]
                    end = total
                mix[off:end] += s
            # Soft-clip to int16 range.
            np.clip(mix, -32768, 32767, out=mix)
            pcm16 = mix.astype(np.int16)

            os.makedirs(RECORDINGS_DIR, exist_ok=True)
            path = os.path.join(RECORDINGS_DIR, f"{self.session_id}.wav")
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(pcm16.tobytes())
            logger.info(
                "Recording saved: %s (%.1fs)", path, total / SAMPLE_RATE
            )
            return path
        except Exception as e:
            logger.error("Failed to finalize recording: %s", e)
            return None
