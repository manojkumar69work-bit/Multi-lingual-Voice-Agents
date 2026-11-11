import pytest
from fastapi.testclient import TestClient

import auth
import server
import tenants


class _FakeRoomService:
    async def create_room(self, req):
        return None


class _FakeLiveKitAPI:
    def __init__(self, *a, **k):
        self.room = _FakeRoomService()

    async def aclose(self):
        return None


@pytest.fixture(autouse=True)
def _no_livekit(monkeypatch):
    from livekit import api
    monkeypatch.setattr(api, "LiveKitAPI", _FakeLiveKitAPI)
    auth.clear_login_failures("ip:testclient")
    auth.clear_login_failures("u:nobody")
    auth.clear_login_failures("u:admin")


@pytest.fixture
def client_tenant():
    return tenants.upsert_tenant({"name": "Sess Co", "password": "pw1", "language": "hi"})


def _login(c, u, p):
    return c.post("/api/auth/login", json={"username": u, "password": p})


def test_pause_invalidates_session(client_tenant):
    c = TestClient(server.app)
    assert _login(c, "sess-co", "pw1").status_code == 200
    assert c.get("/api/client/stats").status_code == 200
    tenants.upsert_tenant({"id": "sess-co", "active": False})
    assert c.get("/api/client/stats").status_code == 401


def test_password_change_invalidates_session(client_tenant):
    c = TestClient(server.app)
    _login(c, "sess-co", "pw1")
    tenants.upsert_tenant({"id": "sess-co", "password": "pw2"})
    assert c.get("/api/client/calls").status_code == 401


def test_login_rate_limit():
    c = TestClient(server.app)
    for _ in range(5):
        assert _login(c, "nobody", "bad").status_code == 401
    assert _login(c, "nobody", "bad").status_code == 429
    assert _login(c, "admin", auth.ADMIN_PASSWORD).status_code == 429  # same IP is blocked too


def test_token_requires_key_or_session(client_tenant):
    c = TestClient(server.app)
    assert c.post("/api/livekit/token", json={"client_id": "sess-co"}).status_code == 401
    assert c.post("/api/livekit/token", json={"client_id": "sess-co", "call_key": "wrong"}).status_code == 401
    key = tenants.get_tenant("sess-co").call_key
    r = c.post("/api/livekit/token", json={"client_id": "sess-co", "call_key": key})
    assert r.status_code == 200 and r.json()["metadata"] == {"tenant": "sess-co"}


def test_paused_client_key_is_refused(client_tenant):
    c = TestClient(server.app)
    key = tenants.get_tenant("sess-co").call_key
    tenants.upsert_tenant({"id": "sess-co", "active": False})
    assert c.post("/api/livekit/token", json={"client_id": "sess-co", "call_key": key}).status_code == 401


def test_language_test_call_needs_admin():
    c = TestClient(server.app)
    assert c.post("/api/livekit/token", json={"language": "ta"}).status_code == 401
    assert _login(c, "admin", auth.ADMIN_PASSWORD).status_code == 200
    assert c.post("/api/livekit/token", json={"language": "ta"}).status_code == 200
