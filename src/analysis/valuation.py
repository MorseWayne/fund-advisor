"""Valuation assessment indicators for the analysis engine."""

from __future__ import annotations

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


def calc_pe_percentile(historical_pe: list[float], current_pe: float) -> float:
    """Return current PE's simple rank percentile in historical PE values."""

    current = _as_float(current_pe)
    history = [_as_float(value) for value in historical_pe or []]
    valid_history = [value for value in history if value is not None]
    if current is None or not valid_history:
        return 0.0
    below_count = sum(1 for value in valid_history if value < current)
    return (below_count / len(valid_history)) * 100.0


def calc_bond_equity_spread(index_pe: float, bond_yield: float) -> float:
    """Calculate equity earnings yield minus 10Y treasury yield."""

    pe = _as_float(index_pe)
    treasury_yield = _as_float(bond_yield)
    if pe is None or pe <= 0 or treasury_yield is None:
        return 0.0
    return (1.0 / pe) - treasury_yield


def detect_etf_premium_alerts(etfs: list[dict[str, Any]]) -> list[str]:
    """Return human-readable alerts for ETF premium/discount extremes."""

    alerts: list[str] = []
    for etf in etfs or []:
        premium_discount = _as_float(etf.get("premium_discount"))
        if premium_discount is None or abs(premium_discount) <= 2.0:
            continue

        code = str(etf.get("code") or etf.get("symbol") or "未知代码")
        name = str(etf.get("name") or code)
        if premium_discount > 2.0:
            alerts.append(f"{name}({code}) 溢价 {premium_discount:.2f}%")
        else:
            alerts.append(f"{name}({code}) 折价 {premium_discount:.2f}%")
    return alerts


def assess_overall_valuation(pe_percentile: float, bond_spread: float) -> str:
    """Classify overall valuation using PE percentile and bond-equity spread."""

    percentile = _as_float(pe_percentile)
    spread = _as_float(bond_spread)
    if percentile is None:
        percentile = 50.0
    if spread is None:
        spread = 0.0

    if percentile < 30 and spread > 0:
        return "便宜"
    if percentile >= 85 or (percentile > 70 and spread < 0):
        return "贵"
    if percentile > 70 or spread < -0.01:
        return "偏贵"
    return "合理"
