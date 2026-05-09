from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    return scheduler


def schedule_daily_collection(scheduler: AsyncIOScheduler, job_func, cron_expr: str = "30 15 * * mon-fri"):
    scheduler.add_job(
        job_func,
        trigger=CronTrigger.from_crontab(cron_expr, timezone="Asia/Shanghai"),
        id="daily_collection",
        name="每日盘后数据采集",
        replace_existing=True,
        max_instances=1,
    )
    logger.info(f"Scheduled daily collection: {cron_expr}")


def schedule_intraday_monitor(scheduler: AsyncIOScheduler, job_func, interval_minutes: int = 5):
    scheduler.add_job(
        job_func,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="intraday_monitor",
        name="交易时段异常检测",
        replace_existing=True,
        max_instances=1,
    )
    logger.info(f"Scheduled intraday monitor: every {interval_minutes} minutes")


def is_trading_time() -> bool:
    from datetime import datetime
    now = datetime.now()
    if now.weekday() >= 5:
        return False

    current_minutes = now.hour * 60 + now.minute
    start = 9 * 60 + 30
    end = 15 * 60
    return start <= current_minutes <= end
