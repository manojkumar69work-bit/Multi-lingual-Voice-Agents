# Agency platform v1 — design

Written 2026-11-11 from the brainstorm in this session.

## Intent

The repo today is a working voice agent with two portals that run on one laptop.
The operator wants to sell it as an agency service: they set up each client's
voice agent themselves, switch it live, and the client logs in to see every call,
transcript, recording and lead. "Real system" means the thing survives being
put on a server and handed to paying clients: config and data that do not live
in a JSON file, auth that cannot be bypassed, a link a client can put on their
website today, a phone number they can plug in when a telephony account exists,
and a one-command deploy.

## Goals

- Operator console (`/admin`): create and edit clients, see each client's health
  (calls, live, minutes, leads, delivery failures), copy the client's call link
  and embed snippet, pause or resume a client. Nothing else.
- Client portal (`/client`): calls with transcript, recording, summary, lead
  fields; lead status workflow with notes; search and date filters that work on
  the server; CSV export of all history.
- Client config and sessions stored in SQLite beside the calls, so an edit is
  live on the next call and a password change or pause logs the client out.
- Public call page per client (`/call/<slug>`) and an embeddable widget, keyed
  by a per-client call key so nobody can burn another client's minutes.
- Phone-ready: SIP calls resolve the tenant by called number and record the
  caller's number, so a Plivo or Exotel trunk is a config change, not code.
- One-command production deploy: Docker image, compose file with LiveKit,
  Redis and HTTPS, secrets from env, nightly backup of DB and recordings.
- The known bugs from `ROADMAP.md` §1 that a paying client would hit in week
  one: B1, B2, B3, B4, B5, B6, B10, B11, B13, B15.

## Non-goals (v1)

- Self-serve signup, billing, Razorpay.
- Multiple agents per client, agent templates gallery, agent versions,
  knowledge-base file upload. The single prompt + business-info model stays.
- Outbound campaigns and contact-list upload.
- Buying a phone number. The operator signs up with a carrier; the code only
  routes.
- Postgres. SQLite in WAL mode is enough for one server and tens of clients;
  the store module keeps every query in one place so a later swap is bounded.

## Anti-goals

- A client seeing any other client's data, even by guessing an ID.
- A change that forces the operator to restart a process to onboard a client.
- Losing a lead because a delivery provider was down.
- Shipping with `devkey: secret` or `admin123` reachable from the internet.

## Constraints

- Keep the existing code paths; add beside them (operator preference recorded
  in memory `additive-changes-only`). Prompts and the voice pipeline are not
  touched by this project.
- Python 3.12 in `.venv12`; FastAPI, SQLite, PyJWT, LiveKit agents 1.3.x.
- No new Python dependencies except `pytest` for tests.
- Portals stay single-file HTML plus `static/portal.js`; no build step.
- Sarvam for speech, as set up on 2026-11-11.

## Approaches considered

1. **Own stack, hardened** (chosen). Keep LiveKit + Groq + Sarvam, move tenants
   into SQLite, fix auth, add call page, SIP routing and deploy. Most code, but
   full control and the margin stays with the operator.
2. **Agency layer on Sarvam Voice Agents.** Sarvam hosts agents, numbers and
   campaigns at ₹3/min; this repo becomes portals and billing over their API.
   Far less code, but no Cartesia, their outages, their price floor, and the
   voice pipeline work in this repo is thrown away. Rejected for v1; can be
   revisited if telephony becomes the blocker.
3. **Postgres plus a proper migration tool now.** Right eventually, but adds a
   database service to run and a migration framework to learn before a single
   client pays. Rejected for v1; the store layer isolates the swap.

Within approach 1, web calls ship first because a +91 number needs a carrier
account and KYC only the operator can do.

## Decisions

