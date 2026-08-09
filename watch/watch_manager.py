"""
watch/watch_manager.py
------------------------
مسئول کل چرخه حیات یک Watch:
  - ثبت Watch جدید بعد از خروجی WATCH (بند ۱۱)
  - بررسی Triggerهای فعال (بند ۱۳) بدون فراخوانی مداوم AI (بند ۱۲)
  - قفل کردن Watch حین بررسی مجدد و جلوگیری از Trigger تکراری (بند ۱۹)
  - جایگزینی Watch با Watch جدید در سناریوی چندمرحله‌ای (بند ۱۵)
  - بستن Watch با انقضا یا ابطال (بند ۲۰)

این ماژول عمداً از AI/Telegram مستقل است - فقط با broker و storage کار
می‌کند تا بتوان آن را جدا تست کرد.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from broker.base import BrokerBase
from core.models import WatchDetails, WatchState, Direction, Grade
from storage import db

logger = logging.getLogger(__name__)


def create_watch_from_details(
    symbol: str,
    watch_details: WatchDetails,
    parent_analysis_id: str | None,
) -> WatchState:
    watch_id = str(uuid.uuid4())
    watch = WatchState(
        watch_id=watch_id,
        symbol=symbol,
        parent_analysis_id=parent_analysis_id,
        direction=watch_details.preferred_direction,
        grade=watch_details.current_or_potential_grade,
        trigger_type=watch_details.trigger_type,
        zone_or_level=watch_details.exact_zone_or_level,
        timeframes_to_recheck=watch_details.timeframes_to_recheck,
        expiration=_parse_expiration(watch_details.expiration),
        invalidation_condition=watch_details.invalidation,
        created_at=datetime.now(timezone.utc),
    )
    db.save_watch({
        "watch_id": watch.watch_id,
        "symbol": watch.symbol,
        "parent_analysis_id": watch.parent_analysis_id,
        "direction": watch.direction.value,
        "grade": watch.grade.value,
        "trigger_type": watch.trigger_type,
        "zone_or_level": watch.zone_or_level,
        "timeframes_to_recheck": watch.timeframes_to_recheck,
        "expiration": watch.expiration.isoformat(),
        "invalidation_condition": watch.invalidation_condition,
        "created_at": watch.created_at.isoformat(),
    })
    db.log_event("WATCH_CREATED", f"Watch جدید ثبت شد: {watch.zone_or_level}", symbol=symbol, watch_id=watch_id)
    return watch


def _parse_expiration(expiration_text: str) -> datetime:
    """
    زمان انقضا از AI به‌صورت متن آزاد ("18:00" یا ISO) می‌آید.
    اینجا سعی می‌کنیم آن را parse کنیم؛ در صورت شکست، ۴ ساعت پیش‌فرض
    در نظر گرفته می‌شود تا Watch هرگز بدون انقضا نماند (ایمنی بند ۲۰).
    """
    from datetime import timedelta
    try:
        return datetime.fromisoformat(expiration_text.replace("Z", "+00:00"))
    except Exception:
        try:
            hh, mm = expiration_text.strip().split(":")
            now = datetime.now(timezone.utc)
            candidate = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if candidate < now:
                candidate += timedelta(days=1)
            return candidate
        except Exception:
            logger.warning("پارس Expiration ناموفق بود ('%s')؛ ۴ ساعت پیش‌فرض اعمال شد.", expiration_text)
            return datetime.now(timezone.utc) + timedelta(hours=4)


def check_trigger(watch_row, broker: BrokerBase) -> tuple[bool, str]:
    """
    بند ۱۳: بررسی این‌که آیا شرط Trigger این Watch فعال شده یا نه.
    برمی‌گرداند (triggered: bool, reason: str).
    بند ۱۹: اگر Watch در حال حاضر قفل است (در حال بررسی مجدد) یا قبلاً
    Trigger شده، دوباره فعال نمی‌شود.

    نکته مهم (اصلاح باگ ارسال مکرر): بررسی شرط Trigger فقط یک‌بار به ازای
    هر کندل M5 تازه‌بسته‌شده انجام می‌شود، نه در هر Poll (هر چند ثانیه).
    بدون این گیت، برای Triggerهایی از نوع «سطح مشخص» که قیمت بعد از عبور
    همچنان بالای سطح می‌ماند، هر بار Poll دوباره True برمی‌گشت و باعث
    ارسال پیام تکراری می‌شد. استثنا: بررسی انقضای زمانی (Expiration) بر
    اساس ساعت است، نه کندل، پس از این گیت مستثنا است.
    """
    if watch_row["is_locked"] or watch_row["is_triggered"] or watch_row["is_closed"]:
        return False, ""

    now = datetime.now(timezone.utc)
    expiration = datetime.fromisoformat(watch_row["expiration"])
    if now >= expiration:
        return True, "EXPIRATION_REACHED"

    symbol = watch_row["symbol"]

    # --- گیت کندل M5: فقط یک‌بار به ازای هر کندل تازه بررسی شود ---
    latest_m5 = broker.get_candles(symbol, "M5", 1)
    if not latest_m5:
        return False, ""
    latest_candle_time = latest_m5[-1]["time"].isoformat()
    last_checked = watch_row["last_checked_candle_time"] if "last_checked_candle_time" in watch_row.keys() else None
    if last_checked == latest_candle_time:
        return False, ""  # این کندل قبلاً بررسی شده - صبر برای کندل بعدی
    # این کندل جدید است؛ صرف‌نظر از نتیجه، ثبت می‌شود که بررسی شد
    db.update_watch_last_checked_candle(watch_row["watch_id"], latest_candle_time)

    bid, ask = broker.get_current_price(symbol)
    mid_price = (bid + ask) / 2
    trigger_type = watch_row["trigger_type"]
    zone_text = watch_row["zone_or_level"]

    # تلاش برای استخراج عدد از zone_or_level (سطح تکی یا محدوده "1.1700-1.1750")
    levels = _extract_levels(zone_text)
    if not levels:
        return False, ""

    if "زون" in trigger_type or "محدوده" in trigger_type or len(levels) == 2:
        low, high = min(levels), max(levels)
        if low <= mid_price <= high:
            return True, f"PRICE_ENTERED_ZONE({low}-{high})"
        return False, ""

    level = levels[0]
    if "سطح" in trigger_type or "level" in trigger_type.lower():
        direction = watch_row["direction"]
        if direction == Direction.BUY.value and mid_price >= level:
            return True, f"PRICE_REACHED_LEVEL({level})"
        if direction == Direction.SELL.value and mid_price <= level:
            return True, f"PRICE_REACHED_LEVEL({level})"
        return False, ""

    if "کندل" in trigger_type or "candle" in trigger_type.lower():
        tf = "M5" if "M5" in trigger_type else "M15"
        candles = broker.get_candles(symbol, tf, 2)
        if not candles:
            return False, ""
        last_closed = candles[-2] if len(candles) >= 2 else candles[-1]
        close_price = last_closed["close"]
        direction = watch_row["direction"]
        if direction == Direction.BUY.value and close_price >= level:
            return True, f"CANDLE_{tf}_CLOSED_ABOVE({level})"
        if direction == Direction.SELL.value and close_price <= level:
            return True, f"CANDLE_{tf}_CLOSED_BELOW({level})"
        return False, ""

    return False, ""


def _extract_levels(text: str) -> list[float]:
    import re
    nums = re.findall(r"\d+\.\d+|\d+", text)
    try:
        return [float(n) for n in nums]
    except ValueError:
        return []


def is_same_setup(parent_watch: WatchState, new_details: WatchDetails, level_tolerance: float = 0.0005) -> bool:
    """
    بند ۲: مقایسه Watch فعلی (WatchState) با نتیجه WATCH تازه‌ای که از
    تحلیل مجدد آمده. اگر Direction، Zone/Level (با تلورانس کوچک برای
    تفاوت‌های اعشاری بی‌اهمیت)، Trigger Type و Invalidation یکسان باشند،
    این «همان ستاپ قبلی» است، نه یک به‌روزرسانی واقعی - نباید Watch جدید
    ساخته شود یا پیام جدید ارسال شود.
    """
    if parent_watch.direction != new_details.preferred_direction:
        return False
    if parent_watch.trigger_type.strip() != new_details.trigger_type.strip():
        return False
    if parent_watch.invalidation_condition.strip() != new_details.invalidation.strip():
        return False

    old_levels = _extract_levels(parent_watch.zone_or_level)
    new_levels = _extract_levels(new_details.exact_zone_or_level)
    if len(old_levels) != len(new_levels):
        return False
    for old_v, new_v in zip(sorted(old_levels), sorted(new_levels)):
        if abs(old_v - new_v) > level_tolerance:
            return False

    return True


def reset_for_continued_monitoring(watch_id: str) -> None:
    """
    وقتی تحلیل مجدد نشان می‌دهد ستاپ واقعاً تغییری نکرده (بند ۲ و ۵)،
    Watch قبلی به‌جای بسته/جایگزین‌شدن، فقط برای ادامه مانیتور باز و
    ریست می‌شود - Expiration و بقیه فیلدها دست‌نخورده می‌مانند.
    """
    db.update_watch_flags(watch_id, is_locked=False, is_triggered=False)
    db.log_event("WATCH_UNCHANGED", "ستاپ تغییر واقعی نداشت - مانیتور ادامه یافت", watch_id=watch_id)


def lock_watch(watch_id: str) -> None:
    db.update_watch_flags(watch_id, is_locked=True)
    db.log_event("WATCH_LOCKED", "قفل شد برای شروع بررسی مجدد", watch_id=watch_id)


def unlock_watch(watch_id: str) -> None:
    db.update_watch_flags(watch_id, is_locked=False)


def mark_triggered(watch_id: str, reason: str) -> None:
    db.update_watch_flags(watch_id, is_triggered=True)
    db.log_event("WATCH_TRIGGERED", reason, watch_id=watch_id)


def close_watch(watch_id: str, reason: str) -> None:
    db.update_watch_flags(watch_id, is_closed=True, is_locked=False)
    db.log_event("WATCH_CLOSED", reason, watch_id=watch_id)
