"""ETL steps. Each is a standalone callable so Airflow can run them as separate tasks;
steps hand data to each other only through the database (staging schema)."""
from __future__ import annotations

import json

import pandas as pd

from bi.config import RAW_DIR
from bi.db import copy_dataframe, copy_frames, execute, read_sql, run_sql_file
from bi.etl import transform as T
from bi.etl.runlog import log_step


def reset_warehouse() -> None:
    """Drop every BI schema (used when raw data is regenerated from scratch)."""
    execute("DROP SCHEMA IF EXISTS mining, dw, staging CASCADE")


def init_warehouse() -> None:
    run_sql_file("01_schema.sql")  # must run before log_step: it creates the log table
    with log_step("init_warehouse") as stats:
        stats["message"] = "schemas staging/dw/mining ready"


def _read_csv(name: str) -> pd.DataFrame:
    # Read everything as text: typing is the transform step's job, not pandas' guesswork.
    return pd.read_csv(RAW_DIR / name, dtype=str, keep_default_na=False)


def extract_transform() -> dict:
    """Extract raw files, clean/conform them, and land the result in the staging schema."""
    with log_step("extract_transform") as stats:
        geo = _read_csv("geo_regions.csv")
        customers = T.clean_customers(_read_csv("customers.csv"), geo)
        catalog = json.loads((RAW_DIR / "products.json").read_text(encoding="utf-8"))
        products = T.clean_products(catalog)
        bad_products = len(catalog["products"]) - len(products)
        stores = T.clean_stores(_read_csv("stores.csv"), geo)

        skus, store_ids = set(products.sku), set(stores.store_id)
        web_raw, pos_raw = _read_csv("orders_web.csv"), _read_csv("orders_pos.csv")
        web, web_rej, cancelled = T.clean_web_orders(web_raw, skus)
        pos, pos_rej = T.clean_pos_orders(pos_raw, skus, store_ids)
        sales = pd.concat([web, pos], ignore_index=True)
        rejects = pd.concat([web_rej, pos_rej], ignore_index=True)

        # one transaction: staging is never left half old / half new if a load fails
        copy_frames([(customers, "customers", "staging"), (products, "products", "staging"),
                     (stores, "stores", "staging"), (sales, "sales", "staging"),
                     (rejects, "rejected_rows", "staging")])

        by_reason = rejects["reason"].value_counts().to_dict()
        stats.update(rows_in=len(web_raw) + len(pos_raw), rows_out=len(sales), rows_rejected=len(rejects),
                     message=f"{len(customers)} customers, {len(products)} products ({bad_products} unusable), "
                             f"{len(stores)} stores; "
                             f"{cancelled} cancelled lines filtered; rejects {by_reason}")
        return {"sales": len(sales), "rejected": len(rejects), "cancelled": cancelled}


def load_warehouse() -> int:
    """Upsert dimensions and append new fact rows (idempotent)."""
    with log_step("load_warehouse") as stats:
        before = int(read_sql("SELECT count(*) AS n FROM dw.fact_sales").n[0])
        run_sql_file("02_load_dw.sql")
        after = int(read_sql("SELECT count(*) AS n FROM dw.fact_sales").n[0])
        staged = int(read_sql("SELECT count(*) AS n FROM staging.sales").n[0])
        stats.update(rows_in=staged, rows_out=after - before,
                     message=f"fact_sales {before} -> {after} rows")
        return after - before


QUALITY_CHECKS = [
    ("fact table not empty", "SELECT count(*) FROM dw.fact_sales", "> 0"),
    ("staged lines missing from fact",
     """SELECT count(*) FROM staging.sales s LEFT JOIN dw.fact_sales f
          ON (f.source_system, f.order_id, f.line_number) = (s.source_system, s.order_id, s.line_number)
        WHERE f.sales_id IS NULL""", "= 0"),
    ("reject rate (%)",
     """SELECT round(100.0 * (SELECT count(*) FROM staging.rejected_rows)
               / nullif((SELECT count(*) FROM staging.sales) + (SELECT count(*) FROM staging.rejected_rows), 0), 3)""",
     "< 2"),
    ("unknown-customer share of revenue (%)",
     """SELECT round(100.0 * sum(net_amount) FILTER (WHERE customer_key = -1) / nullif(sum(net_amount), 0), 3)
        FROM dw.fact_sales""", "< 2"),
    ("negative net amounts", "SELECT count(*) FROM dw.fact_sales WHERE net_amount < 0", "= 0"),
    ("revenue reconciles staging vs fact (abs diff)",
     """SELECT abs(coalesce((SELECT sum(round(quantity * unit_price - discount_amount, 2)) FROM staging.sales), 0)
                 - coalesce((SELECT sum(f.net_amount) FROM dw.fact_sales f JOIN staging.sales s
                     ON (f.source_system, f.order_id, f.line_number) = (s.source_system, s.order_id, s.line_number)), 0))""",
     "< 0.01"),
    ("customers without region", "SELECT count(*) FROM dw.dim_customer WHERE region IS NULL", "= 0"),
]


def quality_checks(fail_on_error: bool = True) -> list[dict]:
    with log_step("quality_checks") as stats:
        results = []
        for name, sql, rule in QUALITY_CHECKS:
            value = read_sql(sql).iloc[0, 0]
            observed = None if pd.isna(value) else float(value)
            op, threshold = rule.split()
            passed = observed is not None and {"=": observed == float(threshold), ">": observed > float(threshold),
                                               "<": observed < float(threshold)}[op]
            results.append({"check_name": name, "passed": passed, "observed": observed, "detail": f"expect {rule}"})
        copy_dataframe(pd.DataFrame(results), "data_quality_checks", "dw", truncate=False)
        failed = [r["check_name"] for r in results if not r["passed"]]
        stats.update(rows_out=len(results), rows_rejected=len(failed),
                     message=f"{len(results) - len(failed)}/{len(results)} checks passed"
                             + (f"; FAILED: {failed}" if failed else ""))
    if failed and fail_on_error:
        raise RuntimeError(f"Data quality checks failed: {failed}")
    return results
