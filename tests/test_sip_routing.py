import pytest

import tenants


@pytest.fixture
def client_with_number():
    tenants.upsert_tenant({"name": "Num Co", "password": "x", "phone_number": "+91 12345 67890"})
    tenants.upsert_tenant({"name": "Other Co", "password": "x"})


def test_sip_called_number_routes(client_with_number):
    t, caller = tenants.resolve_tenant(None, {"sip.trunkPhoneNumber": "+911234567890", "sip.phoneNumber": "+919999999999"})
    assert t.id == "num-co" and caller == "+919999999999"


def test_sip_unknown_number_uses_default():
    t, caller = tenants.resolve_tenant(None, {"sip.trunkPhoneNumber": "+910000000000", "sip.phoneNumber": "+918888888888"})
    assert t.id == "default" and caller == "+918888888888"


def test_metadata_wins_over_sip(client_with_number):
    t, _ = tenants.resolve_tenant('{"tenant":"other-co"}', {"sip.trunkPhoneNumber": "+911234567890"})
    assert t.id == "other-co"


def test_browser_call_has_no_caller(client_with_number):
    t, caller = tenants.resolve_tenant('{"tenant":"num-co"}', {})
    assert t.id == "num-co" and caller == ""


def test_paused_client_number_still_resolves_to_them(client_with_number):
    # The agent declines the call as the paused client (see agent.py entrypoint)
    # rather than answering as the demo agent.
    tenants.upsert_tenant({"id": "num-co", "active": False})
    t, _ = tenants.resolve_tenant(None, {"sip.trunkPhoneNumber": "+911234567890"})
    assert t.id == "num-co" and t.active is False
