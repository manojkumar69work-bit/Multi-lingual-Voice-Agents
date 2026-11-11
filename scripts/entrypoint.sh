#!/usr/bin/env sh
# Entry point for the voice-agent image.
#   scripts/entrypoint.sh server   → the API + portals on :8000
#   scripts/entrypoint.sh tts      → the Sarvam/Edge TTS service on :8002
#   scripts/entrypoint.sh agent    → the LiveKit agent worker
#   scripts/entrypoint.sh livekit  → render livekit.yaml from the template (used by the livekit service)
set -eu
cd "$(dirname "$0")/.."

# Data lives on one volume: the SQLite file and the recordings.
export CALL_DB_PATH="${CALL_DB_PATH:-/app/data/calls.db}"
export RECORDINGS_DIR="${RECORDINGS_DIR:-/app/data/recordings}"
# The config-render mode runs without the data volume; the others need it.
mkdir -p "$(dirname "$CALL_DB_PATH")" "$RECORDINGS_DIR" 2>/dev/null || true

case "${1:-server}" in
  server)
    exec python -m uvicorn server:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' ;;
  tts)
    exec python -m uvicorn tts_server:app --host 0.0.0.0 --port 8002 ;;
  agent)
    exec python agent.py start ;;
  livekit)
    # Substitute ${VAR} references in the template without needing envsubst.
    python - <<'PY'
import os, re, pathlib
out_dir = pathlib.Path(os.environ.get("LIVEKIT_CONFIG_DIR", "/out"))
out_dir.mkdir(parents=True, exist_ok=True)
for name in ("livekit.yaml", "sip.yaml"):
    src = pathlib.Path(name + ".template").read_text()
    out = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), src)
    (out_dir / name).write_text(out)
    print("rendered", name)
PY
    ;;
  *)
    exec "$@" ;;
esac
