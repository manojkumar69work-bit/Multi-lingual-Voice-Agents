"""Render TTS comparison samples so voice changes are judged by ear.

Two modes:

  python generate_voice_samples.py            # A/B pairs for each change we made
  python generate_voice_samples.py speakers   # the full 37-speaker sweep, hi + te

"Sounds more human" cannot be verified from a diff, so every knob this project
turned — script, pace, speaker, temperature, and continuous-vs-chunked
streaming — gets an explicit before/after pair here. Output lands in
static/voices/sarvam/ and is rendered by static/sarvam_demo.html (served at
/demo/sarvam).

Point it at a running TTS service (default http://localhost:8002, override with
TTS_URL).
"""
import base64
import io
import json
import os
import sys
import wave

import httpx

BASE_URL = os.environ.get("TTS_URL", "http://localhost:8002")
SYNTH_URL = f"{BASE_URL}/synthesize"
STREAM_URL = f"{BASE_URL}/stream"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "static", "voices", "sarvam")
SAMPLE_RATE = int(os.environ.get("TTS_SAMPLE_RATE", "24000"))

SPEAKERS = [
    "shubh", "aditya", "ritu", "priya", "neha", "rahul", "pooja", "rohan",
    "simran", "kavya", "amit", "dev", "ishita", "shreya", "ratan", "varun",
    "manan", "sumit", "roopa", "kabir", "aayan", "ashutosh", "advait",
    "anand", "tanya", "tarun", "sunny", "mani", "gokul", "vijay",
    "shruti", "suhani", "mohit", "kavitha", "rehan", "soham", "rupali",
]

TEXTS = {
    "hi": "नमस्ते! मैं आपकी कैसे मदद कर सकता हूं? कृपया अपना सवाल पूछें।",
    "te": "నమస్కారం! నేను మీకు ఎలా సహాయం చేయగలను? దయచేసి మీ ప్రశ్న అడగండి.",
}

# A realistic multi-sentence reply — one sentence would hide exactly the seam
# and prosody problems these samples exist to expose.
AB_TEXTS = {
    # What the agent says: Hinglish in Roman letters, mixed with English. This is
    # the register the product ships, so it is the reference sample.
    "hi_roman": "Ji bilkul! Main aapki madad kar sakti hoon. Aapka budget kitna hai? "
                "Aur kaun si location pasand hai aapko?",
    # The same sentence in Devanagari. Sarvam's docs say native script pronounces
    # better; these two files are what settles that by ear rather than by doc.
    "hi_native": "जी बिल्कुल! मैं आपकी मदद कर सकती हूँ। आपका budget कितना है? "
                 "और कौन सी location पसंद है आपको?",
    # Telugu, with English loanwords left in Latin — the register the te prompt
    # now asks for.
    "te_native": "నమస్కారం! చెప్పండి, మీకు ఎలాంటి ఇల్లు కావాలి? మీ budget ఎంత అండి?",
    # Telugu romanized. Telugu's script axis was never rendered, so the claim
    # that native script matters was carried over from Hindi untested.
    "te_roman": "Namaskaram! Cheppandi, meeku elanti illu kavali? Mee budget entha andi?",
}


# ─── WAV helpers ─────────────────────────────────────────────────────────────

