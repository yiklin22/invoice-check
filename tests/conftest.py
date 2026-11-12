import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from invoicecheck import db, service
from invoicecheck.models import Base

FIXTURES = Path(__file__).parent.parent / "fixtures"


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
    assert service.import_purchase_orders(session, (FIXTURES / "purchase_orders.csv").read_text())["ok"]
    service.create_batch(session, fixture_files())
    service.process_queued(session)
    return session
