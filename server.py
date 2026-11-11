from __future__ import annotations
"""Voice agent server — portals, auth, LiveKit tokens, and dashboard APIs.

The real-time voice pipeline (STT → LLM → TTS) runs inside agent.py via LiveKit.
This server provides:
  - Public test-caller UI (index.html) + login page
  - Admin portal  (/admin)  — manage clients, see per-client usage
  - Client portal (/client) — an agency sees its own calls, minutes, summaries, audio
  - Auth (signed cookie sessions; see auth.py)
  - LiveKit AccessToken generation (browser test calls, tagged to a client)
  - Recording file serving (access-checked)
  - Health check
"""

from dotenv import load_dotenv
load_dotenv()

import asyncio
import csv
import httpx
import hmac
import io
import json
from datetime import datetime, timedelta, timezone
import logging
import os
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import auth
import call_store
import languages
import lead_delivery
import tenants

logger = logging.getLogger("voice-server")
logging.basicConfig(level=logging.INFO)

# ─── Env ────────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

SHEETS_CREDENTIALS = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
SHEETS_ID = os.environ.get("GOOGLE_SHEET_ID", "")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
WHATSAPP_TO = os.environ.get("WHATSAPP_TO", "")

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
LOCAL_TTS_URL = os.environ.get("LOCAL_TTS_URL", "http://localhost:8002")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")

RECORDINGS_DIR = os.environ.get(
    "RECORDINGS_DIR", os.path.join(os.path.dirname(__file__), "recordings")
)

CONTACTS_FILE = os.environ.get(
    "CONTACTS_FILE", os.path.join(os.path.dirname(__file__), "contacts.json")
)

# Browser origins allowed to call the API. The portals are same-origin, so the
# default is "no cross-origin access at all"; set CORS_ORIGINS (comma-separated)
# only when a portal is genuinely served from another host.
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]

# ─── App ────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the lead delivery worker alongside the API.

    The worker lives here rather than in agent.py because this process is
    long-lived: a lead queued by a call that has already ended still gets
    delivered, and retries survive agent restarts.
    """
    auth.check_config()  # raises in production if secrets are still dev defaults
    stop = asyncio.Event()
    worker = asyncio.create_task(lead_delivery.run_worker(stop))
    reaper = asyncio.create_task(_reap_stalled_calls(stop))
    try:
        yield
    finally:
        stop.set()
        for task in (worker, reaper):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


STALLED_AFTER_SECONDS = int(os.environ.get("STALLED_AFTER_SECONDS", str(2 * 3600)))
STALLED_CHECK_SECONDS = int(os.environ.get("STALLED_CHECK_SECONDS", "600"))


async def _reap_stalled_calls(stop: asyncio.Event) -> None:
    """Every 10 minutes, mark calls 'active' for over 2 hours as stalled."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(call_store.mark_stalled, STALLED_AFTER_SECONDS)
        except Exception as e:
            logger.warning("stalled-call reaper failed: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=STALLED_CHECK_SECONDS)
        except asyncio.TimeoutError:
            pass


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=bool(CORS_ORIGINS),
)
APP_DIR = os.path.dirname(os.path.abspath(__file__))


# ─── Models ──────────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    """Caller address; behind a reverse proxy, trust X-Forwarded-For when told to."""
    if os.environ.get("TRUST_PROXY", "0") == "1":
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class LiveKitTokenRequest(BaseModel):
    room: str = ""
    client_id: str = ""        # tag the test call to a client (or "" for default)
    call_key: str | None = None  # the client's public call key (call page / widget)
    language: str = ""         # browser-test language shortcut when no client


class LoginRequest(BaseModel):
    username: str
    password: str


class ContactRequest(BaseModel):
    name: str
    email: str
    company: str = ""
    service: str = ""
    message: str = ""


class LeadUpdate(BaseModel):
    lead_status: str | None = None
    lead_notes: str | None = None


