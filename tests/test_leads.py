"""Lead lifecycle: merge across the call, flag only real content, exclusive
delivery claims, Sheets formula escaping, stalled-call reaping.

The agent.py helpers are loaded out of its AST (as test_voice.py does) so the
test never imports livekit."""
import ast
import json
import time

import pytest

import call_store
import lead_delivery

_src = open("agent.py").read()
_ns = {}
for _n in ast.parse(_src).body:
    if isinstance(_n, ast.FunctionDef) and _n.name in ("merge_lead", "lead_has_content"):
        exec(compile(ast.Module([_n], []), "agent.py", "exec"), _ns)
merge_lead = _ns.get("merge_lead")
lead_has_content = _ns.get("lead_has_content")


def test_merge_lead_keeps_old_and_adds_new():
    merged, changed = merge_lead({"name": "Arjun", "budget": ""}, {"name": "", "budget": "80 lakh"})
    assert merged == {"name": "Arjun", "budget": "80 lakh"} and changed


def test_merge_lead_notes_only_is_not_a_change():
    _, changed = merge_lead({"name": "A", "notes": ""}, {"name": "A", "notes": "called twice"})
    assert not changed


def test_merge_lead_new_value_wins():
    merged, changed = merge_lead({"budget": "50 lakh"}, {"budget": "80 lakh"})
    assert merged["budget"] == "80 lakh" and changed


def test_merge_lead_same_values_unchanged():
    _, changed = merge_lead({"name": "A", "budget": "1"}, {"name": "A", "budget": "1"})
    assert not changed


def test_lead_has_content():
    assert not lead_has_content({"name": "", "notes": "x"})
    assert not lead_has_content({})
    assert lead_has_content({"name": "A"})


def test_escape_cell():
    assert lead_delivery.escape_cell("=IMPORTXML(1)") == "'=IMPORTXML(1)"
    assert lead_delivery.escape_cell("+91 98") == "'+91 98"
    assert lead_delivery.escape_cell("Arjun") == "Arjun"
    assert lead_delivery.escape_cell("") == ""


@pytest.fixture
def queued_rows():
    conn = call_store.get_conn()
    now = time.time()
    for i in range(3):
        conn.execute(
            "INSERT INTO lead_deliveries (session_id, client_id, channel, destination, payload, status, attempts, next_attempt_at, created_at, updated_at) "
            "VALUES (?, 'c', 'webhook', 'http://x', ?, 'pending', 0, ?, ?, ?)",
            (f"s{i}", json.dumps({"lead": {"name": "A"}}), now - 1, now, now),
        )
    conn.commit()


def test_claim_due_is_exclusive(queued_rows):
    a = lead_delivery.claim_due(10)
    b = lead_delivery.claim_due(10)
    assert len(a) == 3 and b == []
    assert {r["status"] for r in lead_delivery.list_deliveries()} == {"sending"}


def test_failed_send_goes_back_to_pending(queued_rows):
    row = lead_delivery.claim_due(1)[0]
    lead_delivery._mark_failed(int(row["id"]), 1, "boom")
    got = [r for r in lead_delivery.list_deliveries() if r["id"] == row["id"]][0]
    assert got["status"] == "pending" and got["attempts"] == 1


def test_mark_stalled():
    call_store.upsert_call("s1", status="active")
    call_store.upsert_call("s2", status="active")
    conn = call_store.get_conn()
    conn.execute("UPDATE calls SET updated_at = updated_at - 10000 WHERE session_id = 's1'")
    conn.commit()
    assert call_store.mark_stalled(7200) == 1
    assert call_store.get_call("s1")["status"] == "stalled"
    assert call_store.get_call("s2")["status"] == "active"
