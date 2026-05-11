"""Canonical symbol ↔ per-provider symbol mapping.

Canonical symbol = yfinance-style (``^GSPC``, ``SPY``, ``USDCNY=X``, ``^TNX``),
because the downstream pipeline already keys indices by these strings.

This module is the single source of truth for:
- Which US ETFs / global indices we monitor
- How each provider names those symbols
- Which provider can serve which asset class

``BackfillPipeline`` reads ``US_ETFS`` and ``GLOBAL_INDICES`` directly (they
used to live as class attrs on ``YFinanceCollector``).
"""

from __future__ import annotations

from src.data.collectors.providers.base import AssetType


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
VIX_NAME: str = "CBOE Volatility Index"

FOREX_SYMBOLS: dict[str, str] = {"USDCNY=X": "USD/CNY"}

TREASURY_YIELDS: dict[str, str] = {
    "^TNX": "US 10Y Treasury Yield",
    "^FVX": "US 5Y Treasury Yield",
    "^IRX": "US 3M Treasury Yield",
}


ASSET_TYPE_BY_SYMBOL: dict[str, AssetType] = {
    **{s: "us_etf" for s in US_ETFS},
    **{s: "global_index" for s in GLOBAL_INDICES},
    VIX_SYMBOL: "volatility_index",
    **{s: "forex" for s in FOREX_SYMBOLS},
    **{s: "treasury_yield" for s in TREASURY_YIELDS},
}

CANONICAL_NAMES: dict[str, str] = {
    **US_ETFS,
    **GLOBAL_INDICES,
    VIX_SYMBOL: VIX_NAME,
    **FOREX_SYMBOLS,
    **TREASURY_YIELDS,
}


def all_symbols() -> list[str]:
    """Complete monitored universe in a deterministic order."""
    return [
        *US_ETFS,
        *GLOBAL_INDICES,
        VIX_SYMBOL,
        *FOREX_SYMBOLS,
        *TREASURY_YIELDS,
    ]


# ---------------------------------------------------------------------------
# Per-provider symbol maps. ``None`` means the provider cannot serve that
# canonical symbol.
# ---------------------------------------------------------------------------

STOOQ_SYMBOLS: dict[str, str | None] = {
    # US ETFs: lowercase + .us suffix
    **{etf: f"{etf.lower()}.us" for etf in US_ETFS},
    # Indices: lowercase with custom stooq tickers
    "^GSPC": "^spx",
    "^IXIC": "^ndq",
    "^HSI": "^hsi",
    "^N225": "^nkx",
    "^STOXX50E": "^stx50",
    "^VIX": "^vix",
    # Forex
    "USDCNY=X": "usdcny",
    # Stooq doesn't have usable tickers for the ^TNX/^FVX/^IRX series —
    # treasuries come from FRED.
    "^TNX": None,
    "^FVX": None,
    "^IRX": None,
}


FRED_SERIES: dict[str, str | None] = {
    "^TNX": "DGS10",
    "^FVX": "DGS5",
    "^IRX": "DGS3MO",
    "^VIX": "VIXCLS",
    "USDCNY=X": "DEXCHUS",
    # FRED does not publish index/ETF price levels in a usable form
    **{etf: None for etf in US_ETFS},
    **{idx: None for idx in GLOBAL_INDICES},
}


# AKShare overseas — stored as opaque keys that the provider interprets.
# Actual calls use ``ak.index_us_stock_sina(symbol=".INX")`` etc.
AKSHARE_GLOBAL_KEYS: dict[str, str | None] = {
    "^GSPC": "us_index:.INX",
    "^IXIC": "us_index:.IXIC",
    "^HSI": "hk_index:HSI",
    "^N225": "global_index:日经225",
    "^STOXX50E": "global_index:欧洲斯托克50",
    "^VIX": None,
    "USDCNY=X": "forex:USD/CNY",
    "^TNX": "treasury:US10Y",
    "^FVX": "treasury:US5Y",
    "^IRX": "treasury:US3M",
    **{etf: None for etf in US_ETFS},
}


# yfinance uses canonical symbols as-is (they ARE yfinance symbols).
def yfinance_symbol(canonical: str) -> str | None:
    return canonical if canonical in ASSET_TYPE_BY_SYMBOL else None


__all__ = [
    "US_ETFS",
    "GLOBAL_INDICES",
    "VIX_SYMBOL",
    "VIX_NAME",
    "FOREX_SYMBOLS",
    "TREASURY_YIELDS",
    "ASSET_TYPE_BY_SYMBOL",
    "CANONICAL_NAMES",
    "all_symbols",
    "STOOQ_SYMBOLS",
    "FRED_SERIES",
    "AKSHARE_GLOBAL_KEYS",
    "yfinance_symbol",
]
