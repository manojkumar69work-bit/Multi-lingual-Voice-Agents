#!/usr/bin/env sh
# Nightly backup of the SQLite database and the recordings.
#   scripts/backup.sh [data-dir] [backup-root]
# Uses sqlite3's online .backup so a live database is copied consistently.
# Cron (host):  15 2 * * *  cd /opt/voice-agent && scripts/backup.sh >> backups/backup.log 2>&1
set -eu
DATA="${1:-./data}"
ROOT="${2:-./backups}"
DAY="$(date +%Y-%m-%d)"
DEST="$ROOT/$DAY"
mkdir -p "$DEST"
sqlite3 "$DATA/calls.db" ".backup '$DEST/calls.db'"
if [ -d "$DATA/recordings" ]; then
  rsync -a --delete "$DATA/recordings/" "$DEST/recordings/"
fi
# Keep 30 days.
find "$ROOT" -maxdepth 1 -mindepth 1 -type d -mtime +30 -exec rm -rf {} +
echo "$(date -u +%FT%TZ) backed up to $DEST"
