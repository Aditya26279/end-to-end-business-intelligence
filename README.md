# End-to-End Business Intelligence Platform

A complete BI system for a multi-channel retailer that covers both **data warehousing** and **data mining**:

```
raw files (CRM, catalog API, web shop, in-store POS)
   │  extract → clean/conform → validate (bad rows quarantined)
   ▼
PostgreSQL ── staging ──► dw (star schema) ──► mining (model outputs)
                              │                     ▲
                              └── segmentation · forecasting · association rules
   ▼
FastAPI + Chart.js dashboard with global filters
```

Orchestrated by **Airflow** (DAG in `airflow/dags/`) or by the plain-Python CLI `run_pipeline.py`.
Both run the same steps, and steps hand data over only through the database.

| Layer | Technology |
|---|---|
| ETL | Python 3, pandas, psycopg `COPY`, SQL upserts |
| Warehouse | PostgreSQL 16, Kimball star schema |
| Mining | scikit-learn (K-Means), statsmodels (Holt-Winters), mlxtend (FP-Growth) |
| Dashboard | FastAPI, vanilla JavaScript, Chart.js |
| Orchestration | Airflow 2.10 (Docker), or `run_pipeline.py` |

---

## Prerequisites

* Python 3.10 or newer (developed and tested on 3.12)
* For the full stack only: Docker with Docker Compose

No PostgreSQL installation is needed for local runs (see below).

## Quick start (no Docker, no Postgres install)

Windows:

```bash
python -m venv .venv
```
```bash
.venv\Scripts\pip install -r requirements.txt
```
```bash
.venv\Scripts\python run_pipeline.py all
```
```bash
.venv\Scripts\python run_pipeline.py serve
```

On macOS or Linux, use `.venv/bin/pip` and `.venv/bin/python` instead.

Open http://127.0.0.1:8000. A full rebuild takes about 25 seconds.

When `DATABASE_URL` is not set, the platform starts an **embedded PostgreSQL 16** (`pgserver`) whose data
lives in `./.pgdata`. It is real Postgres and keeps running between commands, so the pipeline and the dashboard
share it. `python run_pipeline.py db-url` prints its connection string for psql, DBeaver or similar tools.

To stop the embedded server (Windows; the path differs slightly on macOS and Linux):

```bash
.venv\Lib\site-packages\pgserver\pginstall\bin\pg_ctl.exe stop -D .pgdata
```

It starts again automatically on the next command.

## Configuration

All settings are environment variables. Local runs read them from the shell only. Docker Compose also reads
them from a `.env` file; copy `.env.example` to start.

| Variable | Used by | Purpose |
|---|---|---|
| `DATABASE_URL` | pipeline, dashboard | Any PostgreSQL, e.g. `postgresql+psycopg://bi:<password>@localhost:5432/bi`. Unset = embedded Postgres |
| `BI_DASHBOARD_PASSWORD` | dashboard | Turns on HTTP Basic auth. Required when serving on a non-loopback address |
| `BI_DASHBOARD_USER` | dashboard | Basic auth user name (default `admin`) |
| `BI_RAW_DIR` | pipeline | Where raw source files are written and read (default `data/raw`) |
| `POSTGRES_PASSWORD` | Docker Compose | Password for the warehouse database user `bi` |

## Full stack with Docker (Postgres + Airflow + dashboard)

Copy `.env.example` to `.env` and set `POSTGRES_PASSWORD` and `BI_DASHBOARD_PASSWORD`. Compose refuses to
start without them.

```bash
docker compose up -d --build
```

* **Airflow UI:** http://localhost:8080. The user is `admin`; the password is printed by `docker compose logs airflow`. Trigger the `bi_platform_pipeline` DAG; it is scheduled nightly at 02:00.
* **Dashboard:** http://localhost:8000, behind HTTP Basic auth with the credentials from `.env`. It shows data after the DAG's first successful run.

All ports are published on 127.0.0.1 only. Put a TLS reverse proxy in front before exposing anything further.

Airflow 2.x pins SQLAlchemy 1.4, but the pipeline needs SQLAlchemy 2 and pandas 2. The Airflow image
(`docker/airflow.Dockerfile`) therefore installs the pipeline into its own virtualenv, and the DAG runs each
step through a `BashOperator` in that environment.

---

## Pipeline commands

```bash
python run_pipeline.py <command>
```

