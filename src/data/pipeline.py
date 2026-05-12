import asyncio
import math
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.config import AppConfig, load_config
from src.data.collectors.akshare_collector import AKShareCollector
from src.data.collectors.cache import ProviderCache
from src.data.collectors.hhxg_collector import HhxgCollector
from src.data.collectors.providers.akshare_global import AKShareGlobalProvider
from src.data.collectors.providers.base import GlobalMarketProvider, Quote, quote_to_dict
from src.data.collectors.providers.chained import ChainedProvider
from src.data.collectors.providers.fred import FREDProvider
from src.data.collectors.providers.stooq import StooqProvider
from src.data.collectors.providers.symbol_map import (
    GLOBAL_INDICES as MONITORED_GLOBAL_INDICES,
    US_ETFS as MONITORED_US_ETFS,
)
from src.data.collectors.providers.yfinance import YFinanceProvider
from src.data.storage import MarketDB
from src.data.portfolio import load_portfolio
from src.data.models import DailyMarketSnapshot, HoldingStatus, PortfolioStatus
from src.data.validation import validate_snapshot  # pyright: ignore[reportMissingImports]
from src.utils.logging_config import setup_logging


def _percent_points_to_ratio(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    try:
        number = float(str(value).strip().replace("%", "").replace(",", ""))
    except (TypeError, ValueError):
        return value
    if not math.isfinite(number):
        return value
    # AKShare index endpoints (e.g. stock_zh_index_spot_sina) return change_pct
    # as a ratio (e.g. -0.003 for -0.3%), while ETF and yfinance endpoints
    # return percentage points (e.g. 3.91 for 3.91%).  A daily move > 20%
    # is extremely rare for ETFs/indices, so |value| <= 0.2 is treated as
    # already a ratio; anything larger is divided by 100.
    if abs(number) <= 0.2:
        return number
    return number / 100.0


def _normalize_market_record(raw_record: Any) -> dict[str, Any] | None:
    if not isinstance(raw_record, dict):
        return None
    record = dict(raw_record)
    if "change_pct" in record:
        record["change_pct"] = _percent_points_to_ratio(record.get("change_pct"))
    return record


def _normalize_etf_record(raw_record: Any) -> dict[str, Any] | None:
    record = _normalize_market_record(raw_record)
    if record is None:
        return None
    # Drop ETFs without a usable price — typically newly-listed funds whose
    # quote feed hasn't published a print yet, or suspended ones. They have
    # no analysis value and break Pydantic validation downstream.
    price = record.get("price")
    if price is None or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        return None
    if record.get("change_pct") is None:
        record["change_pct"] = 0.0
    for field in ("volume", "amount"):
        if record.get(field) is None:
            record[field] = 0.0
    return record


def _valuation_rows(raw_valuation: Any) -> list[dict[str, Any]]:
    """Coerce fetch_valuation_data output into the row shape upsert_valuation expects.

    The collector returns one dict like ``{index_code, pe_ratio, pb_ratio,
    pe_percentile, pb_percentile, ...}`` but the storage layer wants a list of
    rows keyed by ``pe_current / pb_current``. We translate names here so
    neither side has to know about the other.
    """
    if isinstance(raw_valuation, dict):
        items: list[dict[str, Any]] = [raw_valuation]
    elif isinstance(raw_valuation, list):
        items = [v for v in raw_valuation if isinstance(v, dict)]
    else:
        return []

    rows: list[dict[str, Any]] = []
    for item in items:
        code = item.get("index_code")
        if not code:
            continue
        rows.append({
            "index_code": code,
            "pe_current": item.get("pe_current") or item.get("pe_ratio"),
            "pe_percentile": item.get("pe_percentile"),
            "pb_current": item.get("pb_current") or item.get("pb_ratio"),
            "pb_percentile": item.get("pb_percentile"),
        })
    return rows


class DataPipeline:
    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or load_config()
        setup_logging(
            level=self.config.logging.level,
            log_format=self.config.logging.format,
            rotation=self.config.logging.rotation,
            retention=self.config.logging.retention,
        )
        self.db = MarketDB(self.config.data.storage.path)
        self.akshare = AKShareCollector()
        self.hhxg: HhxgCollector | None = (
            HhxgCollector(
                base_url=self.config.data.hhxg.base_url,
                timeout_seconds=self.config.data.hhxg.timeout_seconds,
            )
            if self.config.data.hhxg.enabled
            else None
        )
        self.global_market = self._build_global_market_provider()

    def _build_global_market_provider(self) -> ChainedProvider:
        gm = self.config.data.global_market
        cache = ProviderCache(self.db, ttl_hours=gm.cache_ttl_hours)
        registry: dict[str, GlobalMarketProvider] = {}
        if gm.stooq.enabled:
            registry["stooq"] = StooqProvider(
                base_url=gm.stooq.base_url,
                timeout_seconds=gm.stooq.timeout_seconds,
                max_concurrency=gm.stooq.max_concurrency,
            )
        if gm.fred.enabled:
            registry["fred"] = FREDProvider(
                api_key_env=gm.fred.api_key_env,
                base_url=gm.fred.base_url,
                timeout_seconds=gm.fred.timeout_seconds,
            )
        if gm.akshare_global.enabled:
            registry["akshare_global"] = AKShareGlobalProvider(
                rate_limit_seconds=gm.akshare_global.rate_limit_seconds,
            )
        if gm.yfinance.enabled:
            registry["yfinance"] = YFinanceProvider(
                rate_limit_seconds=gm.yfinance.rate_limit_seconds,
            )
        ordered = [registry[name] for name in gm.providers if name in registry]
        if not ordered:
            logger.warning("No global market providers enabled — global data will be empty")
        return ChainedProvider(ordered, cache=cache)

    async def collect_a_share_data(self) -> dict[str, Any]:
        logger.info("Collecting A-share data via AKShare...")

        tasks = [
            self.akshare.fetch_etf_spot_data(),
            self.akshare.fetch_index_data(),
            self.akshare.fetch_sector_rankings(),
            self.akshare.fetch_fund_flow_data(),
            self.akshare.fetch_valuation_data(),
            self.akshare.fetch_news_headlines(),
            self.akshare.fetch_precious_metals_data(),
            self.akshare.fetch_qdii_premium(),
            self.akshare.fetch_macro_liquidity(),
            self.akshare.fetch_margin_balance(),
            self.akshare.fetch_hsgt_flow_trend(),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        etf_result, index_result, sector_result, flow_result, val_result, news_result, pm_result, qdii_result, liquidity_result, margin_result, hsgt_result = results

        def _safe_dict(result, key, default=None):
            if isinstance(result, Exception):
                logger.error(f"{key} fetch failed: {result}")
                return default if default is not None else []
            if isinstance(result, dict):
                return result.get(key, result)
            return result

        etfs = _safe_dict(etf_result, "etfs", [])
        indices = _safe_dict(index_result, "indices", [])
        sectors = _safe_dict(sector_result, "sectors", [])
        fund_flows = _safe_dict(flow_result, "flows", {}) if not isinstance(flow_result, Exception) else {}
        # fetch_valuation_data returns a single-index dict {index_code, pe_ratio, ...},
        # not a {"data": [...]} wrapper. Pass it through as a dict and let the
        # downstream snapshot builder coerce it to list[dict].
        if isinstance(val_result, Exception):
            logger.error("valuation fetch failed: {}", val_result)
            valuation: Any = {}
        else:
            valuation = val_result if isinstance(val_result, dict) else {}
        news = _safe_dict(news_result, "news", [])

        sentiment, ladder, hot_themes, focus_news = await self._collect_hhxg()

        return {
            "etfs": etfs if isinstance(etfs, list) else [],
            "indices": indices if isinstance(indices, list) else [],
            "sectors": sectors if isinstance(sectors, list) else [],
            "fund_flows": fund_flows if isinstance(fund_flows, dict) else {},
            "valuation": valuation,
            "news": news if isinstance(news, list) else [],
            "precious_metals": _safe_dict(pm_result, "precious_metals", {}) if isinstance(pm_result, dict) else {},
            "qdii_premiums": _safe_dict(qdii_result, "qdii_premiums", []) if isinstance(qdii_result, dict) else [],
            "liquidity": _safe_dict(liquidity_result, "liquidity", {}) if isinstance(liquidity_result, dict) else {},
            "margin": _safe_dict(margin_result, "margin_balance") if isinstance(margin_result, dict) else {},
            "hsgt_flows": _safe_dict(hsgt_result, "recent_flows", []) if isinstance(hsgt_result, dict) else [],
            "sentiment": sentiment,
            "ladder": ladder,
            "hot_themes": hot_themes,
            "focus_news": focus_news,
        }

    async def _collect_hhxg(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        """Best-effort hhxg.top sentiment fetch. Returns empty containers on any failure."""
        if self.hhxg is None:
            return {}, {}, [], []
        results = await asyncio.gather(
            self.hhxg.fetch_sentiment(),
            self.hhxg.fetch_ladder(),
            self.hhxg.fetch_hot_themes(),
            self.hhxg.fetch_focus_news(),
            return_exceptions=True,
        )
        sentiment = results[0] if isinstance(results[0], dict) else {}
        ladder = results[1] if isinstance(results[1], dict) else {}
        hot_themes = results[2] if isinstance(results[2], list) else []
        focus_news = results[3] if isinstance(results[3], list) else []
        for idx, label in enumerate(("sentiment", "ladder", "hot_themes", "focus_news")):
            if isinstance(results[idx], Exception):
                logger.warning("hhxg {} fetch failed: {}", label, results[idx])
        return sentiment, ladder, hot_themes, focus_news

    async def collect_global_data(self) -> dict[str, Any]:
        logger.info("Collecting global market data via chained providers...")
        try:
            grouped = await self.global_market.fetch_all(trade_date=date.today().isoformat())
        except Exception as exc:
            logger.error(f"Global data fetch failed: {exc}")
            grouped = {
                "us_etf": [],
                "global_index": [],
                "volatility_index": [],
                "forex": [],
                "treasury_yield": [],
            }

        us_etfs = [quote_to_dict(q) for q in grouped.get("us_etf", [])]
        global_indices = [quote_to_dict(q) for q in grouped.get("global_index", [])]
        vix_quotes = grouped.get("volatility_index", [])
        vix_data = quote_to_dict(vix_quotes[0]) if vix_quotes else {}
        forex_list = [quote_to_dict(q) for q in grouped.get("forex", [])]
        yields_list = [
            {**quote_to_dict(q), "yield_pct": q.yield_pct if q.yield_pct is not None else q.price}
            for q in grouped.get("treasury_yield", [])
        ]

        if not global_indices and not us_etfs and not vix_data:
            logger.warning(
                "All global market providers failed or disabled — downstream global data is empty"
            )

        # Per-provider/per-asset-type breakdown is already logged by ChainedProvider._log_summary.

        macro: dict[str, float] = {}
        if isinstance(vix_data, dict) and vix_data.get("price") is not None:
            macro["vix"] = float(vix_data["price"])
        for fx in forex_list:
            if isinstance(fx, dict):
                price = fx.get("price")
                if price is None:
                    continue
                price = float(price)
                symbol = str(fx.get("symbol", "forex"))
                macro[symbol] = price
                if symbol == "USDCNY=X" or str(fx.get("name", "")).upper() == "USD/CNY":
                    macro["usdcny"] = price
        for y in yields_list:
            if not isinstance(y, dict):
                continue
            yield_value = y.get("yield_pct", y.get("price"))
            if yield_value is None:
                continue
            yield_value = float(yield_value)
            symbol = str(y.get("symbol", ""))
            name = str(y.get("name", "")).replace(" ", "_").lower()
            if name:
                macro[name] = yield_value
            if symbol == "^TNX":
                macro["us10y"] = yield_value
            elif symbol == "^FVX":
                macro["us5y"] = yield_value
            elif symbol == "^IRX":
                macro["us3m"] = yield_value

        for fetcher in (
            self.akshare.fetch_cn_10y_yield,
            self.akshare.fetch_cpi,
            self.akshare.fetch_gdp,
            self.akshare.fetch_pmi,
        ):
            macro_data = await fetcher()
            if isinstance(macro_data, dict):
                macro.update(macro_data)

        return {
            "us_etfs": us_etfs,
            "global_indices": global_indices,
            "macro": macro,
        }

    async def run_daily_collection(self, target_date: Optional[str] = None) -> DailyMarketSnapshot:
        today = target_date or date.today().strftime("%Y-%m-%d")
        logger.info(f"Starting daily data collection for {today}")
        started_at = time.monotonic()

        a_share_data, global_data = await asyncio.gather(
            self.collect_a_share_data(),
            self.collect_global_data(),
        )

        etfs = [record for item in a_share_data.get("etfs", []) if (record := _normalize_etf_record(item)) is not None]
        sectors = [record for item in a_share_data.get("sectors", []) if (record := _normalize_market_record(item)) is not None]
        all_indices: dict[str, dict[str, Any]] = {}

        def _add_index(raw_index: Any) -> None:
            index_record = _normalize_market_record(raw_index)
            if index_record is None:
                return
            code = str(index_record.get("code") or index_record.get("symbol") or "").strip()
            if not code:
                logger.warning(f"Skipping index record without code/symbol: {index_record}")
                return
            name = str(index_record.get("name") or index_record.get("label") or code).strip() or code
            index_record["code"] = code
            index_record["name"] = name
            all_indices[code] = index_record

        for idx in a_share_data.get("indices", []):
            _add_index(idx)
        for idx in global_data.get("global_indices", []):
            _add_index(idx)

        macro = global_data.get("macro", {})
        news = a_share_data.get("news", [])

        self.db.upsert_etfs(today, etfs)
        self.db.upsert_indices(today, list(all_indices.values()))
        self.db.upsert_sectors(today, sectors)

        fund_flows = a_share_data.get("fund_flows", {}) or {}
        self.db.upsert_fund_flow(
            today,
            fund_flows.get("north_bound"),
            fund_flows.get("main_force"),
            fund_flows.get("sector_flows"),
        )

        self.db.upsert_macro(today, macro)
        self.db.upsert_news(today, [n.get("title", n) if isinstance(n, dict) else n for n in news])
        self.db.upsert_valuation(today, _valuation_rows(a_share_data.get("valuation")))

        from src.data.models import IndexData, ETFData, SectorData, FundFlowData
        index_configs = {
            "sh000001": "上证指数", "sh000300": "沪深300", "sh000016": "上证50",
            "sh000688": "科创50", "sz399001": "深证成指", "sz399006": "创业板指",
            "^GSPC": "标普500", "^IXIC": "纳斯达克", "^HSI": "恒生指数", "^N225": "日经225",
        }
        index_models = {}
        for idx_dict in all_indices.values():
            code = idx_dict.get("code", "")
            name = idx_dict.get("name", index_configs.get(code, code))
            index_models[code] = IndexData(
                code=code, name=name, price=idx_dict.get("price", 0),
                change_pct=idx_dict.get("change_pct", 0),
                pe_ratio=idx_dict.get("pe_ratio"), pb_ratio=idx_dict.get("pb_ratio"),
                pe_percentile=idx_dict.get("pe_percentile"), pb_percentile=idx_dict.get("pb_percentile"),
            )

        etf_models = []
        for etf_dict in etfs:
            etf_models.append(ETFData(
                code=etf_dict.get("code", ""), name=etf_dict.get("name", ""),
                price=etf_dict.get("price", 0), change_pct=etf_dict.get("change_pct", 0),
                volume=etf_dict.get("volume", 0), amount=etf_dict.get("amount", 0),
                nav=etf_dict.get("nav"), premium_discount=etf_dict.get("premium_discount"),
                pe_ratio=etf_dict.get("pe_ratio"), pb_ratio=etf_dict.get("pb_ratio"),
            ))

        sector_models = {}
        for s in sectors:
            name = s.get("name", "")
            sector_models[name] = SectorData(
                name=name, change_pct=s.get("change_pct", 0),
                momentum_1m=s.get("momentum_1m"), momentum_3m=s.get("momentum_3m"),
                momentum_6m=s.get("momentum_6m"),
            )

        fund_flow_model = None
        if fund_flows:
            fund_flow_model = FundFlowData(
                north_bound=fund_flows.get("north_bound", 0),
                main_force=fund_flows.get("main_force", 0),
            )

        headlines = [n.get("title", n) if isinstance(n, dict) else str(n) for n in (news or [])]

        raw_val = a_share_data.get("valuation", [])
        val_list: list[dict[str, Any]] = [raw_val] if isinstance(raw_val, dict) else (raw_val if isinstance(raw_val, list) else [])
        valuation_summary: dict[str, float] = {}
        for v in val_list:
            if isinstance(v, dict):
                valuation_summary[v.get("index_code", "")] = float(v.get("pe_percentile", 0))

        snapshot = DailyMarketSnapshot(
            date=today, indices=index_models, etfs=etf_models,
            sectors=sector_models, fund_flows=fund_flow_model,
            macro=macro, news_headlines=headlines[:10], valuation=valuation_summary,
            precious_metals=a_share_data.get("precious_metals", {}),
            qdii_premiums=a_share_data.get("qdii_premiums", []),
            liquidity=a_share_data.get("liquidity", {}),
            margin=a_share_data.get("margin", {}),
            hsgt_flows=a_share_data.get("hsgt_flows", []),
            sentiment=a_share_data.get("sentiment", {}),
            ladder=a_share_data.get("ladder", {}),
            hot_themes=a_share_data.get("hot_themes", []),
            focus_news=a_share_data.get("focus_news", []),
        )

        validation = validate_snapshot(snapshot)
        snapshot.validation_errors = validation.errors
        snapshot.validation_warnings = validation.warnings
        if not validation.success:
            logger.error(f"Snapshot validation failed: {validation.errors}")
        if validation.warnings:
            logger.warning(f"Snapshot validation warnings: {validation.warnings}")

        elapsed = time.monotonic() - started_at
        self._log_daily_summary(
            today,
            elapsed,
            validation_ok=validation.success,
            a_share=a_share_data,
            global_data=global_data,
            etf_count=len(etf_models),
            index_count=len(index_models),
            sector_count=len(sector_models),
            news_count=len(headlines),
        )
        return snapshot

    @staticmethod
    def _log_daily_summary(
        today: str,
        elapsed: float,
        *,
        validation_ok: bool,
        a_share: dict[str, Any],
        global_data: dict[str, Any],
        etf_count: int,
        index_count: int,
        sector_count: int,
        news_count: int,
    ) -> None:
        fund_flows = a_share.get("fund_flows") or {}
        liquidity = a_share.get("liquidity") or {}
        margin = a_share.get("margin") or {}
        macro = global_data.get("macro") or {}
        us_etfs = global_data.get("us_etfs") or []
        global_indices = global_data.get("global_indices") or []
        sentiment = a_share.get("sentiment") or {}
        ladder = a_share.get("ladder") or {}
        hot_themes = a_share.get("hot_themes") or []
        focus_news = a_share.get("focus_news") or []
        valuation = a_share.get("valuation") or {}
        if isinstance(valuation, dict):
            valuation_ok = bool(valuation.get("pe_ratio") or valuation.get("pb_ratio"))
        else:
            valuation_ok = bool(valuation)

        # ChainedProvider already logs per-asset-type / per-provider detail.
        # This is the cross-source roll-up so a single log line answers
        # "did today's collection look right?".
        lines = [
            f"=== Daily collection summary {today} ===",
            f"Elapsed: {elapsed:.1f}s  Validation: {'OK' if validation_ok else 'FAIL'}",
            "[A-share]",
            f"  etfs={etf_count}  indices={index_count}  sectors={sector_count}  news={news_count}",
            f"  fund_flows={'y' if fund_flows else 'n'}"
            f"  valuation={'y' if valuation_ok else 'n'}"
            f"  precious_metals={len(a_share.get('precious_metals') or {})}"
            f"  qdii={len(a_share.get('qdii_premiums') or [])}"
            f"  liquidity={'y' if liquidity else 'n'}"
            f"  margin={'y' if margin else 'n'}"
            f"  hsgt={len(a_share.get('hsgt_flows') or [])}",
            "[Global]",
            f"  us_etfs={len(us_etfs)}  global_indices={len(global_indices)}",
            f"  macro_fields={len(macro)} ({', '.join(sorted(macro)[:8])}"
            f"{'…' if len(macro) > 8 else ''})",
        ]

        if sentiment or ladder or hot_themes or focus_news:
            si = sentiment.get("sentiment_index")
            si_label = sentiment.get("sentiment_label") or "?"
            lu = sentiment.get("limit_up")
            fr = sentiment.get("fried")
            ld = sentiment.get("limit_down")
            max_streak = ladder.get("max_streak")
            lines.append("[hhxg]")
            lines.append(
                "  sentiment_index="
                + (f"{si}({si_label})" if si is not None else "—")
                + f"  limit_up={lu if lu is not None else '—'}"
                + f"  fried={fr if fr is not None else '—'}"
                + f"  limit_down={ld if ld is not None else '—'}"
            )
            lines.append(
                f"  max_streak={max_streak if max_streak is not None else '—'}"
                f"  themes={len(hot_themes)}  focus_news={len(focus_news)}"
            )
        logger.info("\n".join(lines))

    def calc_holding_status(self, snapshot: DailyMarketSnapshot) -> PortfolioStatus:
        holdings = load_portfolio()
        etf_map = {e.code: e for e in snapshot.etfs}
        us_etf_map = {}
        for idx_code, idx in snapshot.indices.items():
            us_etf_map[idx_code] = idx

        statuses = []
        for h in holdings:
            price = 0.0
            change_pct = 0.0
            name = h.name

            if h.code in etf_map:
                e = etf_map[h.code]
                price = e.price
                change_pct = e.change_pct
                name = e.name or name
            elif h.code in us_etf_map:
                idx = us_etf_map[h.code]
                price = idx.price
                change_pct = idx.change_pct

            if price > 0 and h.cost_basis > 0:
                profit_loss_pct = (price - h.cost_basis) / h.cost_basis * 100
            else:
                profit_loss_pct = 0.0

            suggestion = "继续持有"
            if profit_loss_pct > 15:
                suggestion = "盈利超15%，可考虑分批止盈"
            elif profit_loss_pct < -10:
                suggestion = "亏损超10%，审视是否止损"

            statuses.append(HoldingStatus(
                code=h.code, name=name, current_price=price, change_pct=change_pct,
                profit_loss_pct=profit_loss_pct, cost_basis=h.cost_basis, suggestion=suggestion,
            ))

        total = sum(s.current_price * h.shares for s, h in zip(statuses, holdings) if s.current_price > 0)
        pnl = sum((s.current_price - s.cost_basis) * h.shares for s, h in zip(statuses, holdings) if s.current_price > 0)
        total_change = pnl / (total - pnl) * 100 if (total - pnl) > 0 else 0

        return PortfolioStatus(
            holdings=statuses, total_value=total, total_change_pct=total_change,
            total_profit_loss=pnl,
        )


class BackfillPipeline:
    """Backfill ETF and index OHLCV history with incremental updates."""

    def __init__(
        self,
        akshare: AKShareCollector,
        db: MarketDB,
        config: AppConfig,
    ) -> None:
        self.akshare = akshare
        self.db = db
        self.config = config
        akshare_source = self.config.data.sources.get("akshare")
        self.akshare_sleep = akshare_source.rate_limit_seconds if akshare_source else 1.0
        self.yfinance_sleep = self.config.data.global_market.yfinance.rate_limit_seconds

    def run_backfill(self, days: int = 365) -> dict[str, int]:
        """Backfill configured holdings and monitored markets for the last ``days`` days."""
        logger.info(f"Starting historical data backfill for {days} days")
        self.db.create_history_tables()

        universe = self._build_backfill_universe()
        summary = {
            "a_share_etfs": 0,
            "global_etfs": 0,
            "global_indices": 0,
            "etf_records": 0,
            "index_records": 0,
        }

        for code, name in universe["a_share_etfs"].items():
            records = self._fetch_a_share_etf_history(code, days)
            if records:
                summary["etf_records"] += self.db.upsert_etf_history(records)
            summary["a_share_etfs"] += 1
            time.sleep(self.akshare_sleep)

        for code, name in universe["global_etfs"].items():
            records = self._fetch_yfinance_history(code, name, days, include_name=False)
            if records:
                summary["etf_records"] += self.db.upsert_etf_history(records)
            summary["global_etfs"] += 1
            time.sleep(self.yfinance_sleep)

        for code, name in universe["global_indices"].items():
            records = self._fetch_yfinance_history(code, name, days, include_name=True)
            if records:
                summary["index_records"] += self.db.upsert_index_history(records)
            summary["global_indices"] += 1
            time.sleep(self.yfinance_sleep)

        logger.info(f"Historical data backfill complete: {summary}")
        return summary

    def _build_backfill_universe(self) -> dict[str, dict[str, str]]:
        """Combine portfolio holdings with configured monitor lists."""
        universe: dict[str, dict[str, str]] = {
            "a_share_etfs": {},
            "global_etfs": {},
            "global_indices": {},
        }

        holdings = load_portfolio()
        for holding in holdings:
            if holding.market.value == "a_share":
                universe["a_share_etfs"][holding.code] = holding.name
            else:
                universe["global_etfs"][holding.code] = holding.name

        for symbol, name in MONITORED_US_ETFS.items():
            universe["global_etfs"].setdefault(symbol, name)
        for symbol, name in MONITORED_GLOBAL_INDICES.items():
            universe["global_indices"].setdefault(symbol, name)

        for item in self._load_config_monitor_items():
            code = str(item.get("code") or item.get("symbol") or "").strip()
            if not code:
                continue
            name = str(item.get("name") or item.get("label") or code)
            market = str(item.get("market") or "").lower()
            asset_type = str(item.get("type") or item.get("asset_type") or item.get("category") or "").lower()
            if market == "a_share":
                universe["a_share_etfs"].setdefault(code, name)
            elif "index" in asset_type or code.startswith("^"):
                universe["global_indices"].setdefault(code, name)
            else:
                universe["global_etfs"].setdefault(code, name)

        logger.info(
            "Backfill universe prepared: "
            f"{len(universe['a_share_etfs'])} A-share ETFs, "
            f"{len(universe['global_etfs'])} global ETFs, "
            f"{len(universe['global_indices'])} global indices"
        )
        return universe

    def _load_config_monitor_items(self) -> list[dict[str, Any]]:
        """Read optional monitor/watchlist entries from config.yaml without changing AppConfig."""
        path = Path("config/config.yaml")
        if not path.exists():
            logger.warning("config/config.yaml not found while loading monitor list")
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception as exc:
            logger.error(f"Failed to read config monitor list: {exc}")
            return []

        items: list[dict[str, Any]] = []
        self._collect_monitor_items(raw, items, parent_key="")
        logger.info(f"Loaded {len(items)} monitor entries from config.yaml")
        return items

    def _collect_monitor_items(self, value: Any, items: list[dict[str, Any]], parent_key: str) -> None:
        monitor_keys = {
            "monitor", "monitors", "monitoring", "watchlist", "watchlists",
            "etfs", "indices", "global_indices", "a_share_etfs", "us_etfs",
        }
        if isinstance(value, dict):
            if "code" in value or "symbol" in value:
                item = dict(value)
                if "index" in parent_key and "type" not in item:
                    item["type"] = "index"
                items.append(item)
                return
            for key, child in value.items():
                key_text = str(key).lower()
                self._collect_monitor_items(child, items, key_text)
        elif isinstance(value, list):
            is_monitor_list = parent_key in monitor_keys or "monitor" in parent_key or "watch" in parent_key
            is_market_list = "etf" in parent_key or "index" in parent_key
            for child in value:
                if isinstance(child, dict):
                    self._collect_monitor_items(child, items, parent_key)
                elif isinstance(child, str) and (is_monitor_list or is_market_list):
                    item: dict[str, Any] = {"code": child, "name": child}
                    if "index" in parent_key:
                        item["type"] = "index"
                    items.append(item)

    def _fetch_a_share_etf_history(self, code: str, days: int) -> list[dict[str, Any]]:
        start_date, end_date = self._date_range_after_latest(code, days, is_index=False)
        if start_date is None:
            logger.info(f"A-share ETF {code} history already up to date")
            return []

        logger.info(f"Fetching A-share ETF history: {code} {start_date} -> {end_date}")
        try:
            import akshare as ak

            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            )
            if df is None or df.empty:
                logger.warning(f"No A-share ETF history returned for {code}")
                return []
            records = []
            for _, row in df.iterrows():
                records.append({
                    "date": self._format_date(row.get("日期")),
                    "code": code,
                    "open": row.get("开盘"),
                    "high": row.get("最高"),
                    "low": row.get("最低"),
                    "close": row.get("收盘"),
                    "volume": row.get("成交量"),
                    "amount": row.get("成交额"),
                })
            logger.info(f"Fetched {len(records)} A-share ETF history rows for {code}")
            return records
        except Exception as exc:
            logger.error(f"Failed to fetch A-share ETF history for {code}: {exc}")
            return []

    def _fetch_yfinance_history(
        self,
        code: str,
        name: str,
        days: int,
        *,
        include_name: bool,
    ) -> list[dict[str, Any]]:
        start_date, end_date = self._date_range_after_latest(code, days, is_index=include_name)
        if start_date is None:
            logger.info(f"yfinance history {code} already up to date")
            return []

        logger.info(f"Fetching yfinance history: {code} {start_date} -> {end_date}")
        try:
            import yfinance as yf

            ticker = yf.Ticker(code)
            hist = ticker.history(start=start_date, end=end_date)
            if hist is None or hist.empty:
                logger.warning(f"No yfinance history returned for {code}")
                return []
            records = []
            for row_date, row in hist.iterrows():
                close = row.get("Close")
                volume = row.get("Volume")
                record = {
                    "date": self._format_date(row_date),
                    "code": code,
                    "open": row.get("Open"),
                    "high": row.get("High"),
                    "low": row.get("Low"),
                    "close": close,
                    "volume": volume,
                    "amount": self._amount_from_close_volume(close, volume),
                }
                if include_name:
                    record["name"] = name
                records.append(record)
            logger.info(f"Fetched {len(records)} yfinance history rows for {code}")
            return records
        except Exception as exc:
            logger.error(f"Failed to fetch yfinance history for {code}: {exc}")
            return []

    def _date_range_after_latest(self, code: str, days: int, *, is_index: bool) -> tuple[Optional[str], str]:
        end = date.today()
        latest = self.db.get_latest_index_history_date(code) if is_index else self.db.get_latest_etf_history_date(code)
        if latest:
            try:
                start = datetime.strptime(latest, "%Y-%m-%d").date() + timedelta(days=1)
            except ValueError:
                logger.warning(f"Invalid latest history date for {code}: {latest}; falling back to {days} days")
                start = end - timedelta(days=days)
        else:
            start = end - timedelta(days=days)

        if start > end:
            return None, end.isoformat()
        return start.isoformat(), end.isoformat()

    @staticmethod
    def _format_date(value: Any) -> str:
        if hasattr(value, "date"):
            return value.date().isoformat()
        text = str(value)
        return text[:10]

    @staticmethod
    def _amount_from_close_volume(close: Any, volume: Any) -> Optional[float]:
        try:
            if close is None or volume is None:
                return None
            return float(close) * float(volume)
        except (TypeError, ValueError):
            return None


async def main():
    pipeline = DataPipeline()


def generate_monthly_pnl_summary(db_path: str = "data/fund_advisor.db") -> str:
    """Generate a month-end portfolio P&L summary.

    Compares the most recent snapshot to the snapshot from the end of the
    previous month. Returns a formatted markdown string suitable for
    push notification or display.

    Returns empty string if insufficient data.
    """

    import sqlite3
    import json
    from datetime import date, timedelta
    from pathlib import Path

    db_file = Path(db_path)
    if not db_file.exists():
        return ""

    today = date.today()
    # First day of current month
    first_of_month = today.replace(day=1)
    # Last day of previous month
    last_of_prev = first_of_month - timedelta(days=1)
    prev_month_start = last_of_prev.replace(day=1)

    try:
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row

        # Get latest snapshot date
        row = conn.execute(
            "SELECT MAX(date) as max_date FROM index_daily"
        ).fetchone()
        if not row or not row["max_date"]:
            conn.close()
            return ""
        latest_date = row["max_date"]

        # Get previous month-end snapshot (closest to last day of previous month)
        row = conn.execute(
            "SELECT MAX(date) as prev_date FROM index_daily WHERE date <= ?",
            (last_of_prev.isoformat(),),
        ).fetchone()
        prev_date = row["prev_date"] if row else None

        # Get portfolio value from macro_daily.extra (saved during snapshot)
        # Fallback: use etf_daily for holdings
        # For MVP, read the audit log for latest report's portfolio data
        audit_path = Path("data/reports/report-audit.jsonl")
        current_value = None
        prev_value = None

        if audit_path.exists():
            lines = audit_path.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                evidence = record.get("evidence_payload", {})
                sections = evidence.get("sections", {})
                portfolio = sections.get("portfolio_status", {})
                if portfolio.get("total_value"):
                    current_value = float(portfolio["total_value"])
                    current_pnl = float(portfolio.get("total_profit_loss", 0))
                    current_change = float(portfolio.get("total_change_pct", 0))
                    break

            # Find previous month-end report
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                as_of = record.get("as_of_date", "")
                if as_of < prev_month_start.isoformat() or as_of > last_of_prev.isoformat():
                    continue
                evidence = record.get("evidence_payload", {})
                sections = evidence.get("sections", {})
                portfolio = sections.get("portfolio_status", {})
                if portfolio.get("total_value"):
                    prev_value = float(portfolio["total_value"])
                    break

        conn.close()

        if current_value is None:
            return ""

        lines_output = [
            f"📊 {latest_date} 月度持仓盈亏汇总",
            "",
            f"当前组合价值：¥{current_value:,.0f}",
        ]

        if prev_value and prev_value > 0:
            monthly_change = (current_value - prev_value) / prev_value * 100
            monthly_pnl = current_value - prev_value
            emoji = "📈" if monthly_change >= 0 else "📉"
            lines_output.append(
                f"本月变动：{emoji} ¥{monthly_pnl:+,.0f} ({monthly_change:+.2f}%)"
            )
            lines_output.append(f"上月结算：¥{prev_value:,.0f}")

        lines_output.append("")
        lines_output.append("详细报告请查看 Streamlit 看板或最新投资报告。")

        return "\n".join(lines_output)

    except Exception:
        return ""


if __name__ == "__main__":
    asyncio.run(main())
