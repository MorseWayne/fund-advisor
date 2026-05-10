"""配置加载器 - 从 config.yaml 读取系统配置"""

from pathlib import Path
from typing import Any
import os
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel


class DataSourceConfig(BaseModel):
    enabled: bool = True
    rate_limit_seconds: float = 1.0
    cache_ttl_minutes: int = 10


class StorageConfig(BaseModel):
    type: str = "sqlite"
    path: str = "data/fund_advisor.db"


class TrendConfig(BaseModel):
    ma_periods: list[int] = [5, 20, 60]
    standing_line_threshold: float = 0.5


class RotationConfig(BaseModel):
    momentum_windows: list[int] = [21, 63, 126]


class ValuationConfig(BaseModel):
    pe_percentile_low: int = 30
    pe_percentile_high: int = 70
    bond_yield_comparison: bool = True


class RiskConfig(BaseModel):
    anomaly_threshold: float = 0.03
    max_drawdown_warning: float = 0.15
    correlation_warning: float = 0.8


class AnalysisConfig(BaseModel):
    trend: TrendConfig = TrendConfig()
    rotation: RotationConfig = RotationConfig()
    valuation: ValuationConfig = ValuationConfig()
    risk: RiskConfig = RiskConfig()


class LLMReportConfig(BaseModel):
    max_length_chars: int = 800
    tone: str = "专业简洁"


class LLMConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.7
    max_tokens: int = 4096
    report: LLMReportConfig = LLMReportConfig()


class SchedulerJobConfig(BaseModel):
    enabled: bool = True
    cron: str | None = None
    interval_minutes: int | None = None
    start_time: str | None = None
    end_time: str | None = None


class SchedulerConfig(BaseModel):
    timezone: str = "Asia/Shanghai"
    jobs: dict[str, SchedulerJobConfig] = {}


class NotifyChannelConfig(BaseModel):
    enabled: bool = False
    webhook_url_env: str = ""


class NotifyConfig(BaseModel):
    wechat_work: NotifyChannelConfig = NotifyChannelConfig()
    feishu: NotifyChannelConfig = NotifyChannelConfig()


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}"
    rotation: str = "10 MB"
    retention: str = "30 days"


class DataConfig(BaseModel):
    a_share_close_time: str = "15:30"
    us_market_fetch_time: str = "08:00"
    sources: dict[str, DataSourceConfig] = {}
    storage: StorageConfig = StorageConfig()


class AppConfig(BaseModel):
    data: DataConfig = DataConfig()
    analysis: AnalysisConfig = AnalysisConfig()
    llm: LLMConfig = LLMConfig()
    notify: NotifyConfig = NotifyConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    logging: LoggingConfig = LoggingConfig()


def load_config(config_path: str | Path = "config/config.yaml") -> AppConfig:
    """Load and validate configuration from YAML file."""
    load_dotenv(dotenv_path=Path(".env"))

    path = Path(config_path)
    if not path.exists():
        return AppConfig(**_apply_env_overrides({}))

    with open(path, "r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    return AppConfig(**_apply_env_overrides(raw))


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Apply deployment-oriented environment overrides to config data."""
    merged = dict(raw)
    llm = dict(merged.get("llm") or {})
    env_map = {
        "provider": os.getenv("LLM_PROVIDER"),
        "model": os.getenv("LLM_MODEL"),
        "base_url": os.getenv("LLM_BASE_URL"),
        "temperature": os.getenv("LLM_TEMPERATURE"),
        "max_tokens": os.getenv("LLM_MAX_TOKENS"),
    }
    for key, value in env_map.items():
        if value not in (None, ""):
            llm[key] = value
    if llm:
        merged["llm"] = llm
    return merged
