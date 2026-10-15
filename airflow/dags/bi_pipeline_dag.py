"""Airflow DAG: raw files -> staging -> star schema -> quality gate -> mining models.

Each task runs one `run_pipeline.py` step inside the project's own virtualenv (BI_PYTHON),
because the pipeline needs SQLAlchemy 2 / pandas 2 while Airflow 2.x pins SQLAlchemy 1.4.
Tasks hand data to each other through PostgreSQL (staging / dw / mining schemas), never
XCom, so any task can be retried on its own.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.empty import EmptyOperator

PROJECT = os.getenv("BI_PROJECT_ROOT", "/opt/bi")
PYTHON = os.getenv("BI_PYTHON", "/opt/bi-venv/bin/python")


def step(task_id: str, command: str) -> BashOperator:
    return BashOperator(task_id=task_id, bash_command=f"cd {PROJECT} && {PYTHON} run_pipeline.py {command}")


default_args = {"owner": "bi", "retries": 1, "retry_delay": timedelta(minutes=5)}

with DAG(
    dag_id="bi_platform_pipeline",
    description="ETL into the PostgreSQL star schema, then segmentation, forecasting and basket analysis",
    schedule="0 2 * * *",  # nightly at 02:00
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["bi", "etl", "mining"],
) as dag:
    # Simulates the upstream exports landing. Replace with a sensor / copy task for real sources.
    land = step("land_raw_files", "generate")
    init = step("init_warehouse", "init")
    extract = step("extract_transform", "extract")
    load = step("load_star_schema", "load")
    quality = step("data_quality_gate", "quality")  # exits non-zero on a failed check -> models don't run
    models = [step("customer_segmentation", "segmentation"),
              step("sales_forecasting", "forecasting"),
              step("association_rules", "association")]
    done = EmptyOperator(task_id="dashboard_ready")

    land >> init >> extract >> load >> quality >> models >> done
