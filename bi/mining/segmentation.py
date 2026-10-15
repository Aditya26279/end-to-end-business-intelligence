"""Customer segmentation: RFM features from the warehouse -> K-Means (k picked by silhouette).

Clusters are named from their centroids (recent? frequent? high value?) so labels stay
meaningful when the data changes, instead of hard-coding "cluster 3 = VIPs".
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

from bi.db import copy_dataframe, execute, read_sql

RFM_SQL = """
WITH orders AS (
    SELECT f.customer_key, f.source_system, f.order_id, min(d.full_date) AS order_date, sum(f.net_amount) AS amount
    FROM dw.fact_sales f JOIN dw.dim_date d USING (date_key)
    WHERE NOT f.is_returned AND f.customer_key <> -1
    GROUP BY 1, 2, 3
), snapshot AS (SELECT max(order_date) + 1 AS d FROM orders)
SELECT o.customer_key,
       (SELECT d FROM snapshot) - max(o.order_date)  AS recency_days,
       count(*)                                     AS frequency,
       sum(o.amount)                                AS monetary,
       avg(o.amount)                                AS avg_order_value,
       (SELECT d FROM snapshot) - min(o.order_date) AS tenure_days
FROM orders o
GROUP BY o.customer_key
"""

ACTIONS = {
    "Champions": "Reward and retain: early access, loyalty perks, referral asks.",
    "Loyal Customers": "Upsell premium lines and bundles; invite to loyalty tier.",
    "Can't Lose Them": "High-value but lapsing: personal outreach and a strong win-back offer.",
    "New & Promising": "Onboarding journey and second-purchase incentive.",
    "At Risk": "Win-back campaign now: personalised offer before they churn.",
    "Hibernating": "Low-cost reactivation emails; suppress from expensive channels.",
}


def _quintile(s: pd.Series, reverse: bool = False) -> pd.Series:
    """1-5 score by percentile. Ties share a score: two customers with the same order count must
    not get different F scores just because of row order (rank(method="first") did that)."""
    pct = s.rank(method="max", pct=True, ascending=not reverse)
    return np.ceil(pct * 5).clip(1, 5).astype(int)


def name_clusters(centroids: pd.DataFrame, customers: pd.DataFrame) -> dict[int, str]:
    """Map cluster id -> business label by comparing each centroid with the customer-level distribution.

    Clusters that land on the same label are merged into one business segment.
    """
    recent_cut = customers.recency_days.median()
    freq_cut = customers.frequency.median()
    top_value_cut = customers.monetary.quantile(0.75)
    names: dict[int, str] = {}
    for cid, c in centroids.iterrows():
        recent = c.recency_days <= recent_cut
        frequent = c.frequency >= freq_cut
        top_value = c.monetary >= top_value_cut
        if recent and frequent:
            names[cid] = "Champions" if top_value else "Loyal Customers"
        elif recent:
            names[cid] = "New & Promising"
        elif top_value:
            names[cid] = "Can't Lose Them"
        elif frequent:
            names[cid] = "At Risk"
        else:
            names[cid] = "Hibernating"
    return names


def run(k_range=range(3, 8), random_state: int = 42) -> str:
    rfm = read_sql(RFM_SQL)
    if len(rfm) < 50:
        raise RuntimeError(f"only {len(rfm)} customers with purchases; need at least 50 to segment")
    k_range = [k for k in k_range if k < len(rfm)]
    for col in ["recency_days", "frequency", "tenure_days"]:
        rfm[col] = rfm[col].astype(int)
    rfm["monetary"] = rfm["monetary"].astype(float)
    rfm["avg_order_value"] = rfm["avg_order_value"].astype(float)

    # log-transform skewed RFM measures, then standardise
    features = np.column_stack([np.log1p(rfm.recency_days), np.log1p(rfm.frequency), np.log1p(rfm.monetary)])
    X = StandardScaler().fit_transform(features)

    scores = {}
    for k in k_range:
        labels = KMeans(n_clusters=k, n_init=10, random_state=random_state).fit_predict(X)
        scores[k] = silhouette_score(X, labels, sample_size=min(3000, len(X)), random_state=random_state)
    # prefer the best silhouette, but avoid degenerate k=2-3 splits when a larger k is nearly as good
    best = max(scores.values())
    k = max(kk for kk, s in scores.items() if s >= best - 0.02)
    model = KMeans(n_clusters=k, n_init=20, random_state=random_state).fit(X)
    rfm["cluster_id"] = model.labels_

    centroids = rfm.groupby("cluster_id")[["recency_days", "frequency", "monetary"]].mean()
    rfm["segment"] = rfm["cluster_id"].map(name_clusters(centroids, rfm))

    rfm["r_score"] = _quintile(rfm.recency_days, reverse=True)
    rfm["f_score"] = _quintile(rfm.frequency)
    rfm["m_score"] = _quintile(rfm.monetary)
    rfm["rfm_code"] = rfm.r_score.astype(str) + rfm.f_score.astype(str) + rfm.m_score.astype(str)

    out = rfm[["customer_key", "recency_days", "frequency", "monetary", "avg_order_value", "tenure_days",
               "r_score", "f_score", "m_score", "rfm_code", "cluster_id", "segment"]].round(2)
    copy_dataframe(out, "customer_segments", "mining")

    total_rev, n = rfm.monetary.sum(), len(rfm)
    prof = rfm.groupby("segment").agg(cluster_id=("cluster_id", "first"), customers=("customer_key", "size"),
                                      avg_recency=("recency_days", "mean"), avg_frequency=("frequency", "mean"),
                                      avg_monetary=("monetary", "mean"), total_revenue=("monetary", "sum")).reset_index()
    prof["share_customers"] = prof.customers / n
    prof["share_revenue"] = prof.total_revenue / total_rev
    prof["action"] = prof.segment.map(ACTIONS)
    prof = prof[["segment", "cluster_id", "customers", "share_customers", "avg_recency", "avg_frequency",
                 "avg_monetary", "total_revenue", "share_revenue", "action"]].round(4)
    copy_dataframe(prof, "segment_profiles", "mining")

    execute("INSERT INTO mining.model_runs (model, params, metrics) VALUES ('kmeans_rfm', :p, :m)",
            {"p": json.dumps({"k": k, "features": ["log recency", "log frequency", "log monetary"]}),
             "m": json.dumps({"silhouette_by_k": {str(kk): round(v, 4) for kk, v in scores.items()},
                              "inertia": round(float(model.inertia_), 2), "customers": n})})
    return f"k={k} (silhouette {scores[k]:.3f}); " + ", ".join(
        f"{r.segment}={r.customers}" for r in prof.sort_values("total_revenue", ascending=False).itertuples())
