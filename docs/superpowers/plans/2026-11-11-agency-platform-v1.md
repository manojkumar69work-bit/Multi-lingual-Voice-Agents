# Agency Platform v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the single-laptop voice agent into an operator-run service: clients in SQLite, auth that holds, a public call page per client, a client portal with lead workflow and server-side search, SIP-ready routing, and a one-command deploy.

**Architecture:** FastAPI `server.py` stays the single API and page server; `tenants.py` keeps its public API but stores rows in the existing SQLite file; `agent.py` is touched only where it resolves the tenant and extracts leads. Portals stay single-file HTML over `static/portal.js`. Deploy is one Docker image run as three services beside LiveKit, Redis and Caddy.

**Tech Stack:** Python 3.12 (`.venv12`), FastAPI, SQLite (WAL), PyJWT, LiveKit agents 1.3.x, pytest, Docker Compose, Caddy.

**Spec:** `docs/superpowers/specs/2026-11-11-agency-platform-v1-design.md`

## Global Constraints

- Add beside existing code; do not remove prompt or voice-pipeline code.
- No new runtime Python dependencies. `pytest` is the only dev addition (`requirements-dev.txt`).
- Tests run with `.venv12/bin/python -m pytest tests -q`. `tests/conftest.py` points `CALL_DB_PATH` and `TENANTS_CONFIG` at a temp dir before any project import.
- Every commit message is plain, imperative, no attribution trailer.
- Public API shapes named in a task's **Interfaces** block are what later tasks and the portals rely on; do not rename.
- Rate limit: 5 failed logins per username or per IP within 15 minutes → 429. Call key: 32 hex chars. Stalled: `active` and `updated_at` older than 2 h, checked every 10 min. Lead re-extraction: every 4 assistant turns after the first submit. CSV/Sheets cells starting with `=`, `+`, `-`, `@` get a leading `'`.

## Review Focus

1. A client slug typed in a different case or with spaces (`Apollo Clinic`) must resolve to the same tenant on login, on `/call/<slug>` and in room metadata. Pinned in Task 1 (`test_slug_normalised_on_lookup`).
2. A paused client's existing session and call page must stop working within one request, not after cookie expiry. Pinned in Task 2 (`test_pause_invalidates_session`) and Task 3 (`test_call_config_refused_when_paused`).
3. A transcript containing a quote, a comma or an Indic script must survive CSV export and search unbroken. Pinned in Task 5 (`test_export_quotes_and_unicode`).
4. Two delivery workers running at once must not send one lead twice. Pinned in Task 4 (`test_claim_due_is_exclusive`).
5. A SIP call whose called number matches no client must fall to the default tenant and still record `caller_phone`. Pinned in Task 8 (`test_sip_unknown_number_uses_default`).

---

### Task 1: Tenants in SQLite

**Files:**
- Create: `tests/conftest.py`, `tests/test_tenants.py`, `requirements-dev.txt`
- Modify: `tenants.py` (replace `load_tenants`, `save_tenants`, `upsert_tenant`, `delete_tenant`, `get_tenant`, `list_tenants`, `reload_tenants` bodies; add columns), `call_store.py:init_db`
- Modify: `server.py:admin_create_client` (duplicate slug → 409)

**Interfaces:**
- Consumes: `call_store.get_conn()`, `passwords.ensure_hashed`, `languages.normalize`.
- Produces: `TenantConfig` gains `call_key: str = ""`, `session_version: int = 1`, `created_at: float = 0`, `updated_at: float = 0`. `tenants.upsert_tenant(data: dict, *, create_only: bool = False) -> TenantConfig` raises `tenants.TenantExists` when `create_only` and the id exists. `tenants.bump_session(tenant_id: str) -> int` increments and returns `session_version`. `tenants.rotate_call_key(tenant_id: str) -> str`. `tenants.slugify` unchanged. `tenants.get_tenant(tenant_id)` lower-cases and slugifies its argument before lookup. `tenants.TENANT_COLUMNS: tuple[str, ...]` lists persisted fields. `tenants.import_json_if_empty(path: str) -> int` returns rows imported.

- [ ] **Step 1: Test harness**

`requirements-dev.txt` containing `pytest`. `tests/conftest.py`:

