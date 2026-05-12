"""AKShare collector for A-share market data.

This module keeps AKShare access isolated from the rest of the data pipeline.
AKShare is synchronous, so every network call is rate-limited asynchronously and
offloaded with ``asyncio.to_thread`` before returning plain Python dictionaries.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import date, datetime, timedelta
from types import ModuleType
from typing import Any, Callable, cast

from loguru import logger

try:
    import akshare as ak
except ModuleNotFoundError:  # Keep module importable in environments before deps are installed.
    ak = None
try:
    import pandas as pd
except ModuleNotFoundError:  # Keep import tests useful before dependencies are installed.
    pd = None

from src.data.collectors.retry import retry_with_backoff


# ---------------------------------------------------------------------------
# Column name mappings: Chinese/API-specific names -> English pipeline names
# ---------------------------------------------------------------------------

_ETF_COLUMN_MAP: dict[str, str] = {
    "代码": "code",
    "名称": "name",
    "最新价": "price",
    "涨跌幅": "change_pct",
    "涨跌额": "change",
    "成交量": "volume",
    "成交额": "amount",
    "振幅": "amplitude",
    "最高": "high",
    "最低": "low",
    "今开": "open",
    "昨收": "previous_close",
    "量比": "volume_ratio",
    "换手率": "turnover_rate",
    "市盈率-动态": "pe_ratio",
    "市净率": "pb_ratio",
    "总市值": "market_cap",
    "流通市值": "float_market_cap",
    "60日涨跌幅": "change_60d",
    "年初至今涨跌幅": "change_ytd",
}

_INDEX_COLUMN_MAP: dict[str, str] = {
    "代码": "code",
    "名称": "name",
    "最新价": "price",
    "涨跌额": "change",
    "涨跌幅": "change_pct",
    "昨收": "previous_close",
    "今开": "open",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "code": "code",
    "name": "name",
    "trade": "price",
    "pricechange": "change",
    "changepercent": "change_pct",
    "settlement": "previous_close",
    "open": "open",
    "high": "high",
    "low": "low",
    "volume": "volume",
    "amount": "amount",
    "ticktime": "tick_time",
}

_SECTOR_COLUMN_MAP: dict[str, str] = {
    "排名": "rank",
    "板块名称": "name",
    "板块代码": "code",
    "最新价": "price",
    "涨跌额": "change",
    "涨跌幅": "change_pct",
    "总市值": "market_cap",
    "换手率": "turnover_rate",
    "上涨家数": "up_count",
    "下跌家数": "down_count",
    "领涨股票": "leading_stock",
    "领涨股票-涨跌幅": "leading_stock_change_pct",
    "领涨涨跌幅": "leading_stock_change_pct",
}

# THS (同花顺) sector column mapping — used as fallback when East Money CDN
# blocks non-browser TLS connections (see fetch_sector_rankings docstring).
_SECTOR_COLUMN_MAP_THS: dict[str, str] = {
    "序号": "rank",
    "板块": "name",
    "涨跌幅": "change_pct",
    "总成交量": "volume",
    "总成交额": "amount",
    "净流入": "net_inflow",
    "上涨家数": "up_count",
    "下跌家数": "down_count",
    "均价": "avg_price",
    "领涨股": "leading_stock",
    "领涨股-最新价": "leading_stock_price",
    "领涨股-涨跌幅": "leading_stock_change_pct",
}

_NORTH_FLOW_COLUMN_MAP: dict[str, str] = {
    "日期": "date",
    "时间": "time",
    "名称": "name",
    "北向资金": "north_bound",
    "北向资金净流入": "north_bound_net_inflow",
    "北向资金净买额": "north_bound_net_buy",
    "沪股通": "shanghai_connect",
    "沪股通净流入": "shanghai_connect_net_inflow",
    "沪股通净买额": "shanghai_connect_net_buy",
    "深股通": "shenzhen_connect",
    "深股通净流入": "shenzhen_connect_net_inflow",
    "深股通净买额": "shenzhen_connect_net_buy",
    "净流入": "net_inflow",
    "净买额": "net_buy",
    "value": "value",
}

_MAIN_FORCE_COLUMN_MAP: dict[str, str] = {
    "序号": "rank",
    "排名": "rank",
    "名称": "name",
    "代码": "code",
    "最新价": "price",
    "今日涨跌幅": "change_pct",
    "涨跌幅": "change_pct",
    "今日主力净流入-净额": "main_force_net_inflow",
    "今日主力净流入-净占比": "main_force_net_inflow_pct",
    "今日超大单净流入-净额": "super_large_net_inflow",
    "今日超大单净流入-净占比": "super_large_net_inflow_pct",
    "今日大单净流入-净额": "large_net_inflow",
    "今日大单净流入-净占比": "large_net_inflow_pct",
    "今日中单净流入-净额": "medium_net_inflow",
    "今日中单净流入-净占比": "medium_net_inflow_pct",
    "今日小单净流入-净额": "small_net_inflow",
    "今日小单净流入-净占比": "small_net_inflow_pct",
    "主力净流入-净额": "main_force_net_inflow",
    "主力净流入-净占比": "main_force_net_inflow_pct",
    "主力净流入": "main_force_net_inflow",
    "主力净流入占比": "main_force_net_inflow_pct",
    "5日主力净流入-净额": "main_force_net_inflow_5d",
    "10日主力净流入-净额": "main_force_net_inflow_10d",
}

_VALUATION_COLUMN_MAP: dict[str, str] = {
    "日期": "date",
    "date": "date",
    "指数": "index_name",
    "指数代码": "index_code",
    "收盘价": "close",
    "close": "close",
    "市盈率": "pe_ratio",
    "市盈率PE": "pe_ratio",
    "市盈率PE(TTM)": "pe_ratio_ttm",
    "PE": "pe_ratio",
    "PE_TTM": "pe_ratio_ttm",
    "pe": "pe_ratio",
    "pe_ttm": "pe_ratio_ttm",
    "averagePETTM": "pe_ratio_ttm",
    "middlePETTM": "median_pe_ratio_ttm",
    "市净率": "pb_ratio",
    "市净率PB": "pb_ratio",
    "PB": "pb_ratio",
    "pb": "pb_ratio",
    "averagePB": "pb_ratio",
    "middlePB": "median_pb_ratio",
    "PE分位点": "pe_percentile",
    "PE百分位": "pe_percentile",
    "市盈率分位": "pe_percentile",
    "PB分位点": "pb_percentile",
    "PB百分位": "pb_percentile",
    "市净率分位": "pb_percentile",
    "quantileInRecent10YearsAveragePeTtm": "pe_percentile",
    "quantileInRecent10YearsMiddlePeTtm": "median_pe_percentile",
    "quantileInRecent10YearsAveragePb": "pb_percentile",
    "quantileInRecent10YearsMiddlePb": "median_pb_percentile",
}

_NEWS_COLUMN_MAP: dict[str, str] = {
    "关键词": "keyword",
    "新闻标题": "title",
    "标题": "title",
    "新闻内容": "content",
    "摘要": "summary",
    "发布时间": "publish_time",
    "时间": "publish_time",
    "文章来源": "source",
    "来源": "source",
    "新闻链接": "url",
    "链接": "url",
    "url": "url",
}

_REQUIRED_INDEX_CODES: tuple[str, ...] = (
    "sh000001",  # 上证指数
    "sh000300",  # 沪深300
    "sh000688",  # 科创50
    "sz399006",  # 创业板指
    "sz399001",  # 深证成指
)


def _rename_columns(df: Any, mapping: dict[str, str]) -> Any:
    """Rename DataFrame columns using mapping entries present in ``df``."""
    _require_pandas()
    return df.rename(columns={key: value for key, value in mapping.items() if key in df.columns})


def _require_akshare() -> ModuleType:
    """Return the AKShare module or raise a clear dependency error."""
    if ak is None:
        raise RuntimeError("akshare is not installed; install project dependencies before collecting data")
    return ak


def _require_pandas() -> ModuleType:
    """Return pandas or raise a clear dependency error."""
    if pd is None:
        raise RuntimeError("pandas is not installed; install project dependencies before collecting data")
    return pd


def _clean_value(value: Any) -> Any:
    """Convert pandas/numpy values into JSON-friendly Python scalars."""
    if isinstance(value, dict):
        return {str(k): _clean_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean_value(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    pandas = _require_pandas()
    if pandas.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 4)
    return value


def _dataframe_records(
    df: Any,
    mapping: dict[str, str],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Rename and sanitize a DataFrame into record dictionaries."""
    df = _rename_columns(df, mapping)
    if limit is not None:
        df = df.head(limit)
    records = df.to_dict(orient="records")
    return [{str(key): _clean_value(value) for key, value in row.items()} for row in records]


