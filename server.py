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
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import auth
import call_store
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
    try:
        yield
    finally:
        stop.set()
        worker.cancel()
        try:
            await worker
        except (asyncio.CancelledError, Exception):
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

class LiveKitTokenRequest(BaseModel):
    room: str = ""
    client_id: str = ""        # tag the test call to a client (or "" for default)
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


class ClientUpsert(BaseModel):
    id: str | None = None
    name: str | None = None
    language: str | None = None
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

@app.get("/dashboard")
def dashboard_page():
    return _page("dashboard.html")

@app.get("/demo")
def voice_demo():
    return FileResponse(os.path.join(APP_DIR, "static", "voices_demo.html"))

@app.get("/demo/sarvam")
def sarvam_demo():
    return FileResponse(os.path.join(APP_DIR, "static", "sarvam_demo.html"))

@app.get("/demo/voice/{filename:path}")
def voice_file(filename: str):
    return FileResponse(os.path.join(APP_DIR, "static", "voices", filename))


# ─── Routes: health + LiveKit token ──────────────────────────────────────────

@app.get("/api/health")
def health():
    lead_channels = []
    if SHEETS_ID and SHEETS_CREDENTIALS:
        lead_channels.append("sheets")
    if TWILIO_SID and WHATSAPP_TO:
        lead_channels.append("whatsapp")
    return {"status": "ok", "groq_configured": bool(GROQ_API_KEY), "lead_channels": lead_channels}


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
async def livekit_token(req: LiveKitTokenRequest):
    """Issue a browser join token. We pre-create the room with metadata
    {"tenant": "<client_id>"} so the agent (which reads room metadata) attributes
    the call to the right client — the exact mechanism a SIP dispatch rule will
    use for real phone numbers later."""
    from livekit import api
    room = req.room or f"call_{uuid.uuid4().hex[:8]}"
    identity = f"user_{uuid.uuid4().hex[:8]}"

    metadata = {}
    if req.client_id and tenants.get_tenant(req.client_id):
        metadata["tenant"] = req.client_id
    elif req.language in ("hi", "te", "en"):
        metadata["language"] = req.language

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
def login(req: LoginRequest):
    user = auth.authenticate(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    resp = JSONResponse(
        {"role": user["role"], "client_id": user["client_id"], "name": user["name"]}
    )
    subject = user["client_id"] if user["role"] == "client" else "admin"
    auth.set_session_cookie(resp, user["role"], subject)
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
        cfg = tenants.upsert_tenant(data)
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


@app.get("/api/client/calls")
def client_calls(client_id: str = Depends(auth.require_client)):
    return _calls_payload(client_id)


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
def dashboard_calls(_admin=Depends(auth.require_admin)):
    """Global cross-tenant view. Was unauthenticated, which exposed every
    client's transcripts and lead data to anyone who knew the URL."""
    active = call_store.get_active_calls()
    recent = call_store.list_calls(status="completed", limit=20)
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