```python
import os, tempfile, pathlib
_tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["CALL_DB_PATH"] = str(_tmp / "calls.db")
os.environ["TENANTS_CONFIG"] = str(_tmp / "tenants.json")
os.environ["RECORDINGS_DIR"] = str(_tmp / "rec")
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin")
```

Run: `.venv12/bin/python -m pip install -r requirements-dev.txt && .venv12/bin/python -m pytest tests -q`
Expected: `no tests ran`

- [ ] **Step 2: Write the failing tests** in `tests/test_tenants.py`

```python
import json, pytest, tenants, call_store

def test_upsert_persists_to_sqlite():
    t = tenants.upsert_tenant({"name": "Apollo Clinic", "language": "ta", "password": "x"})
    row = call_store.get_conn().execute("SELECT id, language, call_key, session_version FROM tenants WHERE id=?", (t.id,)).fetchone()
    assert row["id"] == "apollo-clinic" and row["language"] == "ta"
    assert len(row["call_key"]) == 32 and row["session_version"] == 1

def test_slug_normalised_on_lookup():
    tenants.upsert_tenant({"name": "Mumbai Realty", "password": "x"})
    assert tenants.get_tenant("Mumbai Realty").id == "mumbai-realty"
    assert tenants.get_tenant("MUMBAI-REALTY").id == "mumbai-realty"

def test_create_only_refuses_duplicate():
    tenants.upsert_tenant({"name": "Dup Co", "password": "a"})
    with pytest.raises(tenants.TenantExists):
        tenants.upsert_tenant({"name": "Dup Co", "password": "b"}, create_only=True)

def test_password_change_bumps_session_version():
    t = tenants.upsert_tenant({"name": "Sess Co", "password": "a"})
    tenants.upsert_tenant({"id": t.id, "password": "b"})
    assert tenants.get_tenant(t.id).session_version == 2
    tenants.upsert_tenant({"id": t.id, "active": False})
    assert tenants.get_tenant(t.id).session_version == 3
    tenants.upsert_tenant({"id": t.id, "business_info": "hours 9-5"})
    assert tenants.get_tenant(t.id).session_version == 3

def test_import_json_once(tmp_path):
    p = tmp_path / "tenants.json"
    p.write_text(json.dumps({"default": {}, "tenants": [{"id": "legacy-co", "name": "Legacy", "password": "pw"}]}))
    assert tenants.import_json_if_empty(str(p)) == 1
    assert tenants.get_tenant("legacy-co").name == "Legacy"
    assert (tmp_path / "tenants.json.imported").exists()
    assert tenants.import_json_if_empty(str(p)) == 0

def test_delete_then_recreate_gets_new_key():
    a = tenants.upsert_tenant({"name": "Twice", "password": "a"})
    tenants.delete_tenant(a.id)
    b = tenants.upsert_tenant({"name": "Twice", "password": "a"})
    assert b.call_key != a.call_key
```

- [ ] **Step 3: Run tests, confirm failure**

Run: `.venv12/bin/python -m pytest tests/test_tenants.py -q`
Expected: FAIL, `no such table: tenants` / `AttributeError: TenantExists`

- [ ] **Step 4: Implement**

In `call_store.init_db` add:

```sql
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
```

In `tenants.py`: `_TENANTS` becomes a read-through cache refreshed by `reload_tenants()` from the table; `upsert_tenant` writes the row (`INSERT ... ON CONFLICT(id) DO UPDATE`) and bumps `session_version` when `password` (non-empty) or `active` changes; `call_key` is `secrets.token_hex(16)` on insert. `import_json_if_empty` runs from `load_tenants()` when the table has zero rows and `TENANTS_CONFIG` exists. `get_tenant` applies `slugify` to its argument. `save_tenants` stays as a JSON export (`tenants.export.json`) so nothing that calls it breaks. `agent.py`'s `tenants.reload_tenants()` at call start now hits SQLite, which is the hot-reload path.

In `server.py:admin_create_client` call `upsert_tenant(data, create_only=True)` and map `TenantExists` to `HTTPException(409, "A client with this id already exists")`.

- [ ] **Step 5: Run tests**

