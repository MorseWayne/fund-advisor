from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from src.llm.report_period import ReportPeriod


_WINDOW_DAYS: dict[ReportPeriod, int] = {
    "daily": 1,
    "weekly": 5,
    "monthly": 22,
}


def window_days(period: ReportPeriod) -> int:
    return _WINDOW_DAYS.get(period, 1)


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def aggregate_window_change(daily_records: list[dict[str, Any]]) -> float | None:
    if not daily_records:
        return None

    last = daily_records[-1]
    first = daily_records[0]
    last_close = _as_float(last.get("close") or last.get("price"))
    first_open = _as_float(first.get("open"))
    if last_close is None or first_open is None or first_open == 0:
        last_close = _as_float(last.get("price"))
        first_price = _as_float(first.get("price"))
        if last_close is None or first_price is None or first_price == 0:
            return None
        return (last_close - first_price) / first_price * 100

    return (last_close - first_open) / first_open * 100


def aggregate_window_volume(daily_records: list[dict[str, Any]]) -> float | None:
    total = 0.0
    valid = False
    for record in daily_records:
        vol = _as_float(record.get("volume") or record.get("amount"))
        if vol is not None:
            total += vol
            valid = True
    return total if valid else None


def build_window_snapshot(
    db: Any,
    date: str,
    period: ReportPeriod,
) -> dict[str, Any] | None:
    days = window_days(period)
    if days <= 1:
        return None

    try:
        latest_indices = db.get_latest_indices()
        latest_etfs = db.get_latest_etfs(100)
    except Exception:
        return None

    window_indices = _build_window_indices(db, latest_indices, days)
    window_etfs = _build_window_etfs(db, latest_etfs, days)

    return {
        "indices": _merge_window_indices(latest_indices, window_indices),
        "etfs": _merge_window_etfs(latest_etfs, window_etfs),
    }


def _build_window_indices(
    db: Any,
    indices: list[dict[str, Any]],
    days: int,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for idx in indices:
        code = str(idx.get("code") or "")
        if not code:
            continue
        history = db.get_historical_index(code, days)
        if not history or len(history) < 2:
            continue
        records = sorted(history, key=lambda r: str(r.get("date") or ""))
        change = aggregate_window_change(records)
        volume = aggregate_window_volume(records)
        if change is not None:
            result[code] = {"change_pct": change, "volume": volume}
    return result


def _build_window_etfs(
    db: Any,
    etfs: list[dict[str, Any]],
    days: int,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for etf in etfs:
        code = str(etf.get("code") or "")
        if not code:
            continue
        history = db.get_historical_etf(code, days)
        if not history or len(history) < 2:
            continue
        records = sorted(history, key=lambda r: str(r.get("date") or ""))
        change = aggregate_window_change(records)
        if change is not None:
            result[code] = {"change_pct": change}
    return result


def _merge_window_indices(
    latest: list[dict[str, Any]],
    window: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for idx in latest:
        code = str(idx.get("code") or "")
        merged = dict(idx)
        win = window.get(code)
        if win:
            merged.update(win)
        result[code] = merged
    return result


def _merge_window_etfs(
    latest: list[dict[str, Any]],
    window: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for etf in latest:
        code = str(etf.get("code") or "")
        merged = dict(etf)
        win = window.get(code)
        if win:
            merged.update(win)
        result.append(merged)
    return result


def apply_window_changes(
    indices: list[dict[str, Any]],
    etfs: list[dict[str, Any]],
    window_snapshot: dict[str, Any],
) -> None:
    win_indices = window_snapshot.get("indices") or {}
    for idx in indices:
        code = str(idx.get("code") or "")
        if code in win_indices:
            idx["change_pct"] = win_indices[code].get("change_pct")

    win_etfs = window_snapshot.get("etfs") or []
    etf_map = {str(e.get("code") or ""): e for e in win_etfs if isinstance(e, dict)}
    for etf in etfs:
        code = str(etf.get("code") or "")
        if code in etf_map:
            etf["change_pct"] = etf_map[code].get("change_pct")
