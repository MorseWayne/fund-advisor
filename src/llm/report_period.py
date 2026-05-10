"""Helpers for choosing and naming investment report periods."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal, TypeAlias, cast


ReportPeriod: TypeAlias = Literal["daily", "weekly", "monthly"]

_REPORT_LABELS: dict[ReportPeriod, str] = {
    "daily": "日报",
    "weekly": "周报",
    "monthly": "月报",
}

_REPORT_SCOPES: dict[ReportPeriod, str] = {
    "daily": "今日",
    "weekly": "本周",
    "monthly": "本月",
}

_REPORT_ENGLISH_LABELS: dict[ReportPeriod, str] = {
    "daily": "Daily Report",
    "weekly": "Weekly Report",
    "monthly": "Monthly Report",
}

_PERIOD_ALIASES: dict[str, ReportPeriod] = {
    "daily": "daily",
    "day": "daily",
    "日报": "daily",
    "weekly": "weekly",
    "week": "weekly",
    "周报": "weekly",
    "monthly": "monthly",
    "month": "monthly",
    "月报": "monthly",
}


def select_report_period(report_date: date | datetime | str | None = None) -> ReportPeriod:
    """Choose monthly on month-end, weekly on weekends, otherwise daily."""

    target_date = _coerce_report_date(report_date)
    if (target_date + timedelta(days=1)).month != target_date.month:
        return "monthly"
    if target_date.weekday() >= 5:
        return "weekly"
    return "daily"


def normalize_report_period(period: object | None) -> ReportPeriod:
    if period is None:
        return "daily"
    normalized = str(period).strip().lower()
    return _PERIOD_ALIASES.get(normalized, "daily")


def report_period_label(period: object | None) -> str:
    return _REPORT_LABELS[normalize_report_period(period)]


def report_period_scope(period: object | None) -> str:
    return _REPORT_SCOPES[normalize_report_period(period)]


def report_period_english_label(period: object | None) -> str:
    return _REPORT_ENGLISH_LABELS[normalize_report_period(period)]


def _coerce_report_date(value: date | datetime | str | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return date.today()
