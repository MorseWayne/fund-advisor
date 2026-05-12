"""Tests for canonical ↔ per-provider symbol mappings."""

from __future__ import annotations

from src.data.collectors.providers import symbol_map as sm


def test_all_symbols_covers_every_asset_type():
    symbols = sm.all_symbols()
    asset_types = {sm.ASSET_TYPE_BY_SYMBOL[s] for s in symbols}
    assert asset_types == {
        "us_etf",
        "global_index",
        "volatility_index",
        "forex",
        "treasury_yield",
    }


def test_canonical_names_present_for_every_symbol():
    for symbol in sm.all_symbols():
        assert symbol in sm.CANONICAL_NAMES
        assert sm.CANONICAL_NAMES[symbol]


def test_stooq_serves_etfs_indices_vix_forex_but_not_treasuries():
    for etf in sm.US_ETFS:
        assert sm.STOOQ_SYMBOLS[etf]
    for idx in sm.GLOBAL_INDICES:
        assert sm.STOOQ_SYMBOLS[idx]
    assert sm.STOOQ_SYMBOLS["^VIX"]
    assert sm.STOOQ_SYMBOLS["USDCNY=X"]
    for treasury in sm.TREASURY_YIELDS:
        assert sm.STOOQ_SYMBOLS[treasury] is None


def test_fred_serves_treasuries_vix_forex_only():
    for treasury in sm.TREASURY_YIELDS:
        assert sm.FRED_SERIES[treasury]
    assert sm.FRED_SERIES["^VIX"] == "VIXCLS"
    assert sm.FRED_SERIES["USDCNY=X"] == "DEXCHUS"
    for etf in sm.US_ETFS:
        assert sm.FRED_SERIES[etf] is None
    for idx in sm.GLOBAL_INDICES:
        assert sm.FRED_SERIES[idx] is None


def test_akshare_global_covers_indices_forex_treasuries_not_us_etfs():
    for etf in sm.US_ETFS:
        assert sm.AKSHARE_GLOBAL_KEYS[etf] is None
    for idx in sm.GLOBAL_INDICES:
        assert sm.AKSHARE_GLOBAL_KEYS[idx]
    for treasury in sm.TREASURY_YIELDS:
        assert sm.AKSHARE_GLOBAL_KEYS[treasury]


def test_yfinance_symbol_passes_through_canonical():
    assert sm.yfinance_symbol("^GSPC") == "^GSPC"
    assert sm.yfinance_symbol("BOGUS") is None
