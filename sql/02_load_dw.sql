-- =====================================================================
--  Staging -> star schema. Dimensions are SCD Type 1 upserts on the
--  natural key; the fact is insert-only on its natural key, so re-running
--  the ETL is idempotent.
-- =====================================================================

-- dim_date: cover every year in the data plus one more (forecast horizon)
INSERT INTO dw.dim_date
SELECT to_char(d, 'YYYYMMDD')::int,
       d::date,
       extract(day FROM d),
       extract(isodow FROM d),
       to_char(d, 'FMDay'),
       extract(week FROM d),
       extract(month FROM d),
       to_char(d, 'FMMonth'),
       extract(quarter FROM d),
       extract(year FROM d),
       to_char(d, 'YYYY-MM'),
       extract(isodow FROM d) IN (6, 7),
       (extract(month FROM d) = 11 AND extract(day FROM d) >= 23)
         OR (extract(month FROM d) = 12 AND extract(day FROM d) <= 24)
FROM generate_series(
        (SELECT date_trunc('year', min(order_ts)) FROM staging.sales),
        (SELECT date_trunc('year', max(order_ts)) + interval '2 years' - interval '1 day' FROM staging.sales),
        interval '1 day') AS d
ON CONFLICT (date_key) DO NOTHING;

INSERT INTO dw.dim_customer AS t
       (customer_id, full_name, email, gender, birth_date, age_band, city, country, region, signup_date)
SELECT customer_id, full_name, email, gender, birth_date,
       CASE WHEN birth_date IS NULL THEN 'Unknown'
            WHEN age(birth_date) < interval '25 years' THEN '18-24'
            WHEN age(birth_date) < interval '35 years' THEN '25-34'
            WHEN age(birth_date) < interval '45 years' THEN '35-44'
            WHEN age(birth_date) < interval '55 years' THEN '45-54'
            WHEN age(birth_date) < interval '65 years' THEN '55-64'
            ELSE '65+' END,
       city, country, region, signup_date
FROM staging.customers
ON CONFLICT (customer_id) DO UPDATE SET
       full_name = EXCLUDED.full_name, email = EXCLUDED.email, gender = EXCLUDED.gender,
       birth_date = EXCLUDED.birth_date, age_band = EXCLUDED.age_band, city = EXCLUDED.city,
       country = EXCLUDED.country, region = EXCLUDED.region, signup_date = EXCLUDED.signup_date,
       updated_at = now()
WHERE (t.full_name, t.email, t.gender, t.birth_date, t.age_band, t.city, t.country, t.region, t.signup_date)
      IS DISTINCT FROM
      (EXCLUDED.full_name, EXCLUDED.email, EXCLUDED.gender, EXCLUDED.birth_date, EXCLUDED.age_band, EXCLUDED.city,
       EXCLUDED.country, EXCLUDED.region, EXCLUDED.signup_date);  -- age_band included so customers age over time

INSERT INTO dw.dim_product AS t
       (sku, product_name, brand, category, subcategory, list_price, unit_cost, price_band)
SELECT sku, product_name, brand, category, subcategory, list_price, unit_cost,
       CASE WHEN list_price < 25 THEN 'Under $25'
            WHEN list_price < 100 THEN '$25-99'
            WHEN list_price < 500 THEN '$100-499'
            ELSE '$500+' END
FROM staging.products
ON CONFLICT (sku) DO UPDATE SET
       product_name = EXCLUDED.product_name, brand = EXCLUDED.brand, category = EXCLUDED.category,
       subcategory = EXCLUDED.subcategory, list_price = EXCLUDED.list_price,
       unit_cost = EXCLUDED.unit_cost, price_band = EXCLUDED.price_band, updated_at = now()
WHERE (t.product_name, t.brand, t.category, t.subcategory, t.list_price, t.unit_cost)
      IS DISTINCT FROM
      (EXCLUDED.product_name, EXCLUDED.brand, EXCLUDED.category, EXCLUDED.subcategory,
       EXCLUDED.list_price, EXCLUDED.unit_cost);

