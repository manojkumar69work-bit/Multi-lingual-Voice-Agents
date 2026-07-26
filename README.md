# Real Estate Voice Agent — Multi-Tenant SaaS (Hinglish)

Real-time AI voice agent for **real estate lead capture** over phone calls (WebRTC). Speaks professional **Hinglish**, understands Hindi/English mixed speech, and automatically extracts leads.

Built as a **sellable, multi-tenant product**: you (admin) onboard real-estate agencies as *clients*, each with its own prompt, language, agent name, and login. Each agency sees only its own calls, minutes, summaries, recordings, and leads.

Built for Indian real estate agents who want a 24/7 virtual assistant that answers calls, qualifies leads, and delivers structured data to Google Sheets and WhatsApp.

> **Cost & vendor guide:** see [`COSTS.md`](./COSTS.md) for the best/cheapest option per
> sector (STT, LLM, TTS, telephony, hosting, storage) and what's free now vs paid later.

---

## Portals

| Portal | URL | Who | What |
|--------|-----|-----|------|
| **Login** | `/login` | everyone | Single sign-in; routes by role |
| **Admin** | `/admin` | you | Create/configure clients (prompt, greeting, language, agent name, password, minute quota, WhatsApp); see per-client calls / active / minutes / leads; drill into any client's calls |
| **Client** | `/client` | each agency | Their calls + active calls + minutes; per-call **summary, lead, transcript, and audio playback**; CSV lead export |
| **Test caller** | `/` | you | Browser WebRTC call UI to test the agent |
| **Global dashboard** | `/dashboard` | internal | All calls across clients (legacy view) |

**Default credentials** (change in `.env`): admin `admin` / `admin123`.
Seed clients in `tenants.json`: `mumbai-realty` / `mumbai123`, `bangalore-properties` / `bangalore123`.

### Auth
Signed-cookie sessions (PyJWT, no new deps). Admin credentials come from `.env`
(`ADMIN_USER`/`ADMIN_PASSWORD`); each client logs in with its `id` slug + `password`.
Clients are **self-scoped** — they can never see another agency's data or recordings.

### Call summary & recording
Every completed call gets an **AI summary** (Groq) and a **local mixed-WAV recording**
(caller + agent audio, no extra infra — see `recorder.py`). Recordings live in
`recordings/` and are served access-checked via `/api/recordings/{session_id}`.

---

## Architecture

```
Caller (Browser or Phone)
        │
        ▼
  ┌─ LiveKit Server (WebRTC) ───────────────────────┐
  │  Port 7880 / Docker                    Port 7882 │
  │  Audio routing + VAD (Silero)  ←───────── UDP   │
  └──────────────────────┬──────────────────────────┘
                         │
              agent.py (LiveKit Worker)
         ┌───────────────┼───────────────┐
         ▼               ▼               ▼
   Groq Whisper    Groq Llama-3.3    TTS Service
   (STT) Hindi     (LLM) Hinglish    Port 8002
                           │          Sarvam → Edge
                           ▼
                    Lead Extraction
                    Google Sheets / WhatsApp
```

| Component | File | Port | Purpose |
|-----------|------|------|---------|
| Voice Agent | `agent.py` | — | Real-time STT → LLM → TTS pipeline (LiveKit worker) |
| HTTP Server | `server.py` | 8000 | Browser UI, LiveKit tokens, dashboard API |
| TTS Service | `tts_server.py` | 8002 | Sarvam Bulbul v3 with Edge TTS fallback |
| Frontend | `index.html` | 8000 | Phone-like WebRTC UI with Hinglish labels |
| Dashboard | `dashboard.html` | 8000 | Live call monitoring (internal) |
| LiveKit | `docker-compose.yml` | 7880 | WebRTC audio routing + SIP bridge |
| Redis | `docker-compose.yml` | 6379 | LiveKit session store |

---

## Provider Chain

| Component | Primary | Fallback | Cost |
|-----------|---------|----------|------|
| ASR (STT) | Groq Whisper-large-v3-turbo | — | Free tier |
| LLM | Groq Llama-3.3-70b-versatile | — | Free tier |
| TTS | Sarvam Bulbul v3 | Edge TTS (neural) | Sarvam free tier |
| VAD | Silero (bundled in AgentSession) | — | Free (local) |
| SIP Trunk | Edesy India DID (Mumbai) | — | Paid (~₹500/mo) |

---

## Features

### Current
- **WebRTC browser calls** — open `localhost:8000`, click to call, real-time audio
- **Hinglish AI agent** — Riya, a professional real estate consultant speaking Romanized Hindi with natural warmth
- **Hindi-optimized ASR** — Groq Whisper with Hindi language hint for accurate Hindi/English mixed speech
- **Low-latency TTS** — Sarvam Bulbul v3 with `StreamAdapter` + text pacing for sentence-level streaming; faster speech at `pace=1.2`
- **Smart interruption** — Agent stops speaking when caller talks (LiveKit AgentSession with Silero VAD, endpointing delay 0.4s)
- **Automatic lead extraction** — LLM extracts name, phone, property type, budget, location, timeline mid-call
- **Google Sheets delivery** — New leads appended automatically (optional)
- **WhatsApp alerts** — Lead details sent to owner via Twilio WhatsApp (optional)
- **Live dashboard** — View call history, transcripts, leads at `/dashboard`
- **Production-ready** — Clean .env, dead code removed, proper error handling

