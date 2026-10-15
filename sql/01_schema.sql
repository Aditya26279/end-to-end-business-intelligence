-- =====================================================================
--  Warehouse DDL (idempotent). Three layers:
--    staging : cleaned, conformed source rows (truncated each ETL run)
--    dw      : Kimball star schema (conformed dimensions + sales fact)
--    mining  : outputs of the data-mining models, read by the dashboard
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS dw;
CREATE SCHEMA IF NOT EXISTS mining;

-- ---------------------------------------------------------------- staging
CREATE TABLE IF NOT EXISTS staging.customers (
    customer_id  text PRIMARY KEY,
    full_name    text,
    email        text,
    gender       char(1),
    birth_date   date,
    signup_date  date,
    city         text,
    country      text,
    region       text
);

CREATE TABLE IF NOT EXISTS staging.products (
    sku          text PRIMARY KEY,
    product_name text,
    brand        text,
    category     text,
    subcategory  text,
    list_price   numeric(10,2),
    unit_cost    numeric(10,2)
);

CREATE TABLE IF NOT EXISTS staging.stores (
    store_id     text PRIMARY KEY,
    store_name   text,
    channel      text,
    city         text,
    country      text,
    region       text,
    opened_on    date
);

CREATE TABLE IF NOT EXISTS staging.sales (
    source_system   text,
    order_id        text,
    line_number     int,
    order_ts        timestamp,
    customer_id     text,
    store_id        text,
    sku             text,
    quantity        int,
    unit_price      numeric(10,2),
    discount_amount numeric(12,2),
    is_returned     boolean,
    PRIMARY KEY (source_system, order_id, line_number)
);

CREATE TABLE IF NOT EXISTS staging.rejected_rows (
    source_system text,
    record_key    text,
    reason        text,
    raw_record    text
);

-- ---------------------------------------------------------------- dimensions
CREATE TABLE IF NOT EXISTS dw.dim_date (
    date_key       int PRIMARY KEY,          -- YYYYMMDD
    full_date      date NOT NULL UNIQUE,
    day_of_month   smallint,
    day_of_week    smallint,                 -- ISO: 1 = Monday
    day_name       text,
    week_of_year   smallint,
    month          smallint,
    month_name     text,
    quarter        smallint,
    year           smallint,
    year_month     text,                     -- 'YYYY-MM'
    is_weekend     boolean,
    is_peak_season boolean                   -- Black Friday through Christmas Eve
);

