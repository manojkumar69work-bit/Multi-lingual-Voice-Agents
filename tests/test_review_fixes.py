"""Tests added from the whole-branch review: each reproduces a verified finding."""
import csv
import io
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import auth
import call_store
import server
import tenants

IST = timezone(timedelta(hours=5, minutes=30))


class _FakeRoomService:
    async def create_room(self, req):
        return None


class _FakeLiveKitAPI:
    def __init__(self, *a, **k):
        self.room = _FakeRoomService()

    async def aclose(self):
        return None


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from livekit import api
    monkeypatch.setattr(api, "LiveKitAPI", _FakeLiveKitAPI)
    for k in ("ip:testclient", "u:acme", "u:admin", "tok:testclient"):
        auth.clear_login_failures(k)


def _login(c, u, p):
    return c.post("/api/auth/login", json={"username": u, "password": p})


# 4. a paused client must not download recordings or look signed in
def test_recording_and_me_refused_after_pause(tmp_path, monkeypatch):
    tenants.upsert_tenant({"name": "Acme", "password": "pw"})
    monkeypatch.setattr(server, "RECORDINGS_DIR", str(tmp_path))
    (tmp_path / "a.wav").write_bytes(b"RIFF")
    call_store.upsert_call("a", client_id="acme", status="completed", recording_path="a.wav")
    c = TestClient(server.app)
    assert _login(c, "acme", "pw").status_code == 200
    assert c.get("/api/recordings/a").status_code == 200
    tenants.upsert_tenant({"id": "acme", "active": False})
    assert c.get("/api/recordings/a").status_code == 401
    assert c.get("/api/auth/me").status_code == 401


# 5. delete + recreate the same slug: old cookie dead, old calls not visible
def test_slug_reuse_does_not_leak(monkeypatch):
    tenants.upsert_tenant({"name": "Acme", "password": "pw"})
    call_store.upsert_call("old", client_id="acme", status="completed")
    c = TestClient(server.app)
    assert _login(c, "acme", "pw").status_code == 200
    tenants.delete_tenant("acme")
    tenants.upsert_tenant({"name": "Acme", "password": "pw"})
    assert c.get("/api/client/calls").status_code == 401
    c2 = TestClient(server.app)
    assert _login(c2, "acme", "pw").status_code == 200
    assert c2.get("/api/client/calls").json()["total"] == 0
    assert call_store.get_call("old")["client_id"] != "acme"


# 6. the username lockout keys on the slug, so punctuation variants share it
def test_rate_limit_keys_on_slug():
    tenants.upsert_tenant({"name": "Acme", "password": "pw"})
    c = TestClient(server.app)
    for u in ("acme", "Acme", "acme-", "acme!", "ACME."):
        assert _login(c, u, "bad").status_code == 401
    assert _login(c, "acme~", "pw").status_code == 429


# 3. concurrent CSV exports must each be complete
def test_concurrent_csv_exports_complete():
    tenants.upsert_tenant({"name": "Acme", "password": "pw", "lead_fields": "name,notes"})
    for i in range(40):
        call_store.upsert_call(f"s{i}", client_id="acme", status="completed", lead_data={"name": f"n{i}"})
    c = TestClient(server.app)
    assert _login(c, "acme", "pw").status_code == 200
    results = []

    def go():
        r = c.get("/api/client/export.csv")
        results.append((r.status_code, r.text.count("\n")))

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert all(code == 200 and lines == 41 for code, lines in results), results


# 12. export carries the transcript and notes, keeps Telugu, and opens in Excel (BOM)
def test_export_has_transcript_notes_and_bom():
    tenants.upsert_tenant({"name": "Acme", "password": "pw", "lead_fields": "name,notes"})
    call_store.upsert_call("a", client_id="acme", status="completed", lead_data={"name": "A"},
                           transcript=[{"role": "user", "content": "నమస్కారం"}, {"role": "assistant", "content": "Hi"}],
                           lead_notes="call back Monday")
    c = TestClient(server.app)
    _login(c, "acme", "pw")
    r = c.get("/api/client/export.csv")
    assert r.content.startswith("﻿".encode("utf-8"))
    rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    head = rows[0]
    assert "transcript" in head and "lead_notes" in head
    row = dict(zip(head, rows[1]))
    assert "నమస్కారం" in row["transcript"] and row["lead_notes"] == "call back Monday"


