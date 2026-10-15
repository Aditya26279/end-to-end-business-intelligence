"""Pure cleaning / conforming functions (no I/O) - each source gets normalised to the staging contract."""
from __future__ import annotations

import pandas as pd

GENDER_MAP = {"m": "M", "male": "M", "f": "F", "female": "F"}
SALES_COLUMNS = ["source_system", "order_id", "line_number", "order_ts", "customer_id", "store_id", "sku",
                 "quantity", "unit_price", "discount_amount", "is_returned"]
REJECT_COLUMNS = ["source_system", "record_key", "reason", "raw_record"]


def _blank_to_na(s: pd.Series) -> pd.Series:
    s = s.astype("string").str.strip()
    return s.mask(s == "")


def parse_dates(s: pd.Series, formats: list[str]) -> pd.Series:
    """Try each explicit format in turn; never guess day/month order."""
    s = _blank_to_na(s)
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    for fmt in formats:
        out = out.fillna(pd.to_datetime(s, format=fmt, errors="coerce"))
    return out


def clean_customers(raw: pd.DataFrame, geo: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()
    for col in df.columns:
        df[col] = _blank_to_na(df[col])
    df["full_name"] = df["full_name"].str.split().str.join(" ").str.title()
    df["email"] = df["email"].str.lower()
    df["gender"] = df["gender"].str.lower().map(GENDER_MAP).fillna("U")
    df["birth_date"] = parse_dates(df["birth_date"], ["%Y-%m-%d", "%d/%m/%Y", "%b %d %Y"]).dt.date
    df["signup_date"] = parse_dates(df["signup_date"], ["%Y-%m-%d"]).dt.date
    df["city"] = df["city"].str.title()
    region = geo.assign(country=geo["country"].str.strip()).set_index("country")["region"]
    df["region"] = df["country"].map(region).fillna("Unknown")
    df = (df.dropna(subset=["customer_id"])
            .sort_values("signup_date")
            .drop_duplicates("customer_id", keep="last"))
    return df[["customer_id", "full_name", "email", "gender", "birth_date", "signup_date", "city", "country",
               "region"]].reset_index(drop=True)


def clean_products(payload: dict) -> pd.DataFrame:
    df = pd.json_normalize(payload["products"])
    df = df.rename(columns={"name": "product_name", "category.name": "category", "category.sub": "subcategory",
                            "pricing.list": "list_price", "pricing.cost": "unit_cost"})
    df["sku"] = _blank_to_na(df["sku"])
    df["product_name"] = df["product_name"].str.strip()
    for col in ["list_price", "unit_cost"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # A product without a key or with unusable prices is left out of the catalog; its sales are then
    # quarantined as "unknown SKU" instead of loading with a NULL / overflowing cost.
    ok = (df["sku"].notna() & df["list_price"].between(0, MAX_UNIT_PRICE)
          & df["unit_cost"].between(0, MAX_UNIT_PRICE))
    df = df[ok].drop_duplicates("sku", keep="last")
    return df[["sku", "product_name", "brand", "category", "subcategory", "list_price", "unit_cost"]] \
        .reset_index(drop=True)


def clean_stores(raw: pd.DataFrame, geo: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()
    for col in ["store_name", "channel", "city", "country"]:
        df[col] = _blank_to_na(df[col])
    df["region"] = df["country"].map(geo.set_index("country")["region"])
    df["opened_on"] = parse_dates(df["opened_on"], ["%Y-%m-%d"]).dt.date
    return df[["store_id", "store_name", "channel", "city", "country", "region", "opened_on"]]


# Bounds that keep every value inside the warehouse column types (int, numeric(10,2), numeric(12,2)).
MAX_QUANTITY = 10_000
MAX_UNIT_PRICE = 1_000_000


def _number(s: pd.Series, blank_as=None) -> pd.Series:
    """Parse numbers from text. Blank -> `blank_as`; anything unparseable -> NaN (caller rejects it)."""
    s = _blank_to_na(s)
    out = pd.to_numeric(s, errors="coerce").astype(float)
    if blank_as is not None:
        out = out.mask(s.isna(), blank_as)
    return out


def _is_whole(s: pd.Series) -> pd.Series:
    return s.notna() & (s == s.round())


def _validate(df: pd.DataFrame, raw: pd.DataFrame, valid_skus: set[str], source: str):
    """Split conformed rows into (clean, rejected).

    Every rule here guards something downstream: the staging primary key, integer / numeric column
    ranges, the "no negative net amount" quality gate, and surrogate-key lookups. A single bad
    row must be quarantined, never allowed to crash or poison the whole load.
    """
    gross = df["quantity"] * df["unit_price"]
    checks = [
        (df["order_id"].isna(), "missing order id"),
        (~_is_whole(df["line_number"]) | (df["line_number"] <= 0) | (df["line_number"] > 10_000),
         "invalid line number"),
        (df["order_ts"].isna(), "unparseable timestamp"),
        (df["quantity"].notna() & (df["quantity"] <= 0), "non-positive quantity"),
        (~_is_whole(df["quantity"]) | (df["quantity"] > MAX_QUANTITY), "invalid quantity"),
        (df["unit_price"].isna() | (df["unit_price"] <= 0) | (df["unit_price"] > MAX_UNIT_PRICE),
         "invalid unit price"),
        (df["discount_amount"].isna() | (df["discount_amount"] < 0) | (df["discount_amount"] > gross),
         "invalid discount"),
        (~df["sku"].isin(valid_skus), "unknown SKU"),
        (df["store_id"].isna(), "unknown store"),
    ]
    reason = pd.Series(pd.NA, index=df.index, dtype="string")
    for mask, why in checks:
        reason = reason.mask(reason.isna() & mask.fillna(True), why)

    # Duplicate extracts: among valid rows keep the LAST copy of a line - a re-extract carries
    # corrections (e.g. a late return), so the earlier copy is the stale one.
    valid = reason.isna()
    dup = df[valid].duplicated(["order_id", "line_number"], keep="last").reindex(df.index, fill_value=False)
    reason = reason.mask(dup, "duplicate line")

    bad = reason.notna()
    raw_bad = raw.loc[bad].astype(str)
    rejects = pd.DataFrame({
        "source_system": source,
        "record_key": df.loc[bad, "order_id"].astype(str) + "/" + df.loc[bad, "line_number"].astype(str),
        "reason": reason[bad],
        # agg(axis=1) on an empty frame returns a DataFrame, so build the column explicitly
        "raw_record": raw_bad.agg("|".join, axis=1) if len(raw_bad) else pd.Series(dtype="string"),
    }, index=df.index[bad])
    clean = df.loc[~bad, SALES_COLUMNS].reset_index(drop=True)
    clean = clean.astype({"line_number": "int64", "quantity": "int64"})
    return clean, rejects[REJECT_COLUMNS].reset_index(drop=True)


def clean_web_orders(raw: pd.DataFrame, valid_skus: set[str]):
    """E-commerce export: ISO timestamps, discount %, status incl. cancellations."""
    raw = raw.reset_index(drop=True)
    status = raw["status"].str.strip().str.lower()
    keep = status != "cancelled"                       # cancelled orders are not sales
    raw, status = raw[keep].reset_index(drop=True), status[keep].reset_index(drop=True)
    qty = _number(raw["qty"])
    price = _number(raw["unit_price"]).round(2)
    pct = _number(raw["discount_pct"], blank_as=0.0) / 100  # garbage text -> NaN -> rejected, not silently 0
    df = pd.DataFrame({
        "source_system": "web",
        "order_id": _blank_to_na(raw["order_id"]),
        "line_number": _number(raw["line_number"]),
        "order_ts": parse_dates(raw["order_ts"], ["%Y-%m-%dT%H:%M:%S"]),
        "customer_id": _blank_to_na(raw["customer_id"]),
        "store_id": raw["channel"].str.strip().str.lower().map({"online": "WEB", "mobile": "APP"}),
        "sku": raw["sku"].str.strip(),
        "quantity": qty,
        "unit_price": price,
        "discount_amount": (qty * price * pct).round(2),
        "is_returned": status == "returned",
    })
    clean, rejects = _validate(df, raw, valid_skus, "web")
    return clean, rejects, int((~keep).sum())


def clean_pos_orders(raw: pd.DataFrame, valid_skus: set[str], valid_stores: set[str]):
    """In-store POS export: different column names, dd/mm/yyyy dates, '$'-prefixed prices, Y/N returns."""
    raw = raw.reset_index(drop=True)
    store = raw["StoreCode"].str.strip()
    df = pd.DataFrame({
        "source_system": "pos",
        "order_id": _blank_to_na(raw["TransactionID"]),
        "line_number": _number(raw["LineNo"]),
        "order_ts": parse_dates(raw["Date"], ["%d/%m/%Y %H:%M"]),
        "customer_id": _blank_to_na(raw["CustomerNo"]),
        "store_id": store.where(store.isin(valid_stores)),
        "sku": raw["ItemCode"].str.strip(),
        "quantity": _number(raw["Quantity"]),
        "unit_price": _number(raw["Price"].str.replace(r"[$,\s]", "", regex=True)).round(2),
        "discount_amount": _number(raw["DiscountAmt"], blank_as=0.0).round(2),
        "is_returned": raw["Returned"].str.strip().str.upper().eq("Y"),
    })
    return _validate(df, raw, valid_skus, "pos")
