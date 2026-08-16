"""Local Indic TTS engine — multi-provider with smart fallback.

Supported providers (priority order):
  1. Sarvam Bulbul v3   — best Indic quality, commercial OK
                            requires SARVAM_API_KEY, 1000 free credits
                            default voice `priya` (Tier 1, works hi/te/ta/mr/gu/en)
  2. Edge TTS Neural    — Microsoft cloud, free, ToS-restricted at commercial scale
                            Hindi female: hi-IN-SwaraNeural
                            Telugu female: te-IN-ShrutiNeural
  3. MMS-TTS (local)    — facebook/mms-tts-{hin,tel}
                            ⚠️ CC-BY-NC-4.0 license: NON-COMMERCIAL USE ONLY
                            Keep only for dev/testing. Remove from any product path.

All providers return 16-bit PCM WAV bytes (browser-compatible).

Key design:
  * Pre-process text with `indic-numtowords` for currency / numbers / percents
    (Sarvam & Edge both handle raw digits poorly without this).
  * Script-based language detection (Devanagari vs Telugu vs Latin).
  * Sentence chunking with awareness of Indic punctuation.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import soundfile as sf

try:
    import indic_numtowords as inw
except ImportError:  # pragma: no cover
    inw = None  # type: ignore[assignment]


logger = logging.getLogger("tts")


# ─────────────────────────────────────────────────────────────────────────────
# Voice registry — Sarvam voices are Tier 1 picks from
# https://docs.sarvam.ai/api-reference-docs/api-guides-tutorials/text-to-speech/how-to/change-the-speaker-voice
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VoiceProfile:
    """A single voice configuration across providers."""
    language: str          # "hi" / "te" / "en"
    sarvam_speaker: str    # Sarvam Bulbul v3 speaker (case-sensitive lowercase)
    sarvam_lang: str       # Sarvam target_language_code (e.g., "hi-IN")
    edge_voice: str        # Microsoft Edge TTS voice
    quality_tier: int      # 1 = best, lower is better. 99 = unknown.


# Sarvam's own per-language speaker recommendations, which are chosen on
# character error rate in that language — not one favourite voice reused
# everywhere. This used to be `roopa` for BOTH hi and te; roopa is on neither
# language's recommended list, and using a Hindi-first speaker for Telugu is
# audible in the conjuncts.
#   hi-IN → priya, suhani      te-IN → neha, priya      en-IN → ishita
# Overridable per language via TTS_VOICE_HI / _TE / _EN (see .env.example).
# Which speaker sounds best is finally a listening call, not a spec — audition
# all 37 in static/sarvam_demo.html, which renders each one in hi and te.
DEFAULT_VOICES: dict[str, VoiceProfile] = {
    "hi": VoiceProfile(
        language="hi",
        sarvam_speaker=os.environ.get("TTS_VOICE_HI", "priya"),
        sarvam_lang="hi-IN",
        edge_voice="hi-IN-SwaraNeural",
        quality_tier=1,
    ),
    "te": VoiceProfile(
        language="te",
        sarvam_speaker=os.environ.get("TTS_VOICE_TE", "neha"),
        sarvam_lang="te-IN",
        edge_voice="te-IN-ShrutiNeural",
        quality_tier=1,
    ),
    "en": VoiceProfile(
        language="en",
        # ishita: Tier 1, 0.13% CER for en-IN.
        sarvam_speaker=os.environ.get("TTS_VOICE_EN", "ishita"),
        sarvam_lang="en-IN",
        edge_voice="en-IN-NeerjaNeural",
        quality_tier=1,
    ),
}

# Bulbul v3 sampling temperature (0.01–2.0, model default 0.6) — governs how
# much prosodic variation the model allows itself.
DEFAULT_TEMPERATURE = float(os.environ.get("TTS_TEMPERATURE", "0.6"))


# ─────────────────────────────────────────────────────────────────────────────
# Text normalization — Indian-languages-aware (currency, %, numbers)
# ─────────────────────────────────────────────────────────────────────────────

# Indian magnitude words that follow the amount. "₹50 लाख" has to become
# "50 लाख रुपये" ("fifty lakh rupees"), not "50 रुपये लाख" — and property prices
# are quoted this way in essentially every call this agent takes.
_MAGNITUDES = (
    "लाख|करोड़|करोड|हज़ार|हजार"          # hi
    "|లక్ష|లక్షల|కోటి|కోట్ల|వేల|వేలు"     # te
    "|lakh|lakhs|crore|crores|thousand"  # commonly written in Latin either way
)
_CURRENCY_PATTERN = re.compile(
    r"[₹$¥£€]([\d][\d,]*(?:\.\d+)?)" rf"(\s*(?:{_MAGNITUDES}))?"
)
_PERCENT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_NUMBER_PATTERN = re.compile(r"\b\d+(?:[\.,]\d+)?\b")

# 6+ consecutive digits is an identifier a caller reads out or writes down —
# phone number, PIN code, flat number — never a quantity. Spoken digit by digit.
_LONG_DIGIT_RUN = re.compile(r"\d{6,}")

# Off by default: Bulbul v3's own preprocessing reads plain digits better than
# this module's num2words pass does. See normalize_text(). Turning it on also
# needs `indic-numtowords`, which is not a project dependency.
SPELL_OUT_NUMBERS = os.environ.get("TTS_SPELL_NUMBERS", "0") not in ("0", "false", "False")


def _safe_num2words(s: str, lang: str) -> str:
    if inw is None:
        return s
    try:
        return inw.num2words(s, lang=lang)
    except Exception:
        return s


def _currency_label(symbol: str, lang: str) -> str:
    # Hindi labels are Roman, not Devanagari: the agent replies in Roman Hinglish,
    # so a Devanagari "रुपये" would be the only native-script word in an otherwise
    # Latin sentence. Telugu replies are in Telugu script, so its labels stay native.
    labels = {
        "₹": {"hi": "rupaye", "te": "రూపాయలు"},
        "$": {"hi": "dollar", "te": "డాలర్లు"},
        "¥": {"hi": "yen", "te": "యెన్"},
        "£": {"hi": "pound", "te": "పౌండ్లు"},
        "€": {"hi": "euro", "te": "యూరోలు"},
    }
    return labels.get(symbol, {}).get(lang, "")


def _percent_label(lang: str) -> str:
    # "percent" and "point", not प्रतिशत / दशमलव — same Roman-Hinglish reasoning as
    # _currency_label, and these are the words Hinglish speakers actually use.
    return {"hi": "percent", "te": "శాతం"}.get(lang, "%")


def _decimal_label(lang: str) -> str:
    return {"hi": "point", "te": "దశమాంశం"}.get(lang, ".")


def normalize_text(text: str, lang: str) -> str:
    """Pre-process text for TTS: expand currency/percent symbols, and spell out
    long digit runs (phone numbers, PIN codes) digit by digit.

    What this deliberately does NOT do any more is convert ordinary numbers to
    words. Bulbul v3 always runs its own text preprocessing, and reads plain
    digits correctly — including times and units the hand-rolled pass mangled.
    ``\\b\\d+\\b`` matched each half of "10:30" separately and produced
    "दस:तीस", colon and all; "2 BHK" became "दो BHK". The agent's own prompt
    tells the LLM to write digits precisely because the voice reads them well,
    so converting them here was fighting both the model and the prompt.

    Set TTS_SPELL_NUMBERS=1 to restore the old spell-everything behaviour (only
    useful with a provider whose own normalizer is worse than this one).
    """
    if lang not in ("hi", "te"):
        return text

    def _cur(m: re.Match) -> str:
        """₹50 → "50 रुपये", ₹50 लाख → "50 लाख रुपये"."""
        symbol = m.group(0)[0]
        label = _currency_label(symbol, lang)
        if not label:
            return m.group(0)
        magnitude = (m.group(2) or "").strip()
        return f"{m.group(1)} {magnitude} {label}".replace("  ", " ")

    text = _CURRENCY_PATTERN.sub(_cur, text)
    text = _PERCENT_PATTERN.sub(lambda m: f"{m.group(1)} {_percent_label(lang)}", text)

    def _digits_one_by_one(m: re.Match) -> str:
        """A 10-digit mobile is ten digits, not one astronomical quantity.

        Read as a number, "9876543210" comes out somewhere in the billions —
        both wrong and useless to anyone trying to write it down. Spacing the
        digits is enough for the TTS to read them individually, and needs no
        number-to-words dependency to do it.
        """
        return " ".join(m.group(0))

    text = _LONG_DIGIT_RUN.sub(_digits_one_by_one, text)

    if not SPELL_OUT_NUMBERS:
        return text

    def _num(m: re.Match) -> str:
        s = m.group(0).replace(",", "")
        if "." in s:
            try:
                whole, frac = s.split(".")
                w = _safe_num2words(whole, lang)
                f = _safe_num2words(frac, lang)
                return f"{w} {_decimal_label(lang)} {f}".replace("  ", " ")
            except Exception:
                return m.group(0)
        return _safe_num2words(s, lang)

    return _NUMBER_PATTERN.sub(_num, text)


# ─────────────────────────────────────────────────────────────────────────────
# Sentence chunker — Indic punctuation-aware
# ─────────────────────────────────────────────────────────────────────────────

_SENTENCE_SPLIT = re.compile(
    r"(?<=[।!?])\s+"
    r"|(?<=\.)\s+(?=[A-Z\u0900-\u097F\u0C00-\u0C7F])"
    r"|\n+"
)


def chunk_text(text: str, max_chars: int = 180) -> list[str]:
    text = text.strip()
    if not text:
        return []
    parts = _SENTENCE_SPLIT.split(text)
    chunks: list[str] = []
    current = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        candidate = (current + " " + part).strip() if current else part
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(part) > max_chars:
                sub = _hard_split(part, max_chars)
                chunks.extend(sub[:-1])
                current = sub[-1] if sub else ""
            else:
                current = part
    if current:
        chunks.append(current)
    return chunks or [text]


def _hard_split(text: str, max_chars: int) -> list[str]:
    out: list[str] = []
    while len(text) > max_chars:
        cut = text.rfind(",", 0, max_chars)
        if cut < max_chars // 2:
            cut = text.rfind(" ", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Language detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_lang(text: str) -> str:
    devanagari = sum(1 for c in text if "\u0900" <= c <= "\u097F")
    telugu = sum(1 for c in text if "\u0C00" <= c <= "\u0C7F")
    if telugu > devanagari and telugu > 0:
        return "te"
    if devanagari > 0:
        return "hi"
    return "en"


# ─────────────────────────────────────────────────────────────────────────────
# Provider interface
# ─────────────────────────────────────────────────────────────────────────────

class TTSProvider:
    """Base interface for a TTS provider."""

    name: str = "base"
    available: bool = False

    def synthesize(
        self,
        text: str,
        lang: str,
        voice: Optional[str] = None,
        pace: Optional[float] = None,
        temperature: Optional[float] = None,
    ) -> bytes:
        """Return WAV bytes (16-bit PCM)."""
        raise NotImplementedError


# ─────────────────────────────────────────────────────────────────────────────
# Provider 1: Sarvam Bulbul v3 (best quality)
# ─────────────────────────────────────────────────────────────────────────────

class SarvamProvider(TTSProvider):
    name = "sarvam"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("SARVAM_API_KEY", "")
        self.client = None
        if self.api_key:
            try:
                from sarvamai import SarvamAI
                self.client = SarvamAI(api_subscription_key=self.api_key)
                self.available = True
                logger.info("[sarvam] client initialized")
            except Exception as e:
                logger.warning("[sarvam] failed to init: %s", e)
                self.client = None
                self.available = False

    def synthesize(
        self,
        text: str,
        lang: str,
        voice: Optional[str] = None,
        pace: Optional[float] = None,
        temperature: Optional[float] = None,
    ) -> bytes:
        if not self.available or self.client is None:
            raise RuntimeError("Sarvam provider not available (missing SARVAM_API_KEY)")

        profile = DEFAULT_VOICES.get(lang) or DEFAULT_VOICES["hi"]
        speaker = voice or profile.sarvam_speaker
        text = normalize_text(text.strip(), lang)
        if not text:
            return b""

        try:
            kwargs = dict(
                text=text,
                model="bulbul:v3",
                target_language_code=profile.sarvam_lang,
                speaker=speaker,
            )
            if pace is not None:
                kwargs["pace"] = pace
            # v3-only. Sent unconditionally would break a v2 fallback, so it is
            # tied to the model string above.
            if temperature is not None:
                kwargs["temperature"] = temperature
            resp = self.client.text_to_speech.convert(**kwargs)
            # Response shape varies by SDK version — try common access patterns
            audio_b64: Optional[str] = None
            if hasattr(resp, "audios") and resp.audios:
                audio_b64 = resp.audios[0]
            elif isinstance(resp, dict):
                audios = resp.get("audios") or []
                if audios:
                    audio_b64 = audios[0]
            else:
                # Some SDK versions return the audio object directly
                audio_b64 = getattr(resp, "audio", None) or getattr(resp, "data", None)

            if not audio_b64:
                raise RuntimeError(f"Sarvam returned no audio. Response: {resp!r}")

            wav_bytes = base64.b64decode(audio_b64)
            return wav_bytes
        except Exception as e:
            logger.exception("[sarvam] synthesis failed")
            raise RuntimeError(f"Sarvam TTS failed: {e}") from e


# ─────────────────────────────────────────────────────────────────────────────
# Provider 2: Edge TTS Neural (fallback, ToS-restricted for commercial scale)
# ─────────────────────────────────────────────────────────────────────────────

class EdgeProvider(TTSProvider):
    name = "edge"

    def __init__(self):
        try:
            import edge_tts  # noqa: F401
            self.available = True
        except ImportError:
            logger.warning("[edge] edge-tts package not installed")
            self.available = False

    def synthesize(
        self,
        text: str,
        lang: str,
        voice: Optional[str] = None,
        pace: Optional[float] = None,
        temperature: Optional[float] = None,  # Sarvam-only; accepted and ignored.
    ) -> bytes:
        if not self.available:
            raise RuntimeError("edge-tts package not installed")

        import asyncio
        import subprocess
        import tempfile

        profile = DEFAULT_VOICES.get(lang) or DEFAULT_VOICES["en"]
        edge_voice = voice or profile.edge_voice
        text = normalize_text(text.strip(), lang)

        async def _stream() -> bytes:
            import edge_tts
            rate = f"+{int((pace - 1) * 100)}%" if pace and pace > 1 else None
            communicate = edge_tts.Communicate(text, voice=edge_voice, rate=rate)
            mp3 = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    mp3.extend(chunk["data"])
            if not mp3:
                raise RuntimeError("Edge TTS returned no audio")
            # Decode MP3 → WAV via ffmpeg
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as t1:
                t1.write(mp3)
                mp3_path = t1.name
            wav_path = mp3_path + ".wav"
            subprocess.run(
                ["ffmpeg", "-y", "-i", mp3_path, "-f", "wav",
                 "-acodec", "pcm_s16le", "-ar", "24000", "-ac", "1", wav_path],
                capture_output=True, check=True,
            )
            with open(wav_path, "rb") as f:
                wav = f.read()
            import os
            os.unlink(mp3_path)
            os.unlink(wav_path)
            return wav

        return asyncio.run(_stream())


# ─────────────────────────────────────────────────────────────────────────────
# Provider 3: MMS-TTS local (dev/testing ONLY — non-commercial license)
# ─────────────────────────────────────────────────────────────────────────────

class MMSProvider(TTSProvider):
    """⚠️  facebook/mms-tts-{hin,tel} is CC-BY-NC-4.0 (non-commercial).

    Use ONLY for development / testing. Do NOT route production traffic here
    if you're selling the product — that's a license violation.
    """

    name = "mms"
    LICENSE_NOTICE = (
        "facebook/mms-tts-{hin,tel} is CC-BY-NC-4.0 licensed (NON-COMMERCIAL). "
        "This provider must NOT be used in any product you sell. "
        "Production traffic should go through Sarvam or Edge TTS."
    )

    def __init__(self, device: Optional[str] = None):
        try:
            import torch  # noqa: F401
            from transformers import AutoTokenizer, VitsModel  # noqa: F401
            self._torch = __import__("torch")
            if device is None:
                if self._torch.backends.mps.is_available() and self._torch.backends.mps.is_built():
                    device = "mps"
                else:
                    device = "cpu"
            self.device = device
            self.models: dict = {}
            self.tokenizers: dict = {}
            self.sample_rate = 16_000
            self.available = True
        except ImportError:
            logger.warning("[mms] transformers/torch not installed")
            self.available = False

    def _load_lang(self, lang: str) -> None:
        if lang in self.models:
            return
        from transformers import AutoTokenizer, VitsModel
        model_id = {"hi": "facebook/mms-tts-hin", "te": "facebook/mms-tts-tel"}[lang]
        logger.info("[mms] Loading %s on %s", model_id, self.device)
        self.tokenizers[lang] = AutoTokenizer.from_pretrained(model_id)
        m = VitsModel.from_pretrained(model_id).to(self.device)
        m.eval()
        self.models[lang] = m
        logger.warning("[mms] %s loaded — REMINDER: %s", lang, self.LICENSE_NOTICE)

    def synthesize(
        self,
        text: str,
        lang: str,
        voice: Optional[str] = None,
        pace: Optional[float] = None,        # not supported by MMS
        temperature: Optional[float] = None,  # not supported by MMS
    ) -> bytes:
        if not self.available:
            raise RuntimeError("transformers/torch not installed")
        if lang not in ("hi", "te"):
            raise ValueError(f"MMS provider only supports hi/te, got {lang!r}")
        if lang not in self.models:
            self._load_lang(lang)

        text = normalize_text(text.strip(), lang)
        if not text:
            return b""

        chunks = chunk_text(text)
        segments = []
        with self._torch.inference_mode():
            for ck in chunks:
                inputs = self.tokenizers[lang](ck, return_tensors="pt").to(self.device)
                wav = self.models[lang](**inputs).waveform.squeeze().detach().cpu().numpy()
                segments.append(wav.astype(np.float32))
        full = np.concatenate(segments) if len(segments) > 1 else segments[0]
        clipped = np.clip(full, -1.0, 1.0)
        pcm16 = (clipped * 32767.0).astype(np.int16)
        buf = io.BytesIO()
        sf.write(buf, pcm16, self.sample_rate, format="WAV", subtype="PCM_16")
        buf.seek(0)
        return buf.read()


# ─────────────────────────────────────────────────────────────────────────────
# Multi-provider orchestrator with fallback chain
# ─────────────────────────────────────────────────────────────────────────────

class TTSSynthesizer:
    """Routes requests through the best available provider with graceful fallback."""

    PROVIDER_PRIORITY = ("sarvam", "edge", "mms")

    def __init__(self, sarvam_key: Optional[str] = None, allow_mms: bool = True):
        self.providers: dict[str, TTSProvider] = {}

        sarvam = SarvamProvider(api_key=sarvam_key)
        if sarvam.available:
            self.providers["sarvam"] = sarvam

        edge = EdgeProvider()
        if edge.available:
            self.providers["edge"] = edge

        if allow_mms:
            mms = MMSProvider()
            if mms.available:
                self.providers["mms"] = mms

        logger.info(
            "[tts] active providers (priority order): %s",
            [p for p in self.PROVIDER_PRIORITY if p in self.providers],
        )

    @property
    def primary(self) -> Optional[str]:
        for p in self.PROVIDER_PRIORITY:
            if p in self.providers:
                return p
        return None

    def synthesize(
        self,
        text: str,
        lang: str,
        preferred: Optional[str] = None,
        voice: Optional[str] = None,
        pace: Optional[float] = None,
        temperature: Optional[float] = None,
    ) -> tuple[bytes, str]:
        """Returns (wav_bytes, provider_used).

        Tries `preferred` first if given, then walks PROVIDER_PRIORITY.
        `voice` is a per-request voice override (Sarvam speaker name, Edge voice ID).
        `pace` scales speech rate (1.0 = the rate Bulbul v3 is trained at).
        `temperature` is Sarvam v3's prosody-variation control; other providers
        accept and ignore it.
        """
        # Try the requested/preferred provider first
        order = list(self.PROVIDER_PRIORITY)
        if preferred and preferred in self.providers:
            order.remove(preferred)
            order.insert(0, preferred)

        last_error: Optional[Exception] = None
        for name in order:
            if name not in self.providers:
                continue
            provider = self.providers[name]
            try:
                wav = provider.synthesize(
                    text, lang, voice=voice, pace=pace, temperature=temperature
                )
                if wav:
                    return wav, name
            except Exception as e:
                logger.warning("[tts] provider %s failed: %s — trying next", name, e)
                last_error = e

        raise RuntimeError(
            f"All TTS providers failed for lang={lang}. "
            f"Active: {list(self.providers)}. Last error: {last_error}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────────────────────

_ALLOW_MMS = os.environ.get("TTS_ALLOW_MMS", "1") not in ("0", "false", "False")
synthesizer = TTSSynthesizer(allow_mms=_ALLOW_MMS)


if __name__ == "__main__":
    # CLI smoke: `python tts_engine.py hi "text"` or `python tts_engine.py te "text"`
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    lang = sys.argv[1] if len(sys.argv) > 1 else "hi"
    text = sys.argv[2] if len(sys.argv) > 2 else "नमस्ते, मैं आपकी सहायक हूँ।"
    out = sys.argv[3] if len(sys.argv) > 3 else f"/tmp/tts_{lang}.wav"
    print(f"[smoke] providers available: {list(synthesizer.providers)}")
    print(f"[smoke] primary: {synthesizer.primary}")
    t0 = time.time()
    wav, used = synthesizer.synthesize(text, lang)
    print(f"[smoke] synthesized {len(wav)} bytes in {(time.time()-t0)*1000:.0f} ms via {used}")
    with open(out, "wb") as f:
        f.write(wav)
    print(f"[smoke] wrote {out}")
