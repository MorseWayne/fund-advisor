"""Tests for the SQLite-backed ProviderCache."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.data.collectors.cache import ProviderCache
from src.data.collectors.providers.base import Quote
from src.data.storage import MarketDB


@pytest.fixture()
def db(tmp_path: Path) -> MarketDB:
    return MarketDB(str(tmp_path / "fund.db"))


def _make_quote(**overrides):
    base = dict(
        symbol="^GSPC",
        name="S&P 500",
        asset_type="global_index",
        price=5000.0,
        change_pct=0.5,
        trade_date=date.today().isoformat(),
        source="stooq",
    )
    base.update(overrides)
    return Quote(**base)


def test_put_then_get_returns_same_quote(db: MarketDB):
    cache = ProviderCache(db, ttl_hours=6)
    q = _make_quote()
    cache.put(q)
    out = cache.get("stooq", "^GSPC", q.trade_date)
    assert out == q


def test_historical_trade_date_never_expires(db: MarketDB):
    cache = ProviderCache(db, ttl_hours=0.0001)
    yesterday = (date.today() - timedelta(days=5)).isoformat()
    q = _make_quote(trade_date=yesterday)
    cache.put(q)
    # Stale fetched_at — for historical dates, should still hit
    with sqlite3.connect(str(db.db_path)) as conn:
        conn.execute(
            "UPDATE provider_cache SET fetched_at = ? WHERE symbol = ?",
            ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(), "^GSPC"),
        )
    out = cache.get("stooq", "^GSPC", yesterday)
    assert out is not None


def test_today_cache_expires_after_ttl(db: MarketDB):
    cache = ProviderCache(db, ttl_hours=1)
    q = _make_quote()
    cache.put(q)
    with sqlite3.connect(str(db.db_path)) as conn:
        conn.execute(
            "UPDATE provider_cache SET fetched_at = ?",
            ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),),
        )
    assert cache.get("stooq", "^GSPC", q.trade_date) is None


def test_get_batch_skips_misses(db: MarketDB):
    cache = ProviderCache(db, ttl_hours=6)
    cache.put(_make_quote(symbol="^GSPC"))
    out = cache.get_batch("stooq", ["^GSPC", "^IXIC"], date.today().isoformat())
    assert list(out.keys()) == ["^GSPC"]


def test_put_no_trade_date_is_noop(db: MarketDB):
    cache = ProviderCache(db, ttl_hours=6)
    q = Quote(symbol="X", name="X", asset_type="us_etf", price=1.0)
    cache.put(q)  # no trade_date, no source — should silently skip
    out = cache.get("stooq", "X", date.today().isoformat())
    assert out is None
