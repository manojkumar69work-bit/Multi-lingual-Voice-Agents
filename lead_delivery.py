from __future__ import annotations
"""Durable lead delivery — a lead is never lost because a third party was down.

Before this module, agent.py pushed each lead straight to Google Sheets and
WhatsApp with `asyncio.gather(..., return_exceptions=True)`. Any failure (Sheets
quota, Twilio blip, expired credentials, the call process exiting mid-push) was
logged and dropped. For a product whose entire value is "you never miss a lead",
that is the worst possible failure mode — and a silent one.

Design:
  agent.py            → enqueue() writes one row per destination, then returns.
                        Never blocks the call; never raises into the call path.
  server.py (worker)  → drains due rows with exponential backoff.

The queue lives in the same SQLite file as `calls` (shared WAL), so the agent
and server processes see the same rows. Delivery is therefore decoupled from the
lifetime of the call process: if the agent dies right after a call, the server
still delivers.

Destinations are resolved PER TENANT, falling back to the global env vars:
  sheets    → tenant.sheet_id      or GOOGLE_SHEET_ID
  whatsapp  → tenant.whatsapp_to   or WHATSAPP_TO
  webhook   → tenant.webhook_url   (no global fallback; opt-in per client)

Every attempt is recorded, so the admin portal can show what was delivered,
what is retrying, and what gave up.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone

import call_store

logger = logging.getLogger("lead-delivery")

# ── Config ──────────────────────────────────────────────────────────────────

SHEETS_CREDENTIALS = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
SHEETS_ID = os.environ.get("GOOGLE_SHEET_ID", "")

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
WHATSAPP_TO = os.environ.get("WHATSAPP_TO", "")

# Backoff schedule in seconds, indexed by attempt count. A lead alert is only
# useful while the caller is still warm, so the first two retries are fast; the
# tail is long enough to ride out a multi-hour outage without hammering.
BACKOFF_SECONDS = [30, 120, 600, 3600, 21600]
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1  # one initial try + one per backoff step

WORKER_POLL_SECONDS = float(os.environ.get("LEAD_WORKER_POLL_SECONDS", "2"))
WORKER_BATCH = int(os.environ.get("LEAD_WORKER_BATCH", "10"))

CHANNELS = ("sheets", "whatsapp", "webhook")


# ── Schema ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    conn = call_store.get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lead_deliveries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT    DEFAULT '',
            client_id       TEXT    DEFAULT '',
            channel         TEXT    NOT NULL,
            destination     TEXT    DEFAULT '',
            payload         TEXT    NOT NULL,
            status          TEXT    DEFAULT 'pending',
            attempts        INTEGER DEFAULT 0,
            next_attempt_at REAL    DEFAULT 0,
            last_error      TEXT    DEFAULT '',
            created_at      REAL,
            updated_at      REAL,
            delivered_at    REAL
        );
        CREATE INDEX IF NOT EXISTS idx_deliv_due    ON lead_deliveries(status, next_attempt_at);
        CREATE INDEX IF NOT EXISTS idx_deliv_client ON lead_deliveries(client_id);
        CREATE INDEX IF NOT EXISTS idx_deliv_session ON lead_deliveries(session_id);
        """
    )
    conn.commit()


# ── Enqueue ─────────────────────────────────────────────────────────────────

def _tenant_attr(tenant, name: str, default: str = "") -> str:
    return (getattr(tenant, name, "") or default or "").strip()


def resolve_destinations(tenant) -> list[tuple[str, str]]:
    """Return [(channel, destination)] configured for this tenant.

    Per-tenant values win; global env vars are the fallback so a single-client
    deployment keeps working with no per-tenant setup.
    """
    out: list[tuple[str, str]] = []

    sheet_id = _tenant_attr(tenant, "sheet_id", SHEETS_ID)
    if sheet_id and SHEETS_CREDENTIALS:
        out.append(("sheets", sheet_id))

    to_number = _tenant_attr(tenant, "whatsapp_to", WHATSAPP_TO)
    if to_number and TWILIO_SID and TWILIO_TOKEN and WHATSAPP_FROM:
        out.append(("whatsapp", to_number))

    hook = _tenant_attr(tenant, "webhook_url")
    if hook:
        out.append(("webhook", hook))

    return out


