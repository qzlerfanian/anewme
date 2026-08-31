"""
main.py
--------
نقطه ورود برنامه. تمام سرویس‌ها (بروکر، AI، تلگرام، مانیتور Watch) اینجا
سیم‌کشی (wire) می‌شوند. جریان کلی همان بند ۲۱ سند است.
"""

from __future__ import annotations

import asyncio
import logging
import platform

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from config import config, LOG_DIR
from core.ai_client import AIClient
from core.analysis_service import AnalysisService
from storage.db import init_db
from telegram_bot.handlers import (
    analyze_command,
    analyze_symbol_callback,
    history_command,
    inspect_command,
    performance_command,
    start_command,
    status_command,
    symbols_command,
    unknown_command,
)
from watch.monitor_loop import WatchMonitor
from watch.trade_tracker import TradeTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "anewme_bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def build_broker():
    """
    انتخاب پیاده‌سازی بروکر بر اساس پلتفرم.
    روی ویندوز از MT5Broker واقعی استفاده می‌شود؛ در غیر این صورت
    (توسعه/تست روی لینوکس) باید broker جایگزین (مثلاً یک Mock یا REST
    Broker) تزریق شود.
    """
    if platform.system() == "Windows":
        from broker.mt5_broker import MT5Broker
        import os
        return MT5Broker(
            login=int(os.getenv("MT5_LOGIN", "0")) or None,
            password=os.getenv("MT5_PASSWORD") or None,
            server=os.getenv("MT5_SERVER") or None,
        )
    raise RuntimeError(
        "این پلتفرم (غیر ویندوز) پشتیبانی بروکر واقعی ندارد. "
        "برای اجرای واقعی از یک سرور/VPS ویندوزی با MT5 استفاده کنید، "
        "یا یک broker/rest_broker.py سفارشی بنویسید و اینجا جایگزین کنید."
    )


async def run() -> None:
    init_db()

    if not config.telegram_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN تنظیم نشده است.")
    if not config.telegram_allowed_user_ids and not config.allow_unrestricted_telegram:
        raise RuntimeError(
            "TELEGRAM_ALLOWED_USER_IDS خالی است. برای امنیت حداقل یک شناسه وارد کنید؛ "
            "فقط برای تست محلی می‌توان ALLOW_UNRESTRICTED_TELEGRAM=true گذاشت."
        )

    broker = build_broker()
    broker.connect()

    ai_client = AIClient()
    analysis_service = AnalysisService(broker=broker, ai_client=ai_client)

    application = Application.builder().token(config.telegram_token).build()
    application.bot_data["analysis_service"] = analysis_service

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("analyze", analyze_command))
    application.add_handler(CommandHandler("symbols", symbols_command))
    application.add_handler(CallbackQueryHandler(analyze_symbol_callback, pattern=r"^analyze:(EURUSD|GBPUSD|XAUUSD|USDJPY)$"))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("performance", performance_command))
    application.add_handler(CommandHandler("inspect", inspect_command))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    async def notify(text: str) -> None:
        for user_id in config.telegram_allowed_user_ids:
            try:
                await application.bot.send_message(chat_id=user_id, text=text)
            except Exception:
                logger.exception("ارسال پیام به %s ناموفق بود", user_id)

    monitor = WatchMonitor(broker=broker, analysis_service=analysis_service, notify=notify)
    tracker = TradeTracker(broker=broker)

    async with application:
        # معرفی دستورات به تلگرام تا با زدن "/" منوی خودکار همه دستورات را نشان دهد
        await application.bot.set_my_commands([
            BotCommand("start", "شروع و راهنما"),
            BotCommand("analyze", "شروع تحلیل یک نماد - مثال: /analyze EURUSD"),
            BotCommand("symbols", "انتخاب سریع نماد برای تحلیل"),
            BotCommand("status", "نمایش Watchهای فعال"),
            BotCommand("history", "نمایش سوابق تحلیل"),
            BotCommand("performance", "آمار واقعی برد/باخت TRADEها"),
            BotCommand("inspect", "دیدن کامل ورودی/خروجی آخرین تحلیل"),
        ])
        await application.start()
        await application.updater.start_polling()
        logger.info("ربات ANEWME اجرا شد.")
        try:
            await asyncio.gather(monitor.start(), tracker.start())
        finally:
            monitor.stop()
            tracker.stop()
            await application.updater.stop()
            await application.stop()
            broker.disconnect()


if __name__ == "__main__":
    asyncio.run(run())