CREATE TABLE IF NOT EXISTS dw.dim_customer (
    customer_key serial PRIMARY KEY,
    customer_id  text NOT NULL UNIQUE,       -- natural key from CRM
    full_name    text,
    email        text,
    gender       char(1),
    birth_date   date,
    age_band     text,
    city         text,
    country      text,
    region       text,
    signup_date  date,
    updated_at   timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dw.dim_product (
    product_key  serial PRIMARY KEY,
    sku          text NOT NULL UNIQUE,
    product_name text,
    brand        text,
    category     text,
    subcategory  text,
    list_price   numeric(10,2),
    unit_cost    numeric(10,2),
    price_band   text,
    updated_at   timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dw.dim_store (
    store_key    serial PRIMARY KEY,
    store_id     text NOT NULL UNIQUE,
    store_name   text,
    channel      text,                       -- Retail / Online / Mobile
    city         text,
    country      text,
    region       text,
    opened_on    date,
    updated_at   timestamptz DEFAULT now()
);

-- "Unknown" member so facts never lose rows to missing references (guest checkouts)
INSERT INTO dw.dim_customer (customer_key, customer_id, full_name, age_band, city, country, region)
VALUES (-1, 'UNKNOWN', 'Guest / Unknown', 'Unknown', 'Unknown', 'Unknown', 'Unknown')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------- fact
-- Grain: one row per order line.
CREATE TABLE IF NOT EXISTS dw.fact_sales (
    sales_id        bigserial PRIMARY KEY,
    source_system   text NOT NULL,
    order_id        text NOT NULL,
    line_number     int  NOT NULL,
    date_key        int  NOT NULL REFERENCES dw.dim_date (date_key),
    customer_key    int  NOT NULL REFERENCES dw.dim_customer (customer_key),
    product_key     int  NOT NULL REFERENCES dw.dim_product (product_key),
    store_key       int  NOT NULL REFERENCES dw.dim_store (store_key),
    order_ts        timestamp,
    quantity        int  NOT NULL,
    unit_price      numeric(10,2),
    gross_amount    numeric(12,2),
    discount_amount numeric(12,2),
    net_amount      numeric(12,2),
    cost_amount     numeric(12,2),
    profit          numeric(12,2),
    is_returned     boolean NOT NULL DEFAULT false,
    loaded_at       timestamptz DEFAULT now(),
    UNIQUE (source_system, order_id, line_number)
);
CREATE INDEX IF NOT EXISTS ix_fact_sales_date     ON dw.fact_sales (date_key);
CREATE INDEX IF NOT EXISTS ix_fact_sales_customer ON dw.fact_sales (customer_key);
CREATE INDEX IF NOT EXISTS ix_fact_sales_product  ON dw.fact_sales (product_key);
CREATE INDEX IF NOT EXISTS ix_fact_sales_store    ON dw.fact_sales (store_key);

-- ---------------------------------------------------------------- audit
CREATE TABLE IF NOT EXISTS dw.etl_run_log (
    run_id        serial PRIMARY KEY,
    step          text NOT NULL,
    status        text NOT NULL,             -- running / success / failed
    started_at    timestamptz DEFAULT now(),
    finished_at   timestamptz,
    rows_in       int,
    rows_out      int,
    rows_rejected int,
    message       text
);

CREATE TABLE IF NOT EXISTS dw.data_quality_checks (
    check_id    serial PRIMARY KEY,
    checked_at  timestamptz DEFAULT now(),
    check_name  text,
    passed      boolean,
    observed    numeric,
    detail      text
);

-- ---------------------------------------------------------------- mining outputs
CREATE TABLE IF NOT EXISTS mining.customer_segments (
    customer_key    int PRIMARY KEY,
    recency_days    int,
    frequency       int,
    monetary        numeric(12,2),
    avg_order_value numeric(12,2),
    tenure_days     int,
    r_score         smallint,
    f_score         smallint,
    m_score         smallint,
    rfm_code        text,
    cluster_id      smallint,
    segment         text,
    scored_at       timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mining.segment_profiles (
    segment         text PRIMARY KEY,
    cluster_id      smallint,
    customers       int,
    share_customers numeric(6,4),
    avg_recency     numeric(10,1),
    avg_frequency   numeric(10,2),
    avg_monetary    numeric(12,2),
    total_revenue   numeric(14,2),
    share_revenue   numeric(6,4),
    action          text
);

CREATE TABLE IF NOT EXISTS mining.sales_forecast (
    series      text,                        -- 'All' or a category
    month       date,
    actual      numeric(14,2),
    fitted      numeric(14,2),
    forecast    numeric(14,2),
    lower_95    numeric(14,2),
    upper_95    numeric(14,2),
    PRIMARY KEY (series, month)
);

CREATE TABLE IF NOT EXISTS mining.association_rules (
    rule_id             serial PRIMARY KEY,
    level               text,                -- 'product' or 'subcategory'
    antecedents         text,
    consequents         text,
    antecedent_category text,
    n_items             smallint,
    support             numeric(8,5),
    confidence          numeric(8,5),
    lift                numeric(10,4),
    leverage            numeric(10,6),
    conviction          numeric(12,4),
    basket_count        int
);

CREATE TABLE IF NOT EXISTS mining.model_runs (
    run_id   serial PRIMARY KEY,
    model    text,
    run_at   timestamptz DEFAULT now(),
    params   jsonb,
    metrics  jsonb
);

-- ---------------------------------------------------------------- semantic view
CREATE OR REPLACE VIEW dw.v_sales AS
SELECT f.sales_id, f.source_system, f.order_id, f.line_number, f.order_ts,
       d.full_date, d.year, d.quarter, d.month, d.year_month, d.day_name, d.is_weekend, d.is_peak_season,
       c.customer_key, c.customer_id, c.full_name AS customer_name, c.gender, c.age_band,
       c.region AS customer_region, c.country AS customer_country,
       p.product_key, p.sku, p.product_name, p.brand, p.category, p.subcategory, p.price_band,
       s.store_key, s.store_id, s.store_name, s.channel,
       COALESCE(NULLIF(s.region, ''), c.region) AS sales_region,
       f.quantity, f.unit_price, f.gross_amount, f.discount_amount, f.net_amount, f.cost_amount,
       f.profit, f.is_returned
FROM dw.fact_sales f
JOIN dw.dim_date d     ON d.date_key = f.date_key
JOIN dw.dim_customer c ON c.customer_key = f.customer_key
JOIN dw.dim_product p  ON p.product_key = f.product_key
JOIN dw.dim_store s    ON s.store_key = f.store_key;
