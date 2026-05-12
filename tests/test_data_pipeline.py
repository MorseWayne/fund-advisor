from datetime import date

import pytest

from src.data.collectors.providers.base import Quote
from src.data.portfolio import load_portfolio
from src.data.pipeline import DataPipeline


class FakeMarketDB:
    def __init__(self):
        self.etfs = []
        self.indices = []

    def upsert_etfs(self, date_str, etfs):
        assert all(abs(etf.get("change_pct", 0)) <= 0.2 for etf in etfs)
        self.etfs = etfs
        return len(etfs)

    def upsert_indices(self, date_str, indices):
        assert all(index.get("code") for index in indices)
        assert all(index.get("name") for index in indices)
        self.indices = indices
        return len(indices)

    def upsert_sectors(self, date_str, sectors):
        return len(sectors)

    def upsert_fund_flow(self, date_str, north_bound, main_force, sector_flows):
        return None

    def upsert_macro(self, date_str, macro_data):
        return None

    def upsert_news(self, date_str, headlines):
        return len(headlines)

    def upsert_valuation(self, date_str, valuation):
        return len(valuation)


@pytest.mark.asyncio
async def test_run_daily_collection_normalizes_global_index_symbols():
    pipeline = object.__new__(DataPipeline)
    pipeline.db = FakeMarketDB()

    async def collect_a_share_data():
        return {
            "etfs": [
                {
                    "code": "510300",
                    "name": "CSI 300 ETF",
                    "price": 4.0,
                    "change_pct": 1.0,
                    "volume": None,
                    "amount": None,
                }
            ],
            "indices": [
                {
                    "code": "sh000001",
                    "name": "SSE Composite",
                    "price": 3000.0,
                    "change_pct": -0.5,
                }
            ],
            "sectors": [],
            "fund_flows": {},
            "valuation": [],
            "news": [],
        }

    async def collect_global_data():
        return {
            "global_indices": [
                {
                    "symbol": "^GSPC",
                    "name": "S&P 500",
                    "price": 5000.0,
                    "change_pct": 0.75,
                }
            ],
            "macro": {"vix": 20.0},
        }

    pipeline.collect_a_share_data = collect_a_share_data
    pipeline.collect_global_data = collect_global_data

    snapshot = await pipeline.run_daily_collection(date.today().strftime("%Y-%m-%d"))

    assert "^GSPC" in snapshot.indices
    assert snapshot.indices["^GSPC"].code == "^GSPC"
    assert snapshot.indices["^GSPC"].change_pct == pytest.approx(0.0075)
    assert snapshot.etfs[0].change_pct == pytest.approx(0.01)
    assert snapshot.etfs[0].volume == 0.0
    assert snapshot.etfs[0].amount == 0.0
    assert snapshot.validation_errors == []
    assert {index["code"] for index in pipeline.db.indices} == {"sh000001", "^GSPC"}
    stored_changes = sorted(index["change_pct"] for index in pipeline.db.indices)
    assert stored_changes == pytest.approx([-0.005, 0.0075])


class FakeChainedProvider:
    def __init__(self, grouped):
        self._grouped = grouped

    async def fetch_all(self, trade_date=None, symbols=None):
        return self._grouped


@pytest.mark.asyncio
async def test_collect_global_data_converts_quotes_into_legacy_shape():
    pipeline = object.__new__(DataPipeline)

    class _FakeAk:
        async def fetch_cn_10y_yield(self):
            return {}

        async def fetch_cpi(self):
            return {}

        async def fetch_gdp(self):
            return {}

        async def fetch_pmi(self):
            return {}

    pipeline.akshare = _FakeAk()
    pipeline.global_market = FakeChainedProvider(
        {
            "us_etf": [
                Quote(
                    symbol="SPY",
                    name="SPDR S&P 500 ETF",
                    asset_type="us_etf",
                    price=500.0,
                    change_pct=1.0,
                    volume=1_000_000,
                    source="stooq",
                    trade_date="2026-05-10",
                )
            ],
            "global_index": [
                Quote(
                    symbol="^GSPC",
                    name="S&P 500",
                    asset_type="global_index",
                    price=5000.0,
                    change_pct=0.75,
                    source="stooq",
                    trade_date="2026-05-10",
                )
            ],
            "volatility_index": [
                Quote(
                    symbol="^VIX",
                    name="CBOE Volatility Index",
                    asset_type="volatility_index",
                    price=18.5,
                    change_pct=-2.0,
                    source="stooq",
                    trade_date="2026-05-10",
                )
            ],
            "forex": [
                Quote(
                    symbol="USDCNY=X",
                    name="USD/CNY",
                    asset_type="forex",
                    price=7.21,
                    source="akshare_global",
                    trade_date="2026-05-10",
                )
            ],
            "treasury_yield": [
                Quote(
                    symbol="^TNX",
                    name="US 10Y Treasury Yield",
                    asset_type="treasury_yield",
                    yield_pct=4.25,
                    source="fred",
                    trade_date="2026-05-10",
                ),
                Quote(
                    symbol="^FVX",
                    name="US 5Y Treasury Yield",
                    asset_type="treasury_yield",
                    yield_pct=4.10,
                    source="fred",
                    trade_date="2026-05-10",
                ),
            ],
        }
    )

    result = await pipeline.collect_global_data()

    assert result["us_etfs"][0]["symbol"] == "SPY"
    assert result["global_indices"][0]["symbol"] == "^GSPC"
    macro = result["macro"]
    assert macro["vix"] == 18.5
    assert macro["usdcny"] == 7.21
    assert macro["us10y"] == 4.25
    assert macro["us5y"] == 4.10


def test_load_portfolio_accepts_commodity_category(tmp_path):
    portfolio_file = tmp_path / "portfolio.yaml"
    portfolio_file.write_text(
        """
holdings:
  - code: "518880"
    name: "黄金ETF"
    market: "a_share"
    cost_basis: 5.85
    shares: 1000
    category: "commodity"
""".strip(),
        encoding="utf-8",
    )

    holdings = load_portfolio(portfolio_file)

    assert len(holdings) == 1
    assert holdings[0].code == "518880"
    assert holdings[0].category.value == "commodity"
