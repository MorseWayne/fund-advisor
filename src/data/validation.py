from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError

from src.data.models import DailyMarketSnapshot, ETFDataModel, IndexDataModel


@dataclass
class ValidationResult:
    success: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_etf_data(etf: dict[str, Any]) -> ValidationResult:
    """Validate one ETF record without raising exceptions."""

    errors = _required_market_errors(etf, "ETF")
    warnings: list[str] = []

    try:
        ETFDataModel.model_validate(etf)
    except ValidationError as exc:
        errors.extend(_format_pydantic_errors(exc, "ETF"))

    return ValidationResult(success=not errors, errors=_dedupe(errors), warnings=warnings)


def validate_index_data(idx: dict[str, Any]) -> ValidationResult:
    """Validate one index record without raising exceptions."""

    errors = _required_market_errors(idx, "Index")
    warnings: list[str] = []

    try:
        IndexDataModel.model_validate(idx)
    except ValidationError as exc:
        errors.extend(_format_pydantic_errors(exc, "Index"))

    return ValidationResult(success=not errors, errors=_dedupe(errors), warnings=warnings)


def validate_macro(macro: dict[str, Any]) -> ValidationResult:
    """Validate macro market indicators without raising exceptions."""

    errors: list[str] = []
    warnings: list[str] = []

    vix = macro.get("vix")
    if vix is not None and not _in_range(vix, 5, 80):
        errors.append(f"Macro vix out of range [5, 80]: {vix}")

    us10y = _first_present(macro, ("us10y_yield", "us10y", "us_10y_treasury_yield"))
    if us10y is not None and not _in_range(us10y, 0, 15):
        errors.append(f"Macro us10y_yield out of range [0, 15]: {us10y}")

    return ValidationResult(success=not errors, errors=errors, warnings=warnings)


def validate_snapshot(snapshot: DailyMarketSnapshot) -> ValidationResult:
    """Validate a daily snapshot and return all data-quality issues."""

    errors: list[str] = []
    warnings: list[str] = []

    if not snapshot.etfs:
        errors.append("Snapshot etfs is empty")
    if not snapshot.indices:
        errors.append("Snapshot indices is empty")

    trade_date = _parse_date(snapshot.date)
    if trade_date is None:
        errors.append(f"Snapshot trade_date is invalid: {snapshot.date}")
    else:
        age_days = (date.today() - trade_date).days
        if age_days < 0:
            errors.append(f"Snapshot trade_date is in the future: {snapshot.date}")
        elif age_days > 1:
            errors.append(f"Snapshot trade_date is stale (>1 day): {snapshot.date}")

    for position, etf in enumerate(snapshot.etfs):
        result = validate_etf_data(_as_record(etf))
        errors.extend(_prefix_errors(result.errors, f"etfs[{position}]"))
        warnings.extend(_prefix_errors(result.warnings, f"etfs[{position}]"))

    for code, idx in snapshot.indices.items():
        result = validate_index_data(_as_record(idx))
        errors.extend(_prefix_errors(result.errors, f"indices[{code}]"))
        warnings.extend(_prefix_errors(result.warnings, f"indices[{code}]"))

    macro_result = validate_macro(snapshot.macro)
    errors.extend(_prefix_errors(macro_result.errors, "macro"))
    warnings.extend(_prefix_errors(macro_result.warnings, "macro"))

    return ValidationResult(success=not errors, errors=_dedupe(errors), warnings=_dedupe(warnings))


def _required_market_errors(record: dict[str, Any], label: str) -> list[str]:
    errors: list[str] = []
    if record.get("price") is None:
        errors.append(f"{label} price is missing")
    if record.get("change_pct") is None:
        errors.append(f"{label} change_pct is missing")
    if "volume" in record and record.get("volume") is None:
        errors.append(f"{label} volume is missing")
    return errors


def _format_pydantic_errors(exc: ValidationError, label: str) -> list[str]:
    messages: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ())) or "record"
        messages.append(f"{label} {loc}: {error.get('msg', 'invalid value')}")
    return messages


def _as_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: getattr(value, item.name) for item in fields(value)}
    return {}


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _in_range(value: Any, lower: float, upper: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return lower <= number <= upper


def _first_present(values: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in values:
            return values[key]
    return None


def _prefix_errors(messages: list[str], prefix: str) -> list[str]:
    return [f"{prefix}: {message}" for message in messages]


def _dedupe(messages: list[str]) -> list[str]:
    return list(dict.fromkeys(messages))
