"""Multi-provider orchestrator with cache-first, sequential fallback."""

from __future__ import annotations

from datetime import date as date_cls
from typing import Sequence

from loguru import logger

from src.data.collectors.cache import ProviderCache
from src.data.collectors.providers.base import (
    AssetType,
    GlobalMarketProvider,
    Quote,
)
from src.data.collectors.providers.symbol_map import (
    ASSET_TYPE_BY_SYMBOL,
    all_symbols,
)


class ChainedProvider:
    """Compose multiple ``GlobalMarketProvider`` with cache and fallback.

    For each requested symbol:
    1. Try the cache (per-provider keyed on canonical symbol + trade_date).
    2. Walk ``providers`` in order; first one returning a non-None Quote wins.
    3. Cache the winning quote.
    Symbols that no provider can serve are silently omitted from the output.
    """

    def __init__(
        self,
        providers: list[GlobalMarketProvider],
        cache: ProviderCache | None = None,
    ) -> None:
        self.providers = providers
        self.cache = cache

    async def fetch_all(
        self,
        trade_date: str | None = None,
        symbols: Sequence[str] | None = None,
    ) -> dict[AssetType, list[Quote]]:
        universe = list(symbols) if symbols is not None else all_symbols()
        cache_key_date = trade_date or date_cls.today().isoformat()

        quotes: dict[str, Quote] = {}
        pending = list(universe)

        if self.cache is not None:
            for provider in self.providers:
                if not pending:
                    break
                hits = self.cache.get_batch(provider.name, pending, cache_key_date)
                quotes.update(hits)
                pending = [s for s in pending if s not in hits]

        for provider in self.providers:
            if not pending:
                break
            servable = [s for s in pending if provider.supports(s)]
            if not servable:
                continue
            try:
                result = await provider.fetch(servable)
            except Exception as exc:
                logger.warning(
                    "Provider {} fetch raised: {} — falling back to next source",
                    provider.name,
                    exc,
                )
                continue

            for symbol, quote in result.items():
                if quote is None:
                    continue
                quotes[symbol] = quote
                if self.cache is not None:
                    self.cache.put(quote)
            pending = [s for s in pending if s not in result]

        if pending:
            logger.warning(
                "No provider could serve {} symbols: {}",
                len(pending),
                pending,
            )

        grouped: dict[AssetType, list[Quote]] = {
            "us_etf": [],
            "global_index": [],
            "volatility_index": [],
            "forex": [],
            "treasury_yield": [],
        }
        for symbol in universe:
            quote = quotes.get(symbol)
            if quote is None:
                continue
            asset_type = ASSET_TYPE_BY_SYMBOL.get(symbol, quote.asset_type)
            grouped[asset_type].append(quote)
        return grouped


__all__ = ["ChainedProvider"]