class ClientUpsert(BaseModel):
    id: str | None = None
    name: str | None = None
    language: str | None = None
    mode: str | None = None
    agent_name: str | None = None
    system_prompt: str | None = None
    business_info: str | None = None
    greeting: str | None = None
    phone_number: str | None = None
    password: str | None = None
    whatsapp_to: str | None = None
    sheet_id: str | None = None
    webhook_url: str | None = None
    webhook_secret: str | None = None
    minute_quota: int | None = None
    enforce_quota: bool | None = None
    active: bool | None = None
    business_type: str | None = None
    role_description: str | None = None
    lead_fields: str | None = None
    closing_instructions: str | None = None


# ─── Static assets (logo, images, etc.) ──────────────────────────────────────
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")

# ─── Routes: static pages ────────────────────────────────────────────────────

def _page(name: str, **headers):
    return FileResponse(os.path.join(APP_DIR, name), headers=headers or None)

@app.get("/")
@app.get("/index.html")
def index():
    return _page("index.html", **{"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})

@app.get("/home")
@app.get("/landing")
def landing_page():
    return _page("landing.html")

@app.get("/login")
def login_page():
    return _page("login.html")

@app.get("/admin")
def admin_page():
    return _page("admin.html")

@app.get("/client")
def client_page():
    return _page("client.html")


def _public_tenant(slug: str):
    """The tenant behind a public call page, or 404 when unknown or paused."""
    t = tenants.get_tenant(slug)
    if not t or t.id == "default" or not t.active:
        raise HTTPException(status_code=404, detail="No agent is answering at this address")
    return t


@app.get("/call/{slug}")
def call_page(slug: str):
    """Public call page for one client: what they link to or embed on their site."""
    _public_tenant(slug)
    return _page("call.html")


@app.get("/api/call/{slug}/config")
def call_config(slug: str):
    """What the public call page needs. The call_key is public by design: it only
    lets a browser start a call with this client's agent, which the page exists
    to do; rotating it from the admin invalidates every old embed at once."""
    t = _public_tenant(slug)
    return {
        "client_id": t.id,
        "name": t.name or t.id,
        "agent_name": t.agent_name or "Riya",
        "language": t.language,
        "call_key": t.call_key,
    }

@app.get("/dashboard")
def dashboard_page():
    return _page("dashboard.html")

@app.get("/demo")
def voice_demo():
    return FileResponse(os.path.join(APP_DIR, "static", "voices_demo.html"))

@app.get("/demo/sarvam")
def sarvam_demo():
    return FileResponse(os.path.join(APP_DIR, "static", "sarvam_demo.html"))

# Samples live in subdirectories too (static/voices/sarvam/...), so the route
# has to accept a relative path — which means it must check where that path
# actually lands. Without this, "%2e%2e%2f%2e%2e%2f.env" walked out of the
# directory and served .env — API keys, SESSION_SECRET, the admin password — to
# anyone, unauthenticated.
_VOICES_DIR = os.path.realpath(os.path.join(APP_DIR, "static", "voices"))


@app.get("/demo/voice/{filename:path}")
def voice_file(filename: str):
    path = os.path.realpath(os.path.join(_VOICES_DIR, filename))
    inside = path == _VOICES_DIR or path.startswith(_VOICES_DIR + os.sep)
    if not inside or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)


# ─── Routes: health + LiveKit token ──────────────────────────────────────────

def _probe(url: str) -> str:
    """"ok" when the URL answers any HTTP status within 2 s, else "down"."""
    try:
        httpx.get(url, timeout=2.0)
        return "ok"
    except Exception:
        return "down"


@app.get("/api/health")
def health(response: Response):
    """What the operator needs to know at a glance. 503 when any dependency a
    call needs (TTS, LiveKit, the database) is down."""
    lead_channels = []
    if SHEETS_ID and SHEETS_CREDENTIALS:
        lead_channels.append("sheets")
    if TWILIO_SID and WHATSAPP_TO:
        lead_channels.append("whatsapp")
    try:
        call_store.get_conn().execute("SELECT 1").fetchone()
        db = "ok"
    except Exception:
        db = "down"
    tts = _probe(f"{LOCAL_TTS_URL}/health")
    livekit = _probe(_livekit_http_url())
    queue = lead_delivery.queue_stats() if db == "ok" else {}
    seen = call_store.last_heartbeat("agent") if db == "ok" else None
    ok = db == "ok" and tts == "ok" and livekit == "ok"
    if not ok:
        response.status_code = 503
    return {
        "ok": ok,
        "status": "ok" if ok else "degraded",
        "tts": tts,
        "livekit": livekit,
        "db": db,
        "queue_pending": queue.get("pending", 0),
        "queue_dead": queue.get("dead", 0),
        "stalled": call_store.count_status("stalled") if db == "ok" else 0,
        "worker_seen_at": datetime.fromtimestamp(seen, tz=timezone.utc).isoformat() if seen else None,
        "groq_configured": bool(GROQ_API_KEY),
        "lead_channels": lead_channels,
    }


@app.post("/api/contact")
def contact(req: ContactRequest):
    """Persist a landing-page lead to contacts.json (newest first)."""
    from datetime import datetime, timezone

    entry = req.model_dump()
    entry["ts"] = datetime.now(timezone.utc).isoformat()

    try:
        existing = []
        if os.path.isfile(CONTACTS_FILE):
            with open(CONTACTS_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f) or []
        existing.insert(0, entry)
        with open(CONTACTS_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"failed to persist contact: {e}")
        raise HTTPException(status_code=500, detail="Could not save your message")

    logger.info(f"new contact lead: {entry.get('name')} <{entry.get('email')}>")
    return {"ok": True}


def _livekit_http_url() -> str:
    return LIVEKIT_URL.replace("wss://", "https://").replace("ws://", "http://")


@app.post("/api/livekit/token")
async def livekit_token(req: LiveKitTokenRequest, user=Depends(auth.current_user)):
    """Issue a browser join token. We pre-create the room with metadata
    {"tenant": "<client_id>"} so the agent (which reads room metadata) attributes
    the call to the right client — the exact mechanism a SIP dispatch rule will
    use for real phone numbers later.

    Who may ask: an admin (any client, or a language-only test call), the client
    itself (its own id), or anyone holding the client's call key — the call
    page and widget. Nothing else, so nobody can burn a client's minutes by
    guessing its slug."""
    from livekit import api
    room = req.room or f"call_{uuid.uuid4().hex[:8]}"
    identity = f"user_{uuid.uuid4().hex[:8]}"

    role = (user or {}).get("role")
    metadata = {}
    if req.client_id:
        t = tenants.get_tenant(req.client_id)
        if not t or t.id == "default" or not t.active:
            raise HTTPException(status_code=401, detail="Unknown or paused client")
        allowed = (
            role == "admin"
            or (role == "client" and user.get("client_id") == t.id)
            or (req.call_key and hmac.compare_digest(req.call_key, t.call_key or ""))
        )
        if not allowed:
            raise HTTPException(status_code=401, detail="Not allowed to call this client")
        metadata["tenant"] = t.id
    elif req.language in languages.LANGUAGES:
        if role != "admin":
            raise HTTPException(status_code=401, detail="Sign in as admin to run a test call")
        metadata["language"] = req.language
    else:
        raise HTTPException(status_code=401, detail="client_id with call_key, or an admin session, required")

    # Pre-create the room carrying the metadata the agent will read.
    if metadata:
        try:
            lkapi = api.LiveKitAPI(_livekit_http_url(), LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            try:
                await lkapi.room.create_room(
                    api.CreateRoomRequest(name=room, metadata=json.dumps(metadata))
                )
            finally:
                await lkapi.aclose()
        except Exception as e:
            logger.warning(f"room pre-create failed (continuing): {e}")

    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True))
        .with_metadata(json.dumps(metadata) if metadata else "")
    )
    return {
        "token": token.to_jwt(),
        "room": room,
        "identity": identity,
        "url": LIVEKIT_URL,
        "metadata": metadata,
    }


