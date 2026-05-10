from datetime import date

import pytest

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
