from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import pandas as pd
import numpy as np
from dataclasses import asdict
import os
import asyncio
from loguru import logger

from src.data.storage import MarketDB
from src.data.portfolio import load_portfolio
from src.config import load_config
from src.analysis.trend import calc_sentiment, calc_ma_alignment
from src.analysis.risk import calc_max_drawdown, check_drawdown_warning, calc_correlation_matrix, detect_correlation_breakdown
from src.data.pipeline import DataPipeline
from src.analysis.engine import AnalysisEngine
from src.llm.client import LLMClient
from src.llm.report_generator import ReportGenerator
from src.notify.channels import NotificationManager, WeChatWorkChannel, FeishuChannel

router = APIRouter()

# Dependency/Helper to get DB
def get_db():
    config = load_config()
    return MarketDB(config.data.storage.path)

def get_latest_data():
    db = get_db()
    last_date = db.get_latest_date()
    if not last_date:
        return None, None, None
    etfs = db.get_latest_etfs(100)
    indices = db.get_latest_indices()
    return last_date, etfs, indices

def get_etf_history(code: str, days: int = 60):
    return get_db().get_historical_etf(code, days)

def get_index_history(code: str, days: int = 252):
    return get_db().get_historical_index(code, days)

def get_fund_flow():
    db = get_db()
    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM fund_flow_daily WHERE date = (SELECT MAX(date) FROM fund_flow_daily)"
        ).fetchone()
    return dict(row) if row else None

def get_macro():
    db = get_db()
    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM macro_daily WHERE date = (SELECT MAX(date) FROM macro_daily)"
        ).fetchone()
    return dict(row) if row else None

def get_valuation():
    db = get_db()
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM valuation_daily WHERE date = (SELECT MAX(date) FROM valuation_daily)"
        ).fetchall()
    return [dict(r) for r in rows]

@router.get("/overview")
def get_overview():
    last_date, etfs, indices = get_latest_data()
    if not last_date:
        return {"has_data": False}

    macro = get_macro()
    fund_flow = get_fund_flow()

    index_map = {i.get("code", ""): i for i in (indices or [])}
    primary_index = None
    for code in ["sh000300", "000300", "399300"]:
        if code in index_map:
            primary_index = index_map[code]
            break
    if not primary_index and indices:
        primary_index = indices[0]

    vix = macro.get("vix", 20.0) if macro else 20.0
    advances = sum(1 for e in (etfs or []) if e.get("change_pct", 0) > 0)
    declines = sum(1 for e in (etfs or []) if e.get("change_pct", 0) < 0)
    adr = advances / max(declines, 1)

    sentiment = calc_sentiment(vix, adr)
    sentiment_score = sentiment.get("score", 50)
    sentiment_level = sentiment.get("level", "中性")

    ma_alignment = "交叉震荡"
    if primary_index:
        hist = get_index_history(primary_index.get("code", ""), 60)
        if hist and len(hist) >= 60:
            prices = pd.Series([h.get("price", h.get("close", 0)) for h in sorted(hist, key=lambda x: x.get("date", ""))])
            ma_alignment = calc_ma_alignment(prices)

    if sentiment_level == "恐慌" or ma_alignment == "空头排列":
        direction = "防守"
        direction_desc = "市场偏谨慎，建议降低权益仓位"
    elif ma_alignment == "多头排列" and sentiment_score >= 55:
        direction = "进攻"
        direction_desc = "趋势偏强，可维持或提高权益仓位"
    else:
        direction = "观望"
        direction_desc = "信号不明，建议均衡配置"

    parts = []
    if ma_alignment: parts.append(f"趋势{ma_alignment}")
    if sentiment_level: parts.append(f"情绪{sentiment_level}")
    summary = "，".join(parts) if parts else "数据不足，维持观望"

    valuation = get_valuation()
    pe_pct = None
    if valuation:
        for v in valuation:
            if v.get("index_code", "").endswith("300"):
                pe_pct = v.get("pe_percentile")
                break
        if pe_pct is None and valuation:
            pe_pct = valuation[0].get("pe_percentile")

    us10y = macro.get("us10y", 0) if macro else 0
    spread = None
    if primary_index:
        pe = primary_index.get("pe_ratio", 0)
        if pe and pe > 0:
            bond_yield = us10y / 100.0 if us10y > 1 else us10y
            spread = (1.0 / pe) - bond_yield

    return {
        "has_data": True,
        "last_date": last_date,
        "direction": {
            "status": direction,
            "description": direction_desc,
            "summary": summary,
            "sentiment_score": sentiment_score,
            "sentiment_level": sentiment_level,
            "ma_alignment": ma_alignment,
        },
        "temperature": {
            "pe_percentile": pe_pct,
            "spread": spread,
        },
        "market_breadth": {
            "advances": advances,
            "declines": declines,
            "total_etf": len(etfs) if etfs else 0,
        },
        "fund_flow": fund_flow,
    }

