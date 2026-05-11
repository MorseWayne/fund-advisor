import sqlite3
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Optional, SupportsFloat, cast

from loguru import logger


class MarketDB:
    def __init__(self, db_path: str = "data/fund_advisor.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _batch_upsert(
        self,
        conn: sqlite3.Connection,
        sql: str,
        batch_data: Sequence[tuple[object, ...]],
        batch_size: Optional[int] = None,
    ) -> int:
        if not batch_data:
            return 0

        if batch_size is None:
            conn.executemany(sql, batch_data)
        else:
            for start in range(0, len(batch_data), batch_size):
                conn.executemany(sql, batch_data[start:start + batch_size])
        return len(batch_data)

    def _init_schema(self):
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS etf_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    price REAL,
                    change_pct REAL,
                    volume REAL,
                    amount REAL,
                    nav REAL,
                    premium_discount REAL,
                    pe_ratio REAL,
                    pb_ratio REAL,
                    UNIQUE(date, code)
                );

                CREATE TABLE IF NOT EXISTS index_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    price REAL,
                    change_pct REAL,
                    pe_ratio REAL,
                    pb_ratio REAL,
                    pe_percentile REAL,
                    pb_percentile REAL,
                    UNIQUE(date, code)
                );

                CREATE TABLE IF NOT EXISTS sector_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    name TEXT NOT NULL,
                    change_pct REAL,
                    momentum_1m REAL,
                    momentum_3m REAL,
                    momentum_6m REAL,
                    UNIQUE(date, name)
                );

                CREATE TABLE IF NOT EXISTS fund_flow_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL UNIQUE,
                    north_bound REAL,
                    main_force REAL,
                    sector_flows TEXT
                );

                CREATE TABLE IF NOT EXISTS macro_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL UNIQUE,
                    vix REAL,
                    us10y REAL,
                    us5y REAL,
                    us3m REAL,
                    usdcny REAL,
                    extra TEXT
                );

                CREATE TABLE IF NOT EXISTS news_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    headline TEXT NOT NULL,
                    UNIQUE(date, headline)
                );

                CREATE TABLE IF NOT EXISTS valuation_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    index_code TEXT NOT NULL,
                    pe_current REAL,
                    pe_percentile REAL,
                    pb_current REAL,
                    pb_percentile REAL,
                    UNIQUE(date, index_code)
                );

                CREATE INDEX IF NOT EXISTS idx_etf_daily_date ON etf_daily(date);
                CREATE INDEX IF NOT EXISTS idx_etf_daily_code ON etf_daily(code);
                CREATE INDEX IF NOT EXISTS idx_index_daily_date ON index_daily(date);
                CREATE INDEX IF NOT EXISTS idx_sector_daily_date ON sector_daily(date);
                CREATE INDEX IF NOT EXISTS idx_fund_flow_date ON fund_flow_daily(date);

                CREATE TABLE IF NOT EXISTS provider_cache (
                    provider TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    UNIQUE(provider, symbol, trade_date)
                );
                CREATE INDEX IF NOT EXISTS idx_provider_cache_lookup
                    ON provider_cache(symbol, trade_date);
            """)
        self.create_history_tables()
        logger.info(f"Database initialized at {self.db_path}")

    def create_history_tables(self) -> None:
        """Create OHLCV history tables used by the backfill pipeline."""
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS etf_history (
                    date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    amount REAL,
                    created_at TEXT NOT NULL,
                    UNIQUE(date, code)
                );

                CREATE TABLE IF NOT EXISTS index_history (
                    date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    amount REAL,
                    created_at TEXT NOT NULL,
                    UNIQUE(date, code)
                );

                CREATE INDEX IF NOT EXISTS idx_etf_history_code_date ON etf_history(code, date);
                CREATE INDEX IF NOT EXISTS idx_index_history_code_date ON index_history(code, date);
            """)
        logger.info("History tables initialized")

    @staticmethod
    def _history_float(value: object) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(cast(SupportsFloat | str | bytes | bytearray, value))
        except (TypeError, ValueError):
            return None

    def upsert_etf_history(self, records: list[dict]) -> int:  # pyright: ignore[reportMissingTypeArgument]
        """Upsert ETF OHLCV history records into etf_history."""
        sql = """
            INSERT INTO etf_history (date, code, open, high, low, close, volume, amount, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, code) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                volume=excluded.volume, amount=excluded.amount, created_at=excluded.created_at
        """
        batch_data: list[tuple[object, ...]] = []
        created_at = datetime.now().isoformat(timespec="seconds")
        for record in records:
            date_value = record.get("date")
            code = record.get("code")
            if not date_value or not code:
                logger.warning(f"Skipping ETF history record without date/code: {record}")
                continue
            batch_data.append((
                str(date_value), str(code), self._history_float(record.get("open")),
                self._history_float(record.get("high")), self._history_float(record.get("low")),
                self._history_float(record.get("close", record.get("price"))),
                self._history_float(record.get("volume")),
                self._history_float(record.get("amount", record.get("turnover"))),
                str(record.get("created_at") or created_at),
            ))
        with self._get_conn() as conn:
            count = self._batch_upsert(conn, sql, batch_data, batch_size=500)
        logger.info(f"Upserted {count} ETF history records")
        return count

    def upsert_index_history(self, records: list[dict]) -> int:  # pyright: ignore[reportMissingTypeArgument]
        """Upsert index OHLCV history records into index_history."""
        sql = """
            INSERT INTO index_history (date, code, name, open, high, low, close, volume, amount, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, code) DO UPDATE SET
                name=excluded.name, open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume, amount=excluded.amount,
                created_at=excluded.created_at
        """
        batch_data: list[tuple[object, ...]] = []
        created_at = datetime.now().isoformat(timespec="seconds")
        for record in records:
            date_value = record.get("date")
            code = record.get("code")
            if not date_value or not code:
                logger.warning(f"Skipping index history record without date/code: {record}")
                continue
            batch_data.append((
                str(date_value), str(code), str(record.get("name") or code),
                self._history_float(record.get("open")), self._history_float(record.get("high")),
                self._history_float(record.get("low")),
                self._history_float(record.get("close", record.get("price"))),
                self._history_float(record.get("volume")),
                self._history_float(record.get("amount", record.get("turnover"))),
                str(record.get("created_at") or created_at),
            ))
        with self._get_conn() as conn:
            count = self._batch_upsert(conn, sql, batch_data, batch_size=500)
        logger.info(f"Upserted {count} index history records")
        return count

    def get_latest_etf_history_date(self, code: str) -> Optional[str]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT MAX(date) AS max_date FROM etf_history WHERE code = ?", (code,)).fetchone()
        return row["max_date"] if row else None

    def get_latest_index_history_date(self, code: str) -> Optional[str]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT MAX(date) AS max_date FROM index_history WHERE code = ?", (code,)).fetchone()
        return row["max_date"] if row else None

    def upsert_etfs(self, date_str: str, etfs: list[dict]) -> int:  # pyright: ignore[reportMissingTypeArgument]
        sql = """
            INSERT INTO etf_daily (date, code, name, price, change_pct, volume, amount, nav, premium_discount, pe_ratio, pb_ratio)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, code) DO UPDATE SET
                price=excluded.price, change_pct=excluded.change_pct, volume=excluded.volume,
                amount=excluded.amount, nav=excluded.nav, premium_discount=excluded.premium_discount,
                pe_ratio=excluded.pe_ratio, pb_ratio=excluded.pb_ratio
        """
        batch_data = [
            (date_str, e["code"], e["name"], e.get("price"), e.get("change_pct"),
             e.get("volume"), e.get("amount"), e.get("nav"), e.get("premium_discount"),
             e.get("pe_ratio"), e.get("pb_ratio"))
            for e in etfs
        ]
        with self._get_conn() as conn:
            count = self._batch_upsert(conn, sql, batch_data, batch_size=500)
        logger.info(f"Upserted {count} ETF records for {date_str}")
        return count

    def upsert_indices(self, date_str: str, indices: list[dict]) -> int:  # pyright: ignore[reportMissingTypeArgument]
        sql = """
            INSERT INTO index_daily (date, code, name, price, change_pct, pe_ratio, pb_ratio, pe_percentile, pb_percentile)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, code) DO UPDATE SET
                price=excluded.price, change_pct=excluded.change_pct, pe_ratio=excluded.pe_ratio,
                pb_ratio=excluded.pb_ratio, pe_percentile=excluded.pe_percentile, pb_percentile=excluded.pb_percentile
        """
        batch_data = [
            (date_str, idx["code"], idx["name"], idx.get("price"), idx.get("change_pct"),
             idx.get("pe_ratio"), idx.get("pb_ratio"), idx.get("pe_percentile"), idx.get("pb_percentile"))
            for idx in indices
        ]
        with self._get_conn() as conn:
            count = self._batch_upsert(conn, sql, batch_data)
        logger.info(f"Upserted {count} index records for {date_str}")
        return count

    def upsert_sectors(self, date_str: str, sectors: list[dict]) -> int:  # pyright: ignore[reportMissingTypeArgument]
        sql = """
            INSERT INTO sector_daily (date, name, change_pct, momentum_1m, momentum_3m, momentum_6m)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, name) DO UPDATE SET change_pct=excluded.change_pct
        """
        batch_data = [
            (date_str, s["name"], s.get("change_pct"), s.get("momentum_1m"),
             s.get("momentum_3m"), s.get("momentum_6m"))
            for s in sectors
        ]
        with self._get_conn() as conn:
            count = self._batch_upsert(conn, sql, batch_data, batch_size=500)
        return count

    def upsert_fund_flow(self, date_str: str, north_bound: Optional[float], main_force: Optional[float],
                         sector_flows: Optional[dict] = None) -> None:  # pyright: ignore[reportMissingTypeArgument]
        sql = """
            INSERT INTO fund_flow_daily (date, north_bound, main_force, sector_flows)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                north_bound=excluded.north_bound, main_force=excluded.main_force, sector_flows=excluded.sector_flows
        """
        batch_data = [(date_str, north_bound, main_force, json.dumps(sector_flows or {}, ensure_ascii=False))]
        with self._get_conn() as conn:
            _ = self._batch_upsert(conn, sql, batch_data)

    def upsert_macro(self, date_str: str, macro_data: dict) -> None:  # pyright: ignore[reportMissingTypeArgument]
        sql = """
            INSERT INTO macro_daily (date, vix, us10y, us5y, us3m, usdcny, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                vix=excluded.vix, us10y=excluded.us10y, us5y=excluded.us5y,
                us3m=excluded.us3m, usdcny=excluded.usdcny, extra=excluded.extra
        """
        batch_data = [(
            date_str, macro_data.get("vix"), macro_data.get("us10y"),
            macro_data.get("us5y"), macro_data.get("us3m"), macro_data.get("usdcny"),
            json.dumps({k: v for k, v in macro_data.items() if k not in ("vix", "us10y", "us5y", "us3m", "usdcny")})
        )]
        with self._get_conn() as conn:
            _ = self._batch_upsert(conn, sql, batch_data)

    def upsert_news(self, date_str: str, headlines: list[str]) -> int:
        sql = """
            INSERT OR IGNORE INTO news_daily (date, headline) VALUES (?, ?)
        """
        batch_data = [(date_str, h) for h in headlines[:10]]
        with self._get_conn() as conn:
            before = conn.total_changes
            _ = self._batch_upsert(conn, sql, batch_data)
            count = conn.total_changes - before
        return count

    def upsert_valuation(self, date_str: str, valuation_data: list[dict]) -> int:  # pyright: ignore[reportMissingTypeArgument]
        sql = """
            INSERT INTO valuation_daily (date, index_code, pe_current, pe_percentile, pb_current, pb_percentile)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, index_code) DO UPDATE SET
                pe_current=excluded.pe_current, pe_percentile=excluded.pe_percentile,
                pb_current=excluded.pb_current, pb_percentile=excluded.pb_percentile
        """
        batch_data = [
            (date_str, v["index_code"], v.get("pe_current"), v.get("pe_percentile"),
             v.get("pb_current"), v.get("pb_percentile"))
            for v in valuation_data
        ]
        with self._get_conn() as conn:
            count = self._batch_upsert(conn, sql, batch_data)
        return count

    def get_latest_indices(self) -> list[dict]:  # pyright: ignore[reportMissingTypeArgument]
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM index_daily WHERE date = (SELECT MAX(date) FROM index_daily)
            """).fetchall()
        return [dict(r) for r in rows]

    def get_latest_etfs(self, limit: int = 50) -> list[dict]:  # pyright: ignore[reportMissingTypeArgument]
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM etf_daily WHERE date = (SELECT MAX(date) FROM etf_daily)
                ORDER BY ABS(change_pct) DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_historical_etf(self, code: str, days: int = 60) -> list[dict]:  # pyright: ignore[reportMissingTypeArgument]
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT date, close AS price, open, high, low, close, volume, amount
                FROM etf_history WHERE code = ? ORDER BY date DESC LIMIT ?
            """, (code, days)).fetchall()
            if not rows:
                rows = conn.execute("""
                    SELECT date, price, change_pct, volume, pe_ratio, pb_ratio
                    FROM etf_daily WHERE code = ? ORDER BY date DESC LIMIT ?
                """, (code, days)).fetchall()
        return [dict(r) for r in rows]

    def get_historical_index(self, code: str, days: int = 252) -> list[dict]:  # pyright: ignore[reportMissingTypeArgument]
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT date, code, name, close AS price, open, high, low, close, volume, amount
                FROM index_history WHERE code = ? ORDER BY date DESC LIMIT ?
            """, (code, days)).fetchall()
            if not rows:
                rows = conn.execute("""
                    SELECT date, price, change_pct, pe_ratio, pb_ratio, pe_percentile, pb_percentile
                    FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT ?
                """, (code, days)).fetchall()
        return [dict(r) for r in rows]

    def get_latest_date(self) -> Optional[str]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT MAX(date) as max_date FROM index_daily").fetchone()
        return row["max_date"] if row else None

    def backup(self, backup_path: Optional[str] = None) -> str:
        import shutil
        dest = backup_path or f"data/backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(str(self.db_path), dest)
        logger.info(f"Database backed up to {dest}")
        return dest
