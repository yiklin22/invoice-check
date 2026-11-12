"""Database connection. PostgreSQL only.

Set DATABASE_URL to use your own server. Without it, a private PostgreSQL
is started from the `pgserver` package in ./.pgdata, so the app runs on a
machine with no database installed.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

# The bundled server keeps one database per schema generation. There are no
# migrations yet, so a change to the tables gets a new name and starts empty;
# the previous database is left where it was.
EMBEDDED_DATABASE = "invoicecheck_v2"

_engine: Engine | None = None
_embedded = None  # keeps the pgserver handle alive


def _sqlalchemy_url(url: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def start_embedded(pgdata: str | Path) -> str:
    """Start (or attach to) a PostgreSQL in `pgdata` and return its URL."""
    global _embedded
    import pgserver
    _embedded = pgserver.get_server(Path(pgdata).resolve())
    admin = create_engine(_sqlalchemy_url(_embedded.get_uri()), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": EMBEDDED_DATABASE}):
            conn.execute(text(f'CREATE DATABASE "{EMBEDDED_DATABASE}"'))
    admin.dispose()
    return _sqlalchemy_url(_embedded.get_uri(database=EMBEDDED_DATABASE))


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = os.environ.get("DATABASE_URL")
        url = _sqlalchemy_url(url) if url else start_embedded(os.environ.get("INVOICECHECK_PGDATA", ".pgdata"))
        set_engine(create_engine(url, pool_pre_ping=True))
    return _engine


def set_engine(engine: Engine) -> None:
    global _engine
    tables = inspect(engine)
    if tables.has_table("invoices") and "account_id" not in {c["name"] for c in tables.get_columns("invoices")}:
        raise RuntimeError(
            "This database was created before accounts were added and there is no migration for it. "
            "Point DATABASE_URL at an empty database.")
    _engine = engine
    Base.metadata.create_all(engine)


def new_session() -> Session:
    return sessionmaker(get_engine(), expire_on_commit=False)()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    session = new_session()
    try:
        yield session
    finally:
        session.close()