### Planned
- **Phone calls via SIP** — Mumbai DID number (Edesy) → LiveKit SIP bridge → agent
- **Multi-client admin panel** — Per-client prompts, sheet config, WhatsApp numbers
- **Call recording** — Store audio for quality review

---

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Docker (for LiveKit + Redis)
- API keys: [Groq](https://console.groq.com) + [Sarvam](https://www.sarvam.ai)

### 2. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
```

### 3. Start Services (order matters)

```bash
# Terminal 1: LiveKit + Redis (Docker)
docker compose up -d

# Terminal 2: TTS microservice
uvicorn tts_server:app --host 0.0.0.0 --port 8002

# Terminal 3: Voice agent
python agent.py start

# Terminal 4: HTTP server (browser UI + API)
python server.py
```

### 4. Test

Open **http://localhost:8000** in a browser → click the green call button.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GROQ_API_KEY` | Yes | — | Groq API key (STT + LLM) |
| `SARVAM_API_KEY` | Yes | — | Sarvam API key (TTS) |
| `ADMIN_USER` | No | `admin` | Admin portal username |
| `ADMIN_PASSWORD` | No | `admin123` | Admin portal password — **change in prod** |
| `SESSION_SECRET` | No | dev default | Secret for signing session cookies — **change in prod** |
| `COOKIE_SECURE` | No | `0` | Set `1` when serving over HTTPS |
| `RECORDINGS_DIR` | No | `./recordings` | Where mixed-WAV call recordings are written |
| `RECORDING_MAX_MINUTES` | No | `30` | Per-call recording memory cap |
| `LIVEKIT_URL` | No | `ws://localhost:7880` | LiveKit server URL |
| `LIVEKIT_API_KEY` | No | `devkey` | LiveKit API key |
| `LIVEKIT_API_SECRET` | No | `secret` | LiveKit API secret |
| `LLM_MODEL` | No | `llama-3.3-70b-versatile` | Groq model override |
| `GOOGLE_SHEETS_CREDENTIALS` | No | — | Service account JSON for Google Sheets |
| `GOOGLE_SHEET_ID` | No | — | Google Sheet ID for leads |
| `TWILIO_ACCOUNT_SID` | No | — | Twilio account for WhatsApp |
| `TWILIO_AUTH_TOKEN` | No | — | Twilio auth token |
| `TWILIO_WHATSAPP_FROM` | No | — | Twilio WhatsApp sender number |
| `WHATSAPP_TO` | No | — | Owner WhatsApp number for lead alerts |

---

## Project Structure

```
├── agent.py              LiveKit worker — STT/LLM/TTS pipeline + lead extraction + summary + recording
├── server.py             FastAPI HTTP server — auth, admin/client APIs, tokens, recordings, pages
├── auth.py               Signed-cookie session auth (admin + per-client)
├── recorder.py           Mixes caller + agent audio into one WAV per call (no extra infra)
├── tts_server.py         FastAPI TTS microservice — Sarvam → Edge orchestration
├── tts_engine.py         TTS engine — Sarvam API client + Edge TTS
├── call_store.py         SQLite store for calls, leads, summaries, recordings, per-client stats
├── tenants.py            Multi-tenant (per-client) config + CRUD persisted to tenants.json
├── index.html            LiveKit WebRTC phone-call UI (test caller)
├── login.html            Portal sign-in
├── admin.html            Admin portal — manage clients + per-client usage
├── client.html           Client portal — an agency's calls, minutes, summaries, audio, leads
├── dashboard.html        Internal global call dashboard
├── COSTS.md              Best/cheapest vendor guide per sector
├── recordings/           Mixed-WAV call recordings (git-ignored)
├── docker-compose.yml    LiveKit server + Redis
├── livekit.yaml          LiveKit server config
├── start_services.sh     One-shot start script
├── .env                  Environment variables (git-ignored)
├── .env.example          Environment template
├── requirements.txt      Python dependencies
├── static/               Legacy demo pages (optional)
└── logs/                 Runtime log output
```

---

## Lead Capture Flow

1. Agent greets caller and naturally asks qualifying questions over the conversation
2. LLM extracts structured data: name, phone, email, property type, budget, location, timeline
3. Once name + contact is collected, lead is auto-submitted
4. Data appended to **Google Sheets** (if configured)
5. WhatsApp alert sent to **owner** (if configured)

---

## SIP Trunk Setup (Planned)

For real inbound phone calls:

1. Get **Edesy India DID** (Mumbai number, ~₹500/mo)
2. Configure SIP trunk in `livekit.yaml`
3. Point Edesy SIP to LiveKit server IP
4. Agent auto-answers and processes the call

---

## License

MIT
