import asyncio
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
from src.data.collectors.yfinance_collector import YFinanceCollector
from src.data.storage import MarketDB
from src.data.portfolio import load_portfolio
from src.data.models import DailyMarketSnapshot, HoldingStatus, PortfolioStatus
from src.data.validation import validate_snapshot  # pyright: ignore[reportMissingImports]
from src.utils.logging_config import setup_logging


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
        self.yfinance = YFinanceCollector()

    async def collect_a_share_data(self) -> dict[str, Any]:
        logger.info("Collecting A-share data via AKShare...")

        tasks = [
            self.akshare.fetch_etf_spot_data(),
            self.akshare.fetch_index_data(),
            self.akshare.fetch_sector_rankings(),
            self.akshare.fetch_fund_flow_data(),
            self.akshare.fetch_valuation_data(),
            self.akshare.fetch_news_headlines(),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        etf_result, index_result, sector_result, flow_result, val_result, news_result = results

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
        valuation = _safe_dict(val_result, "data", [])
        news = _safe_dict(news_result, "news", [])

        return {
            "etfs": etfs if isinstance(etfs, list) else [],
            "indices": indices if isinstance(indices, list) else [],
            "sectors": sectors if isinstance(sectors, list) else [],
            "fund_flows": fund_flows if isinstance(fund_flows, dict) else {},
            "valuation": valuation if isinstance(valuation, list) else [],
            "news": news if isinstance(news, list) else [],
        }

    async def collect_global_data(self) -> dict[str, Any]:
        logger.info("Collecting global market data via yfinance...")
        try:
            result = await self.yfinance.fetch_all()
        except Exception as exc:
            logger.error(f"Global data fetch failed: {exc}")
            result = {"us_etfs": [], "global_indices": [], "vix": {}, "forex": [], "treasury_yields": []}

        vix_data = result.get("vix", {})
        forex_list = result.get("forex", [])
        yields_list = result.get("treasury_yields", [])

        macro: dict[str, float] = {}
        if isinstance(vix_data, dict):
            macro["vix"] = float(vix_data.get("price", 0))
        for fx in (forex_list if isinstance(forex_list, list) else []):
            if isinstance(fx, dict):
                price = float(fx.get("price", 0))
                symbol = str(fx.get("symbol", "forex"))
                macro[symbol] = price
                if symbol == "USDCNY=X" or str(fx.get("name", "")).upper() == "USD/CNY":
                    macro["usdcny"] = price
        for y in (yields_list if isinstance(yields_list, list) else []):
            if isinstance(y, dict):
                yield_value = float(y.get("yield_pct", y.get("price", 0)))
                symbol = str(y.get("symbol", ""))
                name = y.get("name", "").replace(" ", "_").lower()
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
            "us_etfs": result.get("us_etfs", []),
            "global_indices": result.get("global_indices", []),
            "macro": macro,
        }

    async def run_daily_collection(self, target_date: Optional[str] = None) -> DailyMarketSnapshot:
        today = target_date or date.today().strftime("%Y-%m-%d")
        logger.info(f"Starting daily data collection for {today}")

        a_share_data, global_data = await asyncio.gather(
            self.collect_a_share_data(),
            self.collect_global_data(),
        )

        all_indices = {}
        for idx in a_share_data.get("indices", []):
            all_indices[idx.get("code", "")] = idx
        for idx in global_data.get("global_indices", []):
            all_indices[idx.get("code", "")] = idx

        macro = global_data.get("macro", {})
        news = a_share_data.get("news", [])

        self.db.upsert_etfs(today, a_share_data.get("etfs", []))
        self.db.upsert_indices(today, list(all_indices.values()))
        self.db.upsert_sectors(today, a_share_data.get("sectors", []))

        fund_flows = a_share_data.get("fund_flows", {}) or {}
        self.db.upsert_fund_flow(
            today,
            fund_flows.get("north_bound"),
            fund_flows.get("main_force"),
            fund_flows.get("sector_flows"),
        )

        self.db.upsert_macro(today, macro)
        self.db.upsert_news(today, [n.get("title", n) if isinstance(n, dict) else n for n in news])
        self.db.upsert_valuation(today, a_share_data.get("valuation", []))

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
        for etf_dict in a_share_data.get("etfs", []):
            etf_models.append(ETFData(
                code=etf_dict.get("code", ""), name=etf_dict.get("name", ""),
                price=etf_dict.get("price", 0), change_pct=etf_dict.get("change_pct", 0),
                volume=etf_dict.get("volume", 0), amount=etf_dict.get("amount", 0),
                nav=etf_dict.get("nav"), premium_discount=etf_dict.get("premium_discount"),
                pe_ratio=etf_dict.get("pe_ratio"), pb_ratio=etf_dict.get("pb_ratio"),
            ))

        sector_models = {}
        for s in a_share_data.get("sectors", []):
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
        )

        validation = validate_snapshot(snapshot)
        snapshot.validation_errors = validation.errors
        snapshot.validation_warnings = validation.warnings
        if not validation.success:
            logger.error(f"Snapshot validation failed: {validation.errors}")
        if validation.warnings:
            logger.warning(f"Snapshot validation warnings: {validation.warnings}")

        logger.info(f"Daily collection complete: {len(etf_models)} ETFs, {len(index_models)} indices, "
                     f"{len(sector_models)} sectors, {len(headlines)} news")
        return snapshot

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
        yfinance: YFinanceCollector,
        db: MarketDB,
        config: AppConfig,
    ) -> None:
        self.akshare = akshare
        self.yfinance = yfinance
        self.db = db
        self.config = config
        akshare_source = self.config.data.sources.get("akshare")
        yfinance_source = self.config.data.sources.get("yfinance")
        self.akshare_sleep = akshare_source.rate_limit_seconds if akshare_source else 1.0
        self.yfinance_sleep = yfinance_source.rate_limit_seconds if yfinance_source else 0.5

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

        for symbol, name in self.yfinance.US_ETFS.items():
            universe["global_etfs"].setdefault(symbol, name)
        for symbol, name in self.yfinance.GLOBAL_INDICES.items():
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
    snapshot = await pipeline.run_daily_collection()
    portfolio = pipeline.calc_holding_status(snapshot)
    print(f"\n✓ Collection complete: {snapshot.date}")
    print(f"  Indices: {len(snapshot.indices)}")
    print(f"  ETFs: {len(snapshot.etfs)}")
    print(f"  Sectors: {len(snapshot.sectors)}")
    print(f"  Portfolio: {len(portfolio.holdings)} holdings, total ¥{portfolio.total_value:,.0f}")


if __name__ == "__main__":
    asyncio.run(main())
