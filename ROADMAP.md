# Roadmap: what to fix, upgrade and build next

Written 2026-09-24 from two passes: a code audit of this repo (file:line references below)
and market research on voice-AI platforms, Indian-language models, telephony and compliance.
Vendor claims are marked as such; anything that couldn't be verified is flagged.

---

## 0. Fix now: the agent's LLM no longer exists

`agent.py:144` defaults to `llama-3.3-70b-versatile`, and `.env` does not override it.
Groq retired that model (and `llama-3.1-8b-instant`) on 2026-08-16
([Groq deprecations](https://console.groq.com/docs/deprecations)). Querying `/openai/v1/models`
with this project's key on 2026-09-24 confirms it is gone; `openai/gpt-oss-120b`,
`openai/gpt-oss-20b`, `whisper-large-v3` and `whisper-large-v3-turbo` are available.

Live replies, lead extraction (`LEAD_EXTRACT_MODEL = CHAT_MODEL`, agent.py:179) and summaries
all use it, so calls are almost certainly failing today.

- Smallest fix: `LLM_MODEL=openai/gpt-oss-120b` in `.env`, with low reasoning effort for voice.
- Better: a separate, cheaper model for extraction and summaries (`openai/gpt-oss-20b`), with JSON mode.
- Re-run `test_voice.py` and a few real Hinglish/Telugu calls: register and romanization may shift with a new model.

---

## 1. Bugs in existing features (confirmed by reading the code)

| # | Problem | Where | Effect |
|---|---|---|---|
| B1 | Lead freezes at the first name | agent.py:1132–1155, 1181 | Once a name exists the lead is submitted and never re-extracted. Budget, location and timeline given later are lost. Only 3 of 29 stored leads have a budget. |
| B2 | Empty leads counted as leads | agent.py:876, 1183 | `lead_extracted=1` even when every field is blank. **Display fixed** in this change (call_store counts only non-empty leads: 29 → 23), but the flag itself is still set. |
| B3 | Sessions survive password change / deactivation | auth.py:154–159 | A deleted or paused client's cookie keeps working for 7 days. A new client reusing the slug inherits the old cookie and old calls. |
| B4 | No login rate limit | server.py:290 | Brute force possible; each attempt runs 260k-round PBKDF2, so a flood pins the CPU. |
| B5 | "Add client" can overwrite an existing client | server.py:358, tenants.py:235 | Two agencies with the same name → the second silently replaces the first's prompt, WhatsApp and password. |
| B6 | `/api/livekit/token` needs no login and trusts any `client_id` | server.py:244 | Anyone can burn a client's minutes, fill their portal with junk calls and fire leads to their WhatsApp. |
| B7 | Agent audio overlaps itself in recordings | recorder.py:67–112 | Chunks are stamped by arrival time, not playback time. |
| B8 | Caller audio can be missing from recordings (likely on SIP) | agent.py:963 vs 1007 | Track handler attached after connect. |
| B9 | Stored XSS in the old dashboard / onclick handlers | old dashboard.html, client.html | **Fixed** — every page is rewritten with full escaping; `/dashboard` now redirects. |
| B10 | Delivery worker has no claim step | lead_delivery.py:176 | Running two server processes sends every WhatsApp/Sheet row twice. |
| B11 | Dev secrets on exposed ports | livekit.yaml:17, docker-compose.yml:14, start_services.sh:47 | `devkey: secret`, password-less Redis on 6379, TTS server open on 0.0.0.0. Plaintext client passwords are committed in tenants.json. |
| B13 | Formula injection into Google Sheets | lead_delivery.py:301 | `USER_ENTERED` evaluates a caller-supplied `=IMPORTXML(...)`. |
| B14 | Clients could see operator's delivery destinations | server.py:469 | **Mitigated in the UI** (client page no longer shows destination or error text); the API still returns them. |
| B15 | Calls stuck "active" forever after a crash | agent.py:1087 | Inflates "live" counts. **The UI now labels these "Stalled"**; a backend reaper is still needed. |
| — | TTS is not one stream per reply | agent.py:1052, tts_server.py:294 | `StreamAdapter` opens a new Sarvam WebSocket per sentence — contradicting the README and costing a handshake + ~650 ms silence gate per sentence. |
| — | Edge fallback repeats speech | agent.py:734 | If Sarvam fails mid-reply, Edge re-speaks the whole reply. |
| — | Whisper prompt biased to the wrong domain | agent.py:468 | Bias list is "AI automation, workflows…", not real-estate vocabulary. |
| — | No recording notice | agent.py:402–426 | See §4. |

---

## 2. Upgrades to the voice pipeline

### Latency (biggest wins first)
1. **Real streaming TTS**: implement `stream()` so LLM tokens feed one Sarvam WebSocket per reply; pre-open it at call start.
2. **Streaming STT**: Sarvam Saaras v3 realtime, `mode="codemix"` — published WER Hindi ~5–6%, Telugu ~18%, best in 13/15 Indian languages ([Voice-of-India, arXiv 2604.19151](https://arxiv.org/html/2604.19151v2)); <150 ms to first token; ₹30/hr ([Sarvam pricing](https://docs.sarvam.ai/api-reference-docs/pricing)). Groq Whisper is batch-only.
3. Reuse one `httpx.AsyncClient` per process (a new TLS connection per utterance today: agent.py:580, 682, 860, 903).
4. Pre-render the greeting per tenant; play it instantly.
5. Short cached filler ("ji…", "achha", "సరే అండి") only when the expected wait is long.
6. Cap reply length (`max_completion_tokens` ~100); extract every few turns, not every assistant turn.
7. Move SQLite writes and `recorder.finalize` off the audio loop.
8. **Measure it**: subscribe to `metrics_collected` and store end-of-utterance, STT, LLM TTFT and TTS TTFB per turn. Sarvam's target: p50 < 900 ms end-to-end ([Sarvam LiveKit guide](https://docs.sarvam.ai/api/integration/livekit-production-best-practices)).

### Model options (Indian languages)

| Piece | Keep / switch | Notes |
|---|---|---|
| STT | Switch live path to **Sarvam Saaras v3**; keep Groq Whisper for offline re-transcription | Deepgram Nova-3 has no Telugu code-switching ([Deepgram](https://deepgram.com/learn/nova-3-multilingual-major-wer-improvements-across-languages)) |
| LLM | `gpt-oss-120b` now; benchmark **Sarvam-30B/105B** (India-hosted, LiveKit plugin) with `bench_translate.py` | Groq has no confirmed India region; co-locating in Mumbai saved ~1 s end-to-end in LiveKit's test ([LiveKit](https://livekit.com/blog/building-performant-voice-agents-india)) |
| TTS | Keep **Bulbul v3** (won Sarvam's own 8 kHz telephony study — vendor data); add **Cartesia Sonic-3.6** or **Google Chirp 3 HD** as a real fallback instead of Edge | Use 8 kHz μ-law output on SIP |
| Speech-to-speech | Offer later as a per-tenant engine; test on your corpus | Gemini Live supports hi/te but caps sessions at 15 min ([Google](https://ai.google.dev/gemini-api/docs/live-api/capabilities)) |

### LiveKit features to adopt
- Audio turn detector `v1` (Hindi supported, **Telugu not**) and adaptive interruption handling — rejects ~51% of false barge-ins like "haan" ([LiveKit](https://livekit.com/blog/adaptive-interruption-handling)).
- `BVCTelephony` noise cancellation for SIP callers.
- Prebuilt **warm transfer** task ([docs](https://docs.livekit.io/telephony/features/transfers/warm/)).
- **Agent Simulations** — simulated Hinglish/Telugu callers as regression tests, free through Oct-2026 ([docs](https://docs.livekit.io/agents/start/testing/simulations/)).
- Region pinning to India for SIP and the `ap-south` agent region.

---

## 3. New features, ranked

| # | Feature | Impact | Effort | Why |
|---|---|---|---|---|
| 1 | Swap the retired LLM (§0) | Critical | S | Calls are broken |
| 2 | **AI + recording disclosure** in the tenant's language, stored as `notice_version` per call; "don't record / delete my data" intent | Very high | S | DPDP itemized-notice duties apply fully from May 2027 ([PIB](https://static.pib.gov.in/WriteReadData/specificdocs/documents/2025/nov/doc20251117695301.pdf)) |
| 3 | Fix lead freezing (B1): merge fields on every extraction, re-deliver on material change | Very high | S | Leads are currently mostly just a name |
| 4 | Streaming Sarvam STT + streaming TTS (§2) | Very high | S–M | Accuracy and ~0.5 s per turn |
| 5 | **Go live on PSTN**: Plivo (₹0.38/min, official LiveKit guide) or Exotel; confirm Edesy supports DLT/140-series before relying on it | Very high | M | No phone calls, no product ([Plivo](https://www.plivo.com/voice/pricing/in/)) |
| 6 | **Speed-to-lead dialer**: 99acres / MagicBricks / Housing.com / Meta lead webhooks trigger an outbound call within 30 s, with retries | Very high revenue | M | Sell.Do already sells "under 30 seconds" ([sell.do](https://www.sell.do/)) |
| 7 | **Outbound compliance gate**: DND scrub, consent record per lead, quiet hours, 140 vs service classification, tenant DLT attestation | Very high | M | TRAI disconnects/blacklists unregistered AI callers; Third Amendment finalized 2026-09-18 ([MediaNama](https://www.medianama.com/2026/09/223-trai-truecaller-140-1600-calls-spam-rules/)) |
| 8 | **Site-visit booking** (Google Calendar/Cal.com) + WhatsApp *utility* confirmation with Maps pin and 24 h / 2 h reminders | High revenue | M | Utility templates ₹0.115 vs marketing ₹0.86 ([MyOperator](https://myoperator.com/blog/whatsapp-business-api-pricing-india-2026)) |
| 9 | **Warm transfer** to a sales rep on hot lead / request / high budget | High | S–M | Table stakes at Retell, Vapi, ElevenLabs |
| 10 | **CRM connectors**: LeadSquared, Sell.Do, Zoho (webhook exists; add native mappings) | High | M | Indian real-estate teams live in these |
| 11 | **Post-call intelligence**: one JSON call for summary + sentiment + intent + lead score + next step + callback flag | High | S | Powers scoring, sorting and alerts in the new portal |
| 12 | **Simulation test suite** per tenant prompt change | High | S–M | Catches regressions like §0 before callers do |
| 13 | **Data lifecycle**: retention + auto-purge, PII redaction, access logs ≥1 year, recordings to India object storage, Postgres instead of tenants.json + local SQLite | Med–High | M–L | DPDP Rule 6; multi-host deploys |
| 14 | **WhatsApp voice calls** into the same agent via LiveKit Connectors (no DLT) | Medium | S–M | GA 2026-09-01, ~$0.003/min ([LiveKit](https://livekit.com/blog/introducing-livekit-connectors)) |
| 15 | Prompt **A/B tests**, **AI QA scoring** of every call, per-project **knowledge base** (price lists, RERA IDs) | Medium | M | Retell ships both; stops invented prices |

---

## 4. Compliance notes (India)

- **DPDP**: a caller who rings in and volunteers details can be processed for that enquiry (s.7(a)) — but that does not obviously cover AI analysis, re-marketing or sharing with third parties. Speak a notice ("this call is with an AI assistant and is recorded and analysed…"), keep retention per purpose, and delete vendor copies too.
- **TRAI**: promotional calls must come from registered 140-series senders; normal 10-digit numbers can't be used for telemarketing; robocall use must be declared to the operator. How a *non-BFSI* business should originate service callbacks is **unverified — ask the carrier in writing.**
- Calling-hours and consent-validity figures seen online come from vendor blogs and are **unverified** against the regulation text.

---

## 5. Portal features for the next UI pass (need backend support)

The redesign in this change surfaces everything the API already returns. These need small backend additions:

- Server-side pagination, date filters and full-text search (`/api/client/calls?q=&from=&to=&has_lead=`); portals currently load at most 200 calls (admin: 500).
- Per-day / per-hour aggregates endpoint, including a "calls outside office hours" view — the strongest sales argument.
- Lead status workflow for clients (new → contacted → site visit → won/lost) with notes: `PATCH /api/client/calls/{id}`.
- Romanized transcript stored per turn, so portals match the live view (stored lines are raw Devanagari/Telugu today).
- Per-turn timestamps, so the turn strip can show real time instead of word counts and audio can seek to a turn.
- `caller_phone` from SIP, enabling repeat-caller detection and click-to-call.
- A real `/api/health` (TTS, LiveKit, DB, queue depth, stalled calls) shown on the admin overview.
- Server-side CSV with formula escaping, covering all history.
