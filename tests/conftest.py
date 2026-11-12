import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from invoicecheck import auth, db, service
from invoicecheck.models import Account, Base

FIXTURES = Path(__file__).parent.parent / "fixtures"
ACC = 1                                  # the account every older test works in
EMAIL, PASSWORD = "ana@example.test", "correct horse battery"
PASSWORD_HASH = auth.hash_password(PASSWORD)     # hashed once; scrypt is slow on purpose


def add_account(session, email, name) -> Account:
    account = Account(email=email, name=name, password_hash=PASSWORD_HASH)
    session.add(account)
    session.commit()
    return account


@pytest.fixture(scope="session")
def engine(tmp_path_factory):
    """A real PostgreSQL for the test run, started from the pgserver package."""
    url = db.start_embedded(tmp_path_factory.mktemp("pg"))
    engine = create_engine(url)
    db.set_engine(engine)
    return engine


@pytest.fixture
def session(engine):
    with engine.begin() as conn:
        names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    s = db.new_session()
    assert add_account(s, EMAIL, "Ana").id == ACC
    yield s
    s.close()


@pytest.fixture(scope="session")
def manifest():
    return json.loads((FIXTURES / "manifest.json").read_text())["invoices"]


def fixture_files(names=None):
    paths = sorted((FIXTURES / "invoices").glob("*.pdf"))
    return [(p.name, p.read_bytes()) for p in paths if names is None or p.name in names]


@pytest.fixture
def loaded(session):
    """All purchase orders and all 20 fixture invoices, processed."""
    assert service.import_purchase_orders(session, ACC, (FIXTURES / "purchase_orders.csv").read_text())["ok"]
    service.create_batch(session, ACC, fixture_files())
    service.process_queued(session)
    return session


def log_in(client, email=EMAIL, password=PASSWORD):
    r = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return client


@pytest.fixture
def anon(session):
    """A browser with no login."""
    from fastapi.testclient import TestClient

    from invoicecheck.app import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(anon):
    """A browser logged in as Ana, the owner of account 1."""
    return log_in(anon)