# 11. a bare date in the filter is an IST day
def test_bare_date_filter_is_ist_day():
    tenants.upsert_tenant({"name": "Acme", "password": "pw"})
    day = (datetime.now(IST) - timedelta(days=3)).replace(hour=1, minute=0, second=0, microsecond=0)
    early = day.timestamp()                       # 01:00 IST that day = 19:30 UTC the day before
    call_store.upsert_call("early", client_id="acme", status="completed")
    conn = call_store.get_conn()
    conn.execute("UPDATE calls SET created_at=?, updated_at=? WHERE session_id='early'", (early, early)); conn.commit()
    c = TestClient(server.app)
    _login(c, "acme", "pw")
    d = day.date().isoformat()
    assert c.get("/api/client/calls", params={"from": d, "to": d}).json()["total"] == 1


# 13. anonymous token issuance is rate-limited per IP, capped per client, and ignores room
def test_token_abuse_limits():
    t = tenants.upsert_tenant({"name": "Acme", "password": "pw"})
    c = TestClient(server.app)
    body = {"client_id": "acme", "call_key": t.call_key, "room": "someone-elses-room"}
    r = c.post("/api/livekit/token", json=body)
    assert r.status_code == 200 and r.json()["room"] != "someone-elses-room"
    for _ in range(server.TOKEN_MAX_PER_WINDOW - 1):
        assert c.post("/api/livekit/token", json=body).status_code == 200
    assert c.post("/api/livekit/token", json=body).status_code == 429


def test_token_refused_when_client_has_too_many_live_calls():
    t = tenants.upsert_tenant({"name": "Acme", "password": "pw"})
    for i in range(server.MAX_CONCURRENT_CALLS):
        call_store.upsert_call(f"live{i}", client_id="acme", status="active")
    c = TestClient(server.app)
    assert c.post("/api/livekit/token", json={"client_id": "acme", "call_key": t.call_key}).status_code == 429


# 9. dev LiveKit keys are refused in production
def test_check_config_flags_dev_livekit_keys(monkeypatch):
    monkeypatch.setattr(auth, "LIVEKIT_API_KEY", "devkey")
    monkeypatch.setattr(auth, "LIVEKIT_API_SECRET", "secret")
    assert any("LIVEKIT" in p for p in auth.check_config())


# 8. the json seed is not imported in production
def test_seed_import_skipped_in_production(tmp_path, monkeypatch):
    p = tmp_path / "tenants.json"
    p.write_text(json.dumps({"tenants": [{"id": "demo-co", "name": "Demo", "password": "pw"}]}))
    monkeypatch.setenv("APP_ENV", "production")
    assert tenants.import_json_if_empty(str(p)) == 0
    assert tenants.get_tenant("demo-co") is None and p.exists()


# 14. the landing page's demo link resolves to a real call page
def test_demo_call_redirects_to_configured_client(monkeypatch):
    tenants.upsert_tenant({"name": "Demo Co", "password": "pw"})
    monkeypatch.setattr(server, "DEMO_CLIENT_ID", "demo-co")
    r = TestClient(server.app).get("/demo/call", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/call/demo-co"


# minor re-graded: a JSON null from the extractor must not become the string "None"
def test_extracted_null_is_blank():
    import ast
    src = open("agent.py").read()
    ns = {}
    for n in ast.parse(src).body:
        if isinstance(n, ast.FunctionDef) and n.name == "_coerce_lead":
            exec(compile(ast.Module([n], []), "agent.py", "exec"), ns)
    assert ns["_coerce_lead"]({"name": None, "budget": 50}, ["name", "budget", "notes"]) == {"name": "", "budget": "50", "notes": ""}


# T8 reversed: a paused client's number is still theirs, so the call can be declined as them
def test_paused_client_number_still_resolves_to_them():
    tenants.upsert_tenant({"name": "Num Co", "password": "x", "phone_number": "+911234567890"})
    tenants.upsert_tenant({"id": "num-co", "active": False})
    t, _ = tenants.resolve_tenant(None, {"sip.trunkPhoneNumber": "+911234567890"})
    assert t.id == "num-co" and t.active is False
