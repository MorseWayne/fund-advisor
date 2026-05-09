"""Risk monitoring indicators for the analysis engine."""

from __future__ import annotations

from itertools import combinations
from typing import Any

import numpy as np


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def detect_anomaly_volatility(change_pct: float, threshold: float = 0.03) -> bool:
    """Return True when daily change exceeds the anomaly threshold.

    ``threshold`` is a decimal ratio (``0.03`` = 3%). Snapshot ``change_pct`` values
    may arrive as either percent points (``3``) or decimal ratios (``0.03``), so the
    input is normalized before comparison.
    """

    change = _as_float(change_pct)
    if change is None:
        return False
    normalized = change / 100.0 if abs(change) > 1 else change
    return abs(normalized) > threshold


def calc_max_drawdown(price_series: list[float]) -> float:
    """Compute maximum drawdown as a negative decimal percentage."""

    prices = np.array([value for value in (_as_float(item) for item in price_series or []) if value is not None], dtype=float)
    if prices.size == 0:
        return 0.0

    running_max = np.maximum.accumulate(prices)
    valid = running_max > 0
    if not np.any(valid):
        return 0.0
    drawdowns = np.zeros_like(prices, dtype=float)
    drawdowns[valid] = (prices[valid] - running_max[valid]) / running_max[valid]
    return float(np.min(drawdowns))


def check_drawdown_warning(current_drawdown: float, warning_threshold: float = 0.15) -> bool:
    """Return True when drawdown breaches the warning threshold."""

    drawdown = _as_float(current_drawdown)
    if drawdown is None:
        return False
    return abs(drawdown) >= warning_threshold if drawdown < 0 else drawdown >= warning_threshold


def calc_correlation_matrix(etf_returns: dict[str, list[float]]) -> dict[str, float]:
    """Calculate average pairwise ETF return correlation.

    Returns a compact dict instead of a full matrix because downstream risk logic
    only needs average diversification pressure.
    """

    valid_returns: dict[str, np.ndarray] = {}
    for code, returns in (etf_returns or {}).items():
        series = np.array([value for value in (_as_float(item) for item in returns or []) if value is not None], dtype=float)
        if series.size >= 2 and np.std(series) > 0:
            valid_returns[str(code)] = series

    correlations: list[float] = []
    for left_code, right_code in combinations(valid_returns, 2):
        left = valid_returns[left_code]
        right = valid_returns[right_code]
        length = min(left.size, right.size)
        if length < 2:
            continue
        corr = float(np.corrcoef(left[-length:], right[-length:])[0, 1])
        if np.isfinite(corr):
            correlations.append(corr)

    average = float(np.mean(correlations)) if correlations else 0.0
    return {"average_correlation": average}


def detect_correlation_breakdown(avg_correlation: float, threshold: float = 0.8) -> bool:
    """Return True when average correlation is too high for diversification."""

    correlation = _as_float(avg_correlation)
    return bool(correlation is not None and correlation >= threshold)
