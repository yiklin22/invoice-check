"""Database connection. PostgreSQL only.

Set DATABASE_URL to use your own server. Without it, a private PostgreSQL
is started from the `pgserver` package in ./.pgdata, so the app runs on a
machine with no database installed.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

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
    return _sqlalchemy_url(_embedded.get_uri())


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = os.environ.get("DATABASE_URL")
        url = _sqlalchemy_url(url) if url else start_embedded(os.environ.get("INVOICECHECK_PGDATA", ".pgdata"))
        set_engine(create_engine(url, pool_pre_ping=True))
    return _engine


def set_engine(engine: Engine) -> None:
    global _engine
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
