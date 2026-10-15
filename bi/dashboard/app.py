"""Dashboard API (FastAPI) + static single-page front end.

Every sales endpoint accepts the same global filters, so all charts on a page agree:
    start, end (YYYY-MM-DD), region, channel, category, segment
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import math
import os
import secrets
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg import sql as pgsql
from sqlalchemy.exc import ProgrammingError

from bi.db import get_engine, read_sql

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="BI Platform API", version="1.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Optional HTTP Basic auth. The dashboard exposes customer names, so it must be set whenever the
# server is reachable from other machines (run_pipeline.py serve refuses a non-loopback host without it).
AUTH_USER = os.getenv("BI_DASHBOARD_USER", "admin")
AUTH_PASSWORD = os.getenv("BI_DASHBOARD_PASSWORD", "")

CSP = ("default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
MIN_DATE, MAX_DATE = dt.date(1900, 1, 1), dt.date(2100, 12, 31)


def _authorized(header: str | None) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:], validate=True).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    # evaluate both comparisons (no short-circuit) so timing does not reveal which part was wrong
    user_ok = secrets.compare_digest(user.encode(), AUTH_USER.encode())
    password_ok = secrets.compare_digest(password.encode(), AUTH_PASSWORD.encode())
    return user_ok & password_ok


@app.middleware("http")
async def security(request: Request, call_next):
    if AUTH_PASSWORD and not _authorized(request.headers.get("authorization")):
        return Response("Authentication required", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="BI dashboard", charset="UTF-8"'})
    response = await call_next(request)
    if not request.url.path.startswith(("/docs", "/redoc")):  # Swagger UI needs its own inline script
        response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"  # responses contain customer data
    return response


@app.exception_handler(ProgrammingError)
async def warehouse_not_ready(request: Request, exc: ProgrammingError):
    # e.g. a schema/table missing because the pipeline has not run yet; never echo SQL to the client
    return JSONResponse({"detail": "warehouse not ready - run: python run_pipeline.py all"}, status_code=503)


class Filters:
    def __init__(self, start: Optional[dt.date] = None, end: Optional[dt.date] = None,
                 region: Optional[str] = Query(None, max_length=100),
                 channel: Optional[str] = Query(None, max_length=100),
                 category: Optional[str] = Query(None, max_length=100),
                 segment: Optional[str] = Query(None, max_length=100)):
        for d in (start, end):
            if d is not None and not MIN_DATE <= d <= MAX_DATE:
                raise HTTPException(422, f"dates must be between {MIN_DATE} and {MAX_DATE}")
        if start and end and start > end:
            raise HTTPException(422, "start must not be after end")
        self.start, self.end = start, end
        self.region, self.channel, self.category, self.segment = region, channel, category, segment

    def sql(self, start=None, end=None, include_returns: bool = False) -> tuple[str, str, dict]:
        """Return (join, where, params) for dw.v_sales aliased as v."""
        start = start if start is not None else self.start
        end = end if end is not None else self.end
        clauses, params, join = [], {}, ""
        if not include_returns:
            clauses.append("NOT v.is_returned")
        if start:
            clauses.append("v.full_date >= :start"); params["start"] = start
        if end:
            clauses.append("v.full_date <= :end"); params["end"] = end
        for col, val in [("sales_region", self.region), ("channel", self.channel), ("category", self.category)]:
            if val:
                clauses.append(f"v.{col} = :{col}"); params[col] = val
        if self.segment:
            join = "JOIN mining.customer_segments cs ON cs.customer_key = v.customer_key"
            clauses.append("cs.segment = :segment"); params["segment"] = self.segment
        return join, ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def records(df: pd.DataFrame) -> list[dict]:
    out = df.to_dict(orient="records")
    for row in out:
        for k, v in row.items():
            if isinstance(v, float) and math.isnan(v):
                row[k] = None
            elif hasattr(v, "isoformat"):
                row[k] = v.isoformat()
            elif isinstance(v, Decimal):
                row[k] = float(v)
    return out


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    rng = read_sql("SELECT min(full_date) AS min_date, max(full_date) AS max_date FROM dw.v_sales")
    if rng.min_date.isna().all():
        raise HTTPException(503, "warehouse is empty - run: python run_pipeline.py all")
    opts = lambda sql: read_sql(sql).iloc[:, 0].dropna().tolist()
    return {
        "min_date": rng.min_date[0].isoformat(), "max_date": rng.max_date[0].isoformat(),
        "regions": opts("SELECT DISTINCT sales_region FROM dw.v_sales ORDER BY 1"),
        "channels": opts("SELECT DISTINCT channel FROM dw.dim_store ORDER BY 1"),
        "categories": opts("SELECT DISTINCT category FROM dw.dim_product ORDER BY 1"),
        "segments": opts("SELECT segment FROM mining.segment_profiles ORDER BY total_revenue DESC"),
        "last_etl": records(read_sql("SELECT step, status, finished_at FROM dw.etl_run_log "
                                     "ORDER BY run_id DESC LIMIT 1")),
    }


KPI_SQL = """
SELECT coalesce(sum(net_amount) FILTER (WHERE NOT is_returned), 0)             AS revenue,
       coalesce(sum(profit) FILTER (WHERE NOT is_returned), 0)                 AS profit,
       count(DISTINCT (source_system, order_id)) FILTER (WHERE NOT is_returned) AS orders,
       count(DISTINCT customer_key) FILTER (WHERE NOT is_returned AND customer_key <> -1) AS customers,
       coalesce(sum(quantity) FILTER (WHERE NOT is_returned), 0)               AS units,
       coalesce(sum(net_amount) FILTER (WHERE is_returned), 0)                 AS returned_value