def _pcm_from_wav(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return w.readframes(w.getnframes())


def _write_wav(path: str, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> int:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return os.path.getsize(path)


# ─── Renderers ───────────────────────────────────────────────────────────────

def render_batch(text: str, lang: str, **opts) -> bytes:
    """One /synthesize call → PCM."""
    body = {"text": text, "language": lang, **opts}
    resp = httpx.post(SYNTH_URL, json=body, timeout=120)
    resp.raise_for_status()
    return _pcm_from_wav(base64.b64decode(resp.json()["audio"]))


def render_stream(text: str, lang: str, **opts) -> bytes:
    """One /stream call → PCM. This is the path a live call actually uses."""
    body = {"text": text, "language": lang, **opts}
    with httpx.stream("POST", STREAM_URL, json=body, timeout=120) as resp:
        resp.raise_for_status()
        fmt = resp.headers.get("X-TTS-Format", "wav_chunks")
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf.extend(chunk)
    if fmt == "pcm_s16le":
        return bytes(buf)
    # Fallback shape: concatenated WAVs, each with its own header.
    return _pcm_concat_wavs(bytes(buf))


def _pcm_concat_wavs(blob: bytes) -> bytes:
    """Pull the PCM out of a run of back-to-back WAV files."""
    out = bytearray()
    pos = 0
    while True:
        idx = blob.find(b"RIFF", pos)
        if idx < 0:
            break
        total = int.from_bytes(blob[idx + 4 : idx + 8], "little") + 8
        out.extend(_pcm_from_wav(blob[idx : idx + total]))
        pos = idx + total
    return bytes(out)


def render_sentence_chunked(text: str, lang: str, **opts) -> bytes:
    """Reproduce the OLD streaming path: one request per sentence, concatenated.

    Kept deliberately, as the control sample. Each sentence is synthesized in
    isolation, so prosody restarts from neutral at every boundary and each
    fragment's own leading/trailing silence lands in the gap — which is what the
    agent used to sound like.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from tts_engine import chunk_text, normalize_text

    out = bytearray()
    for sentence in chunk_text(normalize_text(text, lang)):
        out.extend(render_batch(sentence, lang, **opts))
    return bytes(out)


# ─── Mode: A/B pairs ─────────────────────────────────────────────────────────

def _duration(pcm: bytes) -> float:
    return len(pcm) / (SAMPLE_RATE * 2)


AB_CASES = [
    # (lang, slug, label, note, renderer, text_key, opts)
    ("hi", "script_roman", "Script: Roman Hinglish (shipping)",
     "What the agent actually writes. Sarvam's docs warn a hi-IN voice reads Latin "
     "text with the wrong phonemes — this file is how you judge whether that is audible",
     render_stream, "hi_roman", {"pace": 1.0}),
    ("hi", "script_native", "Script: Devanagari (alternative)",
     "Same sentence in native script. If this is clearly cleaner, the fix is to "
     "transliterate before the TTS call — not to change what the LLM writes",
     render_stream, "hi_native", {"pace": 1.0}),

    ("hi", "chunked", "Streaming: per-sentence (before)",
     "One TTS request per sentence — prosody restarts and each fragment's silence stacks in the gaps",
     render_sentence_chunked, "hi_native", {"pace": 1.0}),
    ("hi", "continuous", "Streaming: one WebSocket (after)",
     "Whole reply in one stream, so Bulbul v3 infers pauses and emphasis across sentences",
     render_stream, "hi_native", {"pace": 1.0}),

    ("hi", "pace_140", "Pace 1.4 (before)", "40% faster than the rate the model is trained at",
     render_stream, "hi_native", {"pace": 1.4}),
    ("hi", "pace_120", "Pace 1.2", "Midpoint, for reference",
     render_stream, "hi_native", {"pace": 1.2}),
    ("hi", "pace_100", "Pace 1.0 (after)", "The rate Bulbul v3 is trained at",
     render_stream, "hi_native", {"pace": 1.0}),

    ("hi", "speaker_roopa", "Speaker: roopa (before)", "Not on Sarvam's recommended list for hi-IN",
     render_stream, "hi_native", {"pace": 1.0, "voice": "roopa"}),
    ("hi", "speaker_priya", "Speaker: priya (after)", "Sarvam's recommended hi-IN voice",
     render_stream, "hi_native", {"pace": 1.0, "voice": "priya"}),
    ("hi", "speaker_suhani", "Speaker: suhani", "Sarvam's other hi-IN recommendation",
     render_stream, "hi_native", {"pace": 1.0, "voice": "suhani"}),

    ("hi", "temp_030", "Temperature 0.3", "Flatter, more repeatable delivery",
     render_stream, "hi_native", {"pace": 1.0, "temperature": 0.3}),
    ("hi", "temp_060", "Temperature 0.6 (default)", "Model default",
     render_stream, "hi_native", {"pace": 1.0, "temperature": 0.6}),
    ("hi", "temp_100", "Temperature 1.0", "More prosodic variation, less predictable",
     render_stream, "hi_native", {"pace": 1.0, "temperature": 1.0}),

    ("te", "speaker_roopa", "Speaker: roopa @1.15 (before)",
     "A Hindi-first speaker reused for Telugu, at the old pace",
     render_stream, "te_native", {"pace": 1.15, "voice": "roopa"}),
    ("te", "speaker_neha", "Speaker: neha @0.95 (after)",
     "Sarvam's recommended te-IN voice, at the new pace",
     render_stream, "te_native", {"pace": 0.95, "voice": "neha"}),
    ("te", "speaker_priya", "Speaker: priya @0.95", "Sarvam's other te-IN recommendation",
     render_stream, "te_native", {"pace": 0.95, "voice": "priya"}),
    ("te", "chunked", "Streaming: per-sentence (before)", "The old per-sentence path, for the seams",
     render_sentence_chunked, "te_native", {"pace": 0.95, "voice": "neha"}),
    ("te", "continuous", "Streaming: one WebSocket (after)",
     "Whole reply in one stream, so Bulbul v3 infers pauses across sentences",
     render_stream, "te_native", {"pace": 0.95, "voice": "neha"}),

    # Telugu's script axis was never rendered — the "native script is better"
    # claim was carried over from Hindi and assumed to hold. These two settle it.
    ("te", "script_roman", "Script: Roman",
     "Telugu romanized, sent to a te-IN voice",
     render_stream, "te_roman", {"pace": 0.95, "voice": "neha"}),
    ("te", "script_native", "Script: తెలుగు (shipping)",
     "What the agent writes: Telugu script with English loanwords left in Latin",
     render_stream, "te_native", {"pace": 0.95, "voice": "neha"}),

    # "Sounds robotic" is usually pace before it is speaker. 0.95 was picked from
    # the docs, not by ear; these bracket it.
    ("te", "pace_115", "Pace 1.15 (before)", "The old Telugu pace",
     render_stream, "te_native", {"pace": 1.15, "voice": "neha"}),
    ("te", "pace_095", "Pace 0.95 (shipping)", "Slightly under the trained rate",
     render_stream, "te_native", {"pace": 0.95, "voice": "neha"}),
    ("te", "pace_085", "Pace 0.85", "Slower still — often reads as more considered, less clipped",
     render_stream, "te_native", {"pace": 0.85, "voice": "neha"}),

    ("te", "temp_030", "Temperature 0.3", "Flatter, more repeatable delivery",
     render_stream, "te_native", {"pace": 0.95, "voice": "neha", "temperature": 0.3}),
    ("te", "temp_060", "Temperature 0.6 (default)", "Model default",
     render_stream, "te_native", {"pace": 0.95, "voice": "neha", "temperature": 0.6}),
    ("te", "temp_100", "Temperature 1.0", "More prosodic variation — the usual cure for flat delivery",
     render_stream, "te_native", {"pace": 0.95, "voice": "neha", "temperature": 1.0}),
]


def gen_ab(prev: dict | None = None) -> dict:
    # A render that fails (service down, one bad speaker) used to drop that entry
    # from the manifest entirely, so a partial run silently deleted good samples
    # that were still sitting on disk. Carry the previous entry forward instead —
    # gen_speakers() already skips existing files for the same reason.
    old = {
        lang: {e.get("slug"): e for e in entries}
        for lang, entries in (prev or {}).items()
        if isinstance(entries, list)
    }

    def _keep(lang: str, slug: str, results: dict) -> None:
        stale = old.get(lang, {}).get(slug)
        if stale and os.path.exists(os.path.join(OUTPUT_DIR, f"ab_{lang}_{slug}.wav")):
            print(f"  {lang}/{slug} → keeping previous render")
            results[lang].append(stale)

    results: dict = {"hi": [], "te": []}
    for lang, slug, label, note, renderer, text_key, opts in AB_CASES:
        name = f"ab_{lang}_{slug}.wav"
        path = os.path.join(OUTPUT_DIR, name)
        text = AB_TEXTS[text_key]
        try:
            pcm = renderer(text, lang, **opts)
            if not pcm:
                print(f"  {lang}/{slug} → no audio")
                _keep(lang, slug, results)
                continue
            size = _write_wav(path, pcm)
            dur = _duration(pcm)
            print(f"  {lang}/{slug:22} → {dur:5.2f}s  {size//1024:4d} KB  {label}")
            results[lang].append({
                "slug": slug, "label": label, "note": note,
                "file": f"sarvam/{name}", "size": size,
                "duration": round(dur, 2), "text": text,
            })
        except Exception as e:
            print(f"  {lang}/{slug} → ERROR: {e}")
            _keep(lang, slug, results)
    return results


# ─── Mode: full speaker sweep ────────────────────────────────────────────────

def gen_speakers() -> dict:
    results: dict = {"hi": [], "te": []}
    for lang, text in TEXTS.items():
        print(f"\n=== Language: {lang} ===")
        for speaker in SPEAKERS:
            name = f"{lang}_{speaker}.wav"
            out_path = os.path.join(OUTPUT_DIR, name)
            if os.path.exists(out_path):
                print(f"  {speaker} → exists, skipping")
                results[lang].append({
                    "speaker": speaker, "file": f"sarvam/{name}",
                    "size": os.path.getsize(out_path),
                })
                continue
            try:
                pcm = render_batch(text, lang, voice=speaker)
                size = _write_wav(out_path, pcm)
                print(f"  {speaker} → {size} bytes")
                results[lang].append(
                    {"speaker": speaker, "file": f"sarvam/{name}", "size": size}
                )
            except Exception as e:
                print(f"  {speaker} → ERROR: {e}")
    return results


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    manifest_path = os.path.join(OUTPUT_DIR, "manifest.json")
    manifest: dict = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path) as f:
                manifest = json.load(f) or {}
        except Exception:
            manifest = {}
    # Older manifests were a bare {hi: [...], te: [...]} speaker map.
    if "hi" in manifest and "speakers" not in manifest:
        manifest = {"speakers": {"hi": manifest.get("hi", []), "te": manifest.get("te", [])}}

    mode = (sys.argv[1] if len(sys.argv) > 1 else "ab").lower()
    if mode.startswith("speak"):
        print(f"Full speaker sweep via {SYNTH_URL}")
        manifest["speakers"] = gen_speakers()
    else:
        print(f"A/B comparison samples via {STREAM_URL}")
        manifest["ab"] = gen_ab(manifest.get("ab"))

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"\nManifest written to {manifest_path}")
    print("Listen at http://localhost:8000/demo/sarvam")


if __name__ == "__main__":
    main()
