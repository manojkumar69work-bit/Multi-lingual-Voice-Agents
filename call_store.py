from __future__ import annotations
"""Shared call store — SQLite, safe for multi-process access.

Stores call sessions, status, transcripts, and leads.
Both agent.py and server.py read/write here.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("call-store")

DB_PATH = os.environ.get("CALL_DB_PATH", os.path.join(os.path.dirname(__file__), "calls.db"))

_LOCAL = threading.local()

def _get_conn() -> sqlite3.Connection:
    if not hasattr(_LOCAL, "conn") or _LOCAL.conn is None:
        _LOCAL.conn = sqlite3.connect(DB_PATH)
        _LOCAL.conn.row_factory = sqlite3.Row
        _LOCAL.conn.execute("PRAGMA journal_mode=WAL")
        _LOCAL.conn.execute("PRAGMA busy_timeout=5000")
    return _LOCAL.conn


def get_conn() -> sqlite3.Connection:
    """Public accessor so sibling modules (lead_delivery) share one DB file,
    one WAL journal, and the same busy-timeout without opening their own."""
    return _get_conn()

def init_db():
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS calls (
            session_id TEXT PRIMARY KEY,
            room_name TEXT,
            caller_phone TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at REAL,
            updated_at REAL,
            duration_seconds REAL DEFAULT 0,
            transcript TEXT DEFAULT '[]',
            lead_extracted INTEGER DEFAULT 0,
            lead_submitted INTEGER DEFAULT 0,
            lead_data TEXT DEFAULT '{}',
            client_id TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            recording_path TEXT DEFAULT '',
            error TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
        CREATE INDEX IF NOT EXISTS idx_calls_created ON calls(created_at);
        CREATE INDEX IF NOT EXISTS idx_calls_client ON calls(client_id);
    """)
    # Idempotent migration for databases created before these columns existed.
    existing = {r[1] for r in conn.execute("PRAGMA table_info(calls)").fetchall()}
    for col, ddl in (
        ("summary", "ALTER TABLE calls ADD COLUMN summary TEXT DEFAULT ''"),
        ("recording_path", "ALTER TABLE calls ADD COLUMN recording_path TEXT DEFAULT ''"),
        ("client_id", "ALTER TABLE calls ADD COLUMN client_id TEXT DEFAULT ''"),
    ):
        if col not in existing:
            conn.execute(ddl)
    conn.commit()

# Columns that may be supplied to upsert_call, with their serializers.
# Only keys actually passed in **kwargs are written, so partial updates
# (e.g. just `summary=...` at call end) never clobber other columns.
_UPSERT_COLUMNS = {
    "room_name": lambda v: v,
    "caller_phone": lambda v: v,
    "status": lambda v: v,
    "duration_seconds": lambda v: v,
    "transcript": lambda v: json.dumps(v),
    "lead_extracted": lambda v: 1 if v else 0,
    "lead_submitted": lambda v: 1 if v else 0,
    "lead_data": lambda v: json.dumps(v),
    "client_id": lambda v: v,
    "summary": lambda v: v,
    "recording_path": lambda v: v,
    "error": lambda v: v,
}


def upsert_call(session_id: str, **kwargs):
    conn = _get_conn()
    now = time.time()
    existing = conn.execute(
        "SELECT created_at FROM calls WHERE session_id = ?", (session_id,)
    ).fetchone()

    # Build the set of columns the caller actually provided.
    provided = {k: ser(kwargs[k]) for k, ser in _UPSERT_COLUMNS.items() if k in kwargs}
    provided["updated_at"] = now

    if existing is None:
        # First write — insert with provided values, defaulting created_at.
        cols = ["session_id", "created_at"] + list(provided.keys())
        vals = {"session_id": session_id, "created_at": now, **provided}
        placeholders = ", ".join(f":{c}" for c in cols)
        conn.execute(
            f"INSERT INTO calls ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
    else:
        # Update only the provided columns; everything else is preserved.
        set_clause = ", ".join(f"{c}=:{c}" for c in provided)
        conn.execute(
            f"UPDATE calls SET {set_clause} WHERE session_id=:session_id",
            {"session_id": session_id, **provided},
        )
    conn.commit()

def get_call(session_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM calls WHERE session_id = ?", (session_id,)
    ).fetchone()
    if not row:
        return None
    return _row_to_dict(row)

def list_calls(
    status: str | None = None,
    client_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    conn = _get_conn()
    where = []
    params: list = []
    if status:
        where.append("status = ?")
        params.append(status)
    if client_id is not None:
        where.append("client_id = ?")
        params.append(client_id)
    sql = "SELECT * FROM calls"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]

def get_active_calls(client_id: str | None = None) -> list[dict]:
    return list_calls(status="active", client_id=client_id)


def _month_start_epoch() -> float:
    """Unix epoch (UTC) for the first instant of the current calendar month."""
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def client_stats(client_id: str) -> dict:
    """Aggregate stats for a single client (business)."""
    conn = _get_conn()
    month0 = _month_start_epoch()
    row = conn.execute(
        """
        SELECT
            COUNT(*)                                              AS calls,
            COALESCE(SUM(CASE WHEN status='active' THEN 1 ELSE 0 END), 0)      AS active,
            COALESCE(SUM(lead_extracted), 0)                      AS lead_count,
            COALESCE(SUM(duration_seconds), 0)                    AS total_seconds,
            COALESCE(SUM(CASE WHEN created_at >= ? THEN duration_seconds ELSE 0 END), 0)
                                                                  AS month_seconds
        FROM calls WHERE client_id = ?
        """,
        (month0, client_id),
    ).fetchone()
    d = dict(row)
    d["total_minutes"] = round(d["total_seconds"] / 60.0, 1)
    d["month_minutes"] = round(d["month_seconds"] / 60.0, 1)
    return d


def all_client_stats() -> dict[str, dict]:
    """Per-client aggregates keyed by client_id (for the admin grid)."""
    conn = _get_conn()
    month0 = _month_start_epoch()
    rows = conn.execute(
        """
        SELECT
            client_id,
            COUNT(*)                                              AS calls,
            COALESCE(SUM(CASE WHEN status='active' THEN 1 ELSE 0 END), 0)      AS active,
            COALESCE(SUM(lead_extracted), 0)                      AS lead_count,
            COALESCE(SUM(duration_seconds), 0)                    AS total_seconds,
            COALESCE(SUM(CASE WHEN created_at >= ? THEN duration_seconds ELSE 0 END), 0)
                                                                  AS month_seconds
        FROM calls GROUP BY client_id
        """,
        (month0,),
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        cid = d.pop("client_id") or ""
        d["total_minutes"] = round(d["total_seconds"] / 60.0, 1)
        d["month_minutes"] = round(d["month_seconds"] / 60.0, 1)
        out[cid] = d
    return out

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["transcript"] = json.loads(d.get("transcript", "[]"))
    d["lead_data"] = json.loads(d.get("lead_data", "{}"))
    d["created_at"] = datetime.fromtimestamp(d["created_at"], tz=timezone.utc).isoformat() if d.get("created_at") else ""
    d["updated_at"] = datetime.fromtimestamp(d["updated_at"], tz=timezone.utc).isoformat() if d.get("updated_at") else ""
    return d

# Initialize on import
init_db()
