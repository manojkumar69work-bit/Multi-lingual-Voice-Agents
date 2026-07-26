"""Generate voice comparison samples for all Sarvam Bulbul v3 speakers.
Run with: .venv/bin/python generate_voice_samples.py
"""
import base64
import json
import os
import sys
import httpx

TTS_URL = "http://localhost:8002/synthesize"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "static", "voices", "sarvam")

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


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    results = {"hi": [], "te": []}

    for lang, text in TEXTS.items():
        print(f"\n=== Language: {lang} ===")
        for speaker in SPEAKERS:
            out_path = os.path.join(OUTPUT_DIR, f"{lang}_{speaker}.wav")
            if os.path.exists(out_path):
                print(f"  {speaker} → exists, skipping")
                results[lang].append({"speaker": speaker, "file": f"sarvam/{lang}_{speaker}.wav", "size": os.path.getsize(out_path)})
                continue

            try:
                resp = httpx.post(
                    TTS_URL,
                    json={"text": text, "language": lang, "voice": speaker},
                    timeout=60,
                )
                if resp.status_code != 200:
                    print(f"  {speaker} → HTTP {resp.status_code}: {resp.text[:80]}")
                    continue

                data = resp.json()
                audio_b64 = data.get("audio")
                if not audio_b64:
                    print(f"  {speaker} → no audio in response")
                    continue

                wav = base64.b64decode(audio_b64)
                with open(out_path, "wb") as f:
                    f.write(wav)
                print(f"  {speaker} → {len(wav)} bytes")
                results[lang].append({"speaker": speaker, "file": f"sarvam/{lang}_{speaker}.wav", "size": len(wav)})
            except Exception as e:
                print(f"  {speaker} → ERROR: {e}")

    # Write manifest JSON
    manifest_path = os.path.join(OUTPUT_DIR, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nManifest written to {manifest_path}")
    print("Done!")


if __name__ == "__main__":
    main()
