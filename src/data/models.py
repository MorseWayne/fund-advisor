import math
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Market(str, Enum):
    A_SHARE = "a_share"
    US = "us"
    HK = "hk"


class HoldingCategory(str, Enum):
    BROAD = "broad"
    SECTOR = "sector"
    THEME = "theme"
    OVERSEAS = "overseas"
    BOND = "bond"
    COMMODITY = "commodity"


class SignalDirection(str, Enum):
    OFFENSIVE = "进攻"
    DEFENSIVE = "防守"
    WAIT = "观望"


class ETFDataModel(BaseModel):
    """Pydantic validation model for ETF quote quality checks.

    The runtime pipeline still uses the dataclass below to preserve the
    dataclass -> dict -> SQLite flow. This model is used by validation helpers
    to express field constraints with Pydantic v2.
    """

    model_config = ConfigDict(extra="allow")

    code: str = Field(default="", min_length=1)
    name: str = Field(default="")
    price: float = Field(default=0.0, gt=0)
    change_pct: float = Field(default=0.0, ge=-0.2, le=0.2)
    volume: float = Field(default=0.0, ge=0)
    amount: float = Field(default=0.0, ge=0)
    nav: Optional[float] = Field(default=None, gt=0)
    premium_discount: Optional[float] = Field(default=None)
    pe_ratio: Optional[float] = Field(default=None, ge=0)
    pb_ratio: Optional[float] = Field(default=None, ge=0)

    @field_validator("price", "change_pct", "volume", "amount", "nav", "premium_discount", "pe_ratio", "pb_ratio")
    @classmethod
    def finite_number(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return value
        if not math.isfinite(value):
            raise ValueError("must be a finite number")
        return value


class IndexDataModel(BaseModel):
    """Pydantic validation model for index quote quality checks."""

    model_config = ConfigDict(extra="allow")

    code: str = Field(default="", min_length=1)
    name: str = Field(default="")
    price: float = Field(default=0.0, gt=0)
    change_pct: float = Field(default=0.0, ge=-0.2, le=0.2)
    volume: Optional[float] = Field(default=None, ge=0)
    pe_ratio: Optional[float] = Field(default=None, ge=0)
    pb_ratio: Optional[float] = Field(default=None, ge=0)
    pe_percentile: Optional[float] = Field(default=None, ge=0, le=100)
    pb_percentile: Optional[float] = Field(default=None, ge=0, le=100)

    @field_validator("price", "change_pct", "volume", "pe_ratio", "pb_ratio", "pe_percentile", "pb_percentile")
    @classmethod
    def finite_number(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return value
        if not math.isfinite(value):
            raise ValueError("must be a finite number")
        return value


@dataclass
class ETFData:
    code: str
    name: str
    price: float
    change_pct: float
    volume: float
    amount: float
    nav: Optional[float] = None
    premium_discount: Optional[float] = None
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None


@dataclass
class IndexData:
    code: str
    name: str
    price: float
    change_pct: float
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    pe_percentile: Optional[float] = None
    pb_percentile: Optional[float] = None


@dataclass
class SectorData:
    name: str
    change_pct: float
    momentum_1m: Optional[float] = None
    momentum_3m: Optional[float] = None
    momentum_6m: Optional[float] = None
    fund_flow_direction: Optional[str] = None


@dataclass
class FundFlowData:
    north_bound: float
    main_force: float
    sector_flows: dict[str, float] = field(default_factory=dict)


@dataclass
class DailyMarketSnapshot:
    date: str
    indices: dict[str, IndexData]
    etfs: list[ETFData]
    sectors: dict[str, SectorData]
    fund_flows: Optional[FundFlowData] = None
    macro: dict[str, float] = field(default_factory=dict)
    news_headlines: list[str] = field(default_factory=list)
    valuation: dict[str, float] = field(default_factory=dict)
    precious_metals: dict[str, Any] = field(default_factory=dict)
    qdii_premiums: list[dict[str, Any]] = field(default_factory=list)
    liquidity: dict[str, float] = field(default_factory=dict)
    margin: dict[str, Any] = field(default_factory=dict)
    hsgt_flows: list[dict[str, Any]] = field(default_factory=list)
    sentiment: dict[str, Any] = field(default_factory=dict)
    ladder: dict[str, Any] = field(default_factory=dict)
    hot_themes: list[dict[str, Any]] = field(default_factory=list)
    focus_news: list[dict[str, Any]] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)


@dataclass
class MarketOverview:
    summary: str
    direction: SignalDirection
    key_events: list[str] = field(default_factory=list)


@dataclass
class TrendSignal:
    ma_alignment: str
    standing_line_ratio: float
    vix_level: Optional[float] = None
    sentiment: Optional[str] = None
    position_advice: str = ""
    confidence: float = 0.0


@dataclass
class SectorPick:
    sector_name: str
    etf_code: str
    etf_name: str
    reason: str
    momentum_rank: int


@dataclass
class ValuationAssessment:
    overall_level: str
    pe_percentile: Optional[float] = None
    bond_equity_spread: Optional[float] = None
    etf_premium_alerts: list[str] = field(default_factory=list)
    continue_sip: bool = True


@dataclass
class RiskAlert:
    level: str
    alert_type: str
    message: str
    affected_assets: list[str] = field(default_factory=list)


@dataclass
class HoldingStatus:
    code: str
    name: str
    current_price: float
    change_pct: float
    profit_loss_pct: float
    cost_basis: float
    suggestion: str = ""


@dataclass
class PortfolioStatus:
    holdings: list[HoldingStatus]
    total_value: float
    total_change_pct: float
    total_profit_loss: float


@dataclass
class AnalysisResult:
    date: str
    overview: MarketOverview
    trend: TrendSignal
    sector_opportunities: list[SectorPick] = field(default_factory=list)
    valuation: Optional[ValuationAssessment] = None
    risk_alerts: list[RiskAlert] = field(default_factory=list)
    portfolio_status: Optional[PortfolioStatus] = None
    daily_report_text: str = ""


@dataclass
class Holding:
    code: str
    name: str
    market: Market
    cost_basis: float
    shares: float
    category: HoldingCategory
    notes: str = ""
