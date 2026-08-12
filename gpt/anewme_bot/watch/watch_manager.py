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
from datetime import datetime, timezone, timedelta

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
        parsed = datetime.fromisoformat(expiration_text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
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
    close_status = watch_row["close_status"] if "close_status" in watch_row.keys() else None
    if watch_row["is_locked"] or watch_row["is_triggered"] or watch_row["is_closed"] or close_status is not None:
        return False, ""

    now = datetime.now(timezone.utc)
    expiration = datetime.fromisoformat(watch_row["expiration"].replace("Z", "+00:00"))
    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=timezone.utc)
    if now >= expiration:
        return True, "EXPIRATION_REACHED"

    symbol = watch_row["symbol"]

    trigger_type = watch_row["trigger_type"]
    trigger_type_lower = trigger_type.lower()
    if "زمان مشخص" in trigger_type_lower or "specific time" in trigger_type_lower:
        created_at = datetime.fromisoformat(watch_row["created_at"].replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        target = _parse_time_trigger(watch_row["zone_or_level"], created_at)
        return (now >= target, f"SPECIFIC_TIME_REACHED({target.isoformat()})" if now >= target else "")

    # --- گیت کندل M5: فقط یک‌بار به ازای هر کندل تازه بررسی شود ---
    latest_m5 = broker.get_candles(symbol, "M5", 1)
    if not latest_m5:
        return False, ""
    latest_candle_time = latest_m5[-1]["time"].isoformat()
    # claim اتمیک: حتی با چند monitor/process فقط یکی این کندل را بررسی می‌کند.
    if not db.claim_watch_candle(watch_row["watch_id"], latest_candle_time):
        return False, ""

    latest_candle = latest_m5[-1]
    candle_high = latest_candle["high"]
    candle_low = latest_candle["low"]
    candle_close = latest_candle["close"]
    zone_text = watch_row["zone_or_level"]
    direction = watch_row["direction"]

    invalidation_text = watch_row["invalidation_condition"] or ""
    invalidation_candle = latest_candle
    if "m15" in invalidation_text.lower():
        m15 = broker.get_candles(symbol, "M15", 1)
        if m15:
            invalidation_candle = m15[-1]
    if _invalidation_reached(invalidation_text, invalidation_candle, direction):
        return True, "INVALIDATION_REACHED"

    # تلاش برای استخراج عدد از zone_or_level (سطح تکی یا محدوده "1.1700-1.1750")
    levels = _extract_levels(zone_text)
    if not levels:
        return False, ""

    # --- ۱) زون/محدوده (دو سطح) ---
    is_zone = "زون" in trigger_type or "محدوده" in trigger_type or "range" in trigger_type_lower or len(levels) == 2
    if is_zone and len(levels) >= 2:
        low, high = min(levels[:2]), max(levels[:2])
        # بازه High/Low کل کندل چک می‌شود، نه فقط قیمت لحظه‌ای - وگرنه اگر
        # قیمت وسط کندل به زون برخورد کند و قبل از پایان کندل برگردد، آن
        # برخورد کاملاً از دست می‌رود.
        if not (candle_high < low or candle_low > high):  # همپوشانی بازه‌ها
            return True, f"PRICE_ENTERED_ZONE({low}-{high})"
        return False, ""

    level = levels[0]

    # --- ۲) صراحتاً «بسته‌شدن کندل» (باید دقیقاً close چک شود، نه لمس) ---
    is_candle_close = (
        "کندل" in trigger_type or "candle" in trigger_type_lower or
        "بسته" in trigger_type or "close" in trigger_type_lower
    )
    if is_candle_close:
        tf = "M15" if "m15" in trigger_type_lower else "M5"
        if tf == "M5":
            trigger_candle = latest_candle
        else:
            candles = broker.get_candles(symbol, tf, 1)
            if not candles:
                return False, ""
            trigger_candle = candles[-1]
        close_price = trigger_candle["close"]
        open_price = trigger_candle["open"]
        if direction == Direction.BUY.value and close_price >= level and close_price > open_price:
            return True, f"CANDLE_{tf}_CLOSED_ABOVE({level})"
        if direction == Direction.SELL.value and close_price <= level and close_price < open_price:
            return True, f"CANDLE_{tf}_CLOSED_BELOW({level})"
        return False, ""

    # --- ۳) fallback ایمن برای هر عبارت دیگری (مثلاً انگلیسی بدون کلیدواژه
    # شناخته‌شده مثل "M5 Close > 1.1560") ---
    # نکته حیاتی (رفع باگ گزارش‌شده): این تابع قبلاً اگر متن Trigger Type
    # هیچ‌کدام از کلیدواژه‌های بالا را نداشت، بی‌صدا False برمی‌گرداند و
    # آن Watch برای همیشه هرگز trigger نمی‌شد - حتی اگر قیمت کاملاً از
    # سطح رد شده بود. حالا به‌جای شکست خاموش، رفتار پیش‌فرض «لمس سطح»
    # (بر اساس High/Low کندل) اعمال می‌شود که امن‌ترین حالت ممکن است.
    if direction == Direction.BUY.value and candle_high >= level:
        return True, f"PRICE_REACHED_LEVEL_FALLBACK({level})"
    if direction == Direction.SELL.value and candle_low <= level:
        return True, f"PRICE_REACHED_LEVEL_FALLBACK({level})"
    return False, ""


def _extract_levels(text: str) -> list[float]:
    import re
    nums = re.findall(r"\d+\.\d+|\d+", text)
    try:
        return [float(n) for n in nums]
    except ValueError:
        return []


def _invalidation_reached(condition: str, candle: dict, direction: str) -> bool:
    """شرط عددی ابطال را روی آخرین کندل بسته ارزیابی می‌کند."""
    levels = _extract_levels(condition or "")
    if not levels:
        return False
    level = levels[-1]
    text = (condition or "").lower()
    close, high, low = candle["close"], candle["high"], candle["low"]
    use_close = any(k in text for k in ("close", "candle", "کندل", "بسته"))
    above = any(k in text for k in ("above", "بالا", "بالاتر"))
    below = any(k in text for k in ("below", "زیر", "پایین", "پایین‌تر"))
    if above:
        return (close if use_close else high) >= level
    if below:
        return (close if use_close else low) <= level
    return low <= level if direction == Direction.BUY.value else high >= level


def _parse_time_trigger(text: str, created_at: datetime) -> datetime:
    """زمان ISO یا HH:MM را به UTC aware تبدیل می‌کند."""
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        match = __import__("re").search(r"(?:[01]?\d|2[0-3]):[0-5]\d", text)
        if not match:
            raise ValueError(f"زمان Trigger قابل پارس نیست: {text}")
        hh, mm = map(int, match.group(0).split(":"))
        candidate = created_at.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return candidate if candidate >= created_at else candidate + timedelta(days=1)


def lock_watch(watch_id: str) -> None:
    db.update_watch_flags(watch_id, is_locked=True)
    db.log_event("WATCH_LOCKED", "قفل شد برای شروع بررسی مجدد", watch_id=watch_id)


def unlock_watch(watch_id: str) -> None:
    db.update_watch_flags(watch_id, is_locked=False)


def close_watch(watch_id: str, status: str, reason: str) -> bool:
    """پایان قطعی Watch؛ فقط سه وضعیت دامنه‌ای مجاز هستند."""
    changed = db.close_watch_lifecycle(watch_id, status, reason)
    if changed:
        db.log_event(f"WATCH_{status}", reason, watch_id=watch_id)
    return changed


def claim_trigger(watch_id: str, reason: str) -> bool:
    return db.claim_watch_trigger(watch_id, reason)


def is_same_triggered_setup(parent_watch: WatchState, new_details: WatchDetails) -> bool:
    """تشخیص بازتولید فوری همان Watch پس از reanalysis همان Trigger."""
    return (
        parent_watch.direction == new_details.preferred_direction
        and _canonical_trigger(parent_watch.trigger_type) == _canonical_trigger(new_details.trigger_type)
        and _extract_levels(parent_watch.zone_or_level) == _extract_levels(new_details.exact_zone_or_level)
    )


def _canonical_trigger(text: str) -> tuple[str, str]:
    """عبارت‌های هم‌معنی Trigger را برای تشخیص چرخه یکسان می‌کند."""
    value = " ".join((text or "").casefold().split())
    timeframe = "M15" if "m15" in value else "M5" if "m5" in value else ""
    if any(k in value for k in ("کندل", "candle", "close", "بسته")):
        kind = "CANDLE_CLOSE"
    elif any(k in value for k in ("زون", "محدوده", "zone", "range")):
        kind = "ZONE"
    elif any(k in value for k in ("زمان", "time")):
        kind = "TIME"
    else:
        kind = "LEVEL"
    return kind, timeframe
