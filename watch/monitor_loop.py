"""
watch/monitor_loop.py
------------------------
بند ۱۲: "بعد از ثبت Watch، ربات باید بدون ارسال مداوم تصویر به هوش
مصنوعی، فقط قیمت و وضعیت کندل‌ها را مانیتور کند. هوش مصنوعی فقط وقتی
دوباره فراخوانی شود که شرط Watch فعال شده باشد یا کاربر بررسی دستی بخواهد."

این حلقه به‌صورت دوره‌ای (config.watch_poll_interval_seconds) فقط قیمت/کندل
چک می‌کند - هیچ تصویری تولید نمی‌شود و هیچ درخواستی به AI نمی‌رود مگر
Trigger فعال شود.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable

from broker.base import BrokerBase
from config import config
from core.analysis_service import AnalysisService
from core.models import AnalysisStatus
from storage import db
from watch import watch_manager

logger = logging.getLogger(__name__)

# سیگنیچر callback برای اعلام نتایج به تلگرام - جداسازی از منطق مانیتور
NotifyCallback = Callable[[str], Awaitable[None]]


class WatchMonitor:
    def __init__(self, broker: BrokerBase, analysis_service: AnalysisService, notify: NotifyCallback):
        self.broker = broker
        self.analysis_service = analysis_service
        self.notify = notify
        self._running = False

    async def start(self) -> None:
        self._running = True
        logger.info("حلقه مانیتور Watch شروع شد (هر %s ثانیه).", config.watch_poll_interval_seconds)
        while self._running:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001 - هرگز نباید حلقه به‌خاطر یک خطا متوقف شود
                logger.exception("خطا در حلقه مانیتور Watch: %s", exc)
                db.log_error("watch_monitor_loop", str(exc))
            await asyncio.sleep(config.watch_poll_interval_seconds)

    def stop(self) -> None:
        self._running = False

    async def _tick(self) -> None:
        active_watches = db.get_active_watches()
        for watch_row in active_watches:
            triggered, reason = watch_manager.check_trigger(watch_row, self.broker)
            if not triggered:
                continue

            watch_id = watch_row["watch_id"]
            symbol = watch_row["symbol"]

            # بند ۱۹: جلوگیری از پردازش تکراری - بلافاصله علامت‌گذاری و قفل
            watch_manager.mark_triggered(watch_id, reason)
            watch_manager.lock_watch(watch_id)
            db.log_event("WATCH_TRIGGER_CHECK", f"بررسی مجدد آغاز شد: {reason}", symbol=symbol, watch_id=watch_id)

            try:
                # رفرش ردیف بعد از قفل شدن
                fresh_row = self._get_watch_row(watch_id)
                result = self.analysis_service.run_watch_recheck(fresh_row)
                # بند ۴: فقط در صورت تغییر واقعی ستاپ (TRADE، Invalidate،
                # Expire یا تغییر واقعی) پیام جدید ارسال شود - اگر ستاپ
                # عیناً همان قبلی بود، analysis_service این پرچم را ست
                # می‌کند تا پیام تکراری فرستاده نشود.
                if not result.suppress_notification:
                    await self.notify(_format_result_message(result))
            except Exception as exc:  # noqa: BLE001
                logger.exception("تحلیل مجدد Watch %s ناموفق بود: %s", watch_id, exc)
                db.log_error("watch_recheck", str(exc), symbol=symbol)
                await self.notify(f"❌ خطا در تحلیل مجدد {symbol}: {exc}")
                # خطای موقتی (مثلاً قطعی شبکه) نباید Watch را برای همیشه
                # غیرفعال کند - قفل و پرچم Trigger هر دو ریست می‌شوند تا
                # در کندل M5 بعدی دوباره تلاش شود.
                watch_manager.reset_for_continued_monitoring(watch_id)

    @staticmethod
    def _get_watch_row(watch_id: str):
        with db.get_connection() as conn:
            cur = conn.execute("SELECT * FROM watches WHERE watch_id = ?", (watch_id,))
            return cur.fetchone()


def _format_result_message(result) -> str:
    from telegram_bot.notifier import format_analysis_message
    return format_analysis_message(result)
