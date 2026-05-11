"""Stooq CSV provider — daily OHLCV for ETFs, indices, VIX, and forex.

Stooq exposes a public CSV endpoint requiring no API key:
``https://stooq.com/q/d/l/?s={symbol}&i=d``. The CSV columns are
``Date,Open,High,Low,Close,Volume``. The provider returns the most recent two
rows to compute ``change_pct`` against the prior close.

Stooq does not document a hard rate limit but throttles aggressive callers.
We cap concurrency at ``max_concurrency`` (default 4) and wrap each fetch in
``retry_with_backoff`` for 429/503 resilience.
"""

from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timezone
from io import StringIO
from typing import TYPE_CHECKING, Sequence

import httpx
from loguru import logger

from src.data.collectors.providers.base import Quote
from src.data.collectors.providers.symbol_map import (
    ASSET_TYPE_BY_SYMBOL,
    CANONICAL_NAMES,
    STOOQ_SYMBOLS,
)
from src.data.collectors.retry import retry_with_backoff

if TYPE_CHECKING:
    pass


class StooqProvider:
    name: str = "stooq"

    def __init__(
        self,
        base_url: str = "https://stooq.com",
        timeout_seconds: float = 10.0,
        max_concurrency: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self.semaphore = asyncio.Semaphore(max_concurrency)

    def supports(self, symbol: str) -> bool:
        return STOOQ_SYMBOLS.get(symbol) is not None

    async def fetch(self, symbols: Sequence[str]) -> dict[str, Quote]:
        servable = [s for s in symbols if self.supports(s)]
        if not servable:
            return {}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            tasks = [self._fetch_one(client, s) for s in servable]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        out: dict[str, Quote] = {}
        for symbol, result in zip(servable, results):
            if isinstance(result, BaseException):
                logger.warning("stooq fetch failed symbol={} error={}", symbol, result)
                continue
            if result is not None:
                out[symbol] = result
        return out

    async def _fetch_one(self, client: httpx.AsyncClient, symbol: str) -> Quote | None:
        stooq_symbol = STOOQ_SYMBOLS[symbol]
        url = f"{self.base_url}/q/d/l/"

        async def _call():
            async with self.semaphore:
                response = await client.get(url, params={"s": stooq_symbol, "i": "d"})
                response.raise_for_status()
                return response.text

        text = await retry_with_backoff(
            _call,
            operation_name=f"stooq.{symbol}",
        )
        return self._parse_csv(text, symbol)

    @staticmethod
    def _parse_csv(text: str, symbol: str) -> Quote | None:
        if not text or text.strip().lower().startswith("no data"):
            return None
        reader = csv.DictReader(StringIO(text))
        rows = [row for row in reader if row.get("Date") and row.get("Close")]
        if not rows:
            return None

        last = rows[-1]
        prev = rows[-2] if len(rows) >= 2 else None
        close = _to_float(last.get("Close"))
        if close is None:
            return None

        prev_close = _to_float(prev["Close"]) if prev else None
        change_pct = None
        if prev_close not in (None, 0):
            change_pct = (close - prev_close) / prev_close * 100

        asset_type = ASSET_TYPE_BY_SYMBOL.get(symbol, "us_etf")
        return Quote(
            symbol=symbol,
            name=CANONICAL_NAMES.get(symbol, symbol),
            asset_type=asset_type,
            price=close,
            previous_close=prev_close,
            change_pct=change_pct,
            open=_to_float(last.get("Open")),
            high=_to_float(last.get("High")),
            low=_to_float(last.get("Low")),
            volume=_to_float(last.get("Volume")),
            trade_date=last["Date"],
            timestamp=datetime.now(timezone.utc).isoformat(),
            source="stooq",
        )


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


__all__ = ["StooqProvider"]
