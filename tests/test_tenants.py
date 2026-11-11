import json

import pytest

import call_store
import tenants


def test_upsert_persists_to_sqlite():
    t = tenants.upsert_tenant({"name": "Apollo Clinic", "language": "ta", "password": "x"})
    row = call_store.get_conn().execute(
        "SELECT id, language, call_key, session_version FROM tenants WHERE id=?", (t.id,)
    ).fetchone()
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
    p.write_text(json.dumps({
        "default": {},
        "tenants": [{"id": "legacy-co", "name": "Legacy", "password": "pw"}],
    }))
    assert tenants.import_json_if_empty(str(p)) == 1
    assert tenants.get_tenant("legacy-co").name == "Legacy"
    assert (tmp_path / "tenants.json.imported").exists()
    assert tenants.import_json_if_empty(str(p)) == 0


def test_delete_then_recreate_gets_new_key():
    a = tenants.upsert_tenant({"name": "Twice", "password": "a"})
    tenants.delete_tenant(a.id)
    b = tenants.upsert_tenant({"name": "Twice", "password": "a"})
    assert b.call_key != a.call_key
