# Multi-lingual Voice Agent

**A real-time AI agent that answers the phone, holds a conversation in Telugu or mixed Hindi/English, and hands you a structured lead when the call ends.**

Multi-tenant from the ground up: each client organisation gets its own prompt, language, agent persona, minute quota, and login, and can never see another's calls or recordings. Retargeting it to a new domain — support, surveys, sales, lead capture — means swapping the prompt and the extraction schema, not writing code.

The shipped configuration is **real-estate lead capture in Hinglish**, which is what the seed tenants and default prompts implement.

`Python` · `LiveKit / WebRTC` · `Groq Whisper + Llama 3.3` · `Sarvam Bulbul v3` · `Docker`

<!-- TODO: this project is audio — a 30-second call recording or screen capture would
     demonstrate it far better than any README text. Consider embedding a demo clip. -->

---

## The hard part: a conversation that doesn't feel like a phone tree

Latency is the whole product. If the agent pauses for two seconds before replying, the caller talks over it and the illusion collapses. Four things buy that back:

- **One continuous TTS stream per reply** — the reply goes to Sarvam over a single WebSocket, so speech starts before the full reply is generated *and* prosody carries across sentence boundaries
- **Semantic turn-taking** — a local end-of-turn model on top of Silero VAD, so a caller drawing breath mid-sentence isn't treated as finished, and an "achha" isn't treated as an interruption
- **A Hindi language hint on ASR** — Whisper transcribes code-switched Hindi/English far better when told to expect it than when left to autodetect
- **A TTS fallback** — Sarvam Bulbul v3 primary, Edge TTS behind it, so a vendor outage degrades voice quality instead of ending the call

### Sounding human is mostly about what you feed the voice, not which voice

Four things mattered more than the model choice, and all four were things this
project was doing to itself:

- **Register.** Hindi calls are Roman Hinglish — `Aapka budget kitna hai?` — because that is how urban callers actually speak, and because Devanagari pulls the LLM toward formal newsreader Hindi. Telugu calls are Telugu script with English loanwords left in Latin (`మీ budget ఎంత అండి?`), the same convention. Both prompts anchor the register with a worked example exchange rather than rules alone, and [`romanize()`](agent.py) still normalizes any Indic script Whisper returns on the caller's side.

  Open trade-off: Sarvam's docs say romanized Indic input degrades pronunciation, since a `hi-IN` voice reads Latin text with the wrong phonemes. The A/B renders that settle it are `ab_hi_script_roman.wav` vs `ab_hi_script_native.wav` at `/demo/sarvam`. **If Roman does sound worse, the fix is a transliteration pass in front of the TTS call, not a change to what the LLM writes** — the register depends on it staying Roman.
- **Whole replies, not fragments.** Bulbul v3 infers emphasis and pauses across the entire input, so synthesizing sentence-by-sentence and concatenating the results threw that away and restarted pitch and energy at every full stop.
- **Pace 1.0.** The model is trained at 1.0; this ran at 1.4 for a long time, which no amount of voice quality rescues.
- **Trimming only the edges.** Sarvam prepends ~650 ms of silence and leaves ~300 ms at the end. That's dead air on every single turn, so it's gated out — while the pauses *between* sentences are held and replayed intact, because those are the prosody we wanted.

Compare all of these by ear at **`/demo/sarvam`** after running
`python generate_voice_samples.py` — before/after pairs for script, streaming,
pace, speaker and temperature.

**Lead extraction runs mid-call, not after.** The LLM pulls name, phone, property type, budget, location, and timeline as the conversation goes; once name plus contact exist, the lead auto-submits to Google Sheets and fires a WhatsApp alert — the caller doesn't have to reach the end for the lead to survive.

**Call recording without extra infrastructure.** Caller and agent audio arrive as separate tracks; [`recorder.py`](recorder.py) mixes them into a single WAV per call locally — no cloud recording service, no per-minute storage bill. Recordings are served access-checked, scoped to the owning tenant.

> **Cost & vendor guide:** see [`COSTS.md`](./COSTS.md) for the best/cheapest option per
> sector (STT, LLM, TTS, telephony, hosting, storage) and what's free now vs paid later.

---

## Portals

| Portal | URL | Who | What |
|--------|-----|-----|------|
| **Login** | `/login` | everyone | Single sign-in; routes by role |
| **Admin** | `/admin` | operator | Create/configure clients (prompt, greeting, language, agent name, password, minute quota, WhatsApp); see per-client calls / active / minutes / leads; drill into any client's calls |
| **Client** | `/client` | each org | Their calls + active calls + minutes; per-call **summary, lead, transcript, and audio playback**; CSV lead export |
| **Test caller** | `/` | operator | Browser WebRTC call UI to test the agent |
| **Global dashboard** | `/dashboard` | internal | All calls across clients (legacy view) |