def _first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, (int, float)):
        number = float(value)
        return round(number, 4) if math.isfinite(number) else None
    if isinstance(value, str):
        cleaned = value.strip().replace("%", "").replace(",", "")
        if cleaned in {"", "-", "--", "—", "nan", "None"}:
            return None
        try:
            number = float(cleaned)
        except ValueError:
            return None
        return round(number, 4) if math.isfinite(number) else None
    return None


def _latest_metric_from_frame(df: Any, keys: tuple[str, ...], *, latest_first: bool) -> float | None:
    if df is None or df.empty:
        return None
    records = _dataframe_records(df, {})
    ordered_records = records if latest_first else list(reversed(records))
    for row in ordered_records:
        value = _to_float(_first_present(row, keys))
        if value is not None:
            return value
    return None


def _extract_latest_metric(records: list[dict[str, Any]], keys: tuple[str, ...]) -> Any:
    for row in reversed(records):
        value = _first_present(row, keys)
        if value is not None:
            return value
    return None


def _percentile_rank(values: list[float], current: float | None) -> float | None:
    if current is None or not values:
        return None
    valid_values = [value for value in values if value is not None and not math.isnan(value)]
    if not valid_values:
        return None
    below_or_equal = sum(1 for value in valid_values if value <= current)
    return round(below_or_equal / len(valid_values) * 100, 2)


