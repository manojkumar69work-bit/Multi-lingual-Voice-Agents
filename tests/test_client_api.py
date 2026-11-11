import csv
import io
import time
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

import auth
import call_store
import server
import tenants

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def portal():
    tenants.upsert_tenant({"name": "Clinic Co", "password": "pw1", "lead_fields": "name,symptom,notes"})
    tenants.upsert_tenant({"name": "Other Co", "password": "pw2"})
    c = TestClient(server.app)
    auth.clear_login_failures("ip:testclient"); auth.clear_login_failures("u:clinic-co")
    assert c.post("/api/auth/login", json={"username": "clinic-co", "password": "pw1"}).status_code == 200
    return c


def _call(sid, client="clinic-co", *, transcript=None, lead=None, status="completed", created=None, duration=60, summary=""):
    call_store.upsert_call(sid, client_id=client, status=status, transcript=transcript or [],
                           lead_data=lead or {}, duration_seconds=duration, summary=summary,
                           lead_extracted=bool(lead and any(v for k, v in lead.items() if k != "notes")))
    if created is not None:
        conn = call_store.get_conn()
        conn.execute("UPDATE calls SET created_at=?, updated_at=? WHERE session_id=?", (created, created, sid))
        conn.commit()


def test_search_matches_transcript_in_telugu(portal):
    _call("a", transcript=[{"role": "user", "content": "నాకు appointment కావాలి"}])
    _call("b", transcript=[{"role": "user", "content": "hello there"}])
    r = portal.get("/api/client/calls", params={"q": "appointment"})
    assert [c["session_id"] for c in r.json()["recent"]] == ["a"]
    r = portal.get("/api/client/calls", params={"q": "కావాలి"})
    assert [c["session_id"] for c in r.json()["recent"]] == ["a"]


def test_has_lead_excludes_empty_leads(portal):
    _call("a", lead={"name": "Arjun", "symptom": "fever"})
    _call("b", lead={"name": "", "symptom": "", "notes": "nothing"})
    _call("c")
    r = portal.get("/api/client/calls", params={"has_lead": 1})
    assert [c["session_id"] for c in r.json()["recent"]] == ["a"]
    r = portal.get("/api/client/calls", params={"has_lead": 0})
    assert sorted(c["session_id"] for c in r.json()["recent"]) == ["b", "c"]


def test_date_window_and_pagination(portal):
    now = time.time()
    for i in range(7):
        _call(f"s{i}", created=now - i * 86400)
    r = portal.get("/api/client/calls", params={"from": datetime.fromtimestamp(now - 2.5 * 86400, tz=timezone.utc).isoformat()})
    assert r.json()["total"] == 3
    r = portal.get("/api/client/calls", params={"per_page": 2, "page": 2})
    body = r.json()
    assert body["total"] == 7 and body["page"] == 2 and [c["session_id"] for c in body["recent"]] == ["s2", "s3"]


def test_other_clients_calls_are_invisible(portal):
    _call("mine")
    _call("theirs", client="other-co")
    ids = [c["session_id"] for c in portal.get("/api/client/calls").json()["recent"]]
    assert ids == ["mine"]


def test_patch_lead_status(portal):
    _call("a", lead={"name": "Arjun"})
    r = portal.patch("/api/client/calls/a", json={"lead_status": "contacted", "lead_notes": "called back"})
    assert r.status_code == 200 and r.json()["lead_status"] == "contacted" and r.json()["lead_notes"] == "called back"
    assert call_store.get_call("a")["lead_status"] == "contacted"
    assert portal.patch("/api/client/calls/a", json={"lead_status": "bogus"}).status_code == 422
    _call("theirs", client="other-co")
    assert portal.patch("/api/client/calls/theirs", json={"lead_status": "won"}).status_code == 404


def test_filter_by_lead_status(portal):
    _call("a", lead={"name": "A"}); _call("b", lead={"name": "B"})
    portal.patch("/api/client/calls/a", json={"lead_status": "won"})
    r = portal.get("/api/client/calls", params={"lead_status": "won"})
    assert [c["session_id"] for c in r.json()["recent"]] == ["a"]


def test_export_quotes_and_unicode(portal):
    _call("a", lead={"name": "=HYPERLINK(\"x\")", "symptom": "fever, \"high\""},
          transcript=[{"role": "user", "content": "నమస్కారం"}], summary='Said "hi", then left')
    r = portal.get("/api/client/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text.lstrip("\ufeff"))))
    assert rows[0][:6] == ["session_id", "started_at", "duration_seconds", "caller_phone", "lead_status", "lead_notes"]
    assert rows[0][6:9] == ["name", "symptom", "notes"]
    row = dict(zip(rows[0], rows[1]))
    assert row["session_id"] == "a" and row["name"] == "'=HYPERLINK(\"x\")" and row["symptom"] == 'fever, "high"'
    assert row["summary"] == 'Said "hi", then left'
    assert "నమస్కారం" in row["transcript"]


def test_daily_counts_after_hours(portal):
    late = datetime.now(IST).replace(hour=23, minute=0, second=0, microsecond=0) - timedelta(days=1)
    noon = late.replace(hour=12)
    _call("late", created=late.timestamp(), lead={"name": "A"}, duration=120)
    _call("noon", created=noon.timestamp(), duration=60)
    r = portal.get("/api/client/calls/daily", params={"days": 7})
    days = r.json()
    assert len(days) == 7
    d = [x for x in days if x["date"] == late.date().isoformat()][0]
    assert d["calls"] == 2 and d["leads"] == 1 and d["after_hours"] == 1 and d["minutes"] == 3.0
