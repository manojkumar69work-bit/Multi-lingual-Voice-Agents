# Real Phone-Call Setup — LiveKit Cloud + Telnyx (Free Path)

Goal: turn your working browser agent into a **real phone number a customer can dial**,
for **₹0 out of pocket today**. No code changes needed — your `agent.py` already routes
real calls via SIP dispatch metadata (`tenants.py:get_tenant_from_metadata`).

> **Honest note on "free":** LiveKit Cloud (SIP bridge) and Telnyx's signup credit make
> this cost ₹0 to prove a *real dialable agent*. The trial number you get is usually a
> US/virtual number — perfect for demos and your video. A local **+91 number** that
> Indian callers dial cheaply needs KYC + DLT (Plivo/Exotel, small monthly cost) — that's
> the step you take **once a client says yes**, not before. See `COSTS.md` §4.

---

## The flow you're building

```
Caller dials your number
        │  (PSTN)
        ▼
   Telnyx  ──SIP──►  LiveKit Cloud SIP  ──►  Inbound Trunk + Dispatch Rule
                                                  │  creates room, sets
                                                  │  metadata {"tenant":"<id>"}
                                                  ▼
                                          agent.py worker (your Mac)
                                          auto-joins → Riya answers
```

---

## Step 1 — LiveKit Cloud (free, replaces the self-hosted SIP container)

1. Sign up at **https://cloud.livekit.io** (free dev tier — no card).
2. Create a project. Copy from **Settings → Keys**:
   - Project URL: `wss://<your-project>.livekit.cloud`
   - API Key + API Secret
   - Note the **SIP URI** under Settings → SIP (looks like `<project>.sip.livekit.cloud`).
3. Edit your `.env` — point the agent at Cloud instead of local Docker:
   ```
   LIVEKIT_URL=wss://<your-project>.livekit.cloud
   LIVEKIT_API_KEY=<cloud api key>
   LIVEKIT_API_SECRET=<cloud api secret>
   ```
4. Install the LiveKit CLI (gives the `lk` command):
   ```bash
   brew install livekit-cli
   lk cloud auth        # opens browser, links the CLI to your project
   ```

> You no longer need `docker compose up` for LiveKit — Cloud handles WebRTC + SIP.
> You still run the **TTS service** and the **agent worker** locally (Steps 4–5).

---

## Step 2 — Telnyx number + SIP connection (free signup credit)

1. Sign up at **https://telnyx.com** → verify email (you get signup credit).
2. **Buy a number:** Numbers → Search & Buy a Number → pick a Voice-capable number
   (US numbers are cheapest/instant; uses a few cents of credit).
3. **Create a SIP Connection** that forwards calls to LiveKit:
   - Voice → SIP Trunking / Connections → **Create → FQDN Connection**.
   - Connection type: **FQDN**. Add an FQDN entry = your LiveKit SIP host from Step 1
     (`<project>.sip.livekit.cloud`), port `5060`, transport `UDP` (or TCP/TLS if Cloud shows TLS).
   - Under **Inbound**, set codecs to include `PCMU`/`PCMA` (G.711) and `OPUS`.
4. **Assign your number** to that SIP Connection (Numbers → your number → set its
   "Connection / Voice Profile" to the FQDN connection above).

> Telnyx's exact menu names shift — the three things you must end up with are:
> a **number**, a **FQDN SIP connection pointing at the LiveKit SIP host**, and the
> **number assigned to that connection**.

---

## Step 3 — LiveKit inbound trunk + dispatch rule (routes the call to a client)

These two JSON files are already created for you in this repo. Edit the placeholders,
then run the `lk` commands.

**`sip-inbound-trunk.json`** — tells LiveKit which numbers to accept:
```bash
lk sip inbound create sip-inbound-trunk.json
# → prints a trunk id like ST_xxxxxxxx — copy it into sip-dispatch-rule.json
```

**`sip-dispatch-rule.json`** — creates a room per call and tags it with the client id so
Riya loads *that client's* prompt/voice. Set `"tenant"` to a real client id from
`tenants.json` (e.g. `mumbai-realty`). Then:
```bash
lk sip dispatch create sip-dispatch-rule.json
```

Because your worker uses **automatic dispatch** (no fixed `agent_name` in
`WorkerOptions`), it auto-joins each new SIP room — nothing else to configure.

---

## Step 4 — Start the TTS service (local)

```bash
source .venv/bin/activate
uvicorn tts_server:app --host 0.0.0.0 --port 8002
```

## Step 5 — Start the agent worker (local, connects to Cloud)

```bash
source .venv/bin/activate
python agent.py start
```
You should see it register with `wss://<your-project>.livekit.cloud`.

---

## Step 6 — Call it 📞

Dial your Telnyx number from any phone. Riya should answer in Hinglish, capture the lead,
and the call appears in the **client portal** (`/client`) with summary + recording —
exactly like your browser demo, but over a real phone line.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Call connects then drops silently | Codec mismatch — ensure G.711 (PCMU/PCMA) enabled on the Telnyx connection. |
| Riya never joins | Agent worker not running or pointing at local LiveKit — confirm `.env` `LIVEKIT_URL` is the **cloud wss URL** and `python agent.py start` shows it registered. |
| Wrong client/prompt answers | `"tenant"` in `sip-dispatch-rule.json` must match an id in `tenants.json`. |
| No audio one direction | NAT/firewall — Cloud handles this; if self-hosting later, open UDP 7882. |
| "unauthorized" from `lk` | Re-run `lk cloud auth`, confirm the right project is selected. |

---

## When you land the paying client (the paid step)

Swap the Telnyx US trial number for a **+91 Indian DID** on **Plivo** or **Exotel**
(needs KYC + DLT, ~₹300–600/mo) pointed at the same LiveKit SIP host. Everything else —
agent, dispatch rule, portals — stays identical. Price the plan to cover it (see
`SALES_PITCH.md`).