Run: `.venv12/bin/python -m pytest tests -q`
Expected: 6 passed

- [ ] **Step 6: Migrate the live file and commit**

Run: `.venv12/bin/python -c "import tenants; print(tenants.list_tenants())"` in the project dir. Expected: the six existing clients listed and `tenants.json.imported` present.

```bash
git add tests requirements-dev.txt tenants.py call_store.py server.py
git commit -m "store tenants in sqlite with call keys and session versions"
```

---

### Task 2: Auth that holds

**Files:**
- Create: `tests/test_auth.py`
- Modify: `auth.py` (`_issue_token`, `require_client`, `authenticate`, new `login_rate`), `server.py:login`, `server.py:livekit_token`, `server.py:LiveKitTokenRequest`, `call_store.init_db` (table `login_attempts`)

**Interfaces:**
- Consumes: `tenants.get_tenant`, `tenants.bump_session`, `TenantConfig.session_version`, `TenantConfig.call_key`.
- Produces: client JWT payload gains `sv` (session_version). `auth.record_login_failure(key: str) -> None`, `auth.login_blocked(key: str) -> bool`, `auth.clear_login_failures(key: str) -> None`, keys are `u:<username>` and `ip:<addr>`. `LiveKitTokenRequest` gains `call_key: str | None`. `/api/livekit/token` returns 401 unless (a) admin session, (b) client session for that `client_id`, or (c) `client_id` + matching `call_key` for an active tenant.

- [ ] **Step 1: Failing tests** in `tests/test_auth.py` using `fastapi.testclient.TestClient(server.app)`

```python
def _login(c, u, p): return c.post("/api/auth/login", json={"username": u, "password": p})

def test_pause_invalidates_session(client_tenant):   # fixture creates tenant "sess-co"/"pw1"
    c = TestClient(server.app); assert _login(c, "sess-co", "pw1").status_code == 200
    assert c.get("/api/client/stats").status_code == 200
    tenants.upsert_tenant({"id": "sess-co", "active": False})
    assert c.get("/api/client/stats").status_code == 401

def test_password_change_invalidates_session(client_tenant):
    c = TestClient(server.app); _login(c, "sess-co", "pw1")
    tenants.upsert_tenant({"id": "sess-co", "password": "pw2"})
    assert c.get("/api/client/calls").status_code == 401

def test_login_rate_limit():
    c = TestClient(server.app)
    for _ in range(5): assert _login(c, "nobody", "bad").status_code == 401
    assert _login(c, "nobody", "bad").status_code == 429
    assert _login(c, "admin", "test-admin").status_code == 429   # same IP is blocked too

def test_token_requires_key_or_session(client_tenant):
    c = TestClient(server.app)
    assert c.post("/api/livekit/token", json={"client_id": "sess-co"}).status_code == 401
    assert c.post("/api/livekit/token", json={"client_id": "sess-co", "call_key": "wrong"}).status_code == 401
    key = tenants.get_tenant("sess-co").call_key
    r = c.post("/api/livekit/token", json={"client_id": "sess-co", "call_key": key})
    assert r.status_code == 200 and r.json()["metadata"] == {"tenant": "sess-co"}

def test_language_test_call_needs_admin():
    c = TestClient(server.app)
    assert c.post("/api/livekit/token", json={"language": "ta"}).status_code == 401
    _login(c, "admin", "test-admin")
    assert c.post("/api/livekit/token", json={"language": "ta"}).status_code == 200
```

The token tests monkeypatch `server.api.LiveKitAPI` so no LiveKit server is needed; `LIVEKIT_API_KEY/SECRET` default to `devkey/secret` in the test env.

- [ ] **Step 2: Run, confirm failure** — `pytest tests/test_auth.py -q`, expected 5 failures.

- [ ] **Step 3: Implement**

`auth._issue_token(role, subject, sv: int = 0)` adds `sv` for clients. `require_client` loads the tenant and raises 401 when `not t or not t.active or data.get("sv") != t.session_version`. `login_attempts(key TEXT, ts REAL)` table; `login_blocked` counts rows with `ts > now-900` for the key and returns `count >= 5`; `record_login_failure` inserts; `clear_login_failures` deletes. `server.login` checks both keys first (429), records on failure, clears on success; the IP comes from `request.client.host` with `X-Forwarded-For` honoured when `TRUST_PROXY=1`. `livekit_token` takes `request: Request` and `user=Depends(auth.current_user)` and applies rule (a)/(b)/(c).

