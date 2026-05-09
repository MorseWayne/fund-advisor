from pathlib import Path


def safe_float(val, default=0.0) -> float:
    try:
        if val is None:
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val, default=0) -> int:
    try:
        if val is None:
            return default
        return int(val)
    except (ValueError, TypeError):
        return default


def today_str() -> str:
    from datetime import date
    return date.today().strftime("%Y-%m-%d")


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent
