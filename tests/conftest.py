"""Point every project module at a throwaway database before it is imported."""
import os
import pathlib
import tempfile

_tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["CALL_DB_PATH"] = str(_tmp / "calls.db")
os.environ["TENANTS_CONFIG"] = str(_tmp / "tenants.json")
os.environ["RECORDINGS_DIR"] = str(_tmp / "rec")
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin")


import pytest


@pytest.fixture(autouse=True)
def _clean_tables():
    """Each test starts from an empty tenants table; calls and queues too."""
    import call_store
    conn = call_store.get_conn()
    for table in ("tenants", "calls", "lead_deliveries"):
        try:
            conn.execute(f"DELETE FROM {table}")
        except Exception:
            pass
    conn.commit()
    yield
