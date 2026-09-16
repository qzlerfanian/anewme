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
    health_command,
    inspect_command,
    performance_command,
    start_command,
    status_command,
    symbols_command,
    unknown_command,
)
from watch.monitor_loop import WatchMonitor
from watch.trade_tracker import TradeTracker

from core.runtime import configure_logging, ProcessLock
from core.worker import AnalysisWorker
from core.scheduled_analysis import ScheduledAnalysisLoop
from telegram_bot.delivery import DeliveryWorker
from storage import work_queue
configure_logging()
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

    if not config.telegram_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN تنظیم نشده است.")
    if not config.telegram_allowed_user_ids and not config.allow_unrestricted_telegram:
        raise RuntimeError(
            "TELEGRAM_ALLOWED_USER_IDS خالی است. برای امنیت حداقل یک شناسه وارد کنید؛ "
            "فقط برای تست محلی می‌توان ALLOW_UNRESTRICTED_TELEGRAM=true گذاشت."
        )

    broker = build_broker()
    try:
        broker.connect()
        init_db()  # migrations/expiry use the synchronized broker UTC clock
        await run_connected(broker)
    finally:
        broker.disconnect()


async def run_connected(broker) -> None:
    ai_client = AIClient()
    analysis_service = AnalysisService(broker=broker, ai_client=ai_client)

    builder = Application.builder().token(config.telegram_token)
    import os
    proxy = os.getenv("TELEGRAM_PROXY_URL")
    if proxy:
        builder = builder.proxy(proxy).get_updates_proxy(proxy)
    application = builder.build()
    async def on_error(update, context):
        logger.error("Telegram handler failed: %s", context.error)
    application.add_error_handler(on_error)
    application.bot_data["analysis_service"] = analysis_service

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("analyze", analyze_command))
    application.add_handler(CommandHandler("symbols", symbols_command))
    application.add_handler(CallbackQueryHandler(analyze_symbol_callback, pattern=r"^analyze:(EURUSD|GBPUSD|XAUUSD|USDJPY)$"))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("health", health_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("performance", performance_command))
    application.add_handler(CommandHandler("inspect", inspect_command))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    monitor = WatchMonitor(broker=broker, analysis_service=analysis_service)
    worker = AnalysisWorker(analysis_service)
    delivery = DeliveryWorker(application.bot)
    worker.recover()
    tracker = TradeTracker(broker=broker)
    scheduler = ScheduledAnalysisLoop(broker, analysis_service)

    async with application:
        # معرفی دستورات به تلگرام تا با زدن "/" منوی خودکار همه دستورات را نشان دهد
        await application.bot.set_my_commands([
            BotCommand("start", "شروع و راهنما"),
            BotCommand("analyze", "شروع تحلیل یک نماد - مثال: /analyze EURUSD"),
            BotCommand("symbols", "انتخاب سریع نماد برای تحلیل"),
            BotCommand("status", "نمایش Watchهای فعال"),
            BotCommand("health", "وضعیت اتصال، کندل‌ها و صف پردازش"),
            BotCommand("history", "نمایش سوابق تحلیل"),
            BotCommand("performance", "آمار تخمینی پیگیری سیگنال‌ها"),
            BotCommand("inspect", "دیدن کامل ورودی/خروجی آخرین تحلیل"),
        ])
        tasks = []
        try:
            await application.start()
            await application.updater.start_polling()
            logger.info("ربات ANEWME اجرا شد.")
            loops = [monitor, tracker, worker, delivery]
            if config.scheduled_analysis_enabled:
                loops.append(scheduler)
            tasks = [asyncio.create_task(loop.start()) for loop in loops]
            await asyncio.gather(*tasks)
        finally:
            monitor.stop()
            tracker.stop()
            worker.stop()
            delivery.stop()
            scheduler.stop()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()
            health_task = application.bot_data.get('health_task')
            if health_task is not None:
                await asyncio.gather(health_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        with ProcessLock():
            asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("ربات متوقف شد.")
