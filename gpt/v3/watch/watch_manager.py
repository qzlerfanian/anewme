"""
watch/watch_manager.py
------------------------
مسئول کل چرخه حیات یک Watch:
  - ثبت Watch جدید بعد از خروجی WATCH (بند ۱۱)
  - بررسی Triggerهای فعال (بند ۱۳) بدون فراخوانی مداوم AI (بند ۱۲)
  - قفل کردن Watch حین بررسی مجدد و جلوگیری از Trigger تکراری (بند ۱۹)
  - بررسی نهایی همان سناریو پس از Trigger، بدون ساخت Watch جایگزین
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
from broker.candle_utils import latest_closed, normalize_candle, utc
from core.models import WatchDetails, WatchState, Direction, Grade
from storage import db

logger = logging.getLogger(__name__)


def create_watch_from_details(
    symbol: str,
    watch_details: WatchDetails,
    parent_analysis_id: str | None,
    baseline_candle: dict | None = None,
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
        # کندلی که تحلیل اولیه بر اساس آن انجام شد دوباره توسط monitor
        # پردازش نمی‌شود؛ فقط کندل‌های بسته‌شده بعد از ایجاد Watch مجازند.
        "last_checked_candle_time": (
            normalize_candle(baseline_candle, "M5")["close_time"].isoformat()
            if baseline_candle else None
        ),
    })
    db.log_event("WATCH_CREATED", f"Watch جدید ثبت شد: {watch.zone_or_level}", symbol=symbol, watch_id=watch_id)
    return watch


def validate_before_creation(watch_details: WatchDetails, latest_candle: dict | None) -> str | None:
    """Return an exact rejection reason if invalidation already happened."""
    if latest_candle is None:
        return "واچ ساخته نشد: آخرین کندل کاملاً بسته‌شده برای اعتبارسنجی سناریو در دسترس نیست."
    candle = normalize_candle(latest_candle, "M5")
    levels = _extract_levels(watch_details.invalidation or "")
    if not levels:
        return "واچ ساخته نشد: سطح عددی ابطال قابل استخراج نیست."
    level = levels[-1]
    close = float(candle["close"])
    invalid = (
        close <= level if watch_details.preferred_direction == Direction.BUY
        else close >= level
    )
    if not invalid:
        return None
    operator = "پایین‌تر یا مساوی" if watch_details.preferred_direction == Direction.BUY else "بالاتر یا مساوی"
    return (
        f"واچ ساخته نشد: سناریو پیش از ثبت باطل شده است؛ Close کندل M5 بسته‌شده در "
        f"{candle['close_time'].isoformat()} برابر {close} و {operator} سطح ابطال {level} است."
    )


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
    # واچ منقضی هیچ‌گاه اجازه ورود به مسیر Trigger ندارد.
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
    latest_m5 = broker.get_candles(symbol, "M5", 2)
    if not latest_m5:
        return (True, "EXPIRATION_REACHED") if now >= expiration else (False, "")
    latest_candle = latest_closed(latest_m5, "M5", now)
    if latest_candle is None:
        return (True, "EXPIRATION_REACHED") if now >= expiration else (False, "")
    latest_candle_time = latest_candle["close_time"].isoformat()

    created_at = utc(datetime.fromisoformat(watch_row["created_at"].replace("Z", "+00:00")))
    # کندل تحلیل اولیه و هر کندل بعد از پایان اعتبار حق تغییر Watch را ندارند.
    eligible = latest_candle["close_time"] > created_at and latest_candle["close_time"] <= expiration

    try:
        bid, ask = broker.get_current_price(symbol)
    except Exception:  # گزارش tick نباید تصمیم مبتنی بر Close را مختل کند.
        bid, ask = None, None
    db.log_event(
        "WATCH_CANDLE_EVIDENCE",
        (
            f"tf=M5; candle_no={latest_candle.get('source_index', 'unknown')}; "
            f"open_time_utc={latest_candle['open_time'].isoformat()}; "
            f"close_time_utc={latest_candle['close_time'].isoformat()}; "
            f"O={latest_candle['open']}; H={latest_candle['high']}; L={latest_candle['low']}; "
            f"C={latest_candle['close']}; bid={bid}; ask={ask}; "
            f"trigger={watch_row['zone_or_level']}; invalidation={watch_row['invalidation_condition']}; "
            f"eligible={eligible}"
        ), symbol=symbol, watch_id=watch_row["watch_id"],
    )
    if not eligible:
        return (True, "EXPIRATION_REACHED") if now >= expiration else (False, "")
    # claim اتمیک: حتی با چند monitor/process فقط یکی این کندل را بررسی می‌کند.
    if not db.claim_watch_candle(watch_row["watch_id"], latest_candle_time):
        return False, ""

    candle_close = latest_candle["close"]
    zone_text = watch_row["zone_or_level"]
    direction = watch_row["direction"]

    # تلاش برای استخراج عدد از zone_or_level (سطح تکی یا محدوده "1.1700-1.1750")
    levels = _extract_levels(zone_text)
    if not levels:
        return False, ""

    # --- ۱) زون/محدوده (دو سطح) ---
    is_zone = "زون" in trigger_type or "محدوده" in trigger_type or "range" in trigger_type_lower or len(levels) == 2
    if is_zone and len(levels) >= 2:
        low, high = min(levels[:2]), max(levels[:2])
        # حتی برای زون نیز فقط Close کندل بسته‌شده معتبر است؛ سایه/لمس کافی نیست.
        if low <= candle_close <= high:
            return True, f"CANDLE_M5_CLOSED_IN_ZONE({low}-{high})"
        if _invalidation_reached(watch_row["invalidation_condition"] or "", latest_candle, direction):
            return True, "INVALIDATION_REACHED"
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
            candles = broker.get_candles(symbol, tf, 2)
            if not candles:
                return False, ""
            trigger_candle = latest_closed(candles, tf, now)
            if trigger_candle is None or trigger_candle["close_time"] <= created_at:
                return False, ""
        close_price = trigger_candle["close"]
        # رنگ کندل اهمیتی ندارد؛ فقط Close کاملاً بسته‌شده نسبت به سطح ملاک است.
        if direction == Direction.BUY.value and close_price > level:
            return True, f"CANDLE_{tf}_CLOSED_ABOVE({level}); close={close_price}; close_time={trigger_candle.get('close_time', trigger_candle['time']).isoformat()}"
        if direction == Direction.SELL.value and close_price < level:
            return True, f"CANDLE_{tf}_CLOSED_BELOW({level}); close={close_price}; close_time={trigger_candle.get('close_time', trigger_candle['time']).isoformat()}"
        # Trigger رخ نداده؛ سپس ابطال همان کندل بسته‌شده بررسی می‌شود.
        invalidation_text = watch_row["invalidation_condition"] or ""
        if _invalidation_reached(invalidation_text, latest_candle, direction):
            return True, "INVALIDATION_REACHED"
        return (True, "EXPIRATION_REACHED") if now >= expiration else (False, "")

    # fallback نیز فقط Close کندل بسته M5 را می‌سنجد؛ عبور لحظه‌ای و Wick ممنوع است.
    if direction == Direction.BUY.value and candle_close > level:
        return True, f"CANDLE_M5_CLOSED_ABOVE_FALLBACK({level})"
    if direction == Direction.SELL.value and candle_close < level:
        return True, f"CANDLE_M5_CLOSED_BELOW_FALLBACK({level})"
    invalidation_text = watch_row["invalidation_condition"] or ""
    if _invalidation_reached(invalidation_text, latest_candle, direction):
        return True, "INVALIDATION_REACHED"
    return (True, "EXPIRATION_REACHED") if now >= expiration else (False, "")


def _extract_levels(text: str) -> list[float]:
    import re
    nums = re.findall(r"\d+\.\d+|\d+", text)
    try:
        return [float(n) for n in nums]
    except ValueError:
        return []


def evaluate_level_candle(direction: str, close: float, trigger_level: float,
                          invalidation_level: float) -> str:
    """Pure deterministic decision for one fully closed candle.

    Trigger is deliberately strict; invalidation is inclusive.  No OHLC field
    other than ``close`` is accepted by this function.
    """
    if direction == Direction.BUY.value:
        if close > trigger_level:
            return "TRIGGERED"
        if close <= invalidation_level:
            return "INVALIDATED"
    else:
        if close < trigger_level:
            return "TRIGGERED"
        if close >= invalidation_level:
            return "INVALIDATED"
    return "ACTIVE"


def _invalidation_reached(condition: str, candle: dict, direction: str) -> bool:
    """شرط عددی ابطال را روی آخرین کندل بسته ارزیابی می‌کند."""
    levels = _extract_levels(condition or "")
    if not levels:
        return False
    level = levels[-1]
    text = (condition or "").lower()
    close = candle["close"]
    above = any(k in text for k in ("above", "بالا", "بالاتر"))
    below = any(k in text for k in ("below", "زیر", "پایین", "پایین‌تر"))
    if above:
        return close >= level
    if below:
        return close <= level
    # شرط ابطال Watch نیز به‌صورت محافظه‌کارانه فقط با Close قطعی می‌شود.
    return close <= level if direction == Direction.BUY.value else close >= level


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


def is_materially_same_scenario(previous: WatchState, new_details: WatchDetails) -> bool:
    """تغییرات جزئی قیمت/نگارش را از سناریوی واقعاً تازه جدا می‌کند."""
    if previous.direction != new_details.preferred_direction:
        return False
    if _canonical_trigger(previous.trigger_type) != _canonical_trigger(new_details.trigger_type):
        return False
    if not _levels_materially_equal(
        _extract_levels(previous.zone_or_level),
        _extract_levels(new_details.exact_zone_or_level),
    ):
        return False
    return _levels_materially_equal(
        _extract_levels(previous.invalidation_condition),
        _extract_levels(new_details.invalidation),
    )


def _levels_materially_equal(old_levels: list[float], new_levels: list[float]) -> bool:
    if not old_levels or len(old_levels) != len(new_levels):
        return False
    for old, new in zip(sorted(old_levels), sorted(new_levels)):
        # حدود یک پیپ برای فارکس و مقیاس متناسب برای JPY/طلا.
        tolerance = max(1e-8, abs(old) * 1e-4)
        if abs(old - new) > tolerance:
            return False
    return True


def _canonical_trigger(text: str) -> tuple[str, str]:
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
