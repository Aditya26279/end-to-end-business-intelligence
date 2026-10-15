"""Audit trail: every pipeline step writes a row to dw.etl_run_log."""
from __future__ import annotations

import time
from contextlib import contextmanager

from sqlalchemy import text

from bi.db import get_engine


@contextmanager
def log_step(step: str):
    """Usage: with log_step("load") as stats: stats["rows_out"] = n"""
    engine = get_engine()
    with engine.begin() as conn:
        run_id = conn.execute(text("INSERT INTO dw.etl_run_log (step, status) VALUES (:s, 'running') "
                                   "RETURNING run_id"), {"s": step}).scalar_one()
    stats: dict = {}
    t0 = time.perf_counter()
    try:
        yield stats
    except BaseException as exc:  # incl. Ctrl+C / task kill, so no row is left 'running'
        with engine.begin() as conn:
            conn.execute(text("UPDATE dw.etl_run_log SET status='failed', finished_at=now(), message=:m "
                              "WHERE run_id=:id"), {"m": f"{type(exc).__name__}: {exc}"[:2000], "id": run_id})
        raise
    msg = stats.get("message", "") + f" ({time.perf_counter() - t0:.1f}s)"
    with engine.begin() as conn:
        conn.execute(text("UPDATE dw.etl_run_log SET status='success', finished_at=now(), rows_in=:i, "
                          "rows_out=:o, rows_rejected=:r, message=:m WHERE run_id=:id"),
                     {"i": stats.get("rows_in"), "o": stats.get("rows_out"), "r": stats.get("rows_rejected"),
                      "m": msg.strip(), "id": run_id})
    print(f"[{step}] ok {msg.strip()}")
