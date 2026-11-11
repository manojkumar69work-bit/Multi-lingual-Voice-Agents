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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
        CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY, name TEXT DEFAULT '', language TEXT DEFAULT 'hi', mode TEXT DEFAULT 'business',
            agent_name TEXT DEFAULT 'Riya', system_prompt TEXT DEFAULT '', business_info TEXT DEFAULT '',
            greeting TEXT DEFAULT '', phone_number TEXT DEFAULT '', password TEXT DEFAULT '',
            whatsapp_to TEXT DEFAULT '', sheet_id TEXT DEFAULT '', webhook_url TEXT DEFAULT '', webhook_secret TEXT DEFAULT '',
            minute_quota INTEGER DEFAULT 0, enforce_quota INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
            business_type TEXT DEFAULT '', role_description TEXT DEFAULT '', lead_fields TEXT DEFAULT '',
            closing_instructions TEXT DEFAULT '', call_key TEXT DEFAULT '', session_version INTEGER DEFAULT 1,
            created_at REAL, updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_tenants_phone ON tenants(phone_number);
        CREATE TABLE IF NOT EXISTS heartbeats (
            name TEXT PRIMARY KEY,
            ts   REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
            key TEXT NOT NULL,
            ts  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(key, ts);
    """)
    # Idempotent migration for databases created before these columns existed.
    existing = {r[1] for r in conn.execute("PRAGMA table_info(calls)").fetchall()}
    for col, ddl in (
        ("summary", "ALTER TABLE calls ADD COLUMN summary TEXT DEFAULT ''"),
        ("recording_path", "ALTER TABLE calls ADD COLUMN recording_path TEXT DEFAULT ''"),
        ("client_id", "ALTER TABLE calls ADD COLUMN client_id TEXT DEFAULT ''"),
        ("lead_status", "ALTER TABLE calls ADD COLUMN lead_status TEXT DEFAULT 'new'"),
        ("lead_notes", "ALTER TABLE calls ADD COLUMN lead_notes TEXT DEFAULT ''"),
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
    # ensure_ascii=False keeps Indic text as written, so a LIKE search for a
    # Telugu word matches; rows written before this stay \u-escaped and are
    # matched through the escaped form of the query in search_calls.
    "transcript": lambda v: json.dumps(v, ensure_ascii=False),
    "lead_extracted": lambda v: 1 if v else 0,
    "lead_submitted": lambda v: 1 if v else 0,
    "lead_data": lambda v: json.dumps(v, ensure_ascii=False),
    "client_id": lambda v: v,
    "summary": lambda v: v,
    "recording_path": lambda v: v,
    "error": lambda v: v,
    "lead_status": lambda v: v,
    "lead_notes": lambda v: v,
}

# Lead workflow states a client can set from their portal.
LEAD_STATUSES = ("new", "contacted", "visit", "won", "lost")

# SQL fragment: the lead has at least one non-empty field other than notes.
# Shared by the stats, the has_lead filter and the daily aggregates so "a lead"
# means one thing everywhere.
_HAS_LEAD_SQL = (
    "EXISTS (SELECT 1 FROM json_each(lead_data) WHERE key != 'notes' AND TRIM(value) != '')"
)


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


def search_calls(
    client_id: str,
    *,
    q: str = "",
    date_from: float | None = None,
    date_to: float | None = None,
    has_lead: bool | None = None,
    status: str | None = None,
    lead_status: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """Filtered, paged calls for one client, newest first.

    `q` is matched case-insensitively against the transcript, the lead fields,
    the summary and the caller's number. Without `status`, live calls are left
    out (the portal lists them separately)."""
    conn = _get_conn()
    where = ["client_id = ?"]
    params: list = [client_id]
    if status:
        where.append("status = ?"); params.append(status)
    else:
        where.append("status != 'active'")
    if date_from is not None:
        where.append("created_at >= ?"); params.append(float(date_from))
    if date_to is not None:
        where.append("created_at <= ?"); params.append(float(date_to))
    if has_lead is True:
        where.append(_HAS_LEAD_SQL)
    elif has_lead is False:
        where.append(f"NOT {_HAS_LEAD_SQL}")
    if lead_status:
        where.append("lead_status = ?"); params.append(lead_status)
    if q:
        like = f"%{q.strip()}%"
        # Older rows hold JSON with non-ASCII escaped (\u0c28...), so try that
        # spelling of the query too.
        escaped = "%" + json.dumps(q.strip(), ensure_ascii=True)[1:-1] + "%"
        where.append(
            "(transcript LIKE ? OR transcript LIKE ? OR lead_data LIKE ? OR lead_data LIKE ? "
            "OR summary LIKE ? OR caller_phone LIKE ?)"
        )
        params.extend([like, escaped, like, escaped, like, like])
    sql_where = " WHERE " + " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM calls{sql_where}", params).fetchone()[0]
    page = max(1, int(page)); per_page = max(1, min(500, int(per_page)))
    rows = conn.execute(
        f"SELECT * FROM calls{sql_where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    return {"items": [_row_to_dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}


def update_lead_status(session_id: str, client_id: str, status: str | None, notes: str | None) -> bool:
    """Set the lead workflow state and/or notes on one of the client's calls.
    Returns False when the call is not theirs. Raises ValueError on a bad status."""
    if status is not None and status not in LEAD_STATUSES:
        raise ValueError(f"lead_status must be one of {', '.join(LEAD_STATUSES)}")
    sets, params = ["updated_at = ?"], [time.time()]
    if status is not None:
        sets.append("lead_status = ?"); params.append(status)
    if notes is not None:
        sets.append("lead_notes = ?"); params.append(notes)
    params.extend([session_id, client_id])
    conn = _get_conn()
    cur = conn.execute(f"UPDATE calls SET {', '.join(sets)} WHERE session_id = ? AND client_id = ?", params)
    conn.commit()
    return cur.rowcount > 0


_IST = ZoneInfo("Asia/Kolkata")
OFFICE_HOURS = (9, 20)   # calls created outside 09:00–20:00 IST count as after-hours


def daily_stats(client_id: str, days: int = 30) -> list[dict]:
    """One row per calendar day (IST), oldest first, zero-filled:
    {date, calls, leads, minutes, after_hours}."""
    days = max(1, min(366, int(days)))
    today = datetime.now(_IST).date()
    start_day = today - timedelta(days=days - 1)
    start_epoch = datetime.combine(start_day, datetime.min.time(), tzinfo=_IST).timestamp()
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT created_at, duration_seconds, {_HAS_LEAD_SQL} AS has_lead "
        "FROM calls WHERE client_id = ? AND created_at >= ? AND status != 'active'",
        (client_id, start_epoch),
    ).fetchall()
    buckets: dict[str, dict] = {}
    for i in range(days):
        d = (start_day + timedelta(days=i)).isoformat()
        buckets[d] = {"date": d, "calls": 0, "leads": 0, "minutes": 0.0, "after_hours": 0}
    for r in rows:
        when = datetime.fromtimestamp(r["created_at"], tz=_IST)
        b = buckets.get(when.date().isoformat())
        if b is None:
            continue
        b["calls"] += 1
        b["leads"] += 1 if r["has_lead"] else 0
        b["minutes"] += (r["duration_seconds"] or 0) / 60.0
        if not (OFFICE_HOURS[0] <= when.hour < OFFICE_HOURS[1]):
            b["after_hours"] += 1
    out = list(buckets.values())
    for b in out:
        b["minutes"] = round(b["minutes"], 1)
    return out


def iter_calls(client_id: str):
    """Every non-live call of a client, oldest first, for exports."""
    conn = _get_conn()
    for r in conn.execute(
        "SELECT * FROM calls WHERE client_id = ? AND status != 'active' ORDER BY created_at ASC",
        (client_id,),
    ):
        yield _row_to_dict(r)


def heartbeat(name: str) -> None:
    """Record that a process (the agent worker) is alive right now."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO heartbeats (name, ts) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET ts=excluded.ts",
        (name, time.time()),
    )
    conn.commit()


