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
from core.clock import tehran_time
import logging
import asyncio
import re
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import ContextTypes

from config import config
from core.analysis_service import AnalysisService
from storage import db, work_queue
from telegram.error import BadRequest, NetworkError
from contextlib import ExitStack
from telegram_bot.notifier import format_analysis_message, format_error_message

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 1800  # کمی کمتر از سقف واقعی ۴۰۹۶ برای احتیاط
ANALYSIS_SYMBOLS = ("EURUSD", "GBPUSD", "XAUUSD", "USDJPY")


def _analysis_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("/analyze EURUSD", callback_data="analyze:EURUSD"),
            InlineKeyboardButton("/analyze GBPUSD", callback_data="analyze:GBPUSD"),
        ],
        [
            InlineKeyboardButton("/analyze XAUUSD", callback_data="analyze:XAUUSD"),
            InlineKeyboardButton("/analyze USDJPY", callback_data="analyze:USDJPY"),
        ],
    ])


def _is_authorized(update: Update) -> bool:
    if not config.telegram_allowed_user_ids:
        return config.allow_unrestricted_telegram
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
        "/symbols - نمایش دکمه‌های آماده تحلیل\n"
        "/analyze SYMBOL - شروع تحلیل جدید (مثال: /analyze EURUSD)\n"
        "/status - نمایش Watchهای فعال\n"
        "/health - وضعیت اتصال، کندل‌ها و صف‌ها (بدون تحلیل جدید)\n"
        "/history [SYMBOL] - نمایش سوابق تحلیل\n"
        "/performance [SYMBOL] - آمار تخمینی پیشنهادها\n"
        "/inspect [SYMBOL] - دیدن کامل ورودی/خروجی آخرین تحلیل (تصاویر، داده، پاسخ AI)\n\n"
        "⚠️ یادآوری: این ربات فقط تحلیل می‌کند. ثبت/مدیریت معامله همیشه دستی است.",
        reply_markup=_analysis_keyboard(),
    )


async def symbols_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text("⛔️ شما مجاز به استفاده از این ربات نیستید.")
        return
    await update.message.reply_text(
        "نماد موردنظر برای تحلیل را انتخاب کنید:",
        reply_markup=_analysis_keyboard(),
    )


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text('⛔️ شما مجاز به استفاده از این ربات نیستید.')
        return
    # Coalesce repeated requests instead of queuing unbounded MT5 calls.
    task = context.bot_data.get('health_task')
    if task is not None and not task.done():
        await update.message.reply_text('بررسی سلامت در حال انجام است؛ لطفاً صبر کنید.')
        return
    from core.health import health_report
    service = context.bot_data['analysis_service']
    task = asyncio.create_task(asyncio.to_thread(health_report, service.broker))
    context.bot_data['health_task'] = task
    await update.message.reply_text('در حال خواندن وضعیت سیستم؛ تحلیل جدید اجرا نمی‌شود.')
    report = await asyncio.shield(task)
    await _send_long_text(update, 'وضعیت سلامت AnewMe', report)


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text("⛔️ شما مجاز به استفاده از این ربات نیستید.")
        return

    if not context.args:
        await update.message.reply_text(
            "لطفاً یک نماد را انتخاب کنید یا دستور /analyze SYMBOL بفرستید:",
            reply_markup=_analysis_keyboard(),
        )
        return

    await _run_analysis(update, context, context.args[0].upper())


async def analyze_symbol_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_authorized(update):
        await query.answer("شما مجاز نیستید.", show_alert=True)
        return
    try:
        await query.answer()
    except BadRequest as exc:
        if not any(t in str(exc).lower() for t in ("query is too old", "query id is invalid", "response timeout")):
            raise
        db.log_event("CALLBACK_EXPIRED", "کلیک منقضی شد؛ تحلیل اجرا نشد.")
        work_queue.queue_message(f"expired:{update.update_id}", update.effective_chat.id,
                                "این کلیک منقضی شده است؛ لطفاً دوباره دکمه تحلیل را بزنید.")
        return
    except NetworkError:
        db.log_event("CALLBACK_NETWORK_ERROR", "پاسخ کلیک به تلگرام نرسید؛ تحلیل اجرا نشد.")
        return
    symbol = query.data.split(":", 1)[1].upper()
    if symbol not in ANALYSIS_SYMBOLS:
        await query.message.reply_text("نماد انتخاب‌شده معتبر نیست.")
        return
    await _run_analysis(update, context, symbol)


