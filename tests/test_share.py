"""Sharing the agent with prospects: the lead prompt asks for a number when the
client wants one, and public mode exposes only the call page."""
import ast

import pytest
from fastapi.testclient import TestClient

import server

_ns = {}
for n in ast.parse(open("agent.py").read()).body:
    if isinstance(n, ast.Assign) and any(getattr(t, "id", "") in ("_LEAD_FIELDS_HINT_TEMPLATE", "_LEAD_FIELDS_HINT_WITH_CONTACT", "_CONTACT_FIELDS") for t in n.targets):
        exec(compile(ast.Module([n], []), "agent.py", "exec"), _ns)
    if isinstance(n, ast.FunctionDef) and n.name == "lead_hint_for":
        exec(compile(ast.Module([n], []), "agent.py", "exec"), _ns)


def test_lead_hint_forbids_number_by_default():
    hint = _ns["lead_hint_for"]("name, budget")
    assert "Do NOT ask for phone number" in hint


def test_lead_hint_asks_for_number_when_field_requested():
    hint = _ns["lead_hint_for"]("name, business_name, phone")
    assert "Do NOT ask for phone number" not in hint
    assert "phone" in hint.lower() and "once" in hint.lower()


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setattr(server, "PUBLIC_CALLS_ONLY", True)
    return TestClient(server.app, base_url="https://demo.example.com")


@pytest.mark.parametrize("path", ["/admin", "/login", "/client", "/api/admin/clients", "/api/auth/me", "/", "/api/health"])
def test_public_mode_hides_everything_but_the_call_page(public, path):
    assert public.get(path).status_code == 404


@pytest.mark.parametrize("path", ["/static/app.css", "/static/widget.js"])
def test_public_mode_serves_call_page_assets(public, path):
    assert public.get(path).status_code == 200


def test_public_mode_allows_call_page_and_token(public):
    import tenants
    t = tenants.upsert_tenant({"name": "Demo Co", "password": "pw"})
    assert public.get("/call/demo-co").status_code == 200
    assert public.get("/api/call/demo-co/config").status_code == 200
    # token endpoint is reachable (it answers 401/200 on its own rules, never 404)
    assert public.post("/api/livekit/token", json={"client_id": "demo-co"}).status_code == 401


def test_public_mode_leaves_localhost_alone(monkeypatch):
    monkeypatch.setattr(server, "PUBLIC_CALLS_ONLY", True)
    local = TestClient(server.app, base_url="http://localhost:8000")
    assert local.get("/login").status_code == 200


def test_agent_admits_being_ai_when_asked():
    """A caller who sincerely asks gets the truth; the old rule made it deny."""
    import re
    ns = {}
    for n in ast.parse(open("agent.py").read()).body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_SCOPE_GUARD" for t in n.targets):
            exec(compile(ast.Module([n], []), "agent.py", "exec"), ns)
    guard = ns["_SCOPE_GUARD"]
    assert "NEVER say you are an AI" not in guard
    assert re.search(r"sincerely asks.*AI assistant", guard, re.S)
