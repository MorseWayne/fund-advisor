# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAny=false, reportExplicitAny=false
"""Global market data collector backed by Yahoo Finance.

This module offloads yfinance's synchronous network calls to worker threads and
wraps them with async retry/backoff handling so the data pipeline event loop is
not blocked by Yahoo Finance rate limits or transient 503 responses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, cast

import pandas as pd
import yfinance as yf

from src.data.collectors.retry import retry_with_backoff


class YFinanceCollector:
    """Collect US ETF, global index, forex, VIX, and US Treasury yield data."""

    US_ETFS: dict[str, str] = {
        "SPY": "SPDR S&P 500 ETF Trust",
        "QQQ": "Invesco QQQ Trust",
        "IWM": "iShares Russell 2000 ETF",
        "DIA": "SPDR Dow Jones Industrial Average ETF Trust",
        "XLF": "Financial Select Sector SPDR Fund",
        "XLK": "Technology Select Sector SPDR Fund",
        "XLE": "Energy Select Sector SPDR Fund",
        "XLV": "Health Care Select Sector SPDR Fund",
        "XLI": "Industrial Select Sector SPDR Fund",
        "XLP": "Consumer Staples Select Sector SPDR Fund",
    }

    GLOBAL_INDICES: dict[str, str] = {
        "^GSPC": "S&P 500",
        "^IXIC": "Nasdaq Composite",
        "^HSI": "Hang Seng Index",
        "^N225": "Nikkei 225",
        "^STOXX50E": "EURO STOXX 50",
    }

    VIX_SYMBOL: str = "^VIX"
    FOREX_SYMBOLS: dict[str, str] = {"USDCNY=X": "USD/CNY"}
    TREASURY_YIELDS: dict[str, str] = {
        "^TNX": "US 10Y Treasury Yield",
        "^FVX": "US 5Y Treasury Yield",
        "^IRX": "US 3M Treasury Yield",
    }

    def __init__(self, rate_limit_seconds: float = 0.5) -> None:
        self.rate_limit_seconds: float = rate_limit_seconds

    async def fetch_us_etfs(self) -> list[dict[str, Any]]:
        """Fetch US ETF quotes in one batch download, with per-ticker fallback."""

        symbols = list(self.US_ETFS)
        try:
            data = await self._call_yfinance(
                yf.download,
                symbols,
                period="2d",
                group_by="ticker",
                progress=False,
                threads=True,
                auto_adjust=True,
                operation_name="yfinance.download.us_etfs",
            )
            await self._sleep()

            if data is None or data.empty:
                return [await self._fetch_quote(symbol, self.US_ETFS[symbol], "us_etf") for symbol in symbols]

            results: list[dict[str, Any]] = []
            for symbol in symbols:
                try:
                    hist = self._extract_ticker_frame(data, symbol, len(symbols))
                    if hist is None or hist.empty or hist.dropna(how="all").empty:
                        results.append(await self._fetch_quote(symbol, self.US_ETFS[symbol], "us_etf"))
                        continue

                    results.append(self._quote_from_history(symbol, self.US_ETFS[symbol], hist, "us_etf"))
                except Exception as exc:
                    results.append({"symbol": symbol, "name": self.US_ETFS[symbol], "asset_type": "us_etf", "error": str(exc)})

            return results
        except Exception:
            return [await self._fetch_quote(symbol, self.US_ETFS[symbol], "us_etf") for symbol in symbols]

    async def fetch_global_indices(self) -> list[dict[str, Any]]:
        """Fetch major global equity indices."""

        return [
            await self._fetch_quote(symbol, name, "global_index")
            for symbol, name in self.GLOBAL_INDICES.items()
        ]

    async def fetch_vix(self) -> dict[str, Any]:
        """Fetch CBOE volatility index data."""

        return await self._fetch_quote(self.VIX_SYMBOL, "CBOE Volatility Index", "volatility_index")

    async def fetch_forex_rates(self) -> list[dict[str, Any]]:
        """Fetch configured forex rates."""

        return [
            await self._fetch_quote(symbol, name, "forex")
            for symbol, name in self.FOREX_SYMBOLS.items()
        ]

    async def fetch_treasury_yields(self) -> list[dict[str, Any]]:
        """Fetch US Treasury yield indices."""

        results = []
        for symbol, name in self.TREASURY_YIELDS.items():
            quote = await self._fetch_quote(symbol, name, "treasury_yield")
            quote["yield_pct"] = quote.get("price")
            results.append(quote)
        return results

    async def fetch_all(self) -> dict[str, Any]:
        """Fetch all supported global market datasets."""

        return {
            "timestamp": self._timestamp(),
            "us_etfs": await self.fetch_us_etfs(),
            "global_indices": await self.fetch_global_indices(),
            "vix": await self.fetch_vix(),
            "forex": await self.fetch_forex_rates(),
            "treasury_yields": await self.fetch_treasury_yields(),
        }

    async def _fetch_quote(self, symbol: str, name: str, asset_type: str) -> dict[str, Any]:
        """Fetch single-symbol info and daily OHLCV using yfinance Ticker."""

        try:
            ticker = await self._call_yfinance(yf.Ticker, symbol, operation_name=f"yfinance.Ticker.{symbol}")
            info = await self._call_yfinance(lambda: ticker.info, operation_name=f"yfinance.Ticker.info.{symbol}")
            await self._sleep()

            hist = await self._call_yfinance(ticker.history, period="1d", operation_name=f"yfinance.Ticker.history.{symbol}")
            await self._sleep()

            if hist.empty:
                return self._quote_from_info(symbol, name, info, asset_type)

            row = hist.dropna(how="all").iloc[-1]
            current_price = self._clean_number(info.get("currentPrice") or info.get("regularMarketPrice"))
            if current_price is None:
                current_price = self._clean_number(row.get("Close"))

            previous_close = self._clean_number(info.get("previousClose"))
            if previous_close is None:
                previous_close = self._clean_number(info.get("regularMarketPreviousClose"))

            change = self._clean_number(info.get("regularMarketChange"))
            change_pct = self._clean_number(info.get("regularMarketChangePercent"))

            if change is None and current_price is not None and previous_close is not None:
                change = current_price - previous_close
            if change_pct is None and change is not None and previous_close:
                change_pct = (change / previous_close) * 100

            volume = self._clean_number(row.get("Volume"))
            amount = current_price * volume if current_price is not None and volume is not None else None

            return self._clean_dict({
                "symbol": symbol,
                "name": self._clean_string(info.get("longName") or info.get("shortName")) or name,
                "asset_type": asset_type,
                "price": current_price,
                "previous_close": previous_close,
                "change": change,
                "change_pct": change_pct,
                "open": self._clean_number(row.get("Open")),
                "high": self._clean_number(row.get("High")),
                "low": self._clean_number(row.get("Low")),
                "volume": volume,
                "amount": amount,
                "currency": self._clean_string(info.get("currency")),
                "exchange": self._clean_string(info.get("exchange")),
                "timestamp": self._timestamp(),
            })
        except Exception as exc:
            return {"symbol": symbol, "name": name, "asset_type": asset_type, "error": str(exc)}

    async def _call_yfinance(
        self,
        func: Callable[..., Any],
        *args: Any,
        operation_name: str,
        **kwargs: Any,
    ) -> Any:
        return await retry_with_backoff(
            lambda: asyncio.to_thread(func, *args, **kwargs),
            max_retries=3,
            operation_name=operation_name,
        )

    def _quote_from_info(self, symbol: str, name: str, info: dict[str, Any], asset_type: str) -> dict[str, Any]:
        current_price = self._clean_number(info.get("currentPrice") or info.get("regularMarketPrice"))
        previous_close = self._clean_number(info.get("previousClose") or info.get("regularMarketPreviousClose"))
        change = self._clean_number(info.get("regularMarketChange"))
        change_pct = self._clean_number(info.get("regularMarketChangePercent"))

        if change is None and current_price is not None and previous_close is not None:
            change = current_price - previous_close
        if change_pct is None and change is not None and previous_close:
            change_pct = (change / previous_close) * 100

        return self._clean_dict({
            "symbol": symbol,
            "name": self._clean_string(info.get("longName") or info.get("shortName")) or name,
            "asset_type": asset_type,
            "price": current_price,
            "previous_close": previous_close,
            "change": change,
            "change_pct": change_pct,
            "open": self._clean_number(info.get("regularMarketOpen")),
            "high": self._clean_number(info.get("dayHigh") or info.get("regularMarketDayHigh")),
            "low": self._clean_number(info.get("dayLow") or info.get("regularMarketDayLow")),
            "volume": self._clean_number(info.get("volume") or info.get("regularMarketVolume")),
            "currency": self._clean_string(info.get("currency")),
            "exchange": self._clean_string(info.get("exchange")),
            "timestamp": self._timestamp(),
        })

    def _quote_from_history(self, symbol: str, name: str, hist: pd.DataFrame, asset_type: str) -> dict[str, Any]:
        hist = hist.dropna(how="all")
        current_row = hist.iloc[-1]
        previous_row = hist.iloc[-2] if len(hist) >= 2 else current_row

        current_price = self._clean_number(current_row.get("Close"))
        previous_close = self._clean_number(previous_row.get("Close"))
        change = current_price - previous_close if current_price is not None and previous_close is not None else None
        change_pct = (change / previous_close) * 100 if change is not None and previous_close else None
        volume = self._clean_number(current_row.get("Volume"))
        amount = current_price * volume if current_price is not None and volume is not None else None

        return self._clean_dict({
            "symbol": symbol,
            "name": name,
            "asset_type": asset_type,
            "price": current_price,
            "previous_close": previous_close,
            "change": change,
            "change_pct": change_pct,
            "open": self._clean_number(current_row.get("Open")),
            "high": self._clean_number(current_row.get("High")),
            "low": self._clean_number(current_row.get("Low")),
            "volume": volume,
            "amount": amount,
            "timestamp": self._timestamp(),
        })

    def _extract_ticker_frame(self, data: pd.DataFrame, symbol: str, symbol_count: int) -> pd.DataFrame | None:
        if symbol_count == 1:
            return data
        if isinstance(data.columns, pd.MultiIndex) and symbol in data.columns.get_level_values(0):
            return cast(pd.DataFrame, data[symbol])
        return None

    async def _sleep(self) -> None:
        if self.rate_limit_seconds > 0:
            await asyncio.sleep(self.rate_limit_seconds)

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _clean_number(value: Any) -> float | None:
        if value is None or pd.isna(value):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clean_string(value: Any) -> str | None:
        if value is None or pd.isna(value):
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _clean_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        return {key: cls._clean_value(value) for key, value in data.items()}

    @classmethod
    def _clean_value(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, float) and pd.isna(value):
            return None
        if isinstance(value, dict):
            return cls._clean_dict(value)
        if isinstance(value, list):
            return [cls._clean_value(item) for item in value]
        return value