- [ ] **Step 4: Run tests** — `pytest tests -q`, expected all pass.

- [ ] **Step 5: Commit**

```bash
git add auth.py server.py call_store.py tests/test_auth.py
git commit -m "invalidate client sessions on pause or password change, rate-limit login, key the token endpoint"
```

---

### Task 3: Public call page and widget

**Files:**
- Create: `call.html`, `static/widget.js`, `tests/test_call_page.py`
- Modify: `server.py` (routes `GET /call/{slug}`, `GET /api/call/{slug}/config`, `POST /api/admin/clients/{client_id}/rotate-key`), `admin.html` (share link + snippet + rotate button in the client dialog), `index.html` (send the admin session; no change to the language picker)

**Interfaces:**
- Consumes: `tenants.get_tenant`, `tenants.rotate_call_key`, `/api/livekit/token` with `call_key` (Task 2).
- Produces: `GET /api/call/{slug}/config -> {"client_id","name","agent_name","language","call_key"}`; 404 when unknown or paused. `GET /call/{slug}` serves `call.html` (same 404 rule). `static/widget.js` reads `data-client` from its own `<script>` tag and renders a fixed bottom-right button that opens `/call/<slug>` in a 420×720 popup.

- [ ] **Step 1: Failing tests**

```python
def test_call_config_public(client_tenant):
    r = TestClient(server.app).get("/api/call/sess-co/config")
    assert r.status_code == 200 and r.json()["agent_name"] and len(r.json()["call_key"]) == 32

def test_call_config_refused_when_paused(client_tenant):
    tenants.upsert_tenant({"id": "sess-co", "active": False})
    assert TestClient(server.app).get("/api/call/sess-co/config").status_code == 404
    assert TestClient(server.app).get("/call/sess-co").status_code == 404

def test_rotate_key_changes_config(client_tenant, admin_client):
    old = admin_client.get("/api/call/sess-co/config").json()["call_key"]
    assert admin_client.post("/api/admin/clients/sess-co/rotate-key").status_code == 200
    assert admin_client.get("/api/call/sess-co/config").json()["call_key"] != old
```

- [ ] **Step 2: Run, confirm failure.**

- [ ] **Step 3: Implement** `call.html` from `index.html`: drop the client selector and language picker, read the slug from the path, call the config endpoint, then `/api/livekit/token` with `{client_id, call_key}`. Keep the live transcript and the dial/mute/hang-up controls. Admin dialog: a read-only field with `https://<host>/call/<slug>`, a copy button, the snippet `<script src="https://<host>/static/widget.js" data-client="<slug>"></script>`, and "Rotate key".

- [ ] **Step 4: Run tests, then open `http://localhost:8000/call/apollo-clinic-chennai` and place a call.** Expected: Tamil greeting, transcript scrolls.

- [ ] **Step 5: Commit** — `git commit -m "add a public call page and embed widget per client"`

---

### Task 4: Leads that survive the call

**Files:**
- Create: `tests/test_leads.py`
- Modify: `agent.py` (`extract_lead` unchanged; new `merge_lead(old: dict, new: dict) -> tuple[dict, bool]`, `lead_has_content(lead: dict) -> bool`; the mid-call loop at `on_conversation_item` and the end-of-call path), `lead_delivery.py` (`claim_due`, `_deliver_sheets_blocking`, new `escape_cell(value: str) -> str`), `server.py:lifespan` (stalled reaper), `call_store.py` (new `mark_stalled(older_than_seconds: float) -> int`)

**Interfaces:**
- Produces: `agent.merge_lead(old, new)` returns merged dict and `changed` (True when any non-`notes` field gained or changed a non-empty value). `agent.lead_has_content(lead)` True when any non-`notes` field is non-empty. `lead_delivery.escape_cell(v)` prefixes `'` when `v` starts with `=`, `+`, `-`, `@`. `call_store.mark_stalled(older_than_seconds)` sets `status='stalled'` and returns the count. `lead_delivery.claim_due` sets `status='sending'` on the rows it returns, in one transaction.

