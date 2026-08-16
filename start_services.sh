#!/usr/bin/env bash
# Start the local Indic TTS service and the main voice-agent server.
#
# Architecture:
#   - TTS microservice on :8002
#   - Main voice server on :8000 (ASR + chat + /api/tts proxy)
#
# Both use .venv12 (Python 3.12). Override with VENV=/path/to/venv.
# The older .venv is Python 3.9 and its livekit-agents install is broken — it
# can serve these two processes but cannot run agent.py, so everything points at
# one environment that runs all four.
#
# /api/tts routes:
#   language=hi|te and no voice set  →  forwarded to local :8002/synthesize
#   language=en OR voice pinned       →  falls back to Edge TTS
#
# Usage:
#   ./start_services.sh              # start both, wait until healthy
#   ./start_services.sh stop         # stop both
#   ./start_services.sh status       # check running state
#   ./start_services.sh logs tts     # tail TTS service logs
#   ./start_services.sh logs main    # tail main server logs

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="${VENV:-$PROJECT_DIR/.venv12}"
TTS_VENV="$VENV"
MAIN_VENV="$VENV"
TTS_LOG="/tmp/tts_service.log"
MAIN_LOG="/tmp/main_server.log"
TTS_PORT=8002
MAIN_PORT=8000

# Track PIDs we launched ourselves (skip on stop if from outside).
TTS_PID_FILE="/tmp/.tts_service.pid"
MAIN_PID_FILE="/tmp/.main_server.pid"

launch_detached() {
    # Launches a command with full session detachment.
    # Args: venv_python app_module port log_file pid_file
    local venv="$1" module="$2" port="$3" log="$4" pid_file="$5"
    "$venv" -c "
import subprocess, sys
p = subprocess.Popen(
    ['$venv', '-m', 'uvicorn', '$module:app',
     '--host', '0.0.0.0', '--port', '$port',
     '--app-dir', '$PROJECT_DIR'],
    stdout=open('$log', 'w'),
    stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL,
    start_new_session=True,
    cwd='$PROJECT_DIR',
)
open('$pid_file', 'w').write(str(p.pid))
print(p.pid)
"
}

wait_for_health() {
    local url="$1" name="$2" max="${3:-30}"
    for i in $(seq 1 "$max"); do
        if curl -sf "$url" >/dev/null 2>&1; then
            echo "  ✓ $name healthy"
            return 0
        fi
        sleep 1
    done
    echo "  ✗ $name did not become healthy in ${max}s"
    return 1
}

case "${1:-start}" in
    start)
        echo "Starting TTS service on :$TTS_PORT..."
        launch_detached "$TTS_VENV/bin/python3" tts_server "$TTS_PORT" "$TTS_LOG" "$TTS_PID_FILE"
        wait_for_health "http://localhost:$TTS_PORT/health" "TTS service" 30

        echo ""
        echo "Starting main voice server on :$MAIN_PORT..."
        launch_detached "$MAIN_VENV/bin/python3" server "$MAIN_PORT" "$MAIN_LOG" "$MAIN_PID_FILE"
        wait_for_health "http://localhost:$MAIN_PORT/api/health" "main server" 15

        echo ""
        echo "=== Both services running ==="
        echo "  TTS service:  http://localhost:$TTS_PORT/health"
        echo "  Main server:  http://localhost:$MAIN_PORT/"
        echo "  Logs:         $TTS_LOG, $MAIN_LOG"
        echo ""
        echo "Try it:"
        echo "  curl -X POST http://localhost:$MAIN_PORT/api/tts \\"
        echo "    -H 'Content-Type: application/json' \\"
        echo "    -d '{\"text\":\"नमस्ते, ₹500 का निवेश 10% ब्याज देगा।\",\"language\":\"hi\"}'"
        ;;

    stop)
        echo "Stopping services..."
        for pid_file in "$TTS_PID_FILE" "$MAIN_PID_FILE"; do
            if [[ -f "$pid_file" ]]; then
                pid=$(cat "$pid_file")
                if kill -0 "$pid" 2>/dev/null; then
                    kill "$pid" && echo "  killed PID $pid"
                fi
                rm -f "$pid_file"
            fi
        done
        # Belt and suspenders
        pkill -f "uvicorn tts_server:app" 2>/dev/null || true
        pkill -f "uvicorn server:app" 2>/dev/null || true
        ;;

    status)
        echo "--- Ports ---"
        lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -E ":($TTS_PORT|$MAIN_PORT)" | head -5 || echo "  (none)"
        echo ""
        echo "--- Health ---"
        curl -sf "http://localhost:$TTS_PORT/health" | python3 -m json.tool 2>/dev/null \
            || echo "  TTS service: DOWN"
        curl -sf "http://localhost:$MAIN_PORT/api/health" | python3 -m json.tool 2>/dev/null \
            || echo "  Main server: DOWN"
        ;;

    logs)
        case "${2:-tts}" in
            tts)  tail -f "$TTS_LOG" ;;
            main) tail -f "$MAIN_LOG" ;;
            *) echo "Usage: $0 logs [tts|main]"; exit 1 ;;
        esac
        ;;

    *)
        echo "Usage: $0 {start|stop|status|logs tts|logs main}"
        exit 1
        ;;
esac
