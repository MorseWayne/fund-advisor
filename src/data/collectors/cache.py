"""SQLite-backed provider cache with trade-date TTL.

Rules:
- Historical trade dates (< today, Asia/Shanghai) cache forever — markets don't
  revise end-of-day prints for our use case.
- Today's trade date cache expires after ``ttl_hours`` from ``fetched_at``, so
  a rerun in the same session is free but the next-day collection refreshes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from loguru import logger

from src.data.collectors.providers.base import Quote

if TYPE_CHECKING:
    from src.data.storage import MarketDB


class ProviderCache:
    def __init__(self, db: "MarketDB", ttl_hours: float = 6.0) -> None:
        self.db = db
        self.ttl = timedelta(hours=ttl_hours)

    def get(self, provider: str, symbol: str, trade_date: str) -> Quote | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT payload_json, fetched_at FROM provider_cache "
                "WHERE provider = ? AND symbol = ? AND trade_date = ?",
                (provider, symbol, trade_date),
            ).fetchone()
        if row is None:
            return None
        if not self._fresh(trade_date, row["fetched_at"]):
            return None
        try:
            payload = json.loads(row["payload_json"])
            return Quote(**payload)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Corrupt provider_cache row dropped: {}", exc)
            return None

    def put(self, quote: Quote) -> None:
        if quote.trade_date is None or quote.source is None:
            return
        payload = json.dumps(_quote_payload(quote), ensure_ascii=False)
        fetched_at = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO provider_cache(provider, symbol, trade_date, payload_json, fetched_at) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(provider, symbol, trade_date) DO UPDATE SET "
                "payload_json = excluded.payload_json, fetched_at = excluded.fetched_at",
                (quote.source, quote.symbol, quote.trade_date, payload, fetched_at),
            )

    def get_batch(
        self,
        provider: str,
        symbols: list[str],
        trade_date: str,
    ) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for symbol in symbols:
            quote = self.get(provider, symbol, trade_date)
            if quote is not None:
                out[symbol] = quote
        return out

    def _fresh(self, trade_date: str, fetched_at_iso: str) -> bool:
        try:
            td = date.fromisoformat(trade_date)
        except ValueError:
            return False
        if td < date.today():
            return True
        try:
            fetched = datetime.fromisoformat(fetched_at_iso)
        except ValueError:
            return False
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - fetched < self.ttl

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db.db_path))
        conn.row_factory = sqlite3.Row
        return conn


def _quote_payload(quote: Quote) -> dict[str, object]:
    from dataclasses import asdict

    return asdict(quote)


__all__ = ["ProviderCache"]