- [ ] **Step 1: Failing tests** (pure functions from `agent.py` loaded the way `test_voice.py` does, via AST, so livekit is not imported)

```python
def test_merge_lead_keeps_old_and_adds_new():
    merged, changed = merge_lead({"name": "Arjun", "budget": ""}, {"name": "", "budget": "80 lakh"})
    assert merged == {"name": "Arjun", "budget": "80 lakh"} and changed

def test_merge_lead_notes_only_is_not_a_change():
    _, changed = merge_lead({"name": "A", "notes": ""}, {"name": "A", "notes": "called twice"})
    assert not changed

def test_lead_has_content():
    assert not lead_has_content({"name": "", "notes": "x"}) and lead_has_content({"name": "A"})

def test_escape_cell():
    assert escape_cell("=IMPORTXML(1)") == "'=IMPORTXML(1)" and escape_cell("Arjun") == "Arjun"

def test_claim_due_is_exclusive(queued_rows):     # fixture inserts 3 pending rows
    a = lead_delivery.claim_due(10); b = lead_delivery.claim_due(10)
    assert len(a) == 3 and b == []
    assert {r["status"] for r in lead_delivery.list_deliveries()} == {"sending"}

def test_mark_stalled():
    call_store.upsert_call("s1", status="active"); call_store.get_conn().execute("UPDATE calls SET updated_at=updated_at-10000 WHERE session_id='s1'").connection.commit()
    assert call_store.mark_stalled(7200) == 1 and call_store.get_call("s1")["status"] == "stalled"
```

- [ ] **Step 2: Run, confirm failure.**

- [ ] **Step 3: Implement.** In the mid-call loop: keep a `lead_data` dict across the call; after the first submit, run extraction again every 4 assistant turns, `merge_lead`, write `lead_data` to the store, and `submit_lead` again only when `changed`. Set `lead_extracted=lead_has_content(lead_data)`. End-of-call path does one final merge and the same conditional re-submit. `claim_due`: `BEGIN IMMEDIATE`, select ids, `UPDATE ... SET status='sending'`, commit. `deliver` must treat `sending` like `pending` for retry bookkeeping. Reaper: `asyncio` task in `lifespan` calling `call_store.mark_stalled(7200)` every 600 s. Sheets: map `escape_cell` over the row before `append_row`.

- [ ] **Step 4: Run tests** — all pass. Also run `test_voice.py` and `test_telugu.py`; the pre-existing chat-mode assertion in `test_voice.py` is known and out of scope.

- [ ] **Step 5: Commit** — `git commit -m "merge lead fields across the call, claim deliveries exclusively, reap stalled calls"`

---

### Task 5: Client API: status, search, aggregates, export

**Files:**
- Create: `tests/test_client_api.py`
- Modify: `call_store.py` (`init_db` columns `lead_status TEXT DEFAULT 'new'`, `lead_notes TEXT DEFAULT ''`; new `search_calls(...)`, `daily_stats(client_id, days)`, `update_lead_status(session_id, client_id, status, notes)`), `server.py` (replace `client_calls` body; add `PATCH /api/client/calls/{session_id}`, `GET /api/client/calls/daily`, `GET /api/client/export.csv`)

**Interfaces:**
- Produces:
  - `call_store.search_calls(client_id: str, *, q: str = "", date_from: float | None = None, date_to: float | None = None, has_lead: bool | None = None, status: str | None = None, lead_status: str | None = None, page: int = 1, per_page: int = 50) -> dict` → `{"items": [...], "total": int, "page": int, "per_page": int}`; `q` is matched with `LIKE` against `transcript`, `lead_data`, `summary`, `caller_phone`.
  - `call_store.daily_stats(client_id: str, days: int = 30) -> list[dict]` → one row per day `{date, calls, leads, minutes, after_hours}`; after-hours is `created_at` local hour (`Asia/Kolkata`) outside 09–20.
  - `call_store.update_lead_status(session_id, client_id, status: str | None, notes: str | None) -> bool`; `status` must be one of `new contacted visit won lost`.
  - `GET /api/client/calls` keeps the keys `active`, `recent`, `total_active`, `total_completed` and adds `total`, `page`, `per_page`; query params as in the spec.
  - `PATCH /api/client/calls/{session_id}` body `{"lead_status"?, "lead_notes"?}` → the updated call; 404 when not that client's.
  - `GET /api/client/export.csv` → `text/csv`, header `session_id,started_at,duration_seconds,caller_phone,lead_status,<lead fields in tenant order>,summary,recording`.