@router.get("/market/indices")
def get_market_indices():
    _, _, indices = get_latest_data()
    return indices or []

@router.get("/market/etfs")
def get_market_etfs():
    _, etfs, _ = get_latest_data()
    return etfs or []

@router.get("/market/sectors")
def get_market_sectors():
    db = get_db()
    last_date = db.get_latest_date()
    if not last_date:
        return []
    import sqlite3
    conn = sqlite3.connect(str(db.db_path))
    df = pd.read_sql_query(f"SELECT * FROM sector_daily WHERE date='{last_date}' ORDER BY change_pct DESC", conn)
    conn.close()
    return df.to_dict(orient="records")

@router.get("/portfolio")
def get_portfolio_status():
    holdings = load_portfolio()
    if not holdings:
        return {"configured": False}

    last_date, etfs, indices = get_latest_data()
    if not etfs:
        return {"configured": True, "has_data": False}

    etf_map = {e.get("code", ""): e for e in etfs}
    idx_map = {i.get("code", ""): i for i in (indices or [])}

    results = []
    total_value = 0.0
    total_cost = 0.0

    for h in holdings:
        price = 0.0
        change_pct = 0.0
        pe = None
        hist = None

        if h.code in etf_map:
            e = etf_map[h.code]
            price = e.get("price", 0)
            change_pct = e.get("change_pct", 0)
            pe = e.get("pe_ratio")
            hist = get_etf_history(h.code, 60)
        elif h.code in idx_map:
            idx = idx_map[h.code]
            price = idx.get("price", 0)
            change_pct = idx.get("change_pct", 0)
            pe = idx.get("pe_ratio")
            hist = get_index_history(h.code, 60)

        market_val = price * h.shares if price > 0 else 0
        cost_val = h.cost_basis * h.shares
        pnl_pct = ((price - h.cost_basis) / h.cost_basis * 100) if price > 0 and h.cost_basis > 0 else 0
        total_value += market_val
        total_cost += cost_val

        # Scoring Logic
        trend_score = 50
        if hist and len(hist) >= 20:
            prices = pd.Series([r.get("close", r.get("price", 0)) for r in sorted(hist, key=lambda x: x.get("date", ""))])
            alignment = calc_ma_alignment(prices)
            if alignment == "多头排列": trend_score = 70
            elif alignment == "空头排列": trend_score = 30
        if change_pct > 2: trend_score = min(100, trend_score + 15)
        elif change_pct > 0: trend_score = min(100, trend_score + 5)
        elif change_pct < -2: trend_score = max(0, trend_score - 15)
        elif change_pct < 0: trend_score = max(0, trend_score - 5)

        val_score = 50
        if pe and pe > 0:
            if pe < 15: val_score = 85
            elif pe < 25: val_score = 70
            elif pe < 40: val_score = 50
            else: val_score = 30

        momentum_score = 50
        if hist and len(hist) >= 21:
            recent = [r.get("close", r.get("price", 0)) for r in sorted(hist, key=lambda x: x.get("date", ""))]
            if len(recent) >= 21 and recent[-21] > 0:
                month_ret = (recent[-1] - recent[-21]) / recent[-21] * 100
                if month_ret > 10: momentum_score = 80
                elif month_ret > 5: momentum_score = 65
                elif month_ret > 0: momentum_score = 55
                elif month_ret > -5: momentum_score = 40
                else: momentum_score = 25

        risk_score = 50
        if abs(change_pct) > 5: risk_score = max(0, risk_score - 30)
        elif abs(change_pct) > 3: risk_score = max(0, risk_score - 15)
        elif abs(change_pct) > 2: risk_score = max(0, risk_score - 5)
        else: risk_score = min(100, risk_score + 10)

        if hist and len(hist) >= 20:
            prices = [r.get("close", r.get("price", 0)) for r in sorted(hist, key=lambda x: x.get("date", ""))]
            dd = calc_max_drawdown(prices)
            if abs(dd) > 0.15: risk_score = max(0, risk_score - 15)
            elif abs(dd) > 0.10: risk_score = max(0, risk_score - 5)

        total_score = int(trend_score * 0.30 + val_score * 0.30 + momentum_score * 0.20 + risk_score * 0.20)

        if total_score >= 80: action = "加仓机会"
        elif total_score >= 60: action = "可维持"
        elif total_score >= 40: action = "观望"
        elif total_score >= 25: action = "谨慎"
        else: action = "控制仓位"

        results.append({
            "code": h.code, "name": h.name, "category": h.category.value,
            "shares": h.shares, "cost_basis": h.cost_basis,
            "current_price": price, "change_pct": change_pct,
            "market_value": market_val, "pnl_pct": pnl_pct,
            "score": total_score, "action": action,
            "score_details": {"trend": trend_score, "valuation": val_score, "momentum": momentum_score, "risk": risk_score},
            "history": hist
        })

    total_pnl = total_value - total_cost
    total_change = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    return {
        "configured": True,
        "has_data": True,
        "summary": {
            "total_value": total_value,
            "total_cost": total_cost,
            "total_pnl": total_pnl,
            "total_change_pct": total_change,
        },
        "holdings": results
    }


