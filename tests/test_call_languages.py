"""Callers pick the language on the call page, within what the client allows."""
import json

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
    auth.clear_login_failures("tok:testclient")


@pytest.fixture
def multi():
    return tenants.upsert_tenant({
        "name": "Multi Co", "password": "pw", "language": "hi", "greeting": "Namaste from Multi",
        "call_languages": "hi,te,ta",
        "greetings": json.dumps({"te": "నమస్కారం Multi", "ta": "வணக்கம் Multi"}),
    })


def test_allowed_languages_default_is_primary_only():
    t = tenants.upsert_tenant({"name": "Solo Co", "password": "pw", "language": "te"})
    assert tenants.allowed_languages(t) == ["te"]


def test_allowed_languages_all_and_list(multi):
    assert tenants.allowed_languages(multi) == ["hi", "te", "ta"]
    t = tenants.upsert_tenant({"name": "All Co", "password": "pw", "call_languages": "all"})
    assert len(tenants.allowed_languages(t)) == 11


def test_unknown_codes_are_dropped_and_primary_kept_first():
    t = tenants.upsert_tenant({"name": "Odd Co", "password": "pw", "language": "en", "call_languages": "ta, zz ,te"})
    assert tenants.allowed_languages(t) == ["en", "ta", "te"]


def test_metadata_language_overrides_for_the_call(multi):
    t, _ = tenants.resolve_tenant(json.dumps({"tenant": "multi-co", "language": "ta"}), {})
    assert t.id == "multi-co" and t.language == "ta" and t.greeting == "வணக்கம் Multi"
    # the stored client is untouched
    assert tenants.get_tenant("multi-co").language == "hi"


def test_override_without_greeting_for_that_language_uses_built_in(multi):
    tenants.upsert_tenant({"id": "multi-co", "greetings": "{}"})
    t, _ = tenants.resolve_tenant(json.dumps({"tenant": "multi-co", "language": "te"}), {})
    assert t.language == "te" and t.greeting == ""


def test_disallowed_language_is_ignored(multi):
    t, _ = tenants.resolve_tenant(json.dumps({"tenant": "multi-co", "language": "bn"}), {})
    assert t.language == "hi" and t.greeting == "Namaste from Multi"


def test_call_config_lists_languages(multi):
    body = TestClient(server.app).get("/api/call/multi-co/config").json()
    assert [l["code"] for l in body["languages"]] == ["hi", "te", "ta"]
    assert body["language"] == "hi" and body["languages"][1]["native"] == "తెలుగు"


def test_token_carries_chosen_language(multi):
    c = TestClient(server.app)
    r = c.post("/api/livekit/token", json={"client_id": "multi-co", "call_key": multi.call_key, "language": "ta"})
    assert r.status_code == 200 and r.json()["metadata"] == {"tenant": "multi-co", "language": "ta"}


def test_token_refuses_language_the_client_does_not_offer(multi):
    c = TestClient(server.app)
    r = c.post("/api/livekit/token", json={"client_id": "multi-co", "call_key": multi.call_key, "language": "bn"})
    assert r.status_code == 422