| Command | What it does |
|---|---|
| `all` | generate raw data → reset → full ETL → quality checks → all three models (full rebuild) |
| `etl` | `init` + `extract` + `load` + `quality`. Idempotent: re-running adds no rows |
| `mine` | `segmentation` + `forecasting` + `association` |
| `generate` / `init` / `extract` / `load` / `quality` | single ETL steps (these are the Airflow tasks) |
| `segmentation` / `forecasting` / `association` | single model runs |
| `reset` | drop the `staging`, `dw` and `mining` schemas |
| `serve` | start the dashboard (`--host`, `--port`) |
| `db-url` | print the database connection string |

Options: `--customers N` and `--seed N` control the generated data. Every step writes a row to
`dw.etl_run_log`, which the dashboard's **Data pipeline** tab displays.

## 1. Raw sources (`bi/generate_raw.py`)

The generator writes deliberately messy data from four source systems that do not share a schema, so the ETL
has real work to do:

| File | Mimics | Problems the ETL must fix |
|---|---|---|
| `customers.csv` | CRM export | duplicate rows, inconsistent casing, stray whitespace, 3 date formats, `Male`/`f`/blank genders |
| `products.json` | catalog API | nested JSON |
| `orders_web.csv` | e-commerce DB | ISO timestamps, discount as %, cancelled orders, guest checkouts |
| `orders_pos.csv` | in-store POS | different column names, `dd/mm/yyyy` dates, `$1,299.50` prices, `Y/N` return flag |
| both order files | | duplicate extracts, zero/negative quantities, unknown SKUs |

The data also contains planted patterns for the models to find: customer archetypes, yearly growth with
Black Friday and Christmas peaks, and brand-ecosystem bundles (laptop → sleeve + mouse, espresso machine → beans).
The default output is about 6,000 customers, 37k orders and 86k order lines from Jan 2023 to Sep 2026. It is
deterministic for a given `--seed`.

## 2. ETL (`bi/etl/`)

* **Extract:** every file is read as text; typing is left to the transform step.
* **Transform** (`transform.py`, pure functions with unit tests): normalises names, emails and genders. Dates are
  parsed only against explicit formats, so day and month are never guessed. The two order sources are conformed
  to one contract.
* **Validation:** a bad row is quarantined in `staging.rejected_rows` with its reason and never crashes or poisons
  the load. That covers non-integer or out-of-range quantities, prices that would overflow the column types,
  discounts larger than the line, missing keys, unknown SKUs and stores. For duplicate extracts the **last** copy
  wins, because a re-extract carries corrections such as late returns.
* **Load:** all staging tables are bulk-loaded with `COPY` in a single transaction. `sql/02_load_dw.sql` then
  upserts the dimensions (SCD Type 1 on natural keys) and upserts facts on their natural key. Changed lines
  (late return, price fix, guest order whose customer later appears) are updated in place, keeping their
  historical unit cost. Guest checkouts map to an "Unknown" customer member (key -1).
* **Quality gate:** 7 SQL checks are recorded in `dw.data_quality_checks`, including staged-vs-fact
  reconciliation, revenue reconciliation, reject rate, unknown-customer share and negative amounts. A failed
  check stops the run, so the models never train on bad data.

## 3. Warehouse: star schema (`sql/01_schema.sql`)

```
                 dim_date (date_key YYYYMMDD, month, quarter, year, is_weekend, is_peak_season)
                     │
dim_customer ── fact_sales ── dim_product
(age_band,       (grain: one     (category, subcategory,
 region, ...)    order line)      brand, price_band)
                     │
                 dim_store (channel: Retail / Online / Mobile, region)
```

`fact_sales` measures: quantity, unit price, gross / discount / net amount, cost, profit, returned flag.
`dw.v_sales` is the semantic view that the dashboard queries. The database has three schemas:

| Schema | Contents |
|---|---|
| `staging` | cleaned source rows from the latest extract, plus rejected rows |
| `dw` | dimensions, the sales fact, run log and quality-check history |
| `mining` | segments, segment profiles, forecasts, association rules and model-run metrics |

## 4. Mining models (`bi/mining/`, results in the `mining` schema)

| Model | Method | Result on the generated data |
|---|---|---|
| **Customer segmentation** | RFM features (log-scaled, standardised) → K-Means, with k chosen by silhouette score. Clusters are named from their centroids against the customer distribution, never hard-coded | k = 5: Champions, Can't Lose Them, At Risk, New & Promising, Hibernating. About 28% of customers produce about 79% of revenue |
| **Sales forecasting** | Holt-Winters per series (total + 5 categories): damped additive trend, multiplicative 12-month seasonality, 95% interval. Backtested on a 6-month holdout against a seasonal-naive baseline | Total revenue MAPE **5.1%** vs **12.4%** for the baseline |
| **Association rules** | FP-Growth on order baskets at product and subcategory level; rules kept when lift ≥ 1.2 | 461 product rules and 91 subcategory rules. They recover the planted bundles, e.g. *Phone Case + Screen Protector → Smartphone* (lift 32.7) and *Coffee Beans → Espresso Machine* (lift 11.6) |

