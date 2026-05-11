"""Tests for the AKShare overseas provider — monkeypatches ak.* directly."""

from __future__ import annotations

import pandas as pd
import pytest

from src.data.collectors.providers import akshare_global as akm
from src.data.collectors.providers.akshare_global import AKShareGlobalProvider


@pytest.fixture(autouse=True)
def fast_rate_limit(monkeypatch):
    # Avoid sleeping between AK calls in tests
    monkeypatch.setattr(akm, "ak", _FakeAK())
    yield


class _FakeAK:
    @staticmethod
    def index_us_stock_sina(symbol: str):
        return pd.DataFrame(
            [
                {"date": "2026-05-09", "open": 4980, "high": 5010, "low": 4970, "close": 5000, "volume": 100},
                {"date": "2026-05-10", "open": 5005, "high": 5050, "low": 4995, "close": 5040, "volume": 200},
            ]
        )

    @staticmethod
    def index_global_spot_em():
        return pd.DataFrame(
            [
                {"名称": "日经225", "最新价": 38000.0, "涨跌幅": 1.2},
                {"名称": "欧洲斯托克50", "最新价": 5000.0, "涨跌幅": -0.4},
                {"名称": "恒生指数", "最新价": 19000.0, "涨跌幅": 0.5},
            ]
        )

    @staticmethod
    def currency_boc_safe():
        return pd.DataFrame(
            [
                {"日期": "2026-05-08", "美元": 7.20},
                {"日期": "2026-05-09", "美元": 7.21},
                {"日期": "2026-05-10", "美元": 7.22},
            ]
        )

    @staticmethod
    def bond_zh_us_rate(start_date=None):
        return pd.DataFrame(
            [
                {
                    "日期": "2026-05-09",
                    "美国国债收益率10年": 4.20,
                    "美国国债收益率5年": 4.10,
                    "美国国债收益率3月": 5.40,
                },
                {
                    "日期": "2026-05-10",
                    "美国国债收益率10年": 4.25,
                    "美国国债收益率5年": 4.15,
                    "美国国债收益率3月": 5.42,
                },
            ]
        )


@pytest.mark.asyncio
async def test_us_index_quote():
    p = AKShareGlobalProvider(rate_limit_seconds=0.0)
    out = await p.fetch(["^GSPC"])
    q = out["^GSPC"]
    assert q.price == 5040.0
    assert q.previous_close == 5000.0
    assert q.change_pct == pytest.approx(0.8)
    assert q.source == "akshare_global"


@pytest.mark.asyncio
async def test_global_indices_filtered_by_name():
    p = AKShareGlobalProvider(rate_limit_seconds=0.0)
    out = await p.fetch(["^N225", "^HSI", "^STOXX50E"])
    assert set(out.keys()) == {"^N225", "^HSI", "^STOXX50E"}
    assert out["^N225"].price == 38000.0
    assert out["^STOXX50E"].change_pct == -0.4


@pytest.mark.asyncio
async def test_forex_picks_latest_usd_cny():
    p = AKShareGlobalProvider(rate_limit_seconds=0.0)
    out = await p.fetch(["USDCNY=X"])
    q = out["USDCNY=X"]
    assert q.price == 7.22
    assert q.previous_close == 7.21
    assert q.trade_date == "2026-05-10"


@pytest.mark.asyncio
async def test_treasury_yields_three_rates():
    p = AKShareGlobalProvider(rate_limit_seconds=0.0)
    out = await p.fetch(["^TNX", "^FVX", "^IRX"])
    assert out["^TNX"].yield_pct == 4.25
    assert out["^FVX"].yield_pct == 4.15
    assert out["^IRX"].yield_pct == 5.42
    assert all(q.price is None for q in out.values())


def test_us_etfs_not_supported():
    p = AKShareGlobalProvider(rate_limit_seconds=0.0)
    assert not p.supports("SPY")
    assert not p.supports("QQQ")
