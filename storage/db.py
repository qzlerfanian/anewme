"""
storage/db.py
--------------
بند ۲۰: "سوابق تحلیل شامل زمان، نماد، تصاویر، داده بازار، نتایج اولیه و
مجدد، Watchها، اطلاعات معامله و خطاها ذخیره شود."

از SQLite ساده استفاده شده (بدون ORM سنگین) چون حجم داده این پروژه
(تحلیل‌های یک/چند نماد در روز) کوچک است و سادگی برای دیباگ توسط کارفرما
مهم‌تر از مقیاس‌پذیری افراطی است. در صورت رشد پروژه، به‌راحتی به
PostgreSQL قابل مهاجرت است چون تمام کوئری‌ها در این یک فایل متمرکزند.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    direction TEXT,
    grade TEXT,
    reason TEXT,
    raw_ai_text TEXT,
    chart_paths TEXT,
    market_snapshot_json TEXT,
    trade_details_json TEXT,
    watch_details_json TEXT,
    parent_watch_id TEXT
);

CREATE TABLE IF NOT EXISTS watches (
    watch_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    parent_analysis_id TEXT,
    direction TEXT NOT NULL,
    grade TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    zone_or_level TEXT NOT NULL,
    timeframes_to_recheck TEXT,
    expiration TEXT,
    invalidation_condition TEXT,
    created_at TEXT NOT NULL,
    is_locked INTEGER DEFAULT 0,
    is_triggered INTEGER DEFAULT 0,
    is_closed INTEGER DEFAULT 0,
    last_checked_candle_time TEXT
);

CREATE TABLE IF NOT EXISTS events_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    symbol TEXT,
    watch_id TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS errors_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT,
    context TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        # migration برای دیتابیس‌های ساخته‌شده قبل از اضافه‌شدن این ستون
        try:
            conn.execute("ALTER TABLE watches ADD COLUMN last_checked_candle_time TEXT")
        except sqlite3.OperationalError:
            pass  # ستون از قبل وجود دارد


# ---------------------------------------------------------------- analyses
def save_analysis(
    analysis_id: str,
    symbol: str,
    status: str,
    direction: str | None,
    grade: str | None,
    reason: str,
    raw_ai_text: str,
    chart_paths: list[str],
    market_snapshot_dict: dict,
    trade_details_dict: dict | None,
    watch_details_dict: dict | None,
    parent_watch_id: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO analyses
               (id, symbol, created_at, status, direction, grade, reason, raw_ai_text,
                chart_paths, market_snapshot_json, trade_details_json, watch_details_json,
                parent_watch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                analysis_id, symbol, datetime.utcnow().isoformat(), status, direction, grade,
                reason, raw_ai_text, json.dumps(chart_paths, ensure_ascii=False),
                json.dumps(market_snapshot_dict, ensure_ascii=False, default=str),
                json.dumps(trade_details_dict, ensure_ascii=False) if trade_details_dict else None,
                json.dumps(watch_details_dict, ensure_ascii=False) if watch_details_dict else None,
                parent_watch_id,
            ),
        )


def get_history(symbol: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
    with get_connection() as conn:
        if symbol:
            cur = conn.execute(
                "SELECT * FROM analyses WHERE symbol = ? ORDER BY created_at DESC LIMIT ?",
                (symbol, limit),
            )
        else:
            cur = conn.execute("SELECT * FROM analyses ORDER BY created_at DESC LIMIT ?", (limit,))
        return cur.fetchall()


# ------------------------------------------------------------------ watches
def save_watch(watch: dict) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO watches
               (watch_id, symbol, parent_analysis_id, direction, grade, trigger_type,
                zone_or_level, timeframes_to_recheck, expiration, invalidation_condition,
                created_at, is_locked, is_triggered, is_closed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                watch["watch_id"], watch["symbol"], watch.get("parent_analysis_id"),
                watch["direction"], watch["grade"], watch["trigger_type"],
                watch["zone_or_level"], json.dumps(watch["timeframes_to_recheck"], ensure_ascii=False),
                watch["expiration"], watch["invalidation_condition"], watch["created_at"],
                int(watch.get("is_locked", False)), int(watch.get("is_triggered", False)),
                int(watch.get("is_closed", False)),
            ),
        )


def get_active_watches() -> list[sqlite3.Row]:
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM watches WHERE is_closed = 0")
        return cur.fetchall()


def update_watch_last_checked_candle(watch_id: str, candle_time_iso: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE watches SET last_checked_candle_time = ? WHERE watch_id = ?",
            (candle_time_iso, watch_id),
        )


def update_watch_flags(watch_id: str, *, is_locked: bool | None = None,
                        is_triggered: bool | None = None, is_closed: bool | None = None) -> None:
    fields, values = [], []
    if is_locked is not None:
        fields.append("is_locked = ?"); values.append(int(is_locked))
    if is_triggered is not None:
        fields.append("is_triggered = ?"); values.append(int(is_triggered))
    if is_closed is not None:
        fields.append("is_closed = ?"); values.append(int(is_closed))
    if not fields:
        return
    values.append(watch_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE watches SET {', '.join(fields)} WHERE watch_id = ?", values)


# -------------------------------------------------------------------- logs
def log_event(event_type: str, message: str, symbol: str | None = None, watch_id: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO events_log (created_at, event_type, symbol, watch_id, message) VALUES (?, ?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), event_type, symbol, watch_id, message),
        )


def log_error(context: str, message: str, symbol: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO errors_log (created_at, symbol, context, message) VALUES (?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), symbol, context, message),
        )


# ---------------------------------------------------------------- settings
def get_setting(key: str, default: str | None = None) -> str | None:
    with get_connection() as conn:
        cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
