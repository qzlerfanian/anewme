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
            triggered, reason = await asyncio.to_thread(watch_manager.check_trigger, watch_row, self.broker)
            if not triggered:
                continue

            watch_id = watch_row["watch_id"]
            symbol = watch_row["symbol"]

            if reason in ("EXPIRATION_REACHED", "INVALIDATION_REACHED"):
                if reason == "EXPIRATION_REACHED":
                    status = "EXPIRED"
                    label = "منقضی شد"
                    detail = "تا زمان انقضا شرط تریگر اتفاق نیفتاد."
                else:
                    status = "INVALIDATED"
                    label = "باطل شد"
                    detail = f"شرط ابطال محقق شد: {watch_row['invalidation_condition']}"
                if not watch_manager.close_watch(watch_id, status, detail):
                    continue
                await self.notify(f"🚫 {symbol} | واچ {label}\nدلیل: {detail}")
                db.record_watch_notification(watch_id, suppressed=False, note=f"پیام پایان {status} ارسال شد.")
                continue

            # Watch قبلی پیش از تحلیل مجدد، قطعی و غیرقابل بازگشت بسته می‌شود.
            trigger_detail = (
                f"شرط تریگر محقق شد: {watch_row['trigger_type']} | "
                f"سطح/محدوده: {watch_row['zone_or_level']} | نتیجه فنی: {reason}"
            )
            # مهم: فقط workerای که انتقال اتمیک ACTIVE -> TRIGGERED را
            # انجام دهد اجازه پیام و reanalysis دارد.
            if not watch_manager.claim_trigger(watch_id, trigger_detail):
                db.log_event(
                    "WATCH_TRIGGER_CLAIM_SKIPPED",
                    "Watch قبلاً توسط worker دیگری تریگر/بسته شده بود.",
                    symbol=symbol, watch_id=watch_id,
                )
                continue
            await self.notify(f"✅ {symbol} | واچ تریگر شد\nدلیل: {trigger_detail}\nتحلیل مجدد خودکار آغاز می‌شود.")
            db.record_watch_notification(watch_id, suppressed=False, note="پیام پایان TRIGGERED ارسال شد.")
            db.mark_reanalysis_started(watch_id)

            try:
                # رفرش ردیف بعد از بسته‌شدن؛ parent بسته است و هر WATCH بعدی
                # الزاماً به‌عنوان رکورد جدید ساخته خواهد شد.
                fresh_row = self._get_watch_row(watch_id)
                result = await asyncio.to_thread(self.analysis_service.run_watch_recheck, fresh_row)
                db.mark_reanalysis_completed(watch_id, result.status.value)
                if not result.suppress_notification:
                    await self.notify(_format_result_message(result))
                    db.record_watch_notification(
                        watch_id, suppressed=False,
                        note=f"پیام نتیجه تحلیل مجدد {result.status.value} ارسال شد.",
                    )
                else:
                    db.record_watch_notification(
                        watch_id, suppressed=True,
                        note=f"پیام نتیجه {result.status.value} به‌علت suppress_notification ارسال نشد.",
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("تحلیل مجدد Watch %s ناموفق بود: %s", watch_id, exc)
                db.log_error("watch_recheck", str(exc), symbol=symbol)
                db.mark_reanalysis_completed(watch_id, "ERROR")
                await self.notify(f"❌ خطا در تحلیل مجدد {symbol}: {exc}")
                db.record_watch_notification(watch_id, suppressed=False, note="پیام خطای تحلیل مجدد ارسال شد.")

    @staticmethod
    def _get_watch_row(watch_id: str):
        with db.get_connection() as conn:
            cur = conn.execute("SELECT * FROM watches WHERE watch_id = ?", (watch_id,))
            return cur.fetchone()


def _format_result_message(result) -> str:
    from telegram_bot.notifier import format_analysis_message
    return format_analysis_message(result)