- **Storage.** One SQLite file (`calls.db`): tables `calls`, `lead_deliveries`
  (exist), new `tenants` and `login_attempts`. `tenants.py` keeps its public API
  (`get_tenant`, `upsert_tenant`, `delete_tenant`, `list_tenants`,
  `get_tenant_from_metadata`, `get_tenant_for_number`) and swaps JSON for the
  table. `tenants.json`, if present and the table is empty, is imported once
  and renamed `tenants.json.imported`. The agent worker already reloads tenants
  at call start (`agent.py` entrypoint), so no restart is needed.
- **Sessions.** Each tenant row carries `session_version` (int). The client JWT
  embeds it; `require_client` rejects a token whose version differs. Password
  change, deactivation and deletion bump it.
- **Login rate limit.** Per username and per IP: after 5 failures in 15 minutes,
  `/api/auth/login` returns 429 for that key until the window passes. Kept in
  the `login_attempts` table so it survives restarts and multiple workers.
- **Call key.** Each tenant has `call_key` (32 hex chars, generated on create,
  rotatable from the admin). `/api/livekit/token` accepts either an admin or
  client session, or `{"client_id", "call_key"}`. The language-only browser test
  path stays but requires an admin session.
- **Call page.** `GET /call/{slug}` serves `call.html`: agent name, client name,
  one dial button, live transcript, built from `index.html`'s caller. The page
  fetches `/api/call/{slug}/config` (public: name, agent name, language,
  call_key) and is refused when the tenant is paused. `static/widget.js`
  injects a floating "Call us" button that opens `/call/{slug}` in a popup.
- **Lead lifecycle.** Extraction runs again every 4 assistant turns after the
  first submit; fields are merged (non-empty new values win); a re-delivery is
  queued only when a field other than `notes` changed. `lead_extracted` is set
  only when at least one non-notes field is non-empty.
- **Lead status.** `calls` gains `lead_status` (`new`, `contacted`, `visit`,
  `won`, `lost`) and `lead_notes`. `PATCH /api/client/calls/{id}` with either.
- **Calls listing.** `GET /api/client/calls?q=&from=&to=&has_lead=&status=&page=&per_page=`
  does the filtering in SQL; `q` matches transcript text, lead fields and
  summary. `GET /api/client/calls/daily?days=30` returns per-day counts
  (calls, leads, minutes, after-hours calls). `GET /api/client/export.csv`
  streams every call with lead fields; cells starting with `= + - @` are
  prefixed with `'`.
- **Delivery.** `claim_due` marks rows `status='sending'` in the same
  transaction it selects them, so two workers never send the same lead.
  Sheets rows get the same formula escaping as the CSV.
- **Stalled calls.** A lifespan task in `server.py` marks `active` calls with
  `updated_at` older than 2 hours as `status='stalled'` every 10 minutes.
- **Health.** `/api/health` returns `{tts, livekit, db, queue_pending,
  queue_dead, stalled, worker_seen_at}` and the admin shows it.
- **Phone.** `get_tenant_from_metadata` also reads participant attributes
  `sip.trunkPhoneNumber` (called number) and `sip.phoneNumber` (caller) when
  room metadata carries no tenant; `caller_phone` is written on the call row.
  `sip-dispatch-rule.json` drops its hard-coded tenant.
- **Secrets.** `livekit.yaml` becomes a template rendered from
  `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` at container start. `auth.check_config`
  already refuses dev defaults in production; the compose file sets
  `APP_ENV=production`.
- **Deploy.** `Dockerfile` (one image; `CMD` chosen by service name),
  `docker-compose.prod.yml` with services `server`, `tts`, `agent`, `livekit`,
  `redis`, `caddy`; `Caddyfile` with the domain from env; `scripts/backup.sh`
  copies `calls.db` (via `sqlite3 .backup`) and `recordings/` to a dated folder.

## Left to the builder

- Exact SQL for the filters, index choices.
- Layout of the call page and the health panel within the existing design
  system in `static/app.css`.
- Backoff values for the stalled-call reaper and the rate limiter beyond the
  numbers fixed above.
