#!/usr/bin/env python3
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import load_config
from src.data.pipeline import DataPipeline
from src.analysis.engine import AnalysisEngine
from src.llm.client import LLMClient
from src.llm.report_generator import ReportGenerator
from src.llm.report_period import report_period_english_label, report_period_label, select_report_period
from src.notify.channels import NotificationManager, WeChatWorkChannel, FeishuChannel
from src.utils.logging_config import setup_logging
from src.scheduler.jobs import create_scheduler, schedule_daily_collection, schedule_intraday_monitor, is_trading_time
from loguru import logger


def _setup_notification_manager(config) -> NotificationManager:
    nm = NotificationManager()
    if config.notify.wechat_work.enabled:
        url = os.environ.get(config.notify.wechat_work.webhook_url_env, "")
        if url:
            nm.add_channel("wechat_work", WeChatWorkChannel(url))
            logger.info("WeChat Work notification enabled")
        else:
            logger.warning(f"WeChat Work enabled but {config.notify.wechat_work.webhook_url_env} not set")
    if config.notify.feishu.enabled:
        url = os.environ.get(config.notify.feishu.webhook_url_env, "")
        if url:
            nm.add_channel("feishu", FeishuChannel(url))
            logger.info("Feishu notification enabled")
        else:
            logger.warning(f"Feishu enabled but {config.notify.feishu.webhook_url_env} not set")
    return nm


async def run_once():
    config = load_config()
    setup_logging(
        level=config.logging.level,
        log_format=config.logging.format,
        rotation=config.logging.rotation,
        retention=config.logging.retention,
    )

    pipeline = DataPipeline(config)
    snapshot = await pipeline.run_daily_collection()
    portfolio = pipeline.calc_holding_status(snapshot)

    engine = AnalysisEngine()
    analysis = engine.analyze(snapshot)
    analysis["portfolio_status"] = {
        "holdings": [{"code": h.code, "name": h.name, "current_price": h.current_price,
                       "change_pct": h.change_pct, "profit_loss_pct": h.profit_loss_pct,
                       "cost_basis": h.cost_basis, "suggestion": h.suggestion}
                      for h in portfolio.holdings],
        "total_value": portfolio.total_value,
        "total_change_pct": portfolio.total_change_pct,
        "total_profit_loss": portfolio.total_profit_loss,
    }

    llm_client = LLMClient.from_config(config.llm)
    report_gen = ReportGenerator(llm_client)
    report_period = select_report_period(snapshot.date)
    report_label = report_period_label(report_period)
    report_text = await report_gen.generate_daily_report(analysis, report_period=report_period)

    print(f"\n{'='*60}")
    print(f"  Fund-Advisor {report_period_english_label(report_period)}: {snapshot.date}")
    print(f"{'='*60}")
    print(f"  A-Share Indices: {len([i for i in snapshot.indices if i.startswith('sh') or i.startswith('sz')])}")
    print(f"  Global Indices:  {len([i for i in snapshot.indices if i.startswith('^')])}")
    print(f"  ETFs collected:  {len(snapshot.etfs)}")
    print(f"  Sector rankings: {len(snapshot.sectors)}")
    if portfolio.total_value > 0:
        print(f"  Portfolio value: ¥{portfolio.total_value:,.0f} ({portfolio.total_change_pct:+.2f}%)")
    print(f"{'='*60}")
    print(f"\n{report_text}\n")

    nm = _setup_notification_manager(config)
    if nm.channels:
        title = f"投资{report_label} {snapshot.date}"
        await nm.broadcast(report_text, title=title)

    return snapshot


async def run_scheduled():
    config = load_config()
    setup_logging(
        level=config.logging.level,
        log_format=config.logging.format,
        rotation=config.logging.rotation,
        retention=config.logging.retention,
    )
    pipeline = DataPipeline(config)
    scheduler = create_scheduler()
    nm = _setup_notification_manager(config)

    async def daily_job():
        logger.info("Daily collection + report job triggered")
        try:
            snapshot = await pipeline.run_daily_collection()
            portfolio = pipeline.calc_holding_status(snapshot)
            engine = AnalysisEngine()
            analysis = engine.analyze(snapshot)
            analysis["portfolio_status"] = {
                "holdings": [{"code": h.code, "name": h.name, "current_price": h.current_price,
                               "change_pct": h.change_pct, "profit_loss_pct": h.profit_loss_pct,
                               "cost_basis": h.cost_basis, "suggestion": h.suggestion}
                              for h in portfolio.holdings],
                "total_value": portfolio.total_value,
                "total_change_pct": portfolio.total_change_pct,
                "total_profit_loss": portfolio.total_profit_loss,
            }
            llm_client = LLMClient.from_config(config.llm)
            report_gen = ReportGenerator(llm_client)
            report_period = select_report_period(snapshot.date)
            report_label = report_period_label(report_period)
            report_text = await report_gen.generate_daily_report(analysis, report_period=report_period)
            if nm.channels:
                await nm.broadcast(report_text, title=f"投资{report_label} {snapshot.date}")
            logger.info("{} generated and pushed", report_label)
        except Exception as e:
            logger.error(f"Daily job failed: {e}")

    async def intraday_job():
        if not is_trading_time():
            return
        try:
            from src.data.collectors.akshare_collector import AKShareCollector
            collector = AKShareCollector()
            result = await collector.fetch_index_data()
            indices = result.get("indices", []) if isinstance(result, dict) else []
            for idx in indices:
                pct = float(idx.get("change_pct", 0))
                if abs(pct) > config.analysis.risk.anomaly_threshold:
                    name = idx.get("name", idx.get("code", "?"))
                    alert = f"异常波动: {name} {pct:+.2%}"
                    logger.warning(alert)
                    if nm.channels:
                        await nm.broadcast(alert, title="异常波动预警")
        except Exception as e:
            logger.error(f"Intraday monitor failed: {e}")

    cron_config = config.scheduler.jobs.get("daily_collection")
    cron_expr = getattr(cron_config, "cron", "30 15 * * mon-fri") if cron_config else "30 15 * * mon-fri"
    schedule_daily_collection(scheduler, daily_job, cron_expr=cron_expr)

    intraday_config = config.scheduler.jobs.get("intraday_monitor")
    interval = getattr(intraday_config, "interval_minutes", 5) if intraday_config else 5
    schedule_intraday_monitor(scheduler, intraday_job, interval_minutes=interval)

    scheduler.start()
    logger.info("Scheduler started. Press Ctrl+C to exit.")
    try:
        while True:
            await asyncio.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        scheduler.shutdown()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fund-Advisor - 基金ETF投资建议系统")
    parser.add_argument("command", nargs="?", default="once", choices=["once", "scheduler"],
                        help="once: single run, scheduler: start scheduler")
    args = parser.parse_args()

    if args.command == "once":
        asyncio.run(run_once())
    elif args.command == "scheduler":
        asyncio.run(run_scheduled())


if __name__ == "__main__":
    main()