@router.get("/risk")
def get_risk_alerts():
    last_date, etfs, indices = get_latest_data()
    if not last_date:
        return {"alerts": []}

    alerts = []
    
    # 1. 异常波动
    for e in (etfs or []):
        cp = e.get("change_pct", 0)
        normalized = cp / 100.0 if abs(cp) > 1 else cp
        if abs(normalized) > 0.03:
            level = "强" if abs(normalized) > 0.05 else "中"
            alerts.append({
                "type": "volatility",
                "level": level,
                "code": e.get("code", ""),
                "name": e.get("name", ""),
                "value": cp,
                "message": f"{e.get('name')} 单日涨跌幅 {cp:+.2f}%"
            })

    # 2. 最大回撤
    index_map = {i.get("code", ""): i for i in (indices or [])}
    primary_index = None
    for code in ["sh000300", "000300", "399300"]:
        if code in index_map:
            primary_index = index_map[code]
            break
    if not primary_index and indices:
        primary_index = indices[0]

    if primary_index:
        hist = get_index_history(primary_index.get("code", ""), 252)
        if hist and len(hist) >= 60:
            prices = [h.get("price", h.get("close", 0)) for h in sorted(hist, key=lambda x: x.get("date", ""))]
            dd = calc_max_drawdown(prices)
            if check_drawdown_warning(dd, 0.15):
                alerts.append({
                    "type": "drawdown",
                    "level": "强" if abs(dd) > 0.20 else "中",
                    "code": primary_index.get("code"),
                    "name": primary_index.get("name", "主要指数"),
                    "value": abs(dd),
                    "message": f"最大回撤 {abs(dd)*100:.1f}% 超过预警线"
                })

    # 3. 相关性
    holdings = load_portfolio()
    if holdings and etfs:
        returns_dict = {}
        for h in holdings:
            hist = get_etf_history(h.code, 60)
            if hist and len(hist) >= 20:
                prices = [r.get("close", r.get("price", 0)) for r in sorted(hist, key=lambda x: x.get("date", ""))]
                if len(prices) >= 2:
                    rets = np.diff(prices) / prices[:-1]
                    returns_dict[h.code] = rets.tolist()
        if len(returns_dict) >= 2:
            corr_result = calc_correlation_matrix(returns_dict)
            avg_corr = corr_result.get("average_correlation", 0)
            if detect_correlation_breakdown(avg_corr, 0.8):
                alerts.append({
                    "type": "correlation",
                    "level": "强" if avg_corr > 0.9 else "中",
                    "code": "PORTFOLIO",
                    "name": "投资组合",
                    "value": avg_corr,
                    "message": f"ETF平均相关性高达 {avg_corr:.2f}"
                })

    return {"alerts": alerts}

