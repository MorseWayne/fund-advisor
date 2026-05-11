"""FRED API provider — US Treasury yields, VIX, USD/CNY from St Louis Fed.

The FRED REST endpoint requires a free API key (``FRED_API_KEY`` env var).
When the key is missing, ``supports()`` returns False for every symbol so the
ChainedProvider transparently skips this source.

Series used:
- ``DGS10`` / ``DGS5`` / ``DGS3MO`` — daily Treasury constant-maturity yields
- ``VIXCLS`` — daily VIX close
- ``DEXCHUS`` — daily USD/CNY noon buying rate

FRED publishes T+1, so ``trade_date`` reflects the latest observation date
returned by the API (not ``today()``).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Sequence

import httpx
from loguru import logger

from src.data.collectors.providers.base import Quote
from src.data.collectors.providers.symbol_map import (
    ASSET_TYPE_BY_SYMBOL,
    CANONICAL_NAMES,
    FRED_SERIES,
)
from src.data.collectors.retry import retry_with_backoff


class FREDProvider:
    name: str = "fred"

    def __init__(
        self,
        api_key_env: str = "FRED_API_KEY",
        base_url: str = "https://api.stlouisfed.org",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.api_key = os.environ.get(api_key_env, "").strip()
        if not self.api_key:
            logger.warning(
                "FRED_API_KEY not set — FRED provider will be skipped. "
                "Register a free key at https://fred.stlouisfed.org/docs/api/api_key.html"
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds

    def supports(self, symbol: str) -> bool:
        return bool(self.api_key) and FRED_SERIES.get(symbol) is not None

    async def fetch(self, symbols: Sequence[str]) -> dict[str, Quote]:
        servable = [s for s in symbols if self.supports(s)]
        if not servable:
            return {}

        out: dict[str, Quote] = {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for symbol in servable:
                try:
                    quote = await self._fetch_one(client, symbol)
                except Exception as exc:
                    logger.warning("fred fetch failed symbol={} error={}", symbol, exc)
                    continue
                if quote is not None:
                    out[symbol] = quote
        return out

    async def _fetch_one(self, client: httpx.AsyncClient, symbol: str) -> Quote | None:
        series_id = FRED_SERIES[symbol]
        url = f"{self.base_url}/fred/series/observations"
        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 5,
        }

        async def _call():
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

        payload: dict[str, Any] = await retry_with_backoff(
            _call,
            operation_name=f"fred.{series_id}",
        )

        observations = [
            obs for obs in payload.get("observations", []) if obs.get("value") not in (None, ".", "")
        ]
        if not observations:
            return None

        latest = observations[0]
        previous = observations[1] if len(observations) >= 2 else None

        latest_value = _to_float(latest.get("value"))
        if latest_value is None:
            return None
        prev_value = _to_float(previous.get("value")) if previous else None

        asset_type = ASSET_TYPE_BY_SYMBOL.get(symbol, "treasury_yield")
        change_pct = None
        if prev_value not in (None, 0) and asset_type != "treasury_yield":
            change_pct = (latest_value - prev_value) / prev_value * 100

        return Quote(
            symbol=symbol,
            name=CANONICAL_NAMES.get(symbol, symbol),
            asset_type=asset_type,
            price=latest_value if asset_type != "treasury_yield" else None,
            yield_pct=latest_value if asset_type == "treasury_yield" else None,
            previous_close=prev_value,
            change_pct=change_pct,
            trade_date=latest.get("date"),
            timestamp=datetime.now(timezone.utc).isoformat(),
            source="fred",
        )


def _to_float(value: object) -> float | None:
    if value is None or value == ".":
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


__all__ = ["FREDProvider"]
