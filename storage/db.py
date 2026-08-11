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
    chart_descriptions_text TEXT,
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

CREATE TABLE IF NOT EXISTS trade_tracking (
    analysis_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    order_type TEXT NOT NULL,
    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    risk_percent REAL,
    reward_risk_ratio REAL,
    expiration TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    filled_at TEXT,
    closed_at TEXT,
    exit_price REAL,
    actual_r_multiple REAL
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
        # migration برای دیتابیس‌های ساخته‌شده قبل از اضافه‌شدن این ستون‌ها
        for stmt in (
            "ALTER TABLE watches ADD COLUMN last_checked_candle_time TEXT",
            "ALTER TABLE analyses ADD COLUMN chart_descriptions_text TEXT",
        ):
            try:
                conn.execute(stmt)
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
    chart_descriptions_text: str = "",
) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO analyses
               (id, symbol, created_at, status, direction, grade, reason, raw_ai_text,
                chart_descriptions_text, chart_paths, market_snapshot_json, trade_details_json,
                watch_details_json, parent_watch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                analysis_id, symbol, datetime.utcnow().isoformat(), status, direction, grade,
                reason, raw_ai_text, chart_descriptions_text,
                json.dumps(chart_paths, ensure_ascii=False),
                json.dumps(market_snapshot_dict, ensure_ascii=False, default=str),
                json.dumps(trade_details_dict, ensure_ascii=False) if trade_details_dict else None,
                json.dumps(watch_details_dict, ensure_ascii=False) if watch_details_dict else None,
                parent_watch_id,
            ),
        )


def get_latest_analysis(symbol: str | None = None) -> sqlite3.Row | None:
    with get_connection() as conn:
        if symbol:
            cur = conn.execute(
                "SELECT * FROM analyses WHERE symbol = ? ORDER BY created_at DESC LIMIT 1", (symbol,)
            )
        else:
            cur = conn.execute("SELECT * FROM analyses ORDER BY created_at DESC LIMIT 1")
        return cur.fetchone()


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


def get_active_watch_for_symbol(symbol: str) -> sqlite3.Row | None:
    """برای جلوگیری از ساخت چند Watch هم‌زمان روی یک نماد (بند جدید)."""
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM watches WHERE symbol = ? AND is_closed = 0 ORDER BY created_at DESC LIMIT 1",
            (symbol,),
        )
        return cur.fetchone()


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


# ---------------------------------------------------------- trade_tracking
def create_trade_tracking(
    analysis_id: str,
    symbol: str,
    direction: str,
    order_type: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    risk_percent: float | None,
    reward_risk_ratio: float | None,
    expiration: str,
) -> None:
    """
    بعد از هر TRADE معتبر (تأییدشده توسط validator.py)، یک رکورد ردیابی
    ثبت می‌شود تا بعداً بشود سنجید که آیا واقعاً به TP رسیده یا SL خورده.
    این تنها راه سنجش عینی «آیا این استراتژی سودآور است؟» است.
    """
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO trade_tracking
               (analysis_id, symbol, direction, order_type, entry, stop_loss, take_profit,
                risk_percent, reward_risk_ratio, expiration, created_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')""",
            (
                analysis_id, symbol, direction, order_type, entry, stop_loss, take_profit,
                risk_percent, reward_risk_ratio, expiration, datetime.utcnow().isoformat(),
            ),
        )


def get_open_trade_trackings() -> list[sqlite3.Row]:
    """معاملاتی که هنوز به نتیجه نرسیده‌اند (PENDING = هنوز پر نشده، FILLED = پر شده و منتظر SL/TP)."""
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM trade_tracking WHERE status IN ('PENDING', 'FILLED')")
        return cur.fetchall()


def update_trade_tracking(
    analysis_id: str,
    status: str,
    filled_at: str | None = None,
    closed_at: str | None = None,
    exit_price: float | None = None,
    actual_r_multiple: float | None = None,
) -> None:
    fields, values = ["status = ?"], [status]
    if filled_at is not None:
        fields.append("filled_at = ?"); values.append(filled_at)
    if closed_at is not None:
        fields.append("closed_at = ?"); values.append(closed_at)
    if exit_price is not None:
        fields.append("exit_price = ?"); values.append(exit_price)
    if actual_r_multiple is not None:
        fields.append("actual_r_multiple = ?"); values.append(actual_r_multiple)
    values.append(analysis_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE trade_tracking SET {', '.join(fields)} WHERE analysis_id = ?", values)


def get_performance_stats(symbol: str | None = None) -> dict:
    """
    آمار خام عملکرد واقعی برای دستور /performance.
    نکته صادقانه: این فقط بر اساس قیمت پیگیری‌شده توسط خودِ ربات است -
    اگر کاربر دستی معامله را زودتر بسته یا حجم را تغییر داده، این آمار
    آن را منعکس نمی‌کند (چون طبق طراحی سیستم، ربات به معاملات واقعی
    بروکر دسترسی/کنترلی ندارد).
    """
    with get_connection() as conn:
        query = "SELECT * FROM trade_tracking"
        params: tuple = ()
        if symbol:
            query += " WHERE symbol = ?"
            params = (symbol,)
        rows = conn.execute(query, params).fetchall()

    total = len(rows)
    wins = [r for r in rows if r["status"] == "WIN"]
    losses = [r for r in rows if r["status"] == "LOSS"]
    expired = [r for r in rows if r["status"] == "EXPIRED"]
    pending = [r for r in rows if r["status"] in ("PENDING", "FILLED")]
    closed = wins + losses

    win_rate = (len(wins) / len(closed) * 100) if closed else None
    r_values = [r["actual_r_multiple"] for r in closed if r["actual_r_multiple"] is not None]
    avg_r = (sum(r_values) / len(r_values)) if r_values else None

    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "expired": len(expired),
        "pending": len(pending),
        "win_rate_percent": win_rate,
        "avg_r_multiple": avg_r,
    }
