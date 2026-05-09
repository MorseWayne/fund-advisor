"""Sector rotation indicators for the analysis engine."""

from __future__ import annotations

from typing import Any

import numpy as np


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(number):
        return default
    return number


def calc_momentum_ranking(sectors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank sectors by weighted 1/3/6-month momentum.

    Composite score = ``0.2 * momentum_1m + 0.3 * momentum_3m + 0.5 * momentum_6m``.
    Missing momentum values are treated as ``0`` to keep partial snapshots usable.
    """

    ranked: list[dict[str, Any]] = []
    for sector in sectors or []:
        momentum_1m = _as_float(sector.get("momentum_1m"))
        momentum_3m = _as_float(sector.get("momentum_3m"))
        momentum_6m = _as_float(sector.get("momentum_6m"))
        composite_score = 0.2 * momentum_1m + 0.3 * momentum_3m + 0.5 * momentum_6m

        enriched = dict(sector)
        enriched["composite_score"] = round(composite_score, 6)
        ranked.append(enriched)

    ranked.sort(key=lambda item: item["composite_score"], reverse=True)
    for index, sector in enumerate(ranked, start=1):
        sector["rank"] = index
    return ranked


def calc_fund_flow_analysis(fund_flows: dict[str, Any]) -> dict[str, float | str]:
    """Summarize north-bound and main-force fund flow direction."""

    north_bound = _as_float(fund_flows.get("north_bound")) if fund_flows else 0.0
    main_force = _as_float(fund_flows.get("main_force")) if fund_flows else 0.0
    direction = "流入" if north_bound + main_force >= 0 else "流出"
    return {
        "direction": direction,
        "north_bound": north_bound,
        "main_force": main_force,
    }


def map_to_economic_cycle(cpi_change: float, gdp_growth: float) -> str:
    """Map growth/inflation changes to a Merrill Lynch clock phase."""

    cpi = _as_float(cpi_change)
    gdp = _as_float(gdp_growth)
    high_growth = gdp >= 0
    high_inflation = cpi >= 0

    if high_growth and high_inflation:
        return "过热"
    if high_growth and not high_inflation:
        return "复苏"
    if not high_growth and high_inflation:
        return "滞胀"
    return "衰退"
