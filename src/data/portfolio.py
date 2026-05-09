from pathlib import Path
from typing import Optional
import yaml
from loguru import logger

from src.data.models import Holding, HoldingCategory, Market


def load_portfolio(path: str | Path = "portfolio.yaml") -> list[Holding]:
    path = Path(path)
    if not path.exists():
        logger.warning(f"Portfolio file not found: {path}")
        return []

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw or "holdings" not in raw:
        return []

    holdings = []
    for h in raw["holdings"]:
        try:
            holdings.append(Holding(
                code=str(h["code"]),
                name=h["name"],
                market=Market(h["market"]),
                cost_basis=float(h["cost_basis"]),
                shares=float(h["shares"]),
                category=HoldingCategory(h["category"]),
                notes=h.get("notes", ""),
            ))
        except (KeyError, ValueError) as e:
            logger.error(f"Invalid holding entry {h.get('code', '?')}: {e}")

    logger.info(f"Loaded {len(holdings)} holdings from portfolio")
    return holdings


def get_holding_codes(holdings: list[Holding]) -> list[str]:
    return [h.code for h in holdings]


def get_holding_by_code(holdings: list[Holding], code: str) -> Optional[Holding]:
    for h in holdings:
        if h.code == code:
            return h
    return None
