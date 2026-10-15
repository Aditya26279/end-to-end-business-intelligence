"""Command-line orchestrator. The Airflow DAG calls the single-step commands as separate tasks.

    python run_pipeline.py all          # generate -> reset -> ETL -> quality -> mining (full rebuild)
    python run_pipeline.py etl          # init + extract/transform + load + quality checks
    python run_pipeline.py mine         # segmentation, forecasting, association rules
    python run_pipeline.py serve        # start the dashboard on http://127.0.0.1:8000
    python run_pipeline.py reset        # drop the warehouse schemas
    python run_pipeline.py db-url       # print the connection string (e.g. for psql / DBeaver)

Single steps: generate, init, extract, load, quality, segmentation, forecasting, association
"""
from __future__ import annotations

import argparse
import sys
import time


def generate(args) -> None:
    from bi.generate_raw import generate as gen
    print("[generate]", gen(args.customers, args.seed))


def reset(args) -> None:
    from bi.etl.pipeline import reset_warehouse
    reset_warehouse()
    print("[reset] dropped schemas staging, dw, mining")


def init(args) -> None:
    from bi.etl.pipeline import init_warehouse
    init_warehouse()


def extract(args) -> None:
    from bi.etl.pipeline import extract_transform
    extract_transform()


def load(args) -> None:
    from bi.etl.pipeline import load_warehouse
    load_warehouse()


def quality(args) -> None:
    from bi.etl.pipeline import quality_checks
    quality_checks()


def _mining(name: str):
    def step(args) -> None:
        from importlib import import_module
        from bi.etl.runlog import log_step
        module = import_module(f"bi.mining.{name}")
        with log_step(name) as stats:
            stats["message"] = module.run()
    return step


segmentation, forecasting, association = (_mining(n) for n in ("segmentation", "forecasting", "association"))


def serve(args) -> None:
    import os
    import uvicorn
    if args.host not in ("127.0.0.1", "localhost", "::1") and not os.getenv("BI_DASHBOARD_PASSWORD"):
        sys.exit("Refusing to serve customer data on a non-loopback address without authentication: "
                 "set BI_DASHBOARD_PASSWORD (and optionally BI_DASHBOARD_USER).")
    from bi.config import database_url
    database_url()  # make sure the (embedded) database is up before serving
    uvicorn.run("bi.dashboard.app:app", host=args.host, port=args.port)


def db_url(args) -> None:
    from bi.config import database_url
    print(database_url())


ETL = [init, extract, load, quality]
MINE = [segmentation, forecasting, association]
PLANS = {
    "all": [generate, reset, *ETL, *MINE], "etl": ETL, "mine": MINE,
    "generate": [generate], "reset": [reset], "init": [init], "extract": [extract], "load": [load],
    "quality": [quality], "segmentation": [segmentation], "forecasting": [forecasting],
    "association": [association], "serve": [serve], "db-url": [db_url],
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=list(PLANS))
    ap.add_argument("--customers", type=int, default=6000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    t0 = time.perf_counter()
    for step in PLANS[args.command]:
        step(args)
    if args.command not in ("serve", "db-url"):
        print(f"done in {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main())
