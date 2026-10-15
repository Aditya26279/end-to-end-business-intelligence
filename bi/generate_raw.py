"""Generate realistic, deliberately messy raw source data for the platform.

Sources produced in data/raw/ (each mimics a different upstream system):
  customers.csv     CRM export      - duplicates, inconsistent casing, mixed date formats, blanks
  products.json     catalog API     - nested JSON
  stores.csv        store master
  geo_regions.csv   reference data  - country -> sales region
  orders_web.csv    e-commerce DB   - ISO timestamps, discount as a percentage, status column
  orders_pos.csv    in-store POS    - different column names, dd/mm/yyyy dates, "$" prices, Y/N returns

Planted patterns the mining layer should rediscover:
  * customer archetypes (champions ... one-timers)      -> segmentation
  * yearly growth, Q4/Black-Friday seasonality, weekends -> forecasting
  * brand-ecosystem bundles (laptop -> sleeve + mouse)    -> association rules
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from bi.config import RAW_DIR

START = date(2023, 1, 1)          # first day of transaction history
END = date(2026, 9, 30)
CRM_START = date(2020, 1, 1)      # customers may pre-date the transaction history

GEO = [  # city, country, region
    ("New York", "United States", "North America"),
    ("Chicago", "United States", "North America"),
    ("Los Angeles", "United States", "North America"),
    ("Toronto", "Canada", "North America"),
    ("London", "United Kingdom", "Europe"),
    ("Manchester", "United Kingdom", "Europe"),
    ("Berlin", "Germany", "Europe"),
    ("Paris", "France", "Europe"),
    ("Madrid", "Spain", "Europe"),
    ("Tokyo", "Japan", "Asia Pacific"),
    ("Singapore", "Singapore", "Asia Pacific"),
    ("Sydney", "Australia", "Asia Pacific"),
    ("Dubai", "United Arab Emirates", "Middle East"),
    ("Riyadh", "Saudi Arabia", "Middle East"),
]
CITY_WEIGHTS = np.array([14, 8, 9, 6, 11, 4, 7, 7, 5, 8, 5, 6, 5, 5], dtype=float)
STORE_CITIES = ["New York", "Chicago", "Los Angeles", "Toronto", "London", "Berlin", "Paris", "Tokyo", "Sydney", "Dubai"]

# item, category, subcategory, base price
ITEMS = [
    ("Laptop 14in", "Electronics", "Computers", 899),
    ("Laptop 16in", "Electronics", "Computers", 1349),
    ("Laptop Sleeve", "Electronics", "Accessories", 39),
    ("Wireless Mouse", "Electronics", "Accessories", 29),
    ("USB-C Hub", "Electronics", "Accessories", 49),
    ("Smartphone", "Electronics", "Phones", 749),
    ("Phone Case", "Electronics", "Accessories", 25),
    ("Screen Protector", "Electronics", "Accessories", 15),
    ("Fast Charger", "Electronics", "Accessories", 35),
    ("Wireless Earbuds", "Electronics", "Audio", 129),
    ("Bluetooth Speaker", "Electronics", "Audio", 89),
    ("Espresso Machine", "Home & Kitchen", "Coffee", 399),
    ("Coffee Beans 1kg", "Home & Kitchen", "Coffee", 24),
    ("Milk Frother", "Home & Kitchen", "Coffee", 59),
    ("Ceramic Mug Set", "Home & Kitchen", "Tableware", 32),
    ("Chef Knife", "Home & Kitchen", "Cookware", 79),
    ("Cutting Board", "Home & Kitchen", "Cookware", 29),
    ("Non-stick Pan", "Home & Kitchen", "Cookware", 55),
    ("Scented Candle", "Home & Kitchen", "Decor", 19),
    ("Yoga Mat", "Sports & Outdoors", "Fitness", 45),
    ("Yoga Blocks", "Sports & Outdoors", "Fitness", 18),
    ("Water Bottle", "Sports & Outdoors", "Fitness", 22),
    ("Dumbbell Set", "Sports & Outdoors", "Fitness", 119),
    ("Running Shoes", "Sports & Outdoors", "Running", 139),
    ("Running Socks", "Sports & Outdoors", "Running", 14),
    ("Tent 2-Person", "Sports & Outdoors", "Camping", 229),
    ("Sleeping Bag", "Sports & Outdoors", "Camping", 99),
    ("Camping Lantern", "Sports & Outdoors", "Camping", 35),
    ("Hoodie", "Apparel", "Tops", 59),
    ("T-Shirt", "Apparel", "Tops", 24),
    ("Jeans", "Apparel", "Bottoms", 69),
    ("Rain Jacket", "Apparel", "Outerwear", 129),
    ("Beanie", "Apparel", "Accessories", 19),
    ("Business Strategy Book", "Books", "Business", 28),
    ("Data Science Handbook", "Books", "Technology", 45),
    ("Mystery Novel", "Books", "Fiction", 16),
    ("Cookbook", "Books", "Lifestyle", 30),
]
BRANDS = {
    "Electronics": ["Nimbus", "Voltix", "Arcadia"],
    "Home & Kitchen": ["Hearth", "Brewline"],
    "Sports & Outdoors": ["Summit", "Stride"],
    "Apparel": ["Northwind", "Urbane"],
    "Books": ["Penrose Press"],
}
# anchor item -> [(companion item, probability)]  (companion keeps the anchor's brand when possible)
BUNDLES = {
    "Laptop 14in": [("Laptop Sleeve", 0.55), ("Wireless Mouse", 0.40), ("USB-C Hub", 0.20)],
    "Laptop 16in": [("Laptop Sleeve", 0.50), ("Wireless Mouse", 0.45), ("USB-C Hub", 0.30)],
    "Smartphone": [("Phone Case", 0.65), ("Screen Protector", 0.50), ("Fast Charger", 0.30)],
    "Espresso Machine": [("Coffee Beans 1kg", 0.70), ("Milk Frother", 0.40), ("Ceramic Mug Set", 0.25)],
    "Yoga Mat": [("Yoga Blocks", 0.50), ("Water Bottle", 0.35)],
    "Running Shoes": [("Running Socks", 0.60), ("Water Bottle", 0.20)],
    "Tent 2-Person": [("Sleeping Bag", 0.60), ("Camping Lantern", 0.45)],
    "Chef Knife": [("Cutting Board", 0.50)],
    "Data Science Handbook": [("Business Strategy Book", 0.25)],
}
CATEGORY_WEIGHT = {"Electronics": 1.0, "Home & Kitchen": 1.1, "Sports & Outdoors": 0.9, "Apparel": 1.2, "Books": 0.8}

# archetype: share, orders/year, active-years range, basket spend multiplier
ARCHETYPES = {
    "champion": (0.07, 14.0, (2.5, 4.0), 1.6),
    "loyal": (0.20, 7.0, (1.5, 4.0), 1.1),
    "regular": (0.33, 3.0, (1.0, 3.0), 1.0),
    "occasional": (0.25, 1.2, (0.5, 3.0), 0.9),
    "one_timer": (0.15, 0.0, (0.0, 0.0), 0.8),
}
FIRST = ["James", "Mary", "Wei", "Aisha", "Carlos", "Yuki", "Fatima", "Liam", "Sofia", "Omar", "Hannah", "Raj",
         "Elena", "Kenji", "Chloe", "Ahmed", "Lucas", "Mia", "Noah", "Zara", "Ethan", "Priya", "Mateo", "Leila"]
LAST = ["Smith", "Garcia", "Chen", "Khan", "Muller", "Tanaka", "Rossi", "Brown", "Silva", "Haddad", "Kim",
        "Novak", "Patel", "Dubois", "Lopez", "Ali", "Schmidt", "Sato", "Wilson", "Ivanova", "Nguyen", "Cohen"]


def _day_weights(days: pd.DatetimeIndex) -> np.ndarray:
    """Relative demand per day: trend x month seasonality x weekday x Black Friday / Christmas peaks."""
    t = (days - pd.Timestamp(START)).days.to_numpy() / 365.0
    trend = 1.0 + 0.18 * t
    month_season = np.array([0.80, 0.75, 0.90, 0.95, 1.00, 0.95, 0.90, 0.95, 1.00, 1.05, 1.35, 1.65])
    season = month_season[days.month.to_numpy() - 1]
    weekday = np.where(days.weekday.to_numpy() >= 5, 1.25, 1.0)
    peak = np.ones(len(days))
    for i, d in enumerate(days):
        if d.month == 11 and 23 <= d.day <= 30:   # Black Friday week
            peak[i] = 2.2
        elif d.month == 12 and 10 <= d.day <= 23:  # Christmas rush
            peak[i] = 1.5
    return trend * season * weekday * peak


def build_catalog(rng: np.random.Generator) -> pd.DataFrame:
    rows, n = [], 1
    for item, cat, sub, price in ITEMS:
        for brand in BRANDS[cat]:
            list_price = round(price * rng.uniform(0.9, 1.15), 2)
            rows.append({
                "sku": f"SKU-{n:04d}", "item": item, "name": f"{brand} {item}", "brand": brand,
                "category": cat, "subcategory": sub, "list_price": list_price,
                "unit_cost": round(list_price * rng.uniform(0.45, 0.7), 2),
            })
            n += 1
    return pd.DataFrame(rows)


def build_customers(rng: np.random.Generator, n: int) -> pd.DataFrame:
    archetypes = list(ARCHETYPES)
    shares = np.array([ARCHETYPES[a][0] for a in archetypes])
    crm_days = (END - CRM_START).days
    rows = []
    for i in range(1, n + 1):
        arch = rng.choice(archetypes, p=shares / shares.sum())
        geo = GEO[rng.choice(len(GEO), p=CITY_WEIGHTS / CITY_WEIGHTS.sum())]
        # an established base signed up before the data window; acquisition accelerates -> growth
        signup = CRM_START + timedelta(days=int(crm_days * rng.beta(1.6, 1.0)))
        rows.append({
            "customer_id": f"C{i:05d}", "archetype": arch,
            "first_name": rng.choice(FIRST), "last_name": rng.choice(LAST),
            "gender": rng.choice(["M", "F"]),
            "birth_date": date(int(rng.integers(1955, 2006)), int(rng.integers(1, 13)), int(rng.integers(1, 29))),
            "signup_date": signup, "city": geo[0], "country": geo[1],
            "pref_channel": rng.choice(["Online", "Mobile", "Retail"], p=[0.45, 0.25, 0.30]),
            "fav_categories": list(rng.choice(list(CATEGORY_WEIGHT), size=2, replace=False)),
        })
    return pd.DataFrame(rows)


def build_orders(rng: np.random.Generator, customers: pd.DataFrame, catalog: pd.DataFrame) -> pd.DataFrame:
    days = pd.date_range(START, END, freq="D")
    weights = _day_weights(days)
    by_item_brand = {(r.item, r.brand): r.sku for r in catalog.itertuples()}
    items_by_item = catalog.groupby("item")["sku"].apply(list).to_dict()
    sku_info = catalog.set_index("sku")
    cats = list(CATEGORY_WEIGHT)
    skus_by_cat = {c: catalog.loc[catalog.category == c, "sku"].tolist() for c in cats}
    anchor_boost = {s: (3.0 if catalog.loc[catalog.sku == s, "item"].iloc[0] in BUNDLES else 1.0) for s in catalog.sku}

    lines, order_no = [], 0
    for c in customers.itertuples():
        share, rate, active, spend = ARCHETYPES[c.archetype]
        signup_idx = (pd.Timestamp(c.signup_date) - days[0]).days  # negative = before history starts
        if c.archetype == "one_timer":
            order_days = [max(signup_idx, int(rng.integers(0, len(days)))) + int(rng.integers(0, 30))]
        else:
            years = rng.uniform(*active)
            if signup_idx < 0:  # existing customer: somewhere in the middle of their lifetime
                years += -signup_idx / 365 * rng.uniform(0.3, 1.0)
            end_idx = signup_idx + int(years * 365)
            if c.archetype in ("champion", "loyal") and rng.random() < 0.15:
                end_idx = signup_idx + int((end_idx - signup_idx) * rng.uniform(0.6, 0.85))  # lapsed -> "at risk"
            lo, hi = max(signup_idx, 0), min(end_idx, len(days) - 1)
            if hi <= lo:
                continue  # all their activity happened before the history window
            n_orders = max(1, rng.poisson(rate * (hi - lo) / 365.0))
            w = weights[lo:hi]
            order_days = rng.choice(np.arange(lo, hi), size=n_orders, p=w / w.sum())
        for d_idx in sorted(order_days):
            if d_idx >= len(days):
                continue
            order_no += 1
            day = days[d_idx]
            ts = day + pd.Timedelta(minutes=int(rng.integers(8 * 60, 22 * 60)))
            channel = c.pref_channel if rng.random() < 0.8 else rng.choice(["Online", "Mobile", "Retail"])
            if channel == "Retail" and c.city not in STORE_CITIES:
                channel = "Online"
            promo = (day.month == 11 and day.day >= 23) or (day.month == 12 and day.day <= 20)
            basket: list[str] = []
            for _ in range(1 + rng.poisson(0.6 * spend)):
                cat = rng.choice(c.fav_categories) if rng.random() < 0.7 else rng.choice(cats)
                pool = skus_by_cat[cat]
                p = np.array([anchor_boost[s] for s in pool])
                sku = rng.choice(pool, p=p / p.sum())
                basket.append(sku)
                info = sku_info.loc[sku]
                for comp_item, prob in BUNDLES.get(info["item"], []):
                    if rng.random() < prob:
                        comp = by_item_brand.get((comp_item, info["brand"])) or rng.choice(items_by_item[comp_item])
                        basket.append(comp)
            basket = list(dict.fromkeys(basket))
            status = rng.choice(["completed", "returned", "cancelled"], p=[0.92, 0.05, 0.03])
            for line_no, sku in enumerate(basket, start=1):
                info = sku_info.loc[sku]
                qty = 1 if info["list_price"] > 60 else int(rng.choice([1, 1, 1, 2, 3]))
                disc = float(rng.choice([0.10, 0.15, 0.20, 0.25])) if promo and rng.random() < 0.7 else (
                    0.05 if rng.random() < 0.08 else 0.0)
                lines.append({
                    "order_no": order_no, "line_no": line_no, "ts": ts, "customer_id": c.customer_id,
                    "channel": channel, "city": c.city, "sku": sku, "qty": qty,
                    "unit_price": round(info["list_price"] * rng.uniform(0.97, 1.03), 2),
                    "discount_pct": disc, "status": status,
                })
    return pd.DataFrame(lines)


def write_sources(rng, customers, catalog, orders, out_dir) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- geo reference + stores
    pd.DataFrame(GEO, columns=["city", "country", "region"])[["country", "region"]].drop_duplicates() \
        .to_csv(out_dir / "geo_regions.csv", index=False)
    stores = [{"store_id": f"ST{i:02d}", "store_name": f"{city} Flagship", "channel": "Retail", "city": city,
               "country": next(g[1] for g in GEO if g[0] == city), "opened_on": "2019-05-01"}
              for i, city in enumerate(STORE_CITIES, start=1)]
    stores += [{"store_id": "WEB", "store_name": "Web Store", "channel": "Online", "city": "", "country": "",
                "opened_on": "2018-01-01"},
               {"store_id": "APP", "store_name": "Mobile App", "channel": "Mobile", "city": "", "country": "",
                "opened_on": "2021-03-15"}]
    pd.DataFrame(stores).to_csv(out_dir / "stores.csv", index=False)

    # --- products.json (nested)
    products = [{"sku": r.sku, "name": r.name, "brand": r.brand,
                 "category": {"name": r.category, "sub": r.subcategory},
                 "pricing": {"currency": "USD", "list": r.list_price, "cost": r.unit_cost}}
                for r in catalog.itertuples()]
    (out_dir / "products.json").write_text(json.dumps({"exported_at": datetime.now().isoformat(),
                                                       "products": products}, indent=1))

    # --- customers.csv with CRM messiness
    cust = customers.copy()
    cust["full_name"] = cust.first_name + " " + cust.last_name
    cust["email"] = (cust.first_name.str.lower() + "." + cust.last_name.str.lower() + cust.customer_id.str[1:]
                     + "@example.com")
    fmt_choice = rng.random(len(cust))
    cust["birth_date"] = [d.strftime("%Y-%m-%d") if f < 0.7 else d.strftime("%d/%m/%Y") if f < 0.9 else
                          d.strftime("%b %d %Y") for d, f in zip(cust.birth_date, fmt_choice)]
    cust["signup_date"] = [d.isoformat() for d in cust.signup_date]
    mess = rng.random(len(cust))
    cust.loc[mess < 0.05, "full_name"] = cust.loc[mess < 0.05, "full_name"].str.upper()
    cust.loc[(mess >= 0.05) & (mess < 0.10), "full_name"] = "  " + cust.loc[(mess >= 0.05) & (mess < 0.10), "full_name"].str.lower() + " "
    cust.loc[(mess >= 0.10) & (mess < 0.15), "email"] = cust.loc[(mess >= 0.10) & (mess < 0.15), "email"].str.upper()
    gender_map = {"M": ["M", "Male", "male", "m"], "F": ["F", "Female", "f", "FEMALE"]}
    cust["gender"] = [rng.choice(gender_map[g]) if rng.random() < 0.3 else g for g in cust.gender]
    cust.loc[rng.random(len(cust)) < 0.03, "gender"] = ""
    cust.loc[rng.random(len(cust)) < 0.01, "city"] = ""
    cust["city"] = [c.upper() if rng.random() < 0.05 else c for c in cust.city]
    cols = ["customer_id", "full_name", "email", "gender", "birth_date", "signup_date", "city", "country"]
    crm = cust[cols]
    dupes = crm.sample(frac=0.02, random_state=1)  # re-exported rows
    pd.concat([crm, dupes]).sample(frac=1, random_state=2).to_csv(out_dir / "customers.csv", index=False)

    # --- split orders into the two source systems
    city_store = {s["city"]: s["store_id"] for s in stores if s["channel"] == "Retail"}
    web = orders[orders.channel != "Retail"].copy()
    pos = orders[orders.channel == "Retail"].copy()

    web_out = pd.DataFrame({
        "order_id": "W" + web.order_no.astype(str).str.zfill(7),
        "line_number": web.line_no,
        "order_ts": web.ts.dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "customer_id": web.customer_id,
        "channel": web.channel.str.lower(),
        "sku": web.sku, "qty": web.qty, "unit_price": web.unit_price,
        "discount_pct": (web.discount_pct * 100).round(0),
        "status": web.status,
    })
    pos = pos[pos.status != "cancelled"]  # POS never records cancelled sales
    pos_out = pd.DataFrame({
        "TransactionID": "P" + pos.order_no.astype(str).str.zfill(7),
        "LineNo": pos.line_no,
        "Date": pos.ts.dt.strftime("%d/%m/%Y %H:%M"),
        "StoreCode": pos.city.map(city_store),
        "CustomerNo": pos.customer_id,
        "ItemCode": pos.sku,
        "Quantity": pos.qty,
        "Price": ["$" + f"{p:.2f}" if rng.random() < 0.4 else f"{p:.2f}" for p in pos.unit_price],
        "DiscountAmt": (pos.unit_price * pos.qty * pos.discount_pct).round(2),
        "Returned": np.where(pos.status == "returned", "Y", "N"),
    })

    # --- inject data-quality problems the ETL must catch
    def corrupt(df, qty_col, sku_col, cust_col):
        n = len(df)
        idx = rng.choice(n, size=int(n * 0.002), replace=False)
        df.iloc[idx, df.columns.get_loc(qty_col)] = rng.choice([0, -1], size=len(idx))
        idx = rng.choice(n, size=int(n * 0.001), replace=False)
        df.iloc[idx, df.columns.get_loc(sku_col)] = "SKU-9999"
        idx = rng.choice(n, size=int(n * 0.005), replace=False)
        df.iloc[idx, df.columns.get_loc(cust_col)] = ""  # guest checkout
        return pd.concat([df, df.sample(frac=0.005, random_state=3)])  # duplicate extracts

    corrupt(web_out, "qty", "sku", "customer_id").to_csv(out_dir / "orders_web.csv", index=False)
    corrupt(pos_out, "Quantity", "ItemCode", "CustomerNo").to_csv(out_dir / "orders_pos.csv", index=False)


def generate(n_customers: int = 6000, seed: int = 42, out_dir=RAW_DIR) -> dict:
    rng = np.random.default_rng(seed)
    catalog = build_catalog(rng)
    customers = build_customers(rng, n_customers)
    orders = build_orders(rng, customers, catalog)
    write_sources(rng, customers, catalog, orders, out_dir)
    return {"customers": len(customers), "products": len(catalog), "order_lines": len(orders),
            "orders": int(orders.order_no.nunique())}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customers", type=int, default=6000)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    print(generate(args.customers, args.seed))