- [ ] **Step 1: Failing tests** covering: search by a Telugu word in the transcript; `has_lead=1` excludes empty leads; date window; pagination `total`; PATCH with bad status → 422; PATCH on another client's call → 404; `test_export_quotes_and_unicode` (transcript with `"`, `,` and `నమస్కారం`; a lead field `=HYPERLINK(...)` comes back as `'=HYPERLINK(...)`); daily has `days` rows with `after_hours` counted for a 23:00 IST call.

- [ ] **Step 2: Run, confirm failure.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run tests.**

- [ ] **Step 5: Commit** — `git commit -m "server-side search, lead status, daily stats and csv export for the client portal"`

---

### Task 6: Client portal wiring

**Files:**
- Modify: `client.html` (calls view uses the server filters; lead status select and notes in the detail pane; "Export CSV" hits `/api/client/export.csv`; chart reads `/api/client/calls/daily`), `static/portal.js` (helper `P.leadStatusHTML(status)`)

**Interfaces:**
- Consumes: Task 5 endpoints exactly as named.

- [ ] **Step 1:** Search box debounced 300 ms → `q`; filter segment → `has_lead`/`status`; date pickers → `from`/`to`; pager under the list. Lead status `<select>` + notes `<textarea>` with Save → PATCH; row badge shows the status. Chart from `daily` with the after-hours series.
- [ ] **Step 2: Manual check.** Log in as `apollo-clinic-chennai` / `apollo123`, place a call from `/call/apollo-clinic-chennai`, confirm it appears, set status `contacted`, reload, status persists, export opens as CSV.
- [ ] **Step 3: Commit** — `git commit -m "client portal: server-side search, lead status workflow, csv export"`

---

### Task 7: Operator console health and pause

**Files:**
- Create: `tests/test_health.py`
- Modify: `server.py:health` (real checks), `admin.html` (health strip on the overview, pause/resume switch per row, share link column)

**Interfaces:**
- Produces: `GET /api/health -> {"ok": bool, "tts": "ok"|"down", "livekit": "ok"|"down", "db": "ok"|"down", "queue_pending": int, "queue_dead": int, "stalled": int, "worker_seen_at": iso|null}`; `ok` is false when any of `tts`, `livekit`, `db` is down. `worker_seen_at` comes from a `heartbeat` table row the agent worker writes every 30 s in `prewarm`/a background task (`call_store.heartbeat(name: str)`, `call_store.last_heartbeat(name) -> float | None`).
- `PUT /api/admin/clients/{id}` with `{"active": false}` already exists; the UI gets a switch.

- [ ] **Step 1: Failing tests:** `test_health_reports_down_tts` (monkeypatch the TTS URL to a closed port → `tts == "down"`, `ok == False`, status 503); `test_health_counts_queue_and_stalled`.
- [ ] **Step 2–4:** implement, run, pass.
- [ ] **Step 5: Commit** — `git commit -m "real health endpoint and operator health strip"`

---

### Task 8: Phone-ready routing

**Files:**
- Create: `tests/test_sip_routing.py`
- Modify: `tenants.py:get_tenant_from_metadata` (new optional `attributes: dict | None`), `agent.py` entrypoint (pass the first remote participant's attributes; write `caller_phone`), `sip-dispatch-rule.json` (remove `metadata`), `PHONE_SETUP.md`

**Interfaces:**
- Produces: `tenants.resolve_tenant(metadata, attributes: dict | None = None) -> tuple[TenantConfig, str]` returning the tenant and the caller number (`""` when not SIP). Attributes read: `sip.trunkPhoneNumber` (called), `sip.phoneNumber` (caller). Order: metadata tenant → called-number match → default.

- [ ] **Step 1: Failing tests**

```python
def test_sip_called_number_routes(client_with_number):   # tenant "num-co" phone_number "+911234567890"
    t, caller = tenants.resolve_tenant(None, {"sip.trunkPhoneNumber": "+911234567890", "sip.phoneNumber": "+919999999999"})
    assert t.id == "num-co" and caller == "+919999999999"

