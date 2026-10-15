"""Market-basket analysis: FP-Growth frequent itemsets -> association rules.

Baskets are orders from the fact table (returns excluded). Rules are mined at two levels:
product (cross-sell recommendations) and subcategory (merchandising / store layout).
"""
from __future__ import annotations

import json

import pandas as pd
from mlxtend.frequent_patterns import association_rules, fpgrowth

from bi.db import copy_dataframe, execute, read_sql

RULE_COLUMNS = ["level", "antecedents", "consequents", "antecedent_category", "n_items", "support", "confidence",
                "lift", "leverage", "conviction", "basket_count"]

BASKET_SQL = """
SELECT f.source_system || ':' || f.order_id AS basket, p.product_name, p.subcategory, p.category
FROM dw.fact_sales f JOIN dw.dim_product p USING (product_key)
WHERE NOT f.is_returned
"""


def mine(lines: pd.DataFrame, item_col: str, min_support: float, min_confidence: float,
         min_lift: float = 1.2) -> tuple[pd.DataFrame, int]:
    baskets = lines[["basket", item_col]].drop_duplicates()
    onehot = pd.crosstab(baskets["basket"], baskets[item_col]).astype(bool)
    onehot = onehot[onehot.sum(axis=1) >= 2]  # single-item baskets cannot produce rules
    if onehot.empty:
        return pd.DataFrame(), 0
    itemsets = fpgrowth(onehot, min_support=min_support, use_colnames=True, max_len=3)
    if itemsets.empty:
        return pd.DataFrame(), len(onehot)
    rules = association_rules(itemsets, num_itemsets=len(onehot), metric="confidence",
                              min_threshold=min_confidence)
    rules = rules[rules["lift"] >= min_lift].copy()
    rules["basket_count"] = (rules["support"] * len(onehot)).round().astype(int)
    return rules, len(onehot)


def _format(rules: pd.DataFrame, level: str, category_of: dict) -> pd.DataFrame:
    if rules.empty:
        return pd.DataFrame(columns=RULE_COLUMNS)
    fmt = lambda s: " + ".join(sorted(s))
    out = pd.DataFrame({
        "level": level,
        "antecedents": rules["antecedents"].map(fmt),
        "consequents": rules["consequents"].map(fmt),
        "antecedent_category": rules["antecedents"].map(lambda s: category_of.get(sorted(s)[0], "")),
        "n_items": (rules["antecedents"].map(len) + rules["consequents"].map(len)),
        "support": rules["support"].round(5),
        "confidence": rules["confidence"].round(5),
        "lift": rules["lift"].round(4),
        "leverage": rules["leverage"].round(6),
        "conviction": rules["conviction"].replace(float("inf"), 9999).round(4),
        "basket_count": rules["basket_count"],
    })
    return out.sort_values(["lift", "confidence"], ascending=False)


def run(min_support_product: float = 0.002, min_support_sub: float = 0.005, min_confidence: float = 0.15) -> str:
    lines = read_sql(BASKET_SQL)
    product_cat = lines.drop_duplicates("product_name").set_index("product_name")["category"].to_dict()
    sub_cat = lines.drop_duplicates("subcategory").set_index("subcategory")["category"].to_dict()

    prod_rules, n_multi = mine(lines, "product_name", min_support_product, min_confidence)
    sub_rules, _ = mine(lines, "subcategory", min_support_sub, min_confidence)
    out = pd.concat([_format(prod_rules, "product", product_cat), _format(sub_rules, "subcategory", sub_cat)],
                    ignore_index=True)

    copy_dataframe(out[RULE_COLUMNS], "association_rules", "mining")  # truncate + load in one transaction
    n_baskets = lines["basket"].nunique()
    execute("INSERT INTO mining.model_runs (model, params, metrics) VALUES ('fpgrowth', :p, :m)",
            {"p": json.dumps({"min_support_product": min_support_product, "min_support_subcategory": min_support_sub,
                              "min_confidence": min_confidence, "min_lift": 1.2, "max_len": 3}),
             "m": json.dumps({"baskets": n_baskets, "multi_item_baskets": n_multi,
                              "product_rules": int((out.level == "product").sum()),
                              "subcategory_rules": int((out.level == "subcategory").sum())})})
    top = out[out.level == "product"].head(1)
    top_txt = (f"; top: {top.antecedents.iloc[0]} -> {top.consequents.iloc[0]} (lift {top.lift.iloc[0]:.1f})"
               if len(top) else "")
    return (f"{n_baskets} baskets ({n_multi} multi-item); {int((out.level == 'product').sum())} product rules, "
            f"{int((out.level == 'subcategory').sum())} subcategory rules{top_txt}")