def _numeric_series(records: list[dict[str, Any]], keys: tuple[str, ...]) -> list[float]:
    values: list[float] = []
    for row in records:
        value = _first_present(row, keys)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return values


class AKShareCollector:
    """Async-safe collector for AKShare A-share market endpoints.

    The public methods return plain dictionaries so the pipeline layer can decide
    how to convert them into dataclasses or persist them.
    """

    def __init__(
        self,
        *,
        rate_limit_seconds: float = 1.0,
        index_codes: tuple[str, ...] | None = None,
    ) -> None:
        self.rate_limit_seconds = rate_limit_seconds
        self.index_codes = index_codes or _REQUIRED_INDEX_CODES
        self._last_request_time = 0.0
        self._rate_limit_lock = asyncio.Lock()

    async def _rate_limit(self) -> None:
        """Ensure at least ``rate_limit_seconds`` between AKShare calls."""
        async with self._rate_limit_lock:
            elapsed = time.monotonic() - self._last_request_time
            if elapsed < self.rate_limit_seconds:
                await asyncio.sleep(self.rate_limit_seconds - elapsed)
            self._last_request_time = time.monotonic()

    async def _call_akshare(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Rate-limit and execute a synchronous AKShare function in a thread."""
        func_name = getattr(func, "__name__", "unknown")

        async def _call() -> Any:
            await self._rate_limit()
            return await asyncio.to_thread(func, *args, **kwargs)

        return await retry_with_backoff(_call, max_retries=3, operation_name=f"akshare.{func_name}")

    async def fetch_etf_spot_data(self) -> dict[str, Any]:
        """Fetch all A-share ETF real-time quotes from East Money."""
        try:
            ak_module = _require_akshare()
            df = await self._call_akshare(ak_module.fund_etf_spot_em)
            if df is None or df.empty:
                return {"error": "No ETF spot data available", "etfs": []}

            records = _dataframe_records(df, _ETF_COLUMN_MAP)
            return {
                "etfs": records,
                "count": len(records),
                "timestamp": int(datetime.now().timestamp()),
            }
        except Exception as e:
            return {"error": str(e), "etfs": []}

    async def fetch_index_data(self) -> dict[str, Any]:
        """Fetch real-time quotes for required major A-share indices."""
        try:
            ak_module = _require_akshare()
            df = await self._call_akshare(ak_module.stock_zh_index_spot_sina)
            if df is None or df.empty:
                return {"error": "No index data available", "indices": []}

            df = _rename_columns(df, _INDEX_COLUMN_MAP)
            if "code" in df.columns:
                filtered = cast(Any, df.loc[df["code"].isin(list(self.index_codes))].copy())
                if not filtered.empty:
                    df = filtered

            records = _dataframe_records(df, {}, limit=None)
            rank = {code: idx for idx, code in enumerate(self.index_codes)}
            records.sort(key=lambda item: rank.get(str(item.get("code", "")), len(rank)))
            return {
                "indices": records,
                "count": len(records),
                "timestamp": int(datetime.now().timestamp()),
            }
        except Exception as e:
            return {"error": str(e), "indices": []}

    async def fetch_sector_rankings(self, *, limit: int | None = 50) -> dict[str, Any]:
        """Fetch industry board gain/loss rankings.

        Tries East Money first, then falls back to THS (同花顺) when the East
        Money CDN blocks non-browser TLS connections (RemoteDisconnected).
        """
        ak_module = _require_akshare()

        # ---- East Money path (push2.eastmoney.com — blocked from some networks) ----
        try:
            df = await self._call_akshare(ak_module.stock_board_industry_name_em)
            if df is not None and not df.empty:
                records = _dataframe_records(df, _SECTOR_COLUMN_MAP, limit=limit)
                return {
                    "sectors": records,
                    "count": len(records),
                    "timestamp": int(datetime.now().timestamp()),
                }
        except Exception:
            pass

        # ---- THS fallback (data.10jqka.com.cn) ----
        try:
            df = await self._call_akshare(ak_module.stock_board_industry_summary_ths)
            if df is not None and not df.empty:
                records = _dataframe_records(df, _SECTOR_COLUMN_MAP_THS, limit=limit)
                return {
                    "sectors": records,
                    "count": len(records),
                    "timestamp": int(datetime.now().timestamp()),
                    "source": "ths",
                }
        except Exception as e:
            return {"error": str(e), "sectors": []}

        return {"error": "No sector ranking data available", "sectors": []}

    async def fetch_fund_flow_data(self, *, limit: int | None = 50) -> dict[str, Any]:
        """Fetch north-bound and main-force fund flow data."""
        ak_module = _require_akshare()

        # North-bound: use stock_hsgt_hist_em (stock_hsgt_north_flow_em removed in recent akshare).
        north_bound: float | None = None
        north_records: list[dict[str, Any]] = []
        try:
            north_df = await self._call_akshare(ak_module.stock_hsgt_hist_em)
            if north_df is not None and not north_df.empty:
                north_records = _dataframe_records(north_df, {})
                latest = north_records[-1] if north_records else {}
                val = latest.get("当日成交净买额")
                north_bound = _to_float(val)
        except Exception:
            pass

        # Main-force: use stock_fund_flow_industry (more stable than sector rank endpoints).
        main_records: list[dict[str, Any]] = []
        try:
            main_df = await self._call_akshare(ak_module.stock_fund_flow_industry)
            if main_df is not None and not main_df.empty:
                main_records = _dataframe_records(main_df, {
                    "行业": "name",
                    "净额": "main_force_net_inflow",
                    "行业-涨跌幅": "change_pct",
                }, limit=limit)
        except Exception:
            pass

        main_force = sum(
            float(row.get("main_force_net_inflow") or 0)
            for row in main_records
            if isinstance(row.get("main_force_net_inflow"), (int, float))
        )
        sector_flows = {
            str(row["name"]): float(row["main_force_net_inflow"])
            for row in main_records
            if row.get("name") is not None and isinstance(row.get("main_force_net_inflow"), (int, float))
        }

        return {
            "north_bound": north_bound,
            "main_force": round(main_force, 4),
            "sector_flows": sector_flows,
            "north_bound_records": north_records,
            "main_force_records": main_records,
            "timestamp": int(datetime.now().timestamp()),
        }

    async def fetch_valuation_data(
        self,
        index_code: str = "000300",
        *,
        limit: int | None = 250,
    ) -> dict[str, Any]:
        """Fetch PE/PB historical valuation data and latest percentiles."""
        ak_module = _require_akshare()

        # Try CSIndex valuation API for latest PE (returns ~20 recent days).
        pe_float: float | None = None
        pb_float: float | None = None
        records: list[dict[str, Any]] = []
        try:
            df = await self._call_akshare(ak_module.stock_zh_index_value_csindex, symbol=index_code)
            if df is not None and not df.empty:
                records = _dataframe_records(df, {})
                latest = records[-1] if records else {}
                pe_raw = latest.get("市盈率2") or latest.get("市盈率1")
                pe_float = _to_float(pe_raw)
        except Exception:
            pass

        # Fallback to all-market PB history for PB and PB percentile.
        if pb_float is None:
            try:
                df_pb = await self._call_akshare(ak_module.stock_a_all_pb)
                if df_pb is not None and not df_pb.empty:
                    pb_records = _dataframe_records(df_pb, {})
                    latest_pb = pb_records[-1] if pb_records else {}
                    pb_val = latest_pb.get("middlePB") or latest_pb.get("equalWeightAveragePB")
                    pb_float = _to_float(pb_val)
                    pb_series = _numeric_series(pb_records, ("middlePB", "equalWeightAveragePB"))
                    pb_percentile = _percentile_rank(pb_series, pb_float)
                else:
                    pb_percentile = None
            except Exception:
                pb_percentile = None
        else:
            pb_percentile = None

        pe_percentile = None
        if records and pe_float is not None:
            pe_series = _numeric_series(records, ("市盈率2", "市盈率1"))
            pe_percentile = _percentile_rank(pe_series, pe_float)

        return {
            "index_code": index_code,
            "pe_ratio": pe_float,
            "pb_ratio": pb_float,
            "pe_percentile": _clean_value(pe_percentile),
            "pb_percentile": _clean_value(pb_percentile),
            "latest": records[-1] if records else {},
            "records": records[-limit:] if limit is not None else records,
            "count": len(records),
            "timestamp": int(datetime.now().timestamp()),
        }

    async def fetch_news_headlines(
        self,
        *,
        limit: int = 20,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Fetch recent financial news headlines."""
        ak_module = _require_akshare()
        df = None
        fallback = False

        if symbol:
            try:
                df = await self._call_akshare(ak_module.stock_news_em, symbol=symbol)
            except Exception:
                pass
        else:
            try:
                df = await self._call_akshare(ak_module.stock_info_global_news_em)
            except Exception:
                try:
                    df = await self._call_akshare(ak_module.stock_news_em, symbol="000001")
                except Exception:
                    pass

        # Fallback to CCTV news when East Money news fails (e.g. pyarrow regex issue).
        if df is None or df.empty:
            try:
                from datetime import date as _date
                df = await self._call_akshare(ak_module.news_cctv, date=_date.today().strftime("%Y%m%d"))
                fallback = True
            except Exception:
                return {"error": "No news headlines available", "headlines": [], "news": []}

        if df is None or df.empty:
            return {"error": "No news headlines available", "headlines": [], "news": []}

        if fallback:
            records = _dataframe_records(df, {"date": "publish_time", "title": "title", "content": "content"}, limit=limit)
        else:
            records = _dataframe_records(df, _NEWS_COLUMN_MAP, limit=limit)
        for row in records:
            content = row.get("content")
            if isinstance(content, str) and len(content) > 200:
                row["content"] = content[:200] + "..."

        headlines = [str(row.get("title")) for row in records if row.get("title")]
        return {
            "headlines": headlines,
            "news": records,
            "count": len(records),
            "timestamp": int(datetime.now().timestamp()),
        }

    async def fetch_cn_10y_yield(self) -> dict[str, float] | None:
        """Fetch latest China 10-year government bond yield."""
        try:
            ak_module = _require_akshare()
            end_date = date.today()
            start_date = end_date - timedelta(days=45)
            start = start_date.strftime("%Y%m%d")
            end = end_date.strftime("%Y%m%d")

            df = None
            if hasattr(ak_module, "macro_china_bond_public_info"):
                df = await self._call_akshare(ak_module.macro_china_bond_public_info)
                cn10y = _latest_metric_from_frame(df, ("10年", "中国国债收益率10年", "收益率"), latest_first=True)
                if cn10y is not None:
                    return {"cn10y": cn10y}

            if hasattr(ak_module, "bond_china_yield"):
                df = await self._call_akshare(ak_module.bond_china_yield, start_date=start, end_date=end)
                cn10y = _latest_metric_from_frame(df, ("10年", "中国国债收益率10年"), latest_first=False)
                if cn10y is not None:
                    return {"cn10y": cn10y}

            if hasattr(ak_module, "bond_zh_us_rate"):
                df = await self._call_akshare(ak_module.bond_zh_us_rate, start_date=start)
                cn10y = _latest_metric_from_frame(df, ("中国国债收益率10年", "10年"), latest_first=False)
                if cn10y is not None:
                    return {"cn10y": cn10y}

            logger.warning("AKShare China 10Y yield fetch returned no usable value")
            return None
        except Exception as exc:
            logger.warning(f"AKShare China 10Y yield fetch failed: {exc}")
            return None

    async def fetch_cpi(self) -> dict[str, float] | None:
        """Fetch latest China CPI YoY change."""
        try:
            ak_module = _require_akshare()
            try:
                df = await self._call_akshare(ak_module.macro_china_cpi)
                cpi_change = _latest_metric_from_frame(df, ("全国-同比增长", "当月同比增长", "今值"), latest_first=True)
            except Exception:
                df = await self._call_akshare(ak_module.macro_china_cpi_monthly)
                cpi_change = _latest_metric_from_frame(df, ("今值", "全国-环比增长", "当月环比增长"), latest_first=False)

            if cpi_change is None:
                logger.warning("AKShare China CPI fetch returned no usable value")
                return None
            return {"cpi_change": cpi_change}
        except Exception as exc:
            logger.warning(f"AKShare China CPI fetch failed: {exc}")
            return None

    async def fetch_gdp(self) -> dict[str, float] | None:
        """Fetch latest China GDP YoY growth."""
        try:
            ak_module = _require_akshare()
            df = await self._call_akshare(ak_module.macro_china_gdp)
            gdp_growth = _latest_metric_from_frame(df, ("国内生产总值-同比增长", "中国GDP年增率", "今值"), latest_first=True)

            if gdp_growth is None and hasattr(ak_module, "macro_china_gdp_yearly"):
                df = await self._call_akshare(ak_module.macro_china_gdp_yearly)
                gdp_growth = _latest_metric_from_frame(df, ("今值", "国内生产总值-同比增长"), latest_first=False)

            if gdp_growth is None:
                logger.warning("AKShare China GDP fetch returned no usable value")
                return None
            return {"gdp_growth": gdp_growth}
        except Exception as exc:
            logger.warning(f"AKShare China GDP fetch failed: {exc}")
            return None

    async def fetch_pmi(self) -> dict[str, float] | None:
        """Fetch latest China official manufacturing PMI."""
        try:
            ak_module = _require_akshare()
            try:
                df = await self._call_akshare(ak_module.macro_china_pmi)
                pmi = _latest_metric_from_frame(df, ("制造业-指数", "今值"), latest_first=True)
            except Exception:
                df = await self._call_akshare(ak_module.macro_china_pmi_yearly)
                pmi = _latest_metric_from_frame(df, ("今值", "制造业-指数"), latest_first=False)

            if pmi is None:
                logger.warning("AKShare China PMI fetch returned no usable value")
                return None
            return {"pmi": pmi}
        except Exception as exc:
            logger.warning(f"AKShare China PMI fetch failed: {exc}")
            return None

    async def fetch_precious_metals_data(self) -> dict[str, Any]:
        """Fetch gold and precious metals market data.

        Returns SGE Au99.99 spot price, COMEX gold futures main contract,
        A-share gold concept board performance, and gold ETF identifiers.
        """
        ak_module = _require_akshare()
        result: dict[str, Any] = {"timestamp": int(datetime.now().timestamp())}

        # ---- Gold spot price (Shanghai Gold Exchange Au99.99) ----
        try:
            df = await self._call_akshare(ak_module.spot_golden_benchmark_sge)
            if df is not None and not df.empty:
                latest = df.iloc[-1]
                result["gold_spot"] = {
                    "price": _to_float(latest.get("晚盘价")),
                    "date": str(latest.get("交易时间", "")),
                }
                if len(df) >= 5:
                    prev = df.iloc[-6]
                    spot_now = _to_float(latest.get("晚盘价"))
                    spot_5d = _to_float(prev.get("晚盘价"))
                    if spot_now is not None and spot_5d is not None and spot_5d != 0:
                        result["gold_spot"]["change_5d"] = round((spot_now - spot_5d) / spot_5d * 100, 2)
        except Exception:
            pass

        # ---- COMEX gold futures (main contract) ----
        try:
            df = await self._call_akshare(ak_module.futures_global_spot_em)
            if df is not None and not df.empty:
                gold = df[df["名称"].str.contains(r"迷你黄金|微型黄金|COMEX金", na=False, regex=True)]
                if not gold.empty:
                    gold_sorted = gold.sort_values("成交量", ascending=False)
                    main = gold_sorted.iloc[0]
                    result["comex_gold"] = {
                        "name": str(main.get("名称", "")),
                        "price": _to_float(main.get("最新价")),
                        "change_pct": _to_float(main.get("涨跌幅")),
                        "volume": _to_float(main.get("成交量")),
                    }
                # Add COMEX silver as well
                silver = df[df["名称"].str.contains(r"COMEX白", na=False)]
                if not silver.empty:
                    silver_sorted = silver.sort_values("成交量", ascending=False)
                    main_silver = silver_sorted.iloc[0]
                    result["comex_silver"] = {
                        "name": str(main_silver.get("名称", "")),
                        "price": _to_float(main_silver.get("最新价")),
                        "change_pct": _to_float(main_silver.get("涨跌幅")),
                    }
        except Exception:
            pass

        # ---- A-share gold concept board (THS source) ----
        try:
            df = await self._call_akshare(ak_module.stock_board_concept_index_ths, symbol="黄金概念")
            if df is not None and not df.empty:
                records = _dataframe_records(df, {
                    "日期": "date",
                    "开盘价": "open",
                    "收盘价": "close",
                    "最高价": "high",
                    "最低价": "low",
                    "成交量": "volume",
                    "成交额": "amount",
                })
                result["gold_concept"] = {
                    "latest": records[-1] if records else {},
                    "records": records[-20:] if len(records) > 20 else records,
                }
        except Exception:
            pass

        # ---- Gold ETF identifiers (filtered from ETF spot data) ----
        _GOLD_ETF_CODES = frozenset({"518880", "159937", "159934", "518800"})
        try:
            etf_df = await self._call_akshare(ak_module.fund_etf_spot_em)
            if etf_df is not None and not etf_df.empty:
                etf_records = _dataframe_records(etf_df, _ETF_COLUMN_MAP)
                gold_etfs = [
                    r for r in etf_records
                    if str(r.get("code", "")) in _GOLD_ETF_CODES
                ]
                if gold_etfs:
                    result["gold_etfs"] = gold_etfs
        except Exception:
            pass

        return result

    # ---- New data sources added for improved decision quality ----

    async def fetch_qdii_premium(self) -> dict[str, Any]:
        """Fetch QDII ETF real-time premium/discount rates.

        QDII ETFs trade in RMB but hold foreign assets. When the fund company
        suspends subscriptions (额度用尽), the ETF can trade at significant
        premiums (5-30%). Buying at a premium means overpaying for the
        underlying assets.

        Returns:
            ``{"qdii_premiums": [...], "max_premium": float, "high_premium_alerts": [...]}``
        """
        ak_module = _require_akshare()
        result: dict[str, Any] = {"timestamp": int(datetime.now().timestamp())}

        try:
            # fund_etf_fund_info_em provides IOPV (实时净值) and current price
            # for all ETFs, allowing premium/discount calculation
            df = await self._call_akshare(ak_module.fund_etf_fund_info_em)
            if df is None or df.empty:
                return {"qdii_premiums": [], "max_premium": 0, "high_premium_alerts": []}

            premiums: list[dict[str, Any]] = []
            high_alerts: list[dict[str, Any]] = []

            for _, row in df.iterrows():
                code = str(row.get("基金代码", row.get("code", "")))
                name = str(row.get("基金简称", row.get("name", "")))
                iopv = _to_float(row.get("IOPV", row.get("iopv", row.get("实时净值"))))
                price = _to_float(row.get("最新价", row.get("price")))

                if iopv is None or price is None or iopv <= 0:
                    continue

                premium_pct = round((price - iopv) / iopv * 100, 2)

                entry = {
                    "code": code,
                    "name": name,
                    "price": price,
                    "iopv": iopv,
                    "premium_pct": premium_pct,
                }
                premiums.append(entry)

                # Alert on significant premiums (>3% is noteworthy, >5% is dangerous)
                if premium_pct > 5:
                    high_alerts.append(entry)
                elif premium_pct > 3:
                    high_alerts.append(entry)

            # Sort by premium descending
            premiums.sort(key=lambda x: abs(x.get("premium_pct", 0)), reverse=True)
            max_premium = max(
                (abs(p.get("premium_pct", 0)) for p in premiums), default=0
            )

            result["qdii_premiums"] = premiums[:30]
            result["max_premium"] = max_premium
            result["high_premium_alerts"] = high_alerts[:10]

        except Exception as exc:
            logger.warning(f"QDII premium fetch failed: {exc}")
            result["error"] = str(exc)

        return result

    async def fetch_macro_liquidity(self) -> dict[str, float]:
        """Fetch China macro liquidity indicators: LPR, social financing, M2.

        These indicators capture domestic monetary conditions:
        - LPR: loan prime rate, the benchmark lending rate
        - M2 growth: broad money supply growth rate (YoY)
        - Social financing (社融): total credit extended to the real economy

        Returns a dict with keys like ``lpr_1y``, ``lpr_5y``, ``m2_yoy``,
        ``social_financing``, or ``None`` on failure.
        """
        ak_module = _require_akshare()
        result: dict[str, float] = {}

        # ---- LPR (Loan Prime Rate) ----
        try:
            df = await self._call_akshare(ak_module.macro_china_lpr)
            if df is not None and not df.empty:
                latest = df.iloc[-1] if len(df) > 0 else None
                if latest is not None:
                    lpr_1y = _to_float(latest.get("1年期LPR", latest.get("LPR1Y")))
                    lpr_5y = _to_float(latest.get("5年期LPR", latest.get("LPR5Y")))
                    if lpr_1y is not None:
                        result["lpr_1y"] = lpr_1y
                    if lpr_5y is not None:
                        result["lpr_5y"] = lpr_5y
        except Exception:
            logger.debug("LPR fetch failed or API not available")

        # ---- M2 Money Supply ----
        try:
            df = await self._call_akshare(ak_module.macro_china_money_supply)
            if df is not None and not df.empty:
                latest = df.iloc[-1] if len(df) > 0 else df.iloc[0]
                m2 = _to_float(latest.get("M2同比", latest.get("M2", latest.get("货币和准货币(M2)"))))
                if m2 is not None:
                    result["m2_yoy"] = m2
        except Exception:
            logger.debug("M2 fetch failed or API not available")

        # ---- Social Financing (社融增量) ----
        try:
            df = await self._call_akshare(ak_module.macro_china_shrzgm)
            if df is not None and not df.empty:
                latest = df.iloc[-1] if len(df) > 0 else df.iloc[0]
                sf = _to_float(latest.get("社会融资规模增量", latest.get("增量")))
                if sf is not None:
                    result["social_financing"] = sf
        except Exception:
            logger.debug("Social financing fetch failed or API not available")

        if not result:
            logger.warning("All macro liquidity fetches returned no data")
            return {}

        return result

    async def fetch_margin_balance(self) -> dict[str, Any]:
        """Fetch margin trading balance (两融余额) from Shanghai/Shenzhen exchanges.

        Margin balance reflects leveraged sentiment:
        - Rising margin balance → bullish sentiment, more leverage
        - Falling margin balance → deleveraging, risk-off

        Returns:
            ``{"margin_balance": float, "date": str}`` or empty dict on failure.
        """
        ak_module = _require_akshare()
        result: dict[str, Any] = {"timestamp": int(datetime.now().timestamp())}

        try:
            # Shanghai margin data
            df = await self._call_akshare(ak_module.stock_margin_detail_sse, date="")
            if df is not None and not df.empty:
                # The function returns the most recent data when date is empty
                latest = df.iloc[-1] if len(df) > 0 else df.iloc[0]
                balance = _to_float(latest.get("融资余额", latest.get("margin_balance")))
                date_val = str(
                    latest.get("信用交易日期", latest.get("date", ""))
                )
                if balance is not None:
                    result["margin_balance"] = balance
                    result["date"] = date_val
        except Exception as exc:
            logger.debug(f"Shanghai margin fetch failed: {exc}")

        # Fallback: use East Money margin summary
        if "margin_balance" not in result:
            try:
                df = await self._call_akshare(ak_module.stock_margin_sse)
                if df is not None and not df.empty:
                    latest = df.iloc[-1]
                    balance = _to_float(latest.get("融资余额", latest.get("margin_value")))
                    if balance is not None:
                        result["margin_balance"] = balance
                        result["date"] = str(latest.get("日期", latest.get("date", "")))
            except Exception as exc:
                logger.debug(f"Margin summary fetch failed: {exc}")

        if "margin_balance" not in result:
            return {}

        return result

    async def fetch_hsgt_flow_trend(self) -> dict[str, Any]:
        """Fetch recent north-bound (沪深港通) capital flow trend.

        North-bound flow is a key sentiment indicator for A-shares:
        - Sustained net inflow → foreign capital bullish on A-shares
        - Sustained net outflow → risk-off or capital repatriation

        Returns:
            ``{"recent_flows": [...], "net_5d": float, "trend": "inflow"|"outflow"|"mixed"}``
        """
        ak_module = _require_akshare()
        result: dict[str, Any] = {"timestamp": int(datetime.now().timestamp())}

        try:
            df = await self._call_akshare(ak_module.stock_hsgt_hist_em)
            if df is None or df.empty:
                return {"recent_flows": [], "net_5d": 0, "trend": "unknown"}

            recent = df.tail(10)
            flows: list[dict[str, Any]] = []
            net_sum = 0.0

            for _, row in recent.iterrows():
                date_val = str(row.get("日期", row.get("date", "")))
                net_inflow = _to_float(
                    row.get("净买入", row.get("净流入", row.get("net_inflow")))
                )
                if net_inflow is not None:
                    flows.append({
                        "date": date_val,
                        "net_inflow": round(net_inflow, 2),
                    })
                    net_sum += net_inflow

            # Determine trend from last 5 trading days
            recent_5 = flows[-5:] if len(flows) >= 5 else flows
            if recent_5:
                positive_days = sum(1 for f in recent_5 if f.get("net_inflow", 0) > 0)
                if positive_days >= 4:
                    trend = "inflow"
                elif positive_days <= 1:
                    trend = "outflow"
                else:
                    trend = "mixed"
            else:
                trend = "unknown"

            result["recent_flows"] = flows
            result["net_5d"] = round(net_sum, 2)
            result["trend"] = trend

        except Exception as exc:
            logger.warning(f"HSGT flow fetch failed: {exc}")
            result["error"] = str(exc)

        return result

    # Compatibility aliases for likely pipeline naming conventions.
    async def get_etf_spot_data(self) -> dict[str, Any]:
        return await self.fetch_etf_spot_data()

    async def get_index_data(self) -> dict[str, Any]:
        return await self.fetch_index_data()

    async def get_sector_rankings(self, *, limit: int | None = 50) -> dict[str, Any]:
        return await self.fetch_sector_rankings(limit=limit)

    async def get_fund_flow_data(self, *, limit: int | None = 50) -> dict[str, Any]:
        return await self.fetch_fund_flow_data(limit=limit)

    async def get_valuation_data(
        self,
        index_code: str = "000300",
        *,
        limit: int | None = 250,
    ) -> dict[str, Any]:
        return await self.fetch_valuation_data(index_code=index_code, limit=limit)

    async def get_news_headlines(self, *, limit: int = 20, symbol: str | None = None) -> dict[str, Any]:
        return await self.fetch_news_headlines(limit=limit, symbol=symbol)


__all__ = ["AKShareCollector"]
