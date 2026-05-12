"""AKShare overseas provider — China-friendly fallback for global markets.

Wraps the AKShare endpoints we use for non-A-share data without touching
``AKShareCollector`` (which stays focused on A-share work).

Endpoints used:
- ``ak.index_us_stock_sina(symbol)`` for ^GSPC, ^IXIC (one call per symbol)
- ``ak.index_global_spot_em()`` for Nikkei / EURO STOXX 50 / Hang Seng
- ``ak.currency_boc_safe()`` for USD/CNY central-parity rate
- ``ak.bond_zh_us_rate(start_date)`` for US 10Y / 5Y / 3M yields

US ETFs are not served (use Stooq).
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from loguru import logger

try:
    import akshare as ak
except ModuleNotFoundError:
    ak = None
try:
    import pandas as pd
except ModuleNotFoundError:
    pd = None

from src.data.collectors.providers.base import Quote
from src.data.collectors.providers.symbol_map import (
    AKSHARE_GLOBAL_KEYS,
    ASSET_TYPE_BY_SYMBOL,
    CANONICAL_NAMES,
)
from src.data.collectors.retry import retry_with_backoff


_GLOBAL_INDEX_NAME_MAP: dict[str, str] = {
    "^N225": "日经225",
    "^STOXX50E": "欧洲斯托克50",
    "^HSI": "恒生指数",
}

_TREASURY_COLUMNS: dict[str, str] = {
    "^TNX": "美国国债收益率10年",
    "^FVX": "美国国债收益率5年",
    "^IRX": "美国国债收益率3月",
}


class AKShareGlobalProvider:
    name: str = "akshare_global"

    def __init__(self, rate_limit_seconds: float = 1.0) -> None:
        self.rate_limit_seconds = rate_limit_seconds
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    def supports(self, symbol: str) -> bool:
        return ak is not None and AKSHARE_GLOBAL_KEYS.get(symbol) is not None

    async def fetch(self, symbols: Sequence[str]) -> dict[str, Quote]:
        servable = [s for s in symbols if self.supports(s)]
        if not servable:
            return {}

        out: dict[str, Quote] = {}

        # US indices — per-symbol calls
        us_idx = [s for s in servable if (AKSHARE_GLOBAL_KEYS.get(s) or "").startswith("us_index:")]
        for symbol in us_idx:
            quote = await self._fetch_us_index(symbol)
            if quote is not None:
                out[symbol] = quote

        # Global / HK indices — one call covers many
        global_idx = [
            s for s in servable
            if (AKSHARE_GLOBAL_KEYS.get(s) or "").startswith(("global_index:", "hk_index:"))
        ]
        if global_idx:
            out.update(await self._fetch_global_indices(global_idx))

        # Forex
        forex = [s for s in servable if (AKSHARE_GLOBAL_KEYS.get(s) or "").startswith("forex:")]
        if forex:
            out.update(await self._fetch_forex(forex))

        # Treasury yields — one call covers all
        treasury = [s for s in servable if (AKSHARE_GLOBAL_KEYS.get(s) or "").startswith("treasury:")]
        if treasury:
            out.update(await self._fetch_treasury(treasury))

        return out

    async def _fetch_us_index(self, symbol: str) -> Quote | None:
        ak_symbol = AKSHARE_GLOBAL_KEYS[symbol].split(":", 1)[1]
        try:
            df = await self._call(ak.index_us_stock_sina, symbol=ak_symbol)
        except Exception as exc:
            logger.warning("akshare_global us_index {} failed: {}", symbol, exc)
            return None
        if df is None or df.empty:
            return None
        df = df.dropna(how="all")
        if df.empty:
            return None
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else None
        close = _to_float(last.get("close") or last.get("收盘"))
        prev_close = _to_float(prev.get("close") or prev.get("收盘")) if prev is not None else None
        if close is None:
            return None
        change_pct = (close - prev_close) / prev_close * 100 if prev_close else None
        return Quote(
            symbol=symbol,
            name=CANONICAL_NAMES.get(symbol, symbol),
            asset_type=ASSET_TYPE_BY_SYMBOL[symbol],
            price=close,
            previous_close=prev_close,
            change_pct=change_pct,
            open=_to_float(last.get("open") or last.get("开盘")),
            high=_to_float(last.get("high") or last.get("最高")),
            low=_to_float(last.get("low") or last.get("最低")),
            volume=_to_float(last.get("volume") or last.get("成交量")),
            trade_date=_format_date(last.get("date") or last.get("日期")),
            timestamp=datetime.now(timezone.utc).isoformat(),
            source=self.name,
        )

    async def _fetch_global_indices(self, symbols: Sequence[str]) -> dict[str, Quote]:
        try:
            df = await self._call(ak.index_global_spot_em)
        except Exception as exc:
            logger.warning("akshare_global index_global_spot_em failed: {}", exc)
            return {}
        if df is None or df.empty:
            return {}
        name_col = "名称" if "名称" in df.columns else "name"
        price_col = "最新价" if "最新价" in df.columns else "price"
        change_pct_col = "涨跌幅" if "涨跌幅" in df.columns else "change_pct"

        out: dict[str, Quote] = {}
        ts = datetime.now(timezone.utc).isoformat()
        today = date.today().isoformat()
        for symbol in symbols:
            target_name = _GLOBAL_INDEX_NAME_MAP.get(symbol)
            if not target_name:
                continue
            matched = df[df[name_col].astype(str).str.contains(target_name, na=False)]
            if matched.empty:
                continue
            row = matched.iloc[0]
            price = _to_float(row.get(price_col))
            if price is None:
                continue
            out[symbol] = Quote(
                symbol=symbol,
                name=CANONICAL_NAMES.get(symbol, symbol),
                asset_type=ASSET_TYPE_BY_SYMBOL[symbol],
                price=price,
                change_pct=_to_float(row.get(change_pct_col)),
                trade_date=today,
                timestamp=ts,
                source=self.name,
            )
        return out

    async def _fetch_forex(self, symbols: Sequence[str]) -> dict[str, Quote]:
        try:
            df = await self._call(ak.currency_boc_safe)
        except Exception as exc:
            logger.warning("akshare_global currency_boc_safe failed: {}", exc)
            return {}
        if df is None or df.empty:
            return {}
        col_date = "日期" if "日期" in df.columns else "date"
        col_usd = next(
            (c for c in df.columns if "美元" in str(c) or "USD" in str(c).upper()),
            None,
        )
        if col_usd is None:
            return {}
        df = df.dropna(subset=[col_usd]).sort_values(col_date)
        if df.empty:
            return {}
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else None
        price = _to_float(last[col_usd])
        prev_price = _to_float(prev[col_usd]) if prev is not None else None
        if price is None:
            return {}
        change_pct = (price - prev_price) / prev_price * 100 if prev_price else None
        ts = datetime.now(timezone.utc).isoformat()
        out: dict[str, Quote] = {}
        for symbol in symbols:
            out[symbol] = Quote(
                symbol=symbol,
                name=CANONICAL_NAMES.get(symbol, symbol),
                asset_type=ASSET_TYPE_BY_SYMBOL[symbol],
                price=price,
                previous_close=prev_price,
                change_pct=change_pct,
                trade_date=_format_date(last[col_date]),
                timestamp=ts,
                source=self.name,
            )
        return out

    async def _fetch_treasury(self, symbols: Sequence[str]) -> dict[str, Quote]:
        start = (date.today() - timedelta(days=7)).strftime("%Y%m%d")
        try:
            df = await self._call(ak.bond_zh_us_rate, start_date=start)
        except Exception as exc:
            logger.warning("akshare_global bond_zh_us_rate failed: {}", exc)
            return {}
        if df is None or df.empty:
            return {}
        date_col = "日期" if "日期" in df.columns else "date"
        df = df.dropna(how="all").sort_values(date_col)
        if df.empty:
            return {}
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else None
        ts = datetime.now(timezone.utc).isoformat()
        out: dict[str, Quote] = {}
        for symbol in symbols:
            col = _TREASURY_COLUMNS.get(symbol)
            if not col or col not in df.columns:
                continue
            value = _to_float(last.get(col))
            if value is None:
                continue
            prev_value = _to_float(prev.get(col)) if prev is not None else None
            out[symbol] = Quote(
                symbol=symbol,
                name=CANONICAL_NAMES.get(symbol, symbol),
                asset_type=ASSET_TYPE_BY_SYMBOL[symbol],
                yield_pct=value,
                previous_close=prev_value,
                trade_date=_format_date(last.get(date_col)),
                timestamp=ts,
                source=self.name,
            )
        return out

    async def _call(self, func: Callable[..., Any], **kwargs: Any) -> Any:
        async with self._lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.rate_limit_seconds:
                await asyncio.sleep(self.rate_limit_seconds - elapsed)
            self._last_request = time.monotonic()

        async def _runner():
            return await asyncio.to_thread(func, **kwargs)

        return await retry_with_backoff(
            _runner,
            operation_name=f"akshare_global.{func.__name__}",
        )


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    if pd is not None and isinstance(value, float):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
    try:
        return float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _format_date(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:
            pass
    text = str(value)
    return text[:10] if text else None


__all__ = ["AKShareGlobalProvider"]
