"""Trend tracking indicators for the analysis engine.

All functions in this module are pure computations: they accept already-processed
market data and return primitive Python values/dicts suitable for serialization.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _as_float(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` when unavailable."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def calc_ma_alignment(prices: pd.Series, periods: list[int] = [5, 20, 60]) -> str:
    """Classify the latest moving-average alignment.

    Returns:
        ``"多头排列"`` when short MA > medium MA > long MA,
        ``"空头排列"`` when short MA < medium MA < long MA,
        otherwise ``"交叉震荡"``.
    """

    if len(prices) == 0 or len(periods) < 3:
        return "交叉震荡"

    numeric_values = [value for value in (_as_float(item) for item in prices.tolist()) if value is not None]
    numeric_prices = pd.Series(numeric_values, dtype="float64")
    if numeric_prices.empty:
        return "交叉震荡"

    ma_values: list[float] = []
    for period in periods[:3]:
        window = int(period)
        if window <= 0 or len(numeric_values) < window:
            return "交叉震荡"
        ma = float(np.mean(numeric_values[-window:]))
        if pd.isna(ma):
            return "交叉震荡"
        ma_values.append(ma)

    short_ma, mid_ma, long_ma = ma_values
    if short_ma > mid_ma > long_ma:
        return "多头排列"
    if short_ma < mid_ma < long_ma:
        return "空头排列"
    return "交叉震荡"


def calc_standing_line_ratio(etf_list: list[dict[str, Any]], period: int = 250) -> float:
    """Estimate the share of ETFs trading above their long moving average.

    The processed snapshot currently supplies latest price/change data rather than
    full histories for every ETF, so ``change_pct > 0`` is used as a conservative
    proxy for being above the ``period``-day moving average.
    """

    del period  # Kept in the public signature for future historical-data support.

    if not etf_list:
        return 0.0

    valid_count = 0
    above_count = 0
    for etf in etf_list:
        change_pct = _as_float(etf.get("change_pct"))
        if change_pct is None:
            continue
        valid_count += 1
        if change_pct > 0:
            above_count += 1

    if valid_count == 0:
        return 0.0
    return above_count / valid_count


def calc_sentiment(vix: float, advance_decline_ratio: float) -> dict[str, float | str]:
    """Combine VIX and advance/decline breadth into a sentiment score.

    VIX drives the base regime (``>25`` panic, ``<15`` greed). Market breadth then
    nudges the 0-100 score up/down without making network calls or external lookups.
    """

    vix_value = _as_float(vix)
    adr_value = _as_float(advance_decline_ratio)

    if vix_value is None:
        base_score = 50.0
    elif vix_value > 25:
        base_score = 25.0
    elif vix_value < 15:
        base_score = 75.0
    else:
        # Linearly map VIX 15..25 to score 75..25.
        base_score = 75.0 - ((vix_value - 15.0) / 10.0) * 50.0

    breadth_adjustment = 0.0
    if adr_value is not None:
        if adr_value > 1.2:
            breadth_adjustment = 10.0
        elif adr_value < 0.8:
            breadth_adjustment = -10.0

    score = float(np.clip(base_score + breadth_adjustment, 0.0, 100.0))

    if vix_value is not None and vix_value > 25 and score < 45:
        level = "恐慌"
    elif vix_value is not None and vix_value < 15 and score > 55:
        level = "贪婪"
    elif score < 40:
        level = "恐慌"
    elif score > 60:
        level = "贪婪"
    else:
        level = "中性"

    return {"level": level, "score": round(score, 2)}