from src.api.tasks import task_manager

STEPS = [
    ("数据采集", "正在获取指数、ETF、行业板块数据..."),
    ("持仓计算", "计算当前持仓市值与盈亏..."),
    ("趋势分析", "计算市场情绪、均线排列、PE 分位..."),
    ("风险评估", "检查异常波动、最大回撤、相关性..."),
    ("生成报告", "调用 LLM 生成投资日报..."),
    ("推送通知", "发送微信/飞书通知..."),
]

async def trigger_analysis_task(task_id: str):
    try:
        config = load_config()

        await task_manager.update_progress(task_id, 1, len(STEPS), *STEPS[0])
        pipeline = DataPipeline(config)
        snapshot = await pipeline.run_daily_collection()

        await task_manager.update_progress(task_id, 2, len(STEPS), *STEPS[1])
        portfolio = pipeline.calc_holding_status(snapshot)

        await task_manager.update_progress(task_id, 3, len(STEPS), *STEPS[2])
        engine = AnalysisEngine()
        analysis = engine.analyze(asdict(snapshot))
        analysis["portfolio_status"] = {
            "holdings": [{"code": h.code, "name": h.name, "current_price": h.current_price,
                           "change_pct": h.change_pct, "profit_loss_pct": h.profit_loss_pct,
                           "cost_basis": h.cost_basis, "suggestion": h.suggestion}
                          for h in portfolio.holdings],
            "total_value": portfolio.total_value,
            "total_change_pct": portfolio.total_change_pct,
            "total_profit_loss": portfolio.total_profit_loss,
        }

        await task_manager.update_progress(task_id, 4, len(STEPS), *STEPS[3])
        # risk analysis already embedded in engine.analyze

        await task_manager.update_progress(task_id, 5, len(STEPS), *STEPS[4])
        llm_client = LLMClient(
            provider=config.llm.provider, model=config.llm.model,
            base_url=config.llm.base_url, temperature=config.llm.temperature,
            max_tokens=config.llm.max_tokens,
        )
        report_gen = ReportGenerator(llm_client)
        report_text = await report_gen.generate_daily_report(analysis)

        await task_manager.update_progress(task_id, 6, len(STEPS), *STEPS[5])
        nm = NotificationManager()
        wx_url = os.environ.get(config.notify.wechat_work.webhook_url_env, "")
        fs_url = os.environ.get(config.notify.feishu.webhook_url_env, "")
        if config.notify.wechat_work.enabled and wx_url:
            nm.add_channel("wechat_work", WeChatWorkChannel(wx_url))
        if config.notify.feishu.enabled and fs_url:
            nm.add_channel("feishu", FeishuChannel(fs_url))
        if nm.channels:
            await nm.broadcast(report_text, title=f"投资日报 {snapshot.date}")

        logger.info("Manual trigger analysis completed successfully.")
    except Exception as e:
        logger.error(f"Manual trigger failed: {e}")
        raise

@router.post("/trigger")
async def manual_trigger():
    if await task_manager.has_active_task():
        raise HTTPException(status_code=409, detail="A task is already running")
    from src.api.tasks import run_task_with_progress
    task_id = await run_task_with_progress("投资分析", trigger_analysis_task)
    return {"task_id": task_id, "message": "Analysis triggered"}