def last_heartbeat(name: str) -> float | None:
    row = _get_conn().execute("SELECT ts FROM heartbeats WHERE name = ?", (name,)).fetchone()
    return float(row["ts"]) if row else None


def count_status(status: str) -> int:
    return _get_conn().execute("SELECT COUNT(*) FROM calls WHERE status = ?", (status,)).fetchone()[0]


def mark_stalled(older_than_seconds: float) -> int:
    """Calls still 'active' with no update for this long never ended cleanly
    (worker crash, lost room). Mark them so live counts stop lying."""
    conn = _get_conn()
    cutoff = time.time() - older_than_seconds
    cur = conn.execute(
        "UPDATE calls SET status='stalled', updated_at=? WHERE status='active' AND updated_at < ?",
        (time.time(), cutoff),
    )
    conn.commit()
    if cur.rowcount:
        logger.warning("Marked %d call(s) as stalled", cur.rowcount)
    return cur.rowcount


def _month_start_epoch() -> float:
    """Unix epoch (UTC) for the first instant of the current calendar month."""
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def client_stats(client_id: str) -> dict:
    """Aggregate stats for a single client (business).

    lead_count counts calls whose lead has at least one non-empty field other
    than notes — `lead_extracted` is set even when extraction returned all
    blanks, so summing it overstated leads."""
    conn = _get_conn()
    month0 = _month_start_epoch()
    row = conn.execute(
        """
        SELECT
            COUNT(*)                                              AS calls,
            COALESCE(SUM(CASE WHEN status='active' THEN 1 ELSE 0 END), 0)      AS active,
            COALESCE(SUM(CASE WHEN EXISTS (SELECT 1 FROM json_each(lead_data) WHERE key != 'notes' AND TRIM(value) != '') THEN 1 ELSE 0 END), 0) AS lead_count,
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
            COALESCE(SUM(CASE WHEN EXISTS (SELECT 1 FROM json_each(lead_data) WHERE key != 'notes' AND TRIM(value) != '') THEN 1 ELSE 0 END), 0) AS lead_count,
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
