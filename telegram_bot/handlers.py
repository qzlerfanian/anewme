"""
telegram_bot/handlers.py
---------------------------
بند ۲: کاربر باید بتواند با ارسال نام نماد، تحلیل جدید را شروع کند:
  /analyze EURUSD
  /analyze GBPUSD
  /status
  /history
  /performance
  /inspect

ربات باید دریافت فرمان و شروع تحلیل را تأیید کند (پاسخ فوری «شروع شد»)
و سپس نتیجه واقعی را جداگانه (بعد از پردازش) بفرستد - چون تحلیل AI چند
ثانیه طول می‌کشد و کاربر نباید بدون پاسخ بماند.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from telegram import InputMediaPhoto, Update
from telegram.ext import ContextTypes

from config import config
from core.analysis_service import AnalysisService
from storage import db
from telegram_bot.notifier import format_analysis_message, format_error_message

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4000  # کمی کمتر از سقف واقعی ۴۰۹۶ برای احتیاط


def _is_authorized(update: Update) -> bool:
    if not config.telegram_allowed_user_ids:
        return True  # اگر لیست سفید تنظیم نشده، محدودیتی اعمال نمی‌شود (برای تست)
    return update.effective_user and update.effective_user.id in config.telegram_allowed_user_ids


async def _send_long_text(update: Update, header: str, body: str) -> None:
    """ارسال متن طولانی با تکه‌تکه‌کردن خودکار زیر سقف کاراکتر تلگرام."""
    full = f"{header}\n{body}" if body.strip() else f"{header}\n(خالی)"
    for i in range(0, len(full), TELEGRAM_MESSAGE_LIMIT):
        await update.message.reply_text(full[i:i + TELEGRAM_MESSAGE_LIMIT])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text("⛔️ شما مجاز به استفاده از این ربات نیستید.")
        return
    await update.message.reply_text(
        "👋 ربات تحلیل ANEWME V3 آماده است.\n\n"
        "دستورات:\n"
        "/analyze SYMBOL - شروع تحلیل جدید (مثال: /analyze EURUSD)\n"
        "/status - نمایش Watchهای فعال\n"
        "/history [SYMBOL] - نمایش سوابق تحلیل\n"
        "/performance [SYMBOL] - آمار واقعی برد/باخت TRADEها\n"
        "/inspect [SYMBOL] - دیدن کامل ورودی/خروجی آخرین تحلیل (تصاویر، داده، پاسخ AI)\n\n"
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


async def performance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text("⛔️ شما مجاز به استفاده از این ربات نیستید.")
        return

    symbol = context.args[0].upper() if context.args else None
    stats = db.get_performance_stats(symbol=symbol)

    if stats["total"] == 0:
        await update.message.reply_text(
            "هنوز هیچ TRADE‌ای ثبت نشده که بتوان عملکردش را سنجید."
        )
        return

    scope = f" ({symbol})" if symbol else " (همه نمادها)"
    win_rate = f"{stats['win_rate_percent']:.1f}%" if stats["win_rate_percent"] is not None else "—"
    avg_r = f"{stats['avg_r_multiple']:.2f}R" if stats["avg_r_multiple"] is not None else "—"

    lines = [
        f"📊 آمار عملکرد واقعی{scope}\n",
        f"مجموع TRADEهای ثبت‌شده: {stats['total']}",
        f"✅ برد (رسیده به TP): {stats['wins']}",
        f"❌ باخت (خورده به SL): {stats['losses']}",
        f"⏳ منقضی‌شده بدون پر شدن: {stats['expired']}",
        f"🔄 هنوز باز/در انتظار: {stats['pending']}",
        "",
        f"نرخ برد (فقط از بین بسته‌شده‌ها): {win_rate}",
        f"میانگین R واقعی: {avg_r}",
        "",
        "⚠️ این آمار بر اساس تعقیب قیمت توسط خودِ ربات است، نه حساب واقعی "
        "شما - اگر معامله را زودتر بسته یا حجم را تغییر داده باشید، این "
        "آمار آن را نشان نمی‌دهد.",
    ]
    await update.message.reply_text("\n".join(lines))


async def inspect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    دیدن کامل ورودی/خروجی یک تحلیل: تصاویر واقعی ارسالی به AI، داده بازار،
    توصیف مرحله دید (Stage 1)، و پاسخ خام نهایی مدل (Stage 2).
    """
    if not _is_authorized(update):
        await update.message.reply_text("⛔️ شما مجاز به استفاده از این ربات نیستید.")
        return

    symbol = context.args[0].upper() if context.args else None
    row = db.get_latest_analysis(symbol=symbol)
    if row is None:
        await update.message.reply_text("هیچ تحلیلی برای نمایش یافت نشد.")
        return

    await update.message.reply_text(
        f"🔎 بازبینی تحلیل {row['symbol']} | {row['created_at']} | {row['status']} | {row['grade'] or '-'}"
    )

    # ۱) تصاویر واقعی ارسالی به AI
    try:
        chart_paths = [Path(p) for p in json.loads(row["chart_paths"] or "[]")]
        existing = [p for p in chart_paths if p.exists()]
        if existing:
            media = [InputMediaPhoto(open(p, "rb"), caption=p.stem) for p in existing[:10]]
            await update.message.reply_media_group(media)
        else:
            await update.message.reply_text("(تصاویر این تحلیل دیگر روی دیسک موجود نیستند.)")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ارسال تصاویر inspect ناموفق بود: %s", exc)
        await update.message.reply_text(f"(خطا در ارسال تصاویر: {exc})")

    # ۲) داده بازار خام
    try:
        snapshot_summary = json.loads(row["market_snapshot_json"] or "{}")
        summary_text = (
            f"Bid: {snapshot_summary.get('bid')} | Ask: {snapshot_summary.get('ask')} | "
            f"Market Open: {snapshot_summary.get('market_open')}"
        )
    except Exception:  # noqa: BLE001
        summary_text = "(قابل خواندن نبود)"
    await _send_long_text(update, "--- داده بازار (خلاصه) ---", summary_text)

    # ۳) توصیف مرحله دید (Stage 1 - چیزی که AI از روی تصویر دیده)
    await _send_long_text(update, "--- مرحله ۱: توصیف بصری AI از تصاویر ---", row["chart_descriptions_text"] or "")

    # ۴) پاسخ خام نهایی مدل (Stage 2)
    await _send_long_text(update, "--- مرحله ۲: پاسخ خام نهایی AI ---", row["raw_ai_text"] or "")


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
        "دستور نامعتبر است. دستورات موجود:\n"
        "/analyze SYMBOL\n/status\n/history [SYMBOL]\n/performance [SYMBOL]\n/inspect [SYMBOL]"
    )
