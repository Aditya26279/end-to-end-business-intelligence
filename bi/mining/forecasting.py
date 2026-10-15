"""Monthly revenue forecasting with Holt-Winters exponential smoothing.

One model per series (total + each category). Each model is backtested on the last
`holdout` months against a seasonal-naive baseline before being refit on all history.
"""
from __future__ import annotations

import json
import warnings

import numpy as np
import pandas as pd
from statsmodels.tsa.holtwinters import ExponentialSmoothing

from bi.db import copy_dataframe, execute, read_sql

MONTHLY_SQL = """
SELECT date_trunc('month', d.full_date)::date AS month, p.category, sum(f.net_amount) AS revenue
FROM dw.fact_sales f
JOIN dw.dim_date d USING (date_key)
JOIN dw.dim_product p USING (product_key)
WHERE NOT f.is_returned
GROUP BY 1, 2
"""


def _fit(y: pd.Series):
    # multiplicative seasonality suits retail (peaks scale with level) but needs strictly positive data
    seasonal = None if len(y) < 24 else ("mul" if (y > 0).all() else "add")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ExponentialSmoothing(y, trend="add", damped_trend=True, seasonal=seasonal,
                                    seasonal_periods=12 if seasonal else None,
                                    initialization_method="estimated").fit(optimized=True)


MIN_HISTORY = 12  # months needed before a series is forecast at all


def _mape(actual, pred) -> float | None:
    """Mean absolute % error over months with non-zero actuals; None when undefined.

    (Zero months would give inf, and NaN/inf are not valid JSON for the jsonb metrics column.)
    """
    actual, pred = np.asarray(actual, float), np.asarray(pred, float)
    ok = np.isfinite(actual) & np.isfinite(pred) & (actual != 0)
    if not ok.any():
        return None
    return round(float(np.mean(np.abs((actual[ok] - pred[ok]) / actual[ok])) * 100), 2)


def forecast_series(y: pd.Series, horizon: int, holdout: int) -> tuple[pd.DataFrame, dict]:
    # --- backtest (only when enough history remains after holding months out)
    metrics = {"mape_holt_winters": None, "mape_seasonal_naive": None}
    if len(y) - holdout >= MIN_HISTORY:
        train, test = y.iloc[:-holdout], y.iloc[-holdout:]
        metrics["mape_holt_winters"] = _mape(test, _fit(train).forecast(holdout))
        metrics["mape_seasonal_naive"] = _mape(test, y.shift(12).iloc[-holdout:])  # same month last year

    # --- final model on full history
    model = _fit(y)
    fc = model.forecast(horizon)
    sigma = float(np.std(y - model.fittedvalues, ddof=1))
    steps = np.arange(1, horizon + 1)
    band = 1.96 * sigma * np.sqrt(steps)
    hist = pd.DataFrame({"month": y.index, "actual": y.values, "fitted": model.fittedvalues.values})
    fut = pd.DataFrame({"month": fc.index, "forecast": fc.values,
                        "lower_95": np.maximum(fc.values - band, 0), "upper_95": fc.values + band})
    return pd.concat([hist, fut], ignore_index=True), metrics


def run(horizon: int = 6, holdout: int = 6) -> str:
    monthly = read_sql(MONTHLY_SQL)
    monthly["month"] = pd.to_datetime(monthly["month"])
    monthly["revenue"] = monthly["revenue"].astype(float)
    pivot = monthly.pivot_table(index="month", columns="category", values="revenue", aggfunc="sum").fillna(0)
    pivot = pivot.asfreq("MS").fillna(0)

    # drop a trailing partial month so it is not mistaken for a collapse in demand
    last_day = read_sql("SELECT max(order_ts)::date AS d FROM dw.fact_sales").d[0]
    if (pd.Timestamp(last_day) + pd.Timedelta(days=1)).month == pd.Timestamp(last_day).month:
        pivot = pivot.iloc[:-1]

    pivot["All"] = pivot.sum(axis=1)
    frames, all_metrics, skipped = [], {}, []
    for series in ["All"] + [c for c in pivot.columns if c != "All"]:
        y = pivot[series]
        if not (y > 0).any():
            skipped.append(series); continue
        y = y.loc[y.gt(0).idxmax():]  # skip months before the category existed
        if len(y) < MIN_HISTORY:
            skipped.append(series); continue
        df, metrics = forecast_series(y, horizon, holdout)
        df.insert(0, "series", series)
        frames.append(df)
        all_metrics[series] = metrics

    if not frames:
        raise RuntimeError(f"no series has {MIN_HISTORY}+ months of sales history to forecast")
    out = pd.concat(frames, ignore_index=True)
    out["month"] = out["month"].dt.date
    copy_dataframe(out.round(2), "sales_forecast", "mining")
    execute("INSERT INTO mining.model_runs (model, params, metrics) VALUES ('holt_winters', :p, :m)",
            {"p": json.dumps({"trend": "add (damped)", "seasonal": "mul (add if zeros)", "seasonal_periods": 12,
                              "horizon_months": horizon, "backtest_holdout_months": holdout,
                              "history_months": len(pivot)}),
             "m": json.dumps(all_metrics, allow_nan=False)})
    m = all_metrics.get("All", {})
    return (f"{len(all_metrics)} series, {len(pivot)} months history, {horizon}-month horizon; "
            f"total backtest MAPE {m.get('mape_holt_winters')}% vs seasonal-naive {m.get('mape_seasonal_naive')}%"
            + (f"; skipped (too little history): {skipped}" if skipped else ""))
