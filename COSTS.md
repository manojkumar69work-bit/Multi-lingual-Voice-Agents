# Cost & Vendor Guide — Best vs Cheapest per Sector

> Goal: run the **whole product for ₹0** today, and know exactly what each piece
> costs to flip on once you have a paying client. Prices are approximate (mid‑2026)
> and **must be re‑checked** before you commit — providers change tiers often. INR
> figures assume ~₹84/USD.

Two columns matter:
- **Free now** — what we run on today, no card required.
- **Best when paid** — what to switch to for quality/scale, and the rough cost.

---

## 1. Speech‑to‑Text (STT) — caller's voice → text

| | Choice | Cost | Notes |
|---|---|---|---|
| **Free now** | **Groq Whisper‑large‑v3‑turbo** | Free tier (rate‑limited) | Already wired. Excellent Hindi/Hinglish. |
| Cheapest paid | Groq paid Whisper | ~$0.04 / hr audio | Same code, just a paid key. |
| Best accuracy | Deepgram Nova‑2 / Sarvam ASR | ~$0.0036–0.0043 / min (~₹0.30/min) | Streaming, lower latency than Whisper batch. |

**Recommendation:** stay on Groq Whisper free → Groq paid. Only move to Deepgram if
latency on long calls becomes a problem.

---

## 2. LLM — the conversation brain + lead extraction + summary

| | Choice | Cost | Notes |
|---|---|---|---|
| **Free now** | **Groq Llama‑3.3‑70B** | Free tier | Already wired. Fast, good Hinglish. |
| Cheapest paid | Groq Llama‑3.3‑70B paid | ~$0.59 in / $0.79 out per 1M tokens | A 5‑min call ≈ a few thousand tokens → **fractions of ₹1/call**. |
| Best quality | Claude Haiku 4.5 / Sonnet | Haiku ~$1/$5 per 1M; Sonnet higher | Switch the `LLM_MODEL` + base URL. Use for tricky objection‑handling. |

**Recommendation:** Groq free → Groq paid. LLM is the *cheapest* part of the stack —
don't optimize it, optimize telephony (below).

---

## 3. Text‑to‑Speech (TTS) — agent's voice

| | Choice | Cost | Notes |
|---|---|---|---|
| **Free now** | **Sarvam Bulbul v3** (free tier) → **Edge TTS** fallback | Free | Already wired. Sarvam = best Indic voices; Edge = unlimited free neural backup. |
| Cheapest paid | Sarvam paid | ~₹ per 10k chars (check tier) | Best price/quality for Hindi/Telugu. |
| Best quality | ElevenLabs (multilingual v2) | ~$0.06–0.30 / 1k chars | Most natural, but **most expensive** — a real cost driver at scale. |

**Recommendation:** Sarvam free → Sarvam paid. Keep Edge TTS as the zero‑cost fallback
(already coded). Avoid ElevenLabs unless a client pays a premium for it.

---

## 4. Telephony — the actual phone number + per‑minute (the real cost)

This is the **only piece that truly can't be free** and the biggest ongoing cost.
Today we use **browser WebRTC calls (free)**. For a real "customer dials a number":

| Provider | Number (DID) rental | Inbound / min | Notes |
|---|---|---|---|
| **Plivo** | ~$0.50–0.80/mo | ~$0.005–0.01/min | Good LiveKit SIP fit, developer‑friendly. **Recommended start.** |
| Twilio | ~$1/mo + India KYC | ~$0.007–0.014/min | Most docs/support; India needs regulatory paperwork. |
| Exotel / Knowlarity | ₹ monthly plan | bundled minutes | India‑native, easiest KYC/DLT, business‑oriented pricing. Best once you have paying clients in India. |

**Rough math:** a client doing 500 min/month ≈ **₹250–600/month** in telephony alone +
number rental. **Price your plans above this.** Wire it via the LiveKit SIP bridge in
`livekit.yaml` (already stubbed) once you buy a number.

**Recommendation:** browser calls now → Plivo for the first pilot → Exotel for India at
scale (KYC + DLT compliance handled).

---

## 5. WebRTC media server (LiveKit)

| | Choice | Cost |
|---|---|---|
| **Free now** | **Self‑hosted LiveKit OSS** (Docker, already running) | Free (just your server) |
| Managed | LiveKit Cloud | Free dev tier → usage‑based | Removes ops; pay per participant‑minute. |

