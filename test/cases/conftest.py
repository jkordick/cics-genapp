"""pytest fixtures for the GenApp off-mainframe tests (run via test/run.sh)."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

from genapp import COPYBOOK_DIR, ROOT, GenApp, Layout

# Full COMMAREA length used by GenApp's own front ends (LINK ... LENGTH(32500)).
CALEN = 32500


@pytest.fixture(scope="session")
def genapp(tmp_path_factory) -> GenApp:
    build = Path(os.environ.get("GENAPP_BUILD", ROOT / "test" / "build"))
    if not (build / "cicsrun").exists():
        pytest.exit(f"{build}/cicsrun not found - run the tests with test/run.sh", returncode=2)
    return GenApp(build, tmp_path_factory.mktemp("genapp-data"), Layout(COPYBOOK_DIR / "lgcmarea.cpy"))


@pytest.fixture(scope="session")
def db():
    """Autocommit connection to the PostgreSQL database standing in for Db2."""
    with psycopg.connect(autocommit=True) as conn:
        yield conn


@pytest.fixture
def new_customer(genapp):
    """Factory: add a customer through LGACUS01 and return its number."""
    def make(first_name="Ada", last_name="Lovelace", **extra) -> int:
        ca = genapp.commarea(
            request_id="01ACUS", customer_num=0,
            first_name=first_name, last_name=last_name, dob="1815-12-10",
            house_name="Byron House", house_num="12", postcode="SO211AB",
            phone_mobile="07700900123", phone_home="01632960123",
            email_address="ada@example.com", **extra)
        r = genapp.call("LGACUS01", ca)
        assert r.abend is None, r.log
        assert r.return_code == "00", f"customer add failed with {r.return_code}"
        return r.ca["CA-CUSTOMER-NUM"]
    return make


@pytest.fixture
def customer(new_customer) -> int:
    return new_customer()