FROM dw.v_sales v {join} {where}
"""


def _kpis(f: Filters, start=None, end=None) -> dict:
    join, where, params = f.sql(start, end, include_returns=True)
    row = {k: float(v) for k, v in read_sql(KPI_SQL.format(join=join, where=where), params).iloc[0].items()}
    row["aov"] = row["revenue"] / row["orders"] if row["orders"] else 0
    row["margin"] = row["profit"] / row["revenue"] if row["revenue"] else 0
    gross = row["revenue"] + row["returned_value"]
    row["return_rate"] = row["returned_value"] / gross if gross else 0
    return row


@app.get("/api/kpis")
def kpis(f: Filters = Depends()):
    current = _kpis(f)
    previous = None
    if f.start and f.end:  # same-length period immediately before
        days = (f.end - f.start).days + 1
        previous = _kpis(f, f.start - dt.timedelta(days=days), f.start - dt.timedelta(days=1))
        if previous["orders"] == 0:
            previous = None
    return {"current": current, "previous": previous}


@app.get("/api/trend")
def trend(f: Filters = Depends()):
    join, where, params = f.sql()
    df = read_sql(f"""SELECT date_trunc('month', v.full_date)::date AS month, sum(net_amount) AS revenue,
                             sum(profit) AS profit, count(DISTINCT (source_system, order_id)) AS orders
                      FROM dw.v_sales v {join} {where} GROUP BY 1 ORDER BY 1""", params)
    return records(df)


@app.get("/api/breakdown")
def breakdown(dim: str = Query(..., pattern="^(category|subcategory|sales_region|channel|age_band|brand|day_name)$"),
              f: Filters = Depends()):
    join, where, params = f.sql()
    df = read_sql(f"""SELECT v.{dim} AS label, sum(net_amount) AS revenue, sum(profit) AS profit,
                             count(DISTINCT (source_system, order_id)) AS orders
                      FROM dw.v_sales v {join} {where} GROUP BY 1 ORDER BY revenue DESC""", params)
    return records(df)


@app.get("/api/top-products")
def top_products(limit: int = Query(10, ge=1, le=50), f: Filters = Depends()):
    join, where, params = f.sql()
    params["limit"] = limit
    df = read_sql(f"""SELECT product_name, category, sum(quantity) AS units, sum(net_amount) AS revenue,
                             sum(profit) / nullif(sum(net_amount), 0) AS margin
                      FROM dw.v_sales v {join} {where}
                      GROUP BY 1, 2 ORDER BY revenue DESC LIMIT :limit""", params)
    return records(df)


# ------------------------------------------------------------------ mining
@app.get("/api/segments")
def segments(f: Filters = Depends()):
    """Segment profiles from the model + revenue within the current filter slice."""
    seg_filter = Filters(f.start, f.end, f.region, f.channel, f.category, None)
    join, where, params = seg_filter.sql()
    sliced = read_sql(f"""SELECT cs.segment, sum(v.net_amount) AS slice_revenue,
                                 count(DISTINCT v.customer_key) AS slice_customers
                          FROM dw.v_sales v JOIN mining.customer_segments cs ON cs.customer_key = v.customer_key
                          {where} GROUP BY 1""", params)
    prof = read_sql("SELECT * FROM mining.segment_profiles ORDER BY total_revenue DESC")
    prof = prof.merge(sliced, on="segment", how="left").fillna({"slice_revenue": 0, "slice_customers": 0})
    run = read_sql("SELECT params, metrics, run_at FROM mining.model_runs WHERE model = 'kmeans_rfm' "
                   "ORDER BY run_id DESC LIMIT 1")
    return {"profiles": records(prof), "model": records(run)[0] if len(run) else None}


@app.get("/api/segment-customers")
def segment_customers(segment: str = Query(..., max_length=100), limit: int = Query(15, ge=1, le=100)):
    df = read_sql("""SELECT c.customer_id, c.full_name, c.region, cs.recency_days, cs.frequency, cs.monetary,
                            cs.rfm_code
                     FROM mining.customer_segments cs JOIN dw.dim_customer c USING (customer_key)
                     WHERE cs.segment = :s ORDER BY cs.monetary DESC LIMIT :l""", {"s": segment, "l": limit})
    return records(df)


@app.get("/api/rfm-grid")
def rfm_grid():
    """Customer counts on the R x F score grid (5 x 5) for a heatmap."""
    return records(read_sql("""SELECT r_score, f_score, count(*) AS customers, avg(monetary) AS avg_monetary
                               FROM mining.customer_segments GROUP BY 1, 2 ORDER BY 1, 2"""))


@app.get("/api/forecast")
def forecast(series: str = Query("All", max_length=100)):
    df = read_sql("SELECT month, actual, fitted, forecast, lower_95, upper_95 FROM mining.sales_forecast "
                  "WHERE series = :s ORDER BY month", {"s": series})
    if df.empty:
        raise HTTPException(404, f"no forecast for series {series!r}")
    run = read_sql("SELECT params, metrics FROM mining.model_runs WHERE model = 'holt_winters' "
                   "ORDER BY run_id DESC LIMIT 1")
    metrics = run.metrics[0].get(series) if len(run) else None
    series_list = read_sql("SELECT series FROM mining.sales_forecast GROUP BY series ORDER BY series = 'All' DESC, series")
    return {"points": records(df), "metrics": metrics, "params": run.params[0] if len(run) else None,
            "series": series_list.series.tolist()}


@app.get("/api/rules")
def rules(level: str = Query("product", pattern="^(product|subcategory)$"),
          category: Optional[str] = Query(None, max_length=100),
          min_lift: float = Query(1.5, ge=0, le=1e6), min_confidence: float = Query(0.2, ge=0, le=1),
          item: Optional[str] = Query(None, max_length=100), limit: int = Query(50, ge=1, le=500)):
    clauses = ["level = :level", "lift >= :min_lift", "confidence >= :min_conf"]
    params = {"level": level, "min_lift": min_lift, "min_conf": min_confidence, "limit": limit}
    if category and level == "product":
        clauses.append("antecedent_category = :cat"); params["cat"] = category
    if item:
        # escape LIKE wildcards so the search is a plain substring match
        escaped = item.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(antecedents ILIKE :item OR consequents ILIKE :item)"); params["item"] = f"%{escaped}%"
    df = read_sql(f"""SELECT antecedents, consequents, antecedent_category, support, confidence, lift,
                             basket_count
                      FROM mining.association_rules WHERE {' AND '.join(clauses)}
                      ORDER BY lift DESC, confidence DESC LIMIT :limit""", params)
    run = read_sql("SELECT metrics FROM mining.model_runs WHERE model = 'fpgrowth' ORDER BY run_id DESC LIMIT 1")
    return {"rules": records(df), "metrics": run.metrics[0] if len(run) else None}


# ------------------------------------------------------------------ pipeline / ops
@app.get("/api/pipeline")
def pipeline():
    runs = read_sql("SELECT run_id, step, status, started_at, finished_at, rows_in, rows_out, rows_rejected, "
                    "message FROM dw.etl_run_log ORDER BY run_id DESC LIMIT 25")
    checks = read_sql("""SELECT DISTINCT ON (check_name) check_name, passed, observed, detail, checked_at
                         FROM dw.data_quality_checks ORDER BY check_name, checked_at DESC""")
    rejects = read_sql("SELECT source_system, reason, count(*) AS rows FROM staging.rejected_rows "
                       "GROUP BY 1, 2 ORDER BY 3 DESC")
    names = read_sql("""SELECT table_schema AS s, table_name AS t FROM information_schema.tables
                        WHERE table_schema IN ('staging', 'dw', 'mining') AND table_type = 'BASE TABLE'
                        ORDER BY table_schema, table_name""")
    # identifiers are quoted by psycopg, never pasted into the SQL text
    query = pgsql.SQL(" UNION ALL ").join(
        pgsql.SQL("SELECT {} AS table_name, count(*) AS rows FROM {}").format(
            pgsql.Literal(f"{r.s}.{r.t}"), pgsql.Identifier(r.s, r.t))
        for r in names.itertuples()) + pgsql.SQL(" ORDER BY 1")
    raw = get_engine().raw_connection()
    try:
        tables = pd.DataFrame(raw.cursor().execute(query).fetchall(), columns=["table_name", "rows"])
    finally:
        raw.close()
    return {"runs": records(runs), "checks": records(checks), "rejects": records(rejects),
            "tables": records(tables)}
