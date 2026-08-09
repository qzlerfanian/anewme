"""
telegram_bot/handlers.py
---------------------------
بند ۲: کاربر باید بتواند با ارسال نام نماد، تحلیل جدید را شروع کند:
  /analyze EURUSD
  /analyze GBPUSD
  /status
  /history

ربات باید دریافت فرمان و شروع تحلیل را تأیید کند (پاسخ فوری «شروع شد»)
و سپس نتیجه واقعی را جداگانه (بعد از پردازش) بفرستد - چون تحلیل AI چند
ثانیه طول می‌کشد و کاربر نباید بدون پاسخ بماند.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from config import config
from core.analysis_service import AnalysisService
from storage import db
from telegram_bot.notifier import format_analysis_message, format_error_message

logger = logging.getLogger(__name__)


def _is_authorized(update: Update) -> bool:
    if not config.telegram_allowed_user_ids:
        return True  # اگر لیست سفید تنظیم نشده، محدودیتی اعمال نمی‌شود (برای تست)
    return update.effective_user and update.effective_user.id in config.telegram_allowed_user_ids


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text("⛔️ شما مجاز به استفاده از این ربات نیستید.")
        return
    await update.message.reply_text(
        "👋 ربات تحلیل ANEWME V3 آماده است.\n\n"
        "دستورات:\n"
        "/analyze SYMBOL - شروع تحلیل جدید (مثال: /analyze EURUSD)\n"
        "/status - نمایش Watchهای فعال\n"
        "/history [SYMBOL] - نمایش سوابق تحلیل\n\n"
        "⚠️ یادآوری: این ربات فقط تحلیل می‌کند. ثبت/مدیریت معامله همیشه دستی است."
    )


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text("⛔️ شما مجاز به استفاده از این ربات نیستید.")
        return

    if not context.args:
        await update.message.reply_text("لطفاً نام نماد را وارد کنید. مثال: /analyze EURUSD")
        return

    symbol = context.args[0].upper()

    # بند ۲: تأیید فوری دریافت فرمان و شروع تحلیل
    await update.message.reply_text(f"🔍 دریافت شد. شروع تحلیل {symbol}...")
    db.log_event("ANALYZE_REQUESTED", f"درخواست تحلیل {symbol} از کاربر", symbol=symbol)

    analysis_service: AnalysisService = context.bot_data["analysis_service"]

    try:
        result = analysis_service.run_initial_analysis(symbol)
        await update.message.reply_text(format_analysis_message(result))
    except Exception as exc:  # noqa: BLE001
        logger.exception("تحلیل %s ناموفق بود", symbol)
        db.log_error("analyze_command", str(exc), symbol=symbol)
        await update.message.reply_text(format_error_message("تحلیل", str(exc), symbol))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text("⛔️ شما مجاز به استفاده از این ربات نیستید.")
        return

    active_watches = db.get_active_watches()
    if not active_watches:
        await update.message.reply_text("در حال حاضر هیچ Watch فعالی وجود ندارد.")
        return

    lines = ["👀 Watchهای فعال:\n"]
    for w in active_watches:
        lock_tag = " (در حال بررسی مجدد)" if w["is_locked"] else ""
        lines.append(
            f"• {w['symbol']} | {w['direction']} | {w['grade']} | "
            f"Zone: {w['zone_or_level']} | Expires: {w['expiration']}{lock_tag}"
        )
    await update.message.reply_text("\n".join(lines))


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text("⛔️ شما مجاز به استفاده از این ربات نیستید.")
        return

    symbol = context.args[0].upper() if context.args else None
    rows = db.get_history(symbol=symbol, limit=10)
    if not rows:
        await update.message.reply_text("سابقه‌ای یافت نشد.")
        return

    lines = ["🗂 آخرین تحلیل‌ها:\n"]
    for r in rows:
        lines.append(f"• {r['created_at']} | {r['symbol']} | {r['status']} | {r['grade'] or '-'}")
    await update.message.reply_text("\n".join(lines))


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "دستور نامعتبر است. دستورات موجود:\n/analyze SYMBOL\n/status\n/history [SYMBOL]"
    )
