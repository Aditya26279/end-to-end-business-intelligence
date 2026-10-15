"""Unit tests for the pure cleaning functions (no database needed)."""
import pandas as pd

from bi.etl import transform as T
from bi.mining.segmentation import name_clusters

GEO = pd.DataFrame({"country": ["United States", "Germany"], "region": ["North America", "Europe"]})
SKUS = {"SKU-0001", "SKU-0002"}


def test_parse_dates_never_swaps_day_and_month():
    s = pd.Series(["2024-03-05", "05/03/2024", "Mar 05 2024", "", "garbage"])
    out = T.parse_dates(s, ["%Y-%m-%d", "%d/%m/%Y", "%b %d %Y"])
    assert (out.iloc[:3] == pd.Timestamp("2024-03-05")).all()
    assert out.iloc[3:].isna().all()


def test_clean_customers_normalises_and_dedupes():
    raw = pd.DataFrame({
        "customer_id": ["C1", "C1", "C2"],
        "full_name": ["  ada   LOVELACE ", "Ada Lovelace", "ALAN TURING"],
        "email": ["ADA@X.COM", "ada@x.com", "alan@x.com"],
        "gender": ["female", "F", ""],
        "birth_date": ["10/12/1990", "1990-12-10", "Jun 23 1985"],
        "signup_date": ["2023-01-01", "2024-01-01", "2023-05-05"],
        "city": ["BERLIN", "Berlin", ""],
        "country": ["Germany", "Germany", "Atlantis"],
    })
    out = T.clean_customers(raw, GEO).set_index("customer_id")
    assert len(out) == 2
    assert out.loc["C1", "full_name"] == "Ada Lovelace"
    assert out.loc["C1", "email"] == "ada@x.com"
    assert out.loc["C1", "gender"] == "F" and out.loc["C2", "gender"] == "U"
    assert str(out.loc["C1", "birth_date"]) == "1990-12-10"
    assert out.loc["C1", "region"] == "Europe" and out.loc["C2", "region"] == "Unknown"
    assert pd.isna(out.loc["C2", "city"])


def test_web_orders_drop_cancelled_and_reject_bad_rows():
    raw = pd.DataFrame({
        "order_id": ["W1", "W1", "W2", "W3", "W4", "W1"],
        "line_number": ["1", "2", "1", "1", "1", "1"],
        "order_ts": ["2024-01-01T10:00:00"] * 6,
        "customer_id": ["C1", "C1", "", "C2", "C3", "C1"],
        "channel": ["online", "online", "mobile", "online", "online", "online"],
        "sku": ["SKU-0001", "SKU-9999", "SKU-0002", "SKU-0001", "SKU-0001", "SKU-0001"],
        "qty": ["2", "1", "1", "0", "1", "2"],
        "unit_price": ["10.00", "5", "20", "10", "10", "10.00"],
        "discount_pct": ["10", "0", "0", "0", "0", "10"],
        "status": ["completed", "completed", "returned", "completed", "cancelled", "completed"],
    })
    clean, rejects, cancelled = T.clean_web_orders(raw, SKUS)
    assert cancelled == 1
    assert sorted(rejects.reason) == ["duplicate line", "non-positive quantity", "unknown SKU"]
    clean = clean.set_index("order_id")
    assert sorted(clean.index) == ["W1", "W2"]
    w1 = clean.loc["W1"]
    assert w1.store_id == "WEB" and w1.discount_amount == 2.0 and not w1.is_returned
    w2 = clean.loc["W2"]
    assert w2.store_id == "APP" and w2.is_returned and pd.isna(w2.customer_id)


def test_pos_orders_parse_dollar_prices_and_ddmm_dates():
    raw = pd.DataFrame({
        "TransactionID": ["P1", "P2"], "LineNo": ["1", "1"],
        "Date": ["05/03/2024 14:30", "06/03/2024 09:00"], "StoreCode": ["ST01", "ST99"],
        "CustomerNo": ["C1", "C2"], "ItemCode": ["SKU-0001", "SKU-0002"], "Quantity": ["1", "1"],
        "Price": ["$1,299.50", "10"], "DiscountAmt": ["0.0", "0"], "Returned": ["Y", "N"],
    })
    clean, rejects = T.clean_pos_orders(raw, SKUS, {"ST01"})
    assert len(clean) == 1 and list(rejects.reason) == ["unknown store"]
    row = clean.iloc[0]
    assert row.unit_price == 1299.50 and row.is_returned
    assert row.order_ts == pd.Timestamp("2024-03-05 14:30")


def test_segment_names_follow_customer_distribution():
    customers = pd.DataFrame({"recency_days": [10, 20, 300, 400, 500, 30],
                              "frequency": [20, 15, 18, 1, 1, 1],
                              "monetary": [9000, 5000, 8000, 50, 40, 60]})
    centroids = pd.DataFrame({"recency_days": [15, 350, 450, 30], "frequency": [17, 18, 1, 1],
                              "monetary": [7500, 8000, 45, 60]})
    names = name_clusters(centroids, customers)
    assert names == {0: "Champions", 1: "Can't Lose Them", 2: "Hibernating", 3: "New & Promising"}