INSERT INTO dw.dim_store AS t (store_id, store_name, channel, city, country, region, opened_on)
SELECT store_id, store_name, channel, city, country, region, opened_on
FROM staging.stores
ON CONFLICT (store_id) DO UPDATE SET
       store_name = EXCLUDED.store_name, channel = EXCLUDED.channel, city = EXCLUDED.city,
       country = EXCLUDED.country, region = EXCLUDED.region, opened_on = EXCLUDED.opened_on,
       updated_at = now()
WHERE (t.store_name, t.channel, t.city, t.country, t.region, t.opened_on)
      IS DISTINCT FROM
      (EXCLUDED.store_name, EXCLUDED.channel, EXCLUDED.city, EXCLUDED.country, EXCLUDED.region,
       EXCLUDED.opened_on);

-- fact: surrogate-key lookups; cost is snapshotted from the product when a line is first loaded.
-- Re-extracted lines that changed at source (late return, price fix, guest order whose customer
-- later appears in the CRM) are updated in place; their historical unit cost is kept.
INSERT INTO dw.fact_sales AS t
       (source_system, order_id, line_number, date_key, customer_key, product_key, store_key, order_ts,
        quantity, unit_price, gross_amount, discount_amount, net_amount, cost_amount, profit, is_returned)
SELECT s.source_system, s.order_id, s.line_number,
       to_char(s.order_ts, 'YYYYMMDD')::int,
       COALESCE(c.customer_key, -1),
       p.product_key,
       st.store_key,
       s.order_ts,
       s.quantity,
       s.unit_price,
       round(s.quantity * s.unit_price, 2),
       s.discount_amount,
       round(s.quantity * s.unit_price - s.discount_amount, 2),
       round(s.quantity * p.unit_cost, 2),
       round(s.quantity * s.unit_price - s.discount_amount - s.quantity * p.unit_cost, 2),
       s.is_returned
FROM staging.sales s
JOIN dw.dim_product p ON p.sku = s.sku
JOIN dw.dim_store st  ON st.store_id = s.store_id
LEFT JOIN dw.dim_customer c ON c.customer_id = s.customer_id
ON CONFLICT (source_system, order_id, line_number) DO UPDATE SET
       date_key        = EXCLUDED.date_key,
       customer_key    = EXCLUDED.customer_key,
       product_key     = EXCLUDED.product_key,
       store_key       = EXCLUDED.store_key,
       order_ts        = EXCLUDED.order_ts,
       quantity        = EXCLUDED.quantity,
       unit_price      = EXCLUDED.unit_price,
       gross_amount    = EXCLUDED.gross_amount,
       discount_amount = EXCLUDED.discount_amount,
       net_amount      = EXCLUDED.net_amount,
       cost_amount     = CASE WHEN EXCLUDED.product_key = t.product_key AND t.quantity > 0
                              THEN round(EXCLUDED.quantity * t.cost_amount / t.quantity, 2)
                              ELSE EXCLUDED.cost_amount END,
       profit          = EXCLUDED.net_amount
                         - CASE WHEN EXCLUDED.product_key = t.product_key AND t.quantity > 0
                                THEN round(EXCLUDED.quantity * t.cost_amount / t.quantity, 2)
                                ELSE EXCLUDED.cost_amount END,
       is_returned     = EXCLUDED.is_returned,
       loaded_at       = now()
WHERE (t.date_key, t.customer_key, t.product_key, t.store_key, t.order_ts, t.quantity, t.unit_price,
       t.discount_amount, t.is_returned)
      IS DISTINCT FROM
      (EXCLUDED.date_key, EXCLUDED.customer_key, EXCLUDED.product_key, EXCLUDED.store_key, EXCLUDED.order_ts,
       EXCLUDED.quantity, EXCLUDED.unit_price, EXCLUDED.discount_amount, EXCLUDED.is_returned);

ANALYZE dw.fact_sales;
