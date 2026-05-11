"""yfinance provider — last-resort fallback using a single batch ``yf.download`` call.

Compared to the old per-symbol ``Ticker.info`` + ``Ticker.history`` flow that
made 20+ HTTP requests, this provider makes ONE bulk download for every
canonical symbol it can serve. Name/labels come from ``symbol_map.CANONICAL_NAMES``,
so we never need ``ticker.info``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Sequence, cast

import pandas as pd
import yfinance as yf
from loguru import logger

from src.data.collectors.providers.base import Quote
from src.data.collectors.providers.symbol_map import (
    ASSET_TYPE_BY_SYMBOL,
    CANONICAL_NAMES,
    TREASURY_YIELDS,
)
from src.data.collectors.retry import retry_with_backoff


class YFinanceProvider:
    name: str = "yfinance"

    def __init__(self, rate_limit_seconds: float = 0.5) -> None:
        self.rate_limit_seconds = rate_limit_seconds

    def supports(self, symbol: str) -> bool:
        return symbol in ASSET_TYPE_BY_SYMBOL

    async def fetch(self, symbols: Sequence[str]) -> dict[str, Quote]:
        servable = [s for s in symbols if self.supports(s)]
        if not servable:
            return {}

        async def _download():
            return await asyncio.to_thread(
                yf.download,
                tickers=servable,
                period="5d",
                group_by="ticker",
                progress=False,
                threads=True,
                auto_adjust=True,
            )

        try:
            data = await retry_with_backoff(
                _download,
                operation_name="yfinance.batch_download",
            )
        except Exception as exc:
            logger.warning("yfinance batch download failed: {}", exc)
            return {}

        if data is None or data.empty:
            return {}

        out: dict[str, Quote] = {}
        for symbol in servable:
            frame = self._extract_frame(data, symbol, len(servable))
            if frame is None or frame.empty:
                continue
            quote = self._frame_to_quote(symbol, frame)
            if quote is not None:
                out[symbol] = quote
        return out

    @staticmethod
    def _extract_frame(data: pd.DataFrame, symbol: str, symbol_count: int) -> pd.DataFrame | None:
        if symbol_count == 1:
            return data
        if isinstance(data.columns, pd.MultiIndex):
            if symbol in data.columns.get_level_values(0):
                return cast(pd.DataFrame, data[symbol])
        return None

    @staticmethod
    def _frame_to_quote(symbol: str, frame: pd.DataFrame) -> Quote | None:
        frame = frame.dropna(how="all")
        if frame.empty:
            return None
        last = frame.iloc[-1]
        prev = frame.iloc[-2] if len(frame) >= 2 else None
        close = _safe_float(last.get("Close"))
        if close is None:
            return None
        prev_close = _safe_float(prev.get("Close")) if prev is not None else None
        change_pct = (close - prev_close) / prev_close * 100 if prev_close else None

        asset_type = ASSET_TYPE_BY_SYMBOL[symbol]
        trade_date = _index_to_date(frame.index[-1])

        is_treasury = symbol in TREASURY_YIELDS
        return Quote(
            symbol=symbol,
            name=CANONICAL_NAMES.get(symbol, symbol),
            asset_type=asset_type,
            price=close if not is_treasury else None,
            yield_pct=close if is_treasury else None,
            previous_close=prev_close,
            change_pct=change_pct,
            open=_safe_float(last.get("Open")),
            high=_safe_float(last.get("High")),
            low=_safe_float(last.get("Low")),
            volume=_safe_float(last.get("Volume")),
            trade_date=trade_date,
            timestamp=datetime.now(timezone.utc).isoformat(),
            source="yfinance",
        )


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _index_to_date(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:
            pass
    text = str(value)
    return text[:10] if text else None


__all__ = ["YFinanceProvider"]
