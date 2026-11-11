#!/usr/bin/env sh
# Share the call pages with people outside this machine.
#
#   scripts/share.sh
#
# Starts everything in public mode (visitors reach only /call/<slug>; the
# operator console stays on localhost), opens a public HTTPS tunnel to :8000,
# and prints the links to send. Ctrl+C stops the tunnel.
#
# Needs: LiveKit Cloud in .env (a local LiveKit cannot carry audio to people
# outside this network), and cloudflared (brew install cloudflared) or an
# ngrok account (ngrok config add-authtoken <token>).
set -eu
cd "$(dirname "$0")/.."

lk_url="$(grep -E '^LIVEKIT_URL=' .env | tail -1 | cut -d= -f2-)"
case "$lk_url" in
  wss://*) ;;
  *)
    echo "LIVEKIT_URL is '$lk_url'. People outside this network cannot reach a local LiveKit."
    echo "Put your LiveKit Cloud project's values in .env and run this again:"
    echo "  LIVEKIT_URL=wss://<project>.livekit.cloud"
    echo "  LIVEKIT_API_KEY=<key>"
    echo "  LIVEKIT_API_SECRET=<secret>"
    exit 1 ;;
esac

export PUBLIC_CALLS_ONLY=1
mkdir -p logs
./start_services.sh stop >/dev/null 2>&1 || true
./start_services.sh >/dev/null
pkill -f 'agent.py dev' 2>/dev/null || true
nohup .venv12/bin/python agent.py dev > logs/agent.log 2>&1 &
echo "Services started in public mode; agent worker starting (logs/agent.log)."

slugs="$(.venv12/bin/python -c "import tenants; print(' '.join(t['id'] for t in tenants.list_tenants() if t.get('active')))" 2>/dev/null)"

print_links() {
  echo
  echo "Send these links. Each opens that client's agent; nothing else is reachable."
  for s in $slugs; do echo "  $1/call/$s"; done
  echo
}

if command -v cloudflared >/dev/null 2>&1; then
  : > logs/tunnel.log
  cloudflared tunnel --no-autoupdate --url http://localhost:8000 > logs/tunnel.log 2>&1 &
  tpid=$!
  trap 'kill $tpid 2>/dev/null' INT TERM EXIT
  for _ in $(seq 1 30); do
    url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' logs/tunnel.log | head -1 || true)"
    [ -n "$url" ] && break
    sleep 1
  done
  [ -n "${url:-}" ] || { echo "Tunnel did not start; see logs/tunnel.log"; exit 1; }
  print_links "$url"
  echo "Tunnel is up. Press Ctrl+C to stop sharing."
  wait $tpid
elif command -v ngrok >/dev/null 2>&1; then
  echo "Starting ngrok; the public URL appears in its window as 'Forwarding https://…'."
  echo "Append /call/<slug> to it, for example /call/$(echo "$slugs" | cut -d' ' -f1)."
  exec ngrok http 8000
else
  echo "Install a tunnel first: brew install cloudflared"
  exit 1
fi