Each run logs its parameters and metrics to `mining.model_runs`.

## 5. Dashboard (`bi/dashboard/`)

FastAPI serves the JSON API and a single-page front end (vanilla JS + Chart.js) with light and dark themes.

* **Global filters:** period presets or a custom range, region, channel, category and segment. They apply to every chart on the Sales and Customers tabs.
* **Sales overview:** KPI tiles with change vs the prior period of equal length, monthly revenue and profit, breakdowns by category, region, channel and age band, and top products.
* **Customer segments:** segment profiles with recommended actions, customer share vs revenue share, an R×F value grid, and the top customers per segment.
* **Forecast:** actuals, model fit and forecast with a 95% band per series, plus backtest metrics.
* **Market basket:** rules browser with level, category, minimum lift, minimum confidence and item search.
* **Data pipeline:** run log, quality checks, rejected rows by reason and table row counts.

Interactive API docs are at http://127.0.0.1:8000/docs.

## Testing

```bash
.venv\Scripts\python -m pytest -q tests
```

23 tests, no database needed. They cover the cleaning functions, validation and quarantine rules, duplicate
handling, segment naming, RFM scoring, forecast metrics, empty rule sets and the dashboard's auth check.
`tests/test_regressions.py` holds one test per bug fixed during review.

## Security notes

* **Dashboard auth:** set `BI_DASHBOARD_PASSWORD` (and optionally `BI_DASHBOARD_USER`) to require HTTP Basic auth.
  `serve` refuses to listen on a non-loopback address without it, because the dashboard shows customer names.
  Basic auth sends credentials on every request, so use it behind TLS.
* **Headers:** responses carry a strict Content-Security-Policy, `nosniff`, `X-Frame-Options: DENY` and
  `no-referrer`. API responses are `no-store`. Chart.js is pinned with a Subresource Integrity hash.
* **SQL:** every user value is a bound parameter; identifiers are allow-listed or quoted by psycopg.
* **Embedded Postgres (local mode):** `pgserver` listens on 127.0.0.1 only, but with passwordless `trust` auth,
  so any process on this machine can connect. It is for single-user development only; use `DATABASE_URL`
  with a password-protected server anywhere else.
* **Secrets:** `.env` is git-ignored; only `.env.example` with placeholder values is committed.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard says the warehouse is empty or not ready (HTTP 503) | Run `python run_pipeline.py all` |
| Port 8000 is already in use | `python run_pipeline.py serve --port 8050` |
| Data looks inconsistent after changing `--seed` or `--customers` | Use `all`, which resets the warehouse; `etl` alone merges into existing data |
| Embedded database will not start after moving the project folder | Stop the old server (see Quick start), or delete `.pgdata/` and run `all` |
| `serve --host 0.0.0.0` exits with "Refusing to serve customer data" | Set `BI_DASHBOARD_PASSWORD` first |
| Run log shows a failed `quality_checks` step | Open the **Data pipeline** tab to see which check failed and why |

## Known limitations

* Order lines deleted at the source stay in the warehouse. The right handling depends on whether extracts are
  full snapshots or incremental, so deletes are not propagated.
* The Docker/Airflow stack has not been run end to end. The local pipeline and dashboard are tested.
* Dimensions are SCD Type 1 (history is overwritten), so a customer who moves region is reported under the
  new region for all past sales.

## Project layout

```
bi/
  config.py, db.py          connection (DATABASE_URL or embedded Postgres), COPY helpers
  generate_raw.py           synthetic messy source data
  etl/transform.py          pure cleaning and validation functions
  etl/pipeline.py           init / extract_transform / load_warehouse / quality_checks
  etl/runlog.py             audit logging to dw.etl_run_log
  mining/segmentation.py    RFM + K-Means
  mining/forecasting.py     Holt-Winters with backtest
  mining/association.py     FP-Growth rules
  dashboard/app.py          FastAPI endpoints, auth and security headers
  dashboard/static/         index.html, app.js, style.css
sql/01_schema.sql           staging / dw / mining DDL + v_sales view
sql/02_load_dw.sql          staging → star schema load
airflow/dags/               Airflow DAG
docker/, docker-compose.yml Postgres + Airflow + dashboard
tests/                      pytest tests
run_pipeline.py             CLI orchestrator
requirements.txt            Python dependencies
.env.example                configuration template
```
