"""Tests for the ChainedProvider orchestrator."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.data.collectors.cache import ProviderCache
from src.data.collectors.providers.base import Quote
from src.data.collectors.providers.chained import ChainedProvider
from src.data.storage import MarketDB


class StubProvider:
    def __init__(self, name: str, served: dict[str, Quote], fail: bool = False):
        self.name = name
        self.served = served
        self.fail = fail
        self.fetch_calls: list[list[str]] = []

    def supports(self, symbol: str) -> bool:
        return symbol in self.served

    async def fetch(self, symbols):
        self.fetch_calls.append(list(symbols))
        if self.fail:
            raise RuntimeError(f"{self.name} provider failed")
        return {s: self.served[s] for s in symbols if s in self.served}


def _quote(symbol: str, source: str, trade_date: str | None = None) -> Quote:
    return Quote(
        symbol=symbol,
        name=symbol,
        asset_type="global_index",
        price=100.0,
        change_pct=1.0,
        source=source,
        trade_date=trade_date or date.today().isoformat(),
    )


@pytest.fixture()
def db(tmp_path: Path) -> MarketDB:
    return MarketDB(str(tmp_path / "fund.db"))


@pytest.mark.asyncio
async def test_first_provider_serves_all_short_circuits(db: MarketDB):
    p1 = StubProvider("p1", {"^GSPC": _quote("^GSPC", "p1")})
    p2 = StubProvider("p2", {"^GSPC": _quote("^GSPC", "p2")})
    chained = ChainedProvider([p1, p2], cache=ProviderCache(db, ttl_hours=6))
    out = await chained.fetch_all(symbols=["^GSPC"])
    assert out["global_index"][0].source == "p1"
    assert p2.fetch_calls == []


@pytest.mark.asyncio
async def test_second_provider_used_when_first_misses(db: MarketDB):
    p1 = StubProvider("p1", {})  # serves nothing
    p2 = StubProvider("p2", {"^GSPC": _quote("^GSPC", "p2")})
    chained = ChainedProvider([p1, p2], cache=ProviderCache(db, ttl_hours=6))
    out = await chained.fetch_all(symbols=["^GSPC"])
    assert out["global_index"][0].source == "p2"


@pytest.mark.asyncio
async def test_provider_exception_falls_back(db: MarketDB):
    p1 = StubProvider("p1", {"^GSPC": _quote("^GSPC", "p1")}, fail=True)
    p2 = StubProvider("p2", {"^GSPC": _quote("^GSPC", "p2")})
    chained = ChainedProvider([p1, p2], cache=ProviderCache(db, ttl_hours=6))
    out = await chained.fetch_all(symbols=["^GSPC"])
    assert out["global_index"][0].source == "p2"


@pytest.mark.asyncio
async def test_cache_hit_skips_providers(db: MarketDB):
    cache = ProviderCache(db, ttl_hours=6)
    cached_quote = _quote("^GSPC", "p1")
    cache.put(cached_quote)
    p1 = StubProvider("p1", {})
    chained = ChainedProvider([p1], cache=cache)
    out = await chained.fetch_all(symbols=["^GSPC"])
    assert out["global_index"][0] == cached_quote
    assert p1.fetch_calls == []


@pytest.mark.asyncio
async def test_unserved_symbols_silently_dropped(db: MarketDB):
    p1 = StubProvider("p1", {})
    chained = ChainedProvider([p1], cache=ProviderCache(db, ttl_hours=6))
    out = await chained.fetch_all(symbols=["BOGUS"])
    assert all(len(v) == 0 for v in out.values())
