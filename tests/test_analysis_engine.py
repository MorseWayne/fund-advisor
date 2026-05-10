from src.analysis.engine import AnalysisEngine
from src.data.models import DailyMarketSnapshot, ETFData, IndexData


def test_analyze_accepts_daily_market_snapshot_dataclass():
    snapshot = DailyMarketSnapshot(
        date="2026-05-10",
        indices={
            "sh000001": IndexData(
                code="sh000001",
                name="SSE Composite",
                price=3000.0,
                change_pct=0.01,
            )
        },
        etfs=[
            ETFData(
                code="510300",
                name="CSI 300 ETF",
                price=4.0,
                change_pct=0.01,
                volume=1000.0,
                amount=4000.0,
            )
        ],
        sectors={},
        macro={"vix": 20.0},
    )

    analysis = AnalysisEngine().analyze(snapshot)

    assert analysis["date"] == "2026-05-10"
    assert analysis["overview"]["direction"] in {"进攻", "防守", "观望"}
    