> **Set your own credentials before running.** `ADMIN_USER` / `ADMIN_PASSWORD` / `SESSION_SECRET`
> come from `.env`, and every tenant in `tenants.json` needs its `password` replaced. The
> values committed to this repo are placeholders for local development only — never deploy them.

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
| TTS Service | `tts_server.py` | 8002 | Sarvam Bulbul v3 over WebSocket, Edge TTS fallback |
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
| Turn detection | LiveKit turn-detector `v1-mini` (hi/en) | Silero VAD alone (te) | Free (local CPU) |
| SIP Trunk | Edesy India DID (Mumbai) | — | Paid (~₹500/mo) |

---

## Features

### Current
- **WebRTC browser calls** — open `localhost:8000`, click to call, real-time audio
- **Hinglish AI agent** — Riya, a professional real estate consultant speaking natural spoken Hinglish (Hindi in Roman letters, freely mixed with English), with a Telugu persona in spoken Vaaduka Bhasha
- **Hindi-optimized ASR** — Groq Whisper with Hindi language hint for accurate Hindi/English mixed speech
- **Low-latency TTS** — one Sarvam WebSocket stream per reply (continuous prosody, raw PCM, no ffmpeg in the call path), with a leading/trailing silence gate
- **Smart interruption** — semantic end-of-turn detection over Silero VAD, with a backchannel guard so "haan"/"achha" doesn't cut the agent off mid-sentence
- **Voice A/B harness** — before/after samples for every voice change at `/demo/sarvam`
- **Automatic lead extraction** — LLM extracts name, phone, property type, budget, location, timeline mid-call
- **Google Sheets delivery** — New leads appended automatically (optional)
- **WhatsApp alerts** — Lead details sent to owner via Twilio WhatsApp (optional)
- **Live dashboard** — View call history, transcripts, leads at `/dashboard`
- **Multi-tenant admin panel** — Per-client prompts, language, persona, quota, WhatsApp config (`admin.html`, `tenants.py`)
- **Call recording** — Caller + agent mixed to one WAV per call, access-checked per tenant (`recorder.py`)
- **AI call summaries** — Groq-generated summary attached to every completed call

### Planned
- **Phone calls via SIP** — Mumbai DID number (Edesy) → LiveKit SIP bridge → agent
  (trunk config is scaffolded in `sip-inbound-trunk.json` / `sip-dispatch-rule.json`; not yet live)
- **Hosted deployment** — currently runs locally via Docker Compose

---

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Docker (for LiveKit + Redis)
- API keys: [Groq](https://console.groq.com) + [Sarvam](https://www.sarvam.ai)

### 2. Install

```bash
python3.12 -m venv .venv12          # 3.12; livekit-agents needs 3.10+
source .venv12/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
```

> A stale `.venv` on Python 3.9 may still be lying around from earlier work. It
> can serve the two HTTP processes but **cannot run `agent.py`** — its
> `livekit-agents` install fails to import. Use `.venv12` for all four.

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

Or `./start_services.sh` for the two HTTP processes (it uses `.venv12`; override
with `VENV=...`).

### 4. Check the voice

```bash
python test_voice.py                  # text reaching the voice + the transcript
python test_telugu.py                 # Telugu STT path
python generate_voice_samples.py      # render before/after samples → /demo/sarvam
```

### 5. Test

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
| `TTS_PACE` / `TTS_PACE_TE` | No | `1.0` / `0.95` | Speech rate (0.5–2.0). Bulbul v3 is trained at 1.0 |
| `TTS_TEMPERATURE` | No | `0.6` | Prosody variation (v3 only, 0.01–2.0) |
| `TTS_VOICE_HI` / `_TE` / `_EN` | No | `priya` / `neha` / `ishita` | Sarvam speaker per language |
| `TTS_SILENCE_RMS` | No | `150` | Silence-gate threshold; trims each reply's leading/trailing silence |
| `TTS_MIN_BUFFER` / `TTS_MAX_CHUNK` | No | `50` / `250` | Sarvam streaming buffer sizes |
| `TTS_SAMPLE_RATE` | No | `24000` | PCM rate; **must match** between agent and TTS service |
| `TTS_SPELL_NUMBERS` | No | `0` | Spell ordinary numbers as words (v3 reads digits better itself) |
| `TURN_DETECTION` | No | `1` | Semantic end-of-turn detection (local, hi/en only) |
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
├── tts_server.py         FastAPI TTS microservice — Sarvam WebSocket streaming + silence gate, Edge fallback
├── tts_engine.py         TTS engine — Sarvam API client + Edge TTS
├── call_store.py         SQLite store for calls, leads, summaries, recordings, per-client stats
├── tenants.py            Multi-tenant (per-client) config + CRUD persisted to tenants.json
├── test_voice.py         Self-check: script/number handling reaching the voice + transcript
├── test_telugu.py        Self-check: Telugu STT bias-prompt path
├── generate_voice_samples.py  Renders before/after voice samples for /demo/sarvam
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
