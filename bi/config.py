"""Paths and database connection settings."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = Path(os.getenv("BI_RAW_DIR", ROOT / "data" / "raw"))
SQL_DIR = ROOT / "sql"
PGDATA_DIR = ROOT / ".pgdata"


@lru_cache(maxsize=1)
def database_url() -> str:
    """Return a SQLAlchemy URL.

    Uses DATABASE_URL when set (Docker / managed Postgres). Otherwise starts an
    embedded PostgreSQL server (pgserver) that persists in ./.pgdata and keeps
    running after this process exits, so the pipeline and dashboard share it.
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        import pgserver

        server = pgserver.get_server(PGDATA_DIR, cleanup_mode=None)
        url = server.get_uri()
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url
