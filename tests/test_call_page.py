import pytest
from fastapi.testclient import TestClient

import auth
import server
import tenants


@pytest.fixture
def client_tenant():
    return tenants.upsert_tenant({"name": "Sess Co", "password": "pw1", "language": "ta", "agent_name": "Meera"})


@pytest.fixture
def admin_client():
    c = TestClient(server.app)
    auth.clear_login_failures("ip:testclient"); auth.clear_login_failures("u:admin")
    assert c.post("/api/auth/login", json={"username": "admin", "password": auth.ADMIN_PASSWORD}).status_code == 200
    return c


def test_call_config_public(client_tenant):
    r = TestClient(server.app).get("/api/call/sess-co/config")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_name"] == "Meera" and body["language"] == "ta" and len(body["call_key"]) == 32
    assert "password" not in body and "webhook_secret" not in body


def test_call_config_refused_when_paused(client_tenant):
    tenants.upsert_tenant({"id": "sess-co", "active": False})
    assert TestClient(server.app).get("/api/call/sess-co/config").status_code == 404
    assert TestClient(server.app).get("/call/sess-co").status_code == 404


def test_call_page_served(client_tenant):
    r = TestClient(server.app).get("/call/sess-co")
    assert r.status_code == 200 and "livekit-client" in r.text


def test_unknown_slug_is_404():
    assert TestClient(server.app).get("/call/nobody-here").status_code == 404
    assert TestClient(server.app).get("/api/call/nobody-here/config").status_code == 404


def test_rotate_key_changes_config(client_tenant, admin_client):
    old = admin_client.get("/api/call/sess-co/config").json()["call_key"]
    assert admin_client.post("/api/admin/clients/sess-co/rotate-key").status_code == 200
    assert admin_client.get("/api/call/sess-co/config").json()["call_key"] != old


def test_rotate_key_needs_admin(client_tenant):
    assert TestClient(server.app).post("/api/admin/clients/sess-co/rotate-key").status_code == 401


def test_widget_served():
    r = TestClient(server.app).get("/static/widget.js")
    assert r.status_code == 200 and "data-client" in r.text
