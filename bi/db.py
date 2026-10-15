"""Database helpers: engine, SQL file execution, fast COPY loads."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd
from psycopg import sql
from sqlalchemy import Engine, create_engine, text

from bi.config import SQL_DIR, database_url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(database_url(), pool_pre_ping=True)


def run_sql_file(name: str | Path) -> None:
    path = Path(name) if Path(name).is_absolute() else SQL_DIR / name
    sql = path.read_text(encoding="utf-8")
    raw = get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(sql)
        raw.commit()
    finally:
        raw.close()


def _python_rows(df: pd.DataFrame):
    """Rows of native Python values with every missing marker (NaN, NA, NaT) as None."""
    columns = [[None if pd.isna(v) else v for v in df[c].tolist()] for c in df.columns]
    return zip(*columns)


def copy_frames(loads: list[tuple[pd.DataFrame, str, str]], truncate: bool = True) -> int:
    """Bulk load several DataFrames with COPY in ONE transaction: all tables change or none do.

    Rows go through psycopg's typed row writer, so NULLs are real NULLs (a CSV NULL marker
    would turn any source text equal to the marker into NULL).
    """
    raw = get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            for df, table, schema in loads:
                target = sql.Identifier(schema, table)
                if truncate:
                    cur.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY").format(target))
                cols = sql.SQL(", ").join(sql.Identifier(c) for c in df.columns)
                with cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(target, cols)) as cp:
                    for row in _python_rows(df):
                        cp.write_row(row)
        raw.commit()
    except BaseException:
        raw.rollback()
        raise
    finally:
        raw.close()
    return sum(len(df) for df, _, _ in loads)


def copy_dataframe(df: pd.DataFrame, table: str, schema: str, truncate: bool = True) -> int:
    """Bulk load one DataFrame (optionally replacing the table's contents atomically)."""
    return copy_frames([(df, table, schema)], truncate)


def read_sql(sql: str, params: dict | None = None) -> pd.DataFrame:
    with get_engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


def execute(sql: str, params: dict | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(text(sql), params or {})