# ─── Routes: auth ─────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request):
    keys = (f"u:{(req.username or '').strip().lower()}", f"ip:{_client_ip(request)}")
    if any(auth.login_blocked(k) for k in keys):
        raise HTTPException(status_code=429, detail="Too many failed sign-ins. Try again in 15 minutes.")
    user = auth.authenticate(req.username, req.password)
    if not user:
        for k in keys:
            auth.record_login_failure(k)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    for k in keys:
        auth.clear_login_failures(k)
    resp = JSONResponse(
        {"role": user["role"], "client_id": user["client_id"], "name": user["name"]}
    )
    subject = user["client_id"] if user["role"] == "client" else "admin"
    auth.set_session_cookie(resp, user["role"], subject, sv=user.get("session_version", 0))
    return resp


@app.post("/api/auth/logout")
def logout():
    resp = JSONResponse({"ok": True})
    auth.clear_session_cookie(resp)
    return resp


@app.get("/api/auth/me")
def me(user=Depends(auth.current_user)):
    if not user or not user.get("role"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ─── Routes: ADMIN API (require_admin) ────────────────────────────────────────

def _client_with_stats(t: dict, stats_map: dict) -> dict:
    # `t` carries the tenant config including the `active` boolean (on/off switch).
    # Stats use `active_calls` for the live-call count so we don't clobber it.
    s = stats_map.get(t["id"], {})
    return {
        **t,
        "calls": s.get("calls", 0),
        "active_calls": s.get("active", 0),
        "lead_count": s.get("lead_count", 0),
        "total_minutes": s.get("total_minutes", 0.0),
        "month_minutes": s.get("month_minutes", 0.0),
    }


@app.get("/api/languages")
async def api_languages():
    """Languages a tenant can be set to; feeds the admin and test-caller pickers."""
    return languages.public_list()


@app.get("/api/admin/clients")
def admin_clients(_admin=Depends(auth.require_admin)):
    stats_map = call_store.all_client_stats()
    clients = [_client_with_stats(t, stats_map) for t in tenants.list_tenants()]
    return {"clients": clients}


@app.get("/api/admin/overview")
def admin_overview(_admin=Depends(auth.require_admin)):
    stats_map = call_store.all_client_stats()
    client_list = tenants.list_tenants()
    real_ids = {t["id"] for t in client_list}
    total_calls = sum(s.get("calls", 0) for s in stats_map.values())
    total_active = sum(s.get("active", 0) for s in stats_map.values())
    total_minutes = round(sum(s.get("total_seconds", 0) for s in stats_map.values()) / 60.0, 1)
    total_leads = sum(s.get("lead_count", 0) for s in stats_map.values())
    return {
        "total_clients": len(client_list),
        "total_calls": total_calls,
        "total_active": total_active,
        "total_minutes": total_minutes,
        "total_leads": total_leads,
    }


@app.post("/api/admin/clients")
def admin_create_client(body: ClientUpsert, _admin=Depends(auth.require_admin)):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    if not data.get("name") and not data.get("id"):
        raise HTTPException(status_code=400, detail="name or id required")
    try:
        cfg = tenants.upsert_tenant(data, create_only=True)
    except tenants.TenantExists:
        raise HTTPException(status_code=409, detail="A client with this id already exists")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"client": cfg.safe_dict()}