def test_sip_unknown_number_uses_default():
    t, caller = tenants.resolve_tenant(None, {"sip.trunkPhoneNumber": "+910000000000", "sip.phoneNumber": "+918888888888"})
    assert t.id == "default" and caller == "+918888888888"

def test_metadata_wins_over_sip(client_with_number):
    t, _ = tenants.resolve_tenant('{"tenant":"other-co"}', {"sip.trunkPhoneNumber": "+911234567890"})
    assert t.id == "other-co"
```

- [ ] **Step 2–4:** implement, run, pass. In `agent.py` the entrypoint uses `resolve_tenant` and passes `caller_phone` to the first `call_store.upsert_call`.
- [ ] **Step 5:** `PHONE_SETUP.md`: the two JSON files, the `lk sip` commands, and the per-client `phone_number` field as the only thing to fill.
- [ ] **Step 6: Commit** — `git commit -m "route sip calls by called number and record the caller"`

---

### Task 9: One-command deploy

**Files:**
- Create: `Dockerfile`, `docker-compose.prod.yml`, `Caddyfile`, `livekit.yaml.template`, `scripts/entrypoint.sh`, `scripts/backup.sh`, `.env.production.example`, `DEPLOY.md`
- Modify: `README.md` (deploy section pointing at `DEPLOY.md`), `.dockerignore`

**Interfaces:**
- `scripts/entrypoint.sh <server|tts|agent>` runs `uvicorn server:app --host 0.0.0.0 --port 8000`, `uvicorn tts_server:app --port 8002`, or `python agent.py start`; renders `livekit.yaml` from the template using `envsubst` when invoked as `livekit`.
- Compose env: `DOMAIN`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `SESSION_SECRET`, `ADMIN_PASSWORD_HASH`, `SARVAM_API_KEY`, `GROQ_API_KEY`, `APP_ENV=production`, `COOKIE_SECURE=1`, `TRUST_PROXY=1`. Volumes: `./data/calls.db`, `./data/recordings`.

- [ ] **Step 1:** Write the files. Caddy terminates TLS for `DOMAIN`, proxies `/` to `server:8000`, and `/rtc`, `/twirp` to `livekit:7880`; LiveKit exposes 7881/tcp and 50000–50100/udp with `use_external_ip: true`.
- [ ] **Step 2: Verify locally** with Docker running: `docker compose -f docker-compose.prod.yml config` prints without error; `docker build -t voice-agent .` succeeds; `docker compose -f docker-compose.prod.yml up -d` then `curl -k https://localhost/api/health` returns `"db":"ok"`. If Docker is not available on the machine, state that in the commit and leave the smoke test to the first server.
- [ ] **Step 3:** `scripts/backup.sh` uses `sqlite3 data/calls.db ".backup backups/<date>/calls.db"` and `rsync -a data/recordings backups/<date>/`; a `cron` line in `DEPLOY.md`.
- [ ] **Step 4: Commit** — `git commit -m "docker image, production compose with livekit, redis and caddy, backups"`

---

## Self-review notes

- Spec coverage: storage (T1), sessions and rate limit and call key (T2), call page and widget (T3), lead lifecycle, delivery claim, Sheets escaping, stalled reaper (T4), lead status, listing, daily, CSV (T5, T6), health (T7), phone (T8), secrets and deploy (T9). Bugs: B1/B2 T4, B3/B4/B6 T2, B5 T1, B10/B13/B15 T4, B11 T9.
- Names reused across tasks: `tenants.get_tenant`, `TenantConfig.call_key`, `TenantConfig.session_version`, `tenants.resolve_tenant`, `call_store.search_calls`, `call_store.mark_stalled`, `lead_delivery.escape_cell`, `/api/call/{slug}/config`, `/api/livekit/token` with `call_key`.
- Not in any task on purpose: Postgres, billing, multiple agents, campaigns.
