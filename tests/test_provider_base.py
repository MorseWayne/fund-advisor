"""Tests for the Quote dataclass and provider protocol contract."""

from __future__ import annotations

from src.data.collectors.providers.base import GlobalMarketProvider, Quote, quote_to_dict


def test_quote_serialises_drops_none_fields():
    q = Quote(
        symbol="^GSPC",
        name="S&P 500",
        asset_type="global_index",
        price=5000.0,
        change_pct=0.75,
        source="stooq",
        trade_date="2026-05-10",
    )
    d = quote_to_dict(q)
    assert d["symbol"] == "^GSPC"
    assert d["price"] == 5000.0
    assert d["change_pct"] == 0.75
    assert "volume" not in d
    assert "yield_pct" not in d


def test_quote_treasury_uses_yield_pct():
    q = Quote(
        symbol="^TNX",
        name="US 10Y Treasury Yield",
        asset_type="treasury_yield",
        yield_pct=4.25,
        source="fred",
        trade_date="2026-05-10",
    )
    d = quote_to_dict(q)
    assert d["yield_pct"] == 4.25
    assert "price" not in d


def test_provider_protocol_is_runtime_checkable():
    class Dummy:
        name = "dummy"

        def supports(self, symbol: str) -> bool:
            return True

        async def fetch(self, symbols):
            return {}

    assert isinstance(Dummy(), GlobalMarketProvider)