@app.put("/api/admin/clients/{client_id}")
def admin_update_client(client_id: str, body: ClientUpsert, _admin=Depends(auth.require_admin)):
    if not tenants.get_tenant(client_id):
        raise HTTPException(status_code=404, detail="Client not found")
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    # The admin form never receives existing secrets back, so it submits them
    # blank when unchanged. Drop blanks rather than wiping the stored value.
    for f in tenants.TenantConfig.SECRET_FIELDS:
        if data.get(f) == "":
            data.pop(f)
    data["id"] = client_id
    try:
        cfg = tenants.upsert_tenant(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"client": cfg.safe_dict()}


@app.post("/api/admin/clients/{client_id}/rotate-key")
def admin_rotate_key(client_id: str, _admin=Depends(auth.require_admin)):
    try:
        key = tenants.rotate_call_key(client_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Client not found")
    return {"call_key": key}


@app.delete("/api/admin/clients/{client_id}")
def admin_delete_client(client_id: str, _admin=Depends(auth.require_admin)):
    try:
        ok = tenants.delete_tenant(client_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="Client not found")
    return {"ok": True}


@app.get("/api/admin/clients/{client_id}/calls")
def admin_client_calls(client_id: str, _admin=Depends(auth.require_admin)):
    return _calls_payload(client_id)


# ─── Routes: CLIENT API (require_client, self-scoped) ─────────────────────────

@app.get("/api/client/stats")
def client_stats(client_id: str = Depends(auth.require_client)):
    t = tenants.get_tenant(client_id)
    stats = call_store.client_stats(client_id)
    stats["minute_quota"] = (t.minute_quota if t else 0)
    stats["name"] = (t.name if t else client_id)
    return stats


def _parse_when(value: str | None, *, end: bool = False) -> float | None:
    """Accept a date (YYYY-MM-DD, taken as the whole day in UTC) or an ISO
    datetime. Returns an epoch, or None when blank."""
    if not value:
        return None
    v = value.strip()
    try:
        if len(v) == 10:
            d = datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
            if end:
                d = d + timedelta(days=1) - timedelta(microseconds=1)
            return d.timestamp()
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Bad date: {value!r}")


@app.get("/api/client/calls")
def client_calls(
    client_id: str = Depends(auth.require_client),
    q: str = "", from_: str | None = Query(default=None, alias="from"), to: str | None = None,
    has_lead: str | None = None, status: str | None = None, lead_status: str | None = None,
    page: int = 1, per_page: int = 50,
):
    """The client's calls: live ones in `active`, the rest filtered and paged in
    `recent`. Query params: q, from, to, has_lead (1/0), status, lead_status,
    page, per_page."""
    hl = None if has_lead in (None, "") else has_lead.lower() in ("1", "true", "yes")
    found = call_store.search_calls(
        client_id, q=q, date_from=_parse_when(from_), date_to=_parse_when(to, end=True),
        has_lead=hl, status=status, lead_status=lead_status, page=page, per_page=per_page,
    )
    active = call_store.get_active_calls(client_id=client_id)
    return {
        "active": active,
        "recent": found["items"],
        "total_active": len(active),
        "total_completed": found["total"],
        "total": found["total"],
        "page": found["page"],
        "per_page": found["per_page"],
    }


@app.get("/api/client/calls/daily")
def client_calls_daily(days: int = 30, client_id: str = Depends(auth.require_client)):
    return call_store.daily_stats(client_id, days)


@app.patch("/api/client/calls/{session_id}")
def client_update_lead(session_id: str, body: LeadUpdate, client_id: str = Depends(auth.require_client)):
    try:
        ok = call_store.update_lead_status(session_id, client_id, body.lead_status, body.lead_notes)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="Call not found")
    return call_store.get_call(session_id)