**Recommendation:** self‑host on your own VPS (below). It's the open‑source core — no
license cost. Move to LiveKit Cloud only if you don't want to run servers.

---

## 6. Hosting / compute

| | Choice | Cost | Notes |
|---|---|---|---|
| **Free now** | Your Mac (local) | Free | Fine for dev + first demos. |
| Cheapest always‑free | **Oracle Cloud Always‑Free** (4 Arm cores / 24 GB) | ₹0 forever | Genuinely free, enough for LiveKit + agent + server. **Best free production host.** |
| Cheap & simple | **Hetzner CX22** (2 vCPU / 4 GB) | ~€4/mo (~₹370) | Rock‑solid, simple. |
| Easiest | Railway / Render / Fly.io | Free tier → ~$5–10/mo | Push‑to‑deploy; less control over UDP/SIP ports. |

**Recommendation:** Oracle Always‑Free for a real internet‑reachable deploy at ₹0;
upgrade to Hetzner when you outgrow it. Note: LiveKit needs UDP ports open (7882) — VPS
beats most PaaS for that.

---

## 7. Database & call storage

| | Choice | Cost | Notes |
|---|---|---|---|
| **Free now** | **SQLite** (`calls.db`, already used) | Free | Perfect up to thousands of calls. |
| Next step | Postgres (Supabase/Neon free tier) | ₹0 → usage | When you need concurrent writes / multiple servers. |

**Recommendation:** SQLite is fine for a long time. Migrate to Postgres only when you
run more than one agent server.

---

## 8. Call‑recording storage

| | Choice | Cost | Notes |
|---|---|---|---|
| **Free now** | **Local mixed WAV** in `recordings/` (already coded) | Free | No extra infra. Fine on a VPS disk. |
| Better fidelity | **LiveKit Egress** → object storage | Egress = free OSS; storage cheap | Separate, clean stereo recording. Needs an egress container. |
| Cheap object storage | **Cloudflare R2** | ~$0.015/GB‑mo, **$0 egress** | Best price for serving audio back. Or Backblaze B2 (~$0.006/GB). |

**Recommendation:** local WAV now → LiveKit Egress + Cloudflare R2 when you need clean
recordings at volume or off‑box storage. WAV is large; convert to MP3/Opus to cut
storage ~10× (add `ffmpeg` step).

---

## 9. Lead delivery (Sheets / WhatsApp)

| | Choice | Cost | Notes |
|---|---|---|---|
| **Free now** | **Google Sheets API** | Free | Already wired; just add a service account. |
| WhatsApp alerts | Twilio WhatsApp / Meta Cloud API | Per‑conversation (~₹0.3–0.8) | Already wired (per‑client number supported). Meta Cloud API has a free monthly tier. |

**Recommendation:** Sheets now (free) → add WhatsApp via Meta Cloud API (cheaper than
Twilio for India) when clients want instant alerts.

---

## Starter "first paying client" stack — monthly estimate

| Sector | Choice | ~Monthly (1 client, ~500 min) |
|---|---|---|
| STT | Groq paid | ~₹30 |
| LLM | Groq paid | ~₹20 |
| TTS | Sarvam paid | ~₹100–200 |
| Telephony + number | Plivo/Exotel | **~₹300–600** ← dominant cost |
| Hosting | Oracle free / Hetzner | ₹0 – ₹370 |
| Media (LiveKit) | self‑host | ₹0 |
| DB + recordings | SQLite + local/R2 | ₹0 – ₹50 |
| **Total** | | **~₹450 – ₹1,250 / client / month** |

**Pricing guidance:** charge a setup fee + a monthly retainer (e.g. ₹3,000–8,000/client)
that comfortably covers telephony + a margin. The voice‑AI parts (STT/LLM/TTS) are
cheap; **telephony minutes are what you must mark up.**

---

## What's still free vs needs money — quick reference

**Free today (already running):** browser calls, Groq STT+LLM, Sarvam/Edge TTS, LiveKit
OSS, SQLite, local recordings, both portals, auth.

**Needs money later (drop‑in, no rewrite):** a phone number + per‑minute (telephony),
optional cloud host, optional paid API tiers for scale, optional R2 storage. Each maps to
config/env changes only — the architecture already accounts for them.
