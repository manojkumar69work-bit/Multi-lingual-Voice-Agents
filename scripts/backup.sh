#!/usr/bin/env sh
# Nightly backup of the SQLite database and the recordings.
#   scripts/backup.sh [data-dir] [backup-root]
# Uses sqlite3's online .backup so a live database is copied consistently.
# Cron (host):  15 2 * * *  cd /opt/voice-agent && mkdir -p backups && scripts/backup.sh ./data ./backups >> backups/backup.log 2>&1
# Needs sqlite3 and rsync on the host. The data files are root-owned (Docker), so run as root.
set -eu
DATA="${1:-./data}"
ROOT="${2:-./backups}"
DAY="$(date +%Y-%m-%d)"
DEST="$ROOT/$DAY"
PREV="$(ls -1d "$ROOT"/20* 2>/dev/null | grep -v "$DAY" | sort | tail -1 || true)"
mkdir -p "$ROOT" "$DEST"
command -v sqlite3 >/dev/null || { echo "sqlite3 is required (apt-get install sqlite3)"; exit 1; }
command -v rsync   >/dev/null || { echo "rsync is required (apt-get install rsync)"; exit 1; }
sqlite3 "$DATA/calls.db" ".backup '$DEST/calls.db'"
if [ -d "$DATA/recordings" ]; then
  # Unchanged recordings are hard-linked to yesterday's copy, so 30 days of
  # backups cost about one copy of the audio.
  if [ -n "$PREV" ] && [ -d "$PREV/recordings" ]; then
    rsync -a --delete --link-dest="$(cd "$PREV/recordings" && pwd)" "$DATA/recordings/" "$DEST/recordings/"
  else
    rsync -a --delete "$DATA/recordings/" "$DEST/recordings/"
  fi
fi
# Keep 30 days.
find "$ROOT" -maxdepth 1 -mindepth 1 -type d -mtime +30 -exec rm -rf {} +
echo "$(date -u +%FT%TZ) backed up to $DEST"