@app.get("/api/client/export.csv")
def client_export_csv(client_id: str = Depends(auth.require_client)):
    """Every call with its lead fields, as CSV. Cells that would start a
    spreadsheet formula are prefixed with an apostrophe."""
    t = tenants.get_tenant(client_id)
    fields = [f.strip().replace(" ", "_").lower() for f in (t.lead_fields if t else "").split(",") if f.strip()] or ["name", "notes"]
    esc = lead_delivery.escape_cell

    def rows():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["session_id", "started_at", "duration_seconds", "caller_phone", "lead_status", *fields, "summary", "recording"])
        yield buf.getvalue(); buf.seek(0); buf.truncate()
        for c in call_store.iter_calls(client_id):
            ld = c.get("lead_data") or {}
            extra = [k for k in ld if k not in fields]
            w.writerow([
                c["session_id"], c.get("created_at", ""), int(c.get("duration_seconds") or 0),
                esc(c.get("caller_phone", "")), c.get("lead_status") or "new",
                *[esc(ld.get(k, "")) for k in fields],
                esc(c.get("summary", "")) + ("" if not extra else " | " + "; ".join(f"{k}: {ld[k]}" for k in extra if ld.get(k))),
                f"/api/recordings/{c['session_id']}" if c.get("recording_path") else "",
            ])
            yield buf.getvalue(); buf.seek(0); buf.truncate()

    name = f"{client_id}-calls-{datetime.now(timezone.utc).date().isoformat()}.csv"
    return StreamingResponse(rows(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/client/calls/{session_id}")
def client_call_detail(session_id: str, client_id: str = Depends(auth.require_client)):
    c = call_store.get_call(session_id)
    if not c or c.get("client_id") != client_id:
        raise HTTPException(status_code=404, detail="Call not found")
    return c


# ─── Routes: recordings (access-checked) ──────────────────────────────────────

@app.get("/api/recordings/{session_id}")
def get_recording(session_id: str, user=Depends(auth.current_user)):
    if not user or not user.get("role"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    c = call_store.get_call(session_id)
    if not c:
        raise HTTPException(status_code=404, detail="Call not found")
    # Clients may only access their own recordings; admin may access any.
    if user["role"] == "client" and c.get("client_id") != user.get("client_id"):
        raise HTTPException(status_code=403, detail="Forbidden")
    rec = c.get("recording_path") or ""
    if not rec:
        raise HTTPException(status_code=404, detail="No recording for this call")
    # Defend against path traversal — only a bare filename inside RECORDINGS_DIR.
    safe = os.path.basename(rec)
    path = os.path.join(RECORDINGS_DIR, safe)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Recording file missing")
    return FileResponse(path, media_type="audio/wav", filename=safe)


# ─── Lead delivery queue (admin) ──────────────────────────────────────────────

@app.get("/api/admin/deliveries")
def admin_deliveries(client_id: str | None = None, status: str | None = None,
                     limit: int = 100, _admin=Depends(auth.require_admin)):
    return {
        "deliveries": lead_delivery.list_deliveries(client_id=client_id, status=status, limit=limit),
        "stats": lead_delivery.queue_stats(),
    }


@app.post("/api/admin/deliveries/{delivery_id}/retry")
def admin_retry_delivery(delivery_id: int, _admin=Depends(auth.require_admin)):
    if not lead_delivery.retry_now(delivery_id):
        raise HTTPException(status_code=404, detail="Delivery not found or already delivered")
    return {"ok": True}


@app.get("/api/client/deliveries")
def client_deliveries(client_id: str = Depends(auth.require_client)):
    """A client can see whether its own leads actually reached Sheets/WhatsApp/CRM."""
    return {
        "deliveries": lead_delivery.list_deliveries(client_id=client_id, limit=100),
        "stats": lead_delivery.queue_stats(client_id=client_id),
    }


# ─── Internal dashboard (kept; global view — admin only) ──────────────────────

@app.get("/api/dashboard/calls")
def dashboard_calls(limit: int = 20, _admin=Depends(auth.require_admin)):
    """Global cross-tenant view. Was unauthenticated, which exposed every
    client's transcripts and lead data to anyone who knew the URL."""
    active = call_store.get_active_calls()
    recent = call_store.list_calls(status="completed", limit=max(1, min(limit, 500)))
    completed_list = call_store.list_calls(status="completed", limit=9999)
    return {
        "active": active,
        "recent": recent,
        "total_active": len(active),
        "total_completed": len(completed_list),
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _calls_payload(client_id: str) -> dict:
    active = call_store.get_active_calls(client_id=client_id)
    recent = call_store.list_calls(client_id=client_id, limit=200)
    completed = [c for c in recent if c.get("status") == "completed"]
    return {
        "active": active,
        "recent": completed,
        "total_active": len(active),
        "total_completed": len(completed),
    }


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
