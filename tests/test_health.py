import json
import time

import pytest
from fastapi.testclient import TestClient

import call_store
import lead_delivery
import server


@pytest.fixture
def down_services(monkeypatch):
    # Closed ports: nothing listens on 1 in a test environment.
    monkeypatch.setattr(server, "LOCAL_TTS_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(server, "LIVEKIT_URL", "ws://127.0.0.1:1")


def test_health_reports_down_tts(down_services):
    r = TestClient(server.app).get("/api/health")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False and body["tts"] == "down" and body["livekit"] == "down" and body["db"] == "ok"


def test_health_counts_queue_and_stalled(down_services):
    conn = call_store.get_conn()
    now = time.time()
    for st in ("pending", "sending", "dead", "delivered"):
        conn.execute(
            "INSERT INTO lead_deliveries (session_id, client_id, channel, destination, payload, status, attempts, next_attempt_at, created_at, updated_at) "
            "VALUES ('s', 'c', 'webhook', 'u', ?, ?, 0, ?, ?, ?)", (json.dumps({}), st, now, now, now))
    conn.commit()
    call_store.upsert_call("x", status="stalled")
    call_store.upsert_call("y", status="stalled")
    body = TestClient(server.app).get("/api/health").json()
    assert body["queue_pending"] == 2 and body["queue_dead"] == 1 and body["stalled"] == 2


def test_health_reports_worker_heartbeat(down_services):
    assert TestClient(server.app).get("/api/health").json()["worker_seen_at"] is None
    call_store.heartbeat("agent")
    seen = TestClient(server.app).get("/api/health").json()["worker_seen_at"]
    assert seen and abs(call_store.last_heartbeat("agent") - time.time()) < 5