def enqueue_lead(lead: dict, tenant, session_id: str = "", lead_fields: list[str] | None = None) -> int:
    """Queue one delivery row per configured destination. Returns rows written.

    Safe to call from the call path: it only touches local SQLite and swallows
    its own errors, so a queue problem can never break a live call.
    """
    if not lead or not any(lead.values()):
        return 0

    destinations = resolve_destinations(tenant)
    if not destinations:
        logger.info("No lead destinations configured for tenant %r — nothing queued",
                    getattr(tenant, "id", "?"))
        return 0

    payload = json.dumps(
        {
            "lead": lead,
            "lead_fields": lead_fields or list(lead.keys()),
            "client_name": getattr(tenant, "name", "") or getattr(tenant, "id", ""),
            "client_id": getattr(tenant, "id", ""),
            "session_id": session_id,
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
    )

    now = time.time()
    written = 0
    try:
        conn = call_store.get_conn()
        for channel, destination in destinations:
            conn.execute(
                """
                INSERT INTO lead_deliveries
                    (session_id, client_id, channel, destination, payload,
                     status, attempts, next_attempt_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (session_id, getattr(tenant, "id", ""), channel, destination,
                 payload, now, now, now),
            )
            written += 1
        conn.commit()
        logger.info("Queued lead for %d destination(s): %s",
                    written, ", ".join(c for c, _ in destinations))
    except Exception as e:  # never propagate into the call
        logger.error("Failed to queue lead: %s", e)
        return 0
    return written


# ── Queue mechanics ─────────────────────────────────────────────────────────

def claim_due(limit: int = WORKER_BATCH) -> list[dict]:
    """Return pending rows whose next_attempt_at has passed, oldest first."""
    conn = call_store.get_conn()
    rows = conn.execute(
        """
        SELECT * FROM lead_deliveries
        WHERE status = 'pending' AND next_attempt_at <= ?
        ORDER BY next_attempt_at ASC
        LIMIT ?
        """,
        (time.time(), limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _mark_delivered(row_id: int) -> None:
    now = time.time()
    conn = call_store.get_conn()
    conn.execute(
        """UPDATE lead_deliveries
           SET status='delivered', delivered_at=?, updated_at=?, last_error=''
           WHERE id=?""",
        (now, now, row_id),
    )
    conn.commit()


def _mark_failed(row_id: int, attempts: int, error: str) -> None:
    """Schedule the next retry, or give up once the backoff schedule is spent."""
    now = time.time()
    conn = call_store.get_conn()
    if attempts >= MAX_ATTEMPTS:
        conn.execute(
            """UPDATE lead_deliveries
               SET status='dead', attempts=?, updated_at=?, last_error=?
               WHERE id=?""",
            (attempts, now, error[:500], row_id),
        )
        logger.error("Delivery %d gave up after %d attempts: %s", row_id, attempts, error)
    else:
        delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
        conn.execute(
            """UPDATE lead_deliveries
               SET status='pending', attempts=?, next_attempt_at=?, updated_at=?, last_error=?
               WHERE id=?""",
            (attempts, now + delay, now, error[:500], row_id),
        )
        logger.warning("Delivery %d attempt %d failed (%s) — retrying in %ds",
                       row_id, attempts, error, delay)
    conn.commit()


def retry_now(row_id: int) -> bool:
    """Admin action: put a dead or pending row back at the front of the queue."""
    conn = call_store.get_conn()
    cur = conn.execute(
        """UPDATE lead_deliveries
           SET status='pending', next_attempt_at=?, updated_at=?
           WHERE id=? AND status IN ('pending','dead')""",
        (0, time.time(), row_id),
    )
    conn.commit()
    return cur.rowcount > 0


def list_deliveries(client_id: str | None = None, status: str | None = None,
                    limit: int = 100) -> list[dict]:
    conn = call_store.get_conn()
    where, params = [], []
    if client_id is not None:
        where.append("client_id = ?")
        params.append(client_id)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM lead_deliveries"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def queue_stats(client_id: str | None = None) -> dict:
    """Counts by status — drives the admin health tile."""
    conn = call_store.get_conn()
    sql = "SELECT status, COUNT(*) AS n FROM lead_deliveries"
    params: list = []
    if client_id is not None:
        sql += " WHERE client_id = ?"
        params.append(client_id)
    sql += " GROUP BY status"
    counts = {r["status"]: r["n"] for r in conn.execute(sql, params).fetchall()}
    return {
        "pending": counts.get("pending", 0),
        "delivered": counts.get("delivered", 0),
        "dead": counts.get("dead", 0),
    }


# ── Channel implementations ─────────────────────────────────────────────────

def _format_lead_message(data: dict) -> str:
    lead = data.get("lead", {})
    lines = [f"🔔 *New Lead — {data.get('client_name', 'Your Business')}*"]
    for key in data.get("lead_fields") or lead.keys():
        value = lead.get(key)
        if value:
            lines.append(f"*{key.replace('_', ' ').title()}:* {value}")
    lines.append(f"\n{data.get('captured_at', '')}")
    return "\n".join(lines)


def _deliver_sheets_blocking(data: dict, sheet_id: str) -> None:
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        SHEETS_CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(sheet_id).sheet1
    lead = data.get("lead", {})
    fields = data.get("lead_fields") or list(lead.keys())
    row = [lead.get(k, "") for k in fields] + [data.get("captured_at", "")]
    ws.append_row(row, value_input_option="USER_ENTERED")


def _deliver_whatsapp_blocking(data: dict, to_number: str) -> None:
    from twilio.rest import Client

    Client(TWILIO_SID, TWILIO_TOKEN).messages.create(
        body=_format_lead_message(data), from_=WHATSAPP_FROM, to=to_number
    )


async def _deliver_webhook(data: dict, url: str, secret: str = "") -> None:
    """POST the lead as JSON. When the tenant set a webhook_secret we sign the
    body so the receiver can verify it really came from us."""
    import httpx

    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-MEA-Signature"] = "sha256=" + hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, content=body, headers=headers)
        resp.raise_for_status()


async def deliver(row: dict) -> None:
    """Dispatch one queued row. Raises on failure so the caller can back off."""
    channel = row["channel"]
    destination = row.get("destination") or ""
    data = json.loads(row["payload"])

    if channel == "sheets":
        if not SHEETS_CREDENTIALS:
            raise RuntimeError("GOOGLE_SHEETS_CREDENTIALS not configured")
        await asyncio.to_thread(_deliver_sheets_blocking, data, destination)

    elif channel == "whatsapp":
        if not (TWILIO_SID and TWILIO_TOKEN and WHATSAPP_FROM):
            raise RuntimeError("Twilio WhatsApp not configured")
        await asyncio.to_thread(_deliver_whatsapp_blocking, data, destination)

    elif channel == "webhook":
        import tenants
        t = tenants.get_tenant(row.get("client_id") or "")
        await _deliver_webhook(data, destination, getattr(t, "webhook_secret", "") if t else "")

    else:
        raise RuntimeError(f"unknown channel {channel!r}")


# ── Worker ──────────────────────────────────────────────────────────────────

async def drain_once(limit: int = WORKER_BATCH) -> int:
    """Attempt every due delivery. Returns how many succeeded."""
    due = claim_due(limit)
    if not due:
        return 0

    delivered = 0
    for row in due:
        attempts = int(row.get("attempts", 0)) + 1
        try:
            await deliver(row)
            _mark_delivered(int(row["id"]))
            delivered += 1
            logger.info("Delivered lead %s via %s", row["id"], row["channel"])
        except Exception as e:
            _mark_failed(int(row["id"]), attempts, f"{type(e).__name__}: {e}")
    return delivered


async def run_worker(stop: asyncio.Event | None = None) -> None:
    """Poll the queue forever. Started by server.py on startup."""
    logger.info("Lead delivery worker started (poll=%.1fs)", WORKER_POLL_SECONDS)
    while not (stop and stop.is_set()):
        try:
            await drain_once()
        except Exception as e:  # a bug here must not kill the worker
            logger.error("Delivery worker iteration failed: %s", e)
        try:
            if stop:
                await asyncio.wait_for(stop.wait(), timeout=WORKER_POLL_SECONDS)
            else:
                await asyncio.sleep(WORKER_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("Lead delivery worker stopped")


# Initialize on import so either process can enqueue immediately.
init_db()
