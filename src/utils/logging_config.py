import sys
from loguru import logger


def setup_logging(level: str = "INFO", log_format: str = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
                  rotation: str = "10 MB", retention: str = "30 days") -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format=log_format,
        level=level,
        colorize=True,
        backtrace=False,
        diagnose=False,
    )
    logger.add(
        "data/logs/fund_advisor_{time:YYYY-MM-DD}.log",
        format=log_format,
        level=level,
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        backtrace=False,
        diagnose=False,
    )
    logger.info("Logging configured")