async def _run_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str) -> None:
    message = update.callback_query.message if update.callback_query else update.message
    if not re.fullmatch(r"[A-Z0-9._#-]{1,32}", symbol):
        await message.reply_text("نام نماد نامعتبر است؛ فقط حروف، عدد و . _ # - مجازند.")
        return

    job_id = f"manual:{update.update_id}"
    queued = work_queue.enqueue(job_id, symbol, update.effective_chat.id)
    text = (f"🔍 درخواست تحلیل {symbol} ثبت شد؛ نتیجه جداگانه ارسال می‌شود."
            if queued else f"برای {symbol} یک تحلیل در صف یا در حال اجراست؛ درخواست تکراری اجرا نشد.")
    work_queue.queue_message("ack:" + job_id, update.effective_chat.id, text)
    db.log_event("ANALYZE_REQUESTED" if queued else "ANALYZE_DUPLICATE_SUPPRESSED", text, symbol=symbol)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text("⛔️ شما مجاز به استفاده از این ربات نیستید.")
        return

    active_watches = db.get_active_watches()
    if not active_watches:
        await update.message.reply_text("در حال حاضر هیچ Watch فعالی وجود ندارد.")
        return

    lines = ["👀 واچ‌های فعال:\n"]
    for w in active_watches:
        state = "در حال پردازش" if w["is_locked"] else "در انتظار تریگر"
        direction = "خرید" if w["direction"] == "BUY" else "فروش"
        lines += [
            f"{w['symbol']} | واچ فعال",
            f"جهت: {direction}",
            f"شرط تریگر: {w['trigger_type']}",
            f"سطح/محدوده: {w['zone_or_level']}",
            f"شرط ابطال: {w['invalidation_condition']}",
            f"زمان انقضا: {tehran_time(w['expiration'])}",
            f"وضعیت فعلی: {state}",
            "",
        ]
    await _send_long_text(update, "", "\n".join(lines))


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
        f"📊 آمار تخمینی پیشنهادها{scope}\n",
        f"مجموع TRADEهای ثبت‌شده: {stats['total']}",
        f"✅ برد (رسیده به TP): {stats['wins']}",
        f"❌ باخت (خورده به SL): {stats['losses']}",
        f"⏳ منقضی‌شده بدون پر شدن: {stats['expired']}",
        f"❓ مبهم (TP و SL در یک کندل): {stats['ambiguous']}",
        f"🔄 هنوز باز/در انتظار: {stats['pending']}",
        "",
        f"نرخ برد (فقط از بین بسته‌شده‌ها): {win_rate}",
        f"میانگین R تخمینی: {avg_r}",
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
    with db.get_connection() as conn:
        passport_row = conn.execute('SELECT payload FROM decision_passports WHERE analysis_id=?', (row['id'],)).fetchone()
        replay_row = conn.execute('SELECT 1 FROM replay_capsules WHERE analysis_id=?', (row['id'],)).fetchone()
    if passport_row:
        passport = json.loads(passport_row['payload'])
        await _send_long_text(update, 'شناسنامه تصمیم',
            f"Analysis ID: {row['id']}\nPhase: {passport['phase']}\n"
            f"Build: {passport['build']['version']}\n"
            f"Source SHA256: {passport['build']['source_sha256']}\n"
            f"Input SHA256: {passport['input_sha256']}\n"
            f"Parent Watch: {passport['parent_watch_id'] or '-'}\n"
            f"بازپخش مرحله پس از پاسخ مدل: {'موجود' if replay_row else 'موجود نیست'}")

    # ۱) تصاویر واقعی ارسالی به AI
    try:
        chart_paths = [Path(p) for p in json.loads(row["chart_paths"] or "[]")]
        existing = [p for p in chart_paths if p.exists()]
        if existing:
            with ExitStack() as stack:
                media = [InputMediaPhoto(stack.enter_context(open(p, "rb")), caption=p.stem) for p in existing[:10]]
                if len(media) == 1:
                    await update.message.reply_photo(media[0].media)
                else:
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
    if not _is_authorized(update):
        await update.message.reply_text("⛔️ شما مجاز به استفاده از این ربات نیستید.")
        return
    await update.message.reply_text(
        "دستور نامعتبر است. دستورات موجود:\n"
        "/symbols\n/analyze SYMBOL\n/status\n/history [SYMBOL]\n/performance [SYMBOL]\n/inspect [SYMBOL]"
    )
