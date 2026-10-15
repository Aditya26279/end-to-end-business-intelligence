"""Regression tests for bugs found in review: each test failed (crashed or gave a wrong answer) before its fix."""
import base64
import json

import pandas as pd
import pytest

from bi.etl import transform as T
from bi.mining import association, forecasting
from bi.mining.segmentation import _quintile

SKUS = {"SKU-0001"}


def web_row(**over):
    row = dict(order_id="W1", line_number="1", order_ts="2024-01-01T10:00:00", customer_id="C1", channel="online",
               sku="SKU-0001", qty="1", unit_price="10", discount_pct="0", status="completed")
    row.update(over)
    return row


def web(*rows):
    return T.clean_web_orders(pd.DataFrame(list(rows)), SKUS)


def test_clean_extract_with_zero_rejects_does_not_crash():
    clean, rejects, _ = web(web_row(), web_row(order_id="W2"))
    assert len(clean) == 2 and rejects.empty
    assert list(rejects.columns) == T.REJECT_COLUMNS


@pytest.mark.parametrize("override, reason", [
    ({"qty": "1.5"}, "invalid quantity"),
    ({"qty": "abc"}, "invalid quantity"),
    ({"qty": "999999999999"}, "invalid quantity"),       # would overflow the int column
    ({"unit_price": "1e12"}, "invalid unit price"),        # would overflow numeric(10,2)
    ({"discount_pct": "150"}, "invalid discount"),        # negative net amount -> quality gate failure
    ({"discount_pct": "-5"}, "invalid discount"),
    ({"discount_pct": "abc"}, "invalid discount"),        # used to become 0 silently
    ({"line_number": "x"}, "invalid line number"),        # NULL primary key -> COPY failure
    ({"order_id": " "}, "missing order id"),
])
def test_bad_values_are_quarantined_not_crashing(override, reason):
    clean, rejects, _ = web(web_row(**override), web_row(order_id="W9"))
    assert list(clean.order_id) == ["W9"]
    assert list(rejects.reason) == [reason]


def test_reextracted_correction_wins_over_stale_copy():
    clean, rejects, _ = web(web_row(), web_row(status="returned"))
    assert len(clean) == 1 and clean.is_returned.iloc[0]
    assert list(rejects.reason) == ["duplicate line"]


def test_valid_copy_survives_an_invalid_duplicate():
    clean, rejects, _ = web(web_row(qty="0"), web_row())
    assert len(clean) == 1 and list(rejects.reason) == ["non-positive quantity"]


def test_pos_blank_discount_is_zero_but_garbage_is_rejected():
    base = {"TransactionID": "P1", "LineNo": "1", "Date": "05/03/2024 14:30", "StoreCode": "ST01",
            "CustomerNo": "C1", "ItemCode": "SKU-0001", "Quantity": "1", "Price": "10", "Returned": "N"}
    raw = pd.DataFrame([{**base, "DiscountAmt": ""}, {**base, "TransactionID": "P2", "DiscountAmt": "n/a"}])
    clean, rejects = T.clean_pos_orders(raw, SKUS, {"ST01"})
    assert list(clean.order_id) == ["P1"] and clean.discount_amount.iloc[0] == 0
    assert list(rejects.reason) == ["invalid discount"]


def test_products_with_unusable_prices_are_left_out():
    payload = {"products": [
        {"sku": "A", "name": "ok", "brand": "b", "category": {"name": "c", "sub": "s"}, "pricing": {"list": 10, "cost": 5}},
        {"sku": "B", "name": "bad", "brand": "b", "category": {"name": "c", "sub": "s"}, "pricing": {"list": "?", "cost": 5}},
        {"sku": "", "name": "nokey", "brand": "b", "category": {"name": "c", "sub": "s"}, "pricing": {"list": 1, "cost": 1}},
    ]}
    assert list(T.clean_products(payload).sku) == ["A"]


def test_mape_never_produces_values_jsonb_rejects():
    assert forecasting._mape([0, 10], [1, 10]) == 0.0      # zero month skipped, not inf
    assert forecasting._mape([0, 0], [1, 1]) is None
    assert forecasting._mape([10, float("nan")], [10, 5]) == 0.0
    json.dumps({"m": forecasting._mape([0], [1])}, allow_nan=False)


def test_association_with_no_rules_returns_typed_empty_frame():
    lines = pd.DataFrame({"basket": ["a", "b"], "product_name": ["x", "y"], "subcategory": ["s", "t"],
                          "category": ["c", "c"]})
    rules, n = association.mine(lines, "product_name", 0.5, 0.5)
    out = association._format(rules, "product", {})
    assert n == 0 and list(out.columns) == association.RULE_COLUMNS


def test_rfm_scores_give_ties_the_same_score():
    freq = pd.Series([1] * 60 + [2] * 20 + [5] * 10 + [9] * 10)
    scores = _quintile(freq)
    assert scores[freq == 1].nunique() == 1          # rank(method="first") split these into 3 scores
    assert scores.between(1, 5).all()
    assert scores[freq == 9].iloc[0] == 5
    recency = pd.Series([1, 1, 50, 300, 300])
    r = _quintile(recency, reverse=True)
    assert r.iloc[0] == r.iloc[1] == 5 and r.iloc[3] == r.iloc[4] < r.iloc[2]


def test_basic_auth_check(monkeypatch):
    from bi.dashboard import app as dash
    monkeypatch.setattr(dash, "AUTH_USER", "admin")
    monkeypatch.setattr(dash, "AUTH_PASSWORD", "s3cret")
    hdr = lambda s: "Basic " + base64.b64encode(s.encode()).decode()
    assert dash._authorized(hdr("admin:s3cret"))
    assert not dash._authorized(hdr("admin:wrong"))
    assert not dash._authorized(hdr("root:s3cret"))
    assert not dash._authorized("Basic !!!not-base64")
    assert not dash._authorized(None)
