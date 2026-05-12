"""Provider contract for global market data.

Every global market data source (Stooq, FRED, AKShare overseas, yfinance)
implements the ``GlobalMarketProvider`` protocol and returns ``Quote`` records
keyed by canonical symbol (yfinance-style: ``^GSPC``, ``SPY``, ``USDCNY=X``,
``^TNX``). Downstream code groups quotes by ``asset_type`` to rebuild the dict
shape that ``DataPipeline.collect_global_data`` historically produced.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol, runtime_checkable


AssetType = Literal[
    "us_etf",
    "global_index",
    "volatility_index",
    "forex",
    "treasury_yield",
]


@dataclass(frozen=True, slots=True)
class Quote:
    """Canonical market quote. Fields irrelevant to an asset class stay None."""

    symbol: str
    name: str
    asset_type: AssetType
    price: float | None = None
    change_pct: float | None = None
    previous_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    amount: float | None = None
    yield_pct: float | None = None
    currency: str | None = None
    exchange: str | None = None
    trade_date: str | None = None
    timestamp: str | None = None
    source: str | None = None


def quote_to_dict(quote: Quote) -> dict[str, Any]:
    """Serialise a Quote to the dict shape consumed by DataPipeline."""
    return {k: v for k, v in asdict(quote).items() if v is not None}


@runtime_checkable
class GlobalMarketProvider(Protocol):
    """Async batch fetcher for global market quotes."""

    name: str

    def supports(self, symbol: str) -> bool:
        """Whether this provider can serve the given canonical symbol."""
        ...

    async def fetch(self, symbols: Sequence[str]) -> dict[str, Quote]:
        """Fetch quotes for the given canonical symbols.

        Returns a dict keyed by canonical symbol. Symbols the provider cannot
        serve, or which yield empty/invalid data, are simply omitted from the
        result — callers use ``ChainedProvider`` to try the next source.
        """
        ...
