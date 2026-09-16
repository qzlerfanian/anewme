"""
core/validator.py
-------------------
بند ۱۷: "پیش از ارسال TRADE، نتیجه باید از نظر منطقی کنترل شود."
این ماژول آخرین خط دفاعی قبل از رسیدن پیام TRADE به کاربر است.
اگر نتیجه ناقص/ناسازگار باشد، به‌جای ارسال TRADE اشتباه، تبدیل به
NO_TRADE می‌شود و دلیل دقیق ثبت می‌شود (نه سکوت، نه Silent-fail).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from core.clock import utc_now
from datetime import datetime, timezone
import math
from decimal import Decimal, ROUND_HALF_UP

from config import config, GRADES_ALLOWING_TRADE
from core.models import AnalysisResult, AnalysisStatus, Direction, Grade, MarketSnapshot
from broker.candle_utils import latest_closed, TIMEFRAME_MINUTES

logger = logging.getLogger(__name__)


@dataclass
class ValidationOutcome:
    is_valid: bool
    downgraded_status: AnalysisStatus | None = None
    reasons: list[str] = None

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []


def _risk_cap_for_grade(grade: Grade, is_friday: bool) -> float:
    r = config.risk
    if is_friday:
        return r.risk_percent_friday
    mapping = {
        Grade.A_PLUS: r.risk_percent_A_plus,
        Grade.A: r.risk_percent_A,
        Grade.B_PLUS: r.risk_percent_B_plus,
    }
    return mapping.get(grade, 0.0)


def _normalize_trade_prices(result: AnalysisResult, snapshot: MarketSnapshot) -> None:
    td = result.trade_details
    tick = snapshot.symbol_tick_size
    if tick is not None and math.isfinite(tick) and tick > 0:
        def normalize(value):
            return float((Decimal(str(value)) / Decimal(str(tick))).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * Decimal(str(tick)))
    elif snapshot.symbol_digits is not None and snapshot.symbol_digits >= 0:
        normalize = lambda value: round(value, int(snapshot.symbol_digits))
    else:
        normalize = lambda value: value
    td.entry = normalize(td.entry)
    td.stop_loss = normalize(td.stop_loss)
    td.take_profit = normalize(td.take_profit)


def validate_trade_result(
    result: AnalysisResult,
    snapshot: MarketSnapshot,
) -> ValidationOutcome:
    """
    اجرای چک‌لیست کامل بند ۱۷. اگر هر شرط شکسته شود، نتیجه نامعتبر اعلام
    می‌شود و باید در لایه بالاتر به NO_TRADE (یا WATCH، بسته به مورد)
    تبدیل شود - هرگز مستقیم به کاربر به‌عنوان TRADE ارسال نشود.
    """
    reasons: list[str] = []

    if result.status != AnalysisStatus.TRADE:
        return ValidationOutcome(is_valid=True)  # این ولیدیتور فقط برای TRADE است

    now = utc_now()
    for tf, bars in (('M5', snapshot.candles_m5), ('M15', snapshot.candles_m15), ('H1', snapshot.candles_h1)):
        candle = latest_closed(bars, tf, now)
        if candle is None or (now - candle['close_time']).total_seconds() > TIMEFRAME_MINUTES[tf]*60 + 60:
            reasons.append(f'داده بسته‌شده {tf} موجود یا تازه نیست.')
    if not {'M5', 'M15', 'H1'}.issubset(result.timeframes_checked):
        reasons.append('هر سه تایم‌فریم در نتیجه بررسی نشده‌اند.')
    if not all(math.isfinite(v) and v > 0 for v in (snapshot.bid, snapshot.ask)) or snapshot.ask < snapshot.bid:
        reasons.append('قیمت Bid/Ask معتبر نیست.')

    td = result.trade_details
    if td is None:
        return ValidationOutcome(
            is_valid=False,
            downgraded_status=AnalysisStatus.NO_TRADE,
            reasons=["فیلدهای Trade Details موجود نیست."],
        )

    numeric_values = (td.entry, td.stop_loss, td.take_profit, td.risk_percent, td.reward_risk_ratio)
    if not all(math.isfinite(v) for v in numeric_values):
        reasons.append("یکی از مقادیر عددی معامله NaN یا Infinity است.")
    if any(v <= 0 for v in numeric_values):
        reasons.append('مقادیر معامله باید مثبت باشند.')
    if not reasons:
        _normalize_trade_prices(result, snapshot)

    # ۱) نوع سفارش Pending باشد (در parser.py تضمین شده اما دوباره چک می‌شود)
    from core.models import OrderType
    if td.order_type not in OrderType:
        reasons.append("نوع سفارش Pending معتبر نیست.")

    # ۲) Entry/SL/TP کامل و سازگار با جهت سفارش
    if result.direction == Direction.BUY:
        if td.order_type not in (OrderType.BUY_LIMIT, OrderType.BUY_STOP):
            reasons.append("نوع سفارش با Direction=BUY سازگار نیست.")
        if not (td.stop_loss < td.entry < td.take_profit):
            reasons.append("رابطه Entry/SL/TP با جهت BUY سازگار نیست.")
        if td.order_type == OrderType.BUY_LIMIT and not (td.entry < snapshot.ask):
            reasons.append("BUY_LIMIT باید پایین‌تر از قیمت فعلی باشد.")
        if td.order_type == OrderType.BUY_STOP and not (td.entry > snapshot.ask):
            reasons.append("BUY_STOP باید بالاتر از قیمت فعلی باشد.")
    elif result.direction == Direction.SELL:
        if td.order_type not in (OrderType.SELL_LIMIT, OrderType.SELL_STOP):
            reasons.append("نوع سفارش با Direction=SELL سازگار نیست.")
        if not (td.take_profit < td.entry < td.stop_loss):
            reasons.append("رابطه Entry/SL/TP با جهت SELL سازگار نیست.")
        if td.order_type == OrderType.SELL_LIMIT and not (td.entry > snapshot.bid):
            reasons.append("SELL_LIMIT باید بالاتر از قیمت فعلی باشد.")
        if td.order_type == OrderType.SELL_STOP and not (td.entry < snapshot.bid):
            reasons.append("SELL_STOP باید پایین‌تر از قیمت فعلی باشد.")
    else:
        reasons.append("Direction نامشخص است.")

    # ۳) RR مرجع همیشه از قیمت‌های Entry/SL/TP خود ربات محاسبه می‌شود.
    # مقدار API فقط یک کنترل ثانویه است؛ اختلاف کوچکِ گردکردن نباید ستاپ را رد کند.
    risk_distance = abs(td.entry - td.stop_loss)
    reward_distance = abs(td.take_profit - td.entry)
    calculated_rr = reward_distance / risk_distance if math.isfinite(risk_distance) and risk_distance > 0 else 0.0
    if calculated_rr <= 0 or not math.isfinite(calculated_rr):
        reasons.append("RR قابل محاسبه نیست: فاصله Entry و Stop Loss صفر یا نامعتبر است.")
    else:
        api_rr = td.reward_risk_ratio
        if api_rr is not None and math.isfinite(api_rr) and api_rr > 0:
            absolute_difference = abs(api_rr - calculated_rr)
            percentage_difference = absolute_difference / calculated_rr * 100.0
            minor = absolute_difference <= 0.10 or percentage_difference <= 5.0
            logger.info("RR_VALIDATION api=%.8f calculated=%.8f final=%.8f absolute=%.8f percentage=%.3f minor=%s",
                        api_rr, calculated_rr, calculated_rr, absolute_difference, percentage_difference, minor)
            if not minor:
                reasons.append(
                    f"RR mismatch جدی: API={api_rr:.2f}; Calculated={calculated_rr:.2f}; "
                    f"اختلاف مطلق={absolute_difference:.2f}R؛ اختلاف درصدی={percentage_difference:.2f}٪."
                )
        elif api_rr is not None:
            logger.warning("RR_VALIDATION API value invalid; calculated RR is authoritative: calculated=%.8f", calculated_rr)
        # Downstream sizing/tracking/formatting must use the robot's value.
        td.reward_risk_ratio = calculated_rr
        minimum_rr = config.risk.minimum_reward_risk_ratio
        if calculated_rr < minimum_rr:
            reasons.append(f"MINIMUM_RR_BELOW_MINIMUM: Calculated RR={calculated_rr:.2f}; Minimum Required RR={minimum_rr:.2f}.")

    # ۴) درصد ریسک از سقف مجاز بیشتر نباشد
    is_friday = snapshot.market_time_utc.weekday() == 4  # Friday = 4
    cap = _risk_cap_for_grade(result.grade, is_friday) if result.grade else 0.0
    grade_label = result.grade.value if result.grade else "نامشخص"
    if td.risk_percent <= 0:
        reasons.append("درصد ریسک باید بزرگ‌تر از صفر باشد.")
    elif td.risk_percent > cap or td.risk_percent > config.risk.max_risk_percent_hard_cap:
        reasons.append(
            f"درصد ریسک ({td.risk_percent}%) بیش از سقف مجاز ({cap}%) برای گرید {grade_label} است."
        )

    # ۵) Grade اجازه TRADE داشته باشد
    if result.grade not in GRADES_ALLOWING_TRADE:
        reasons.append(f"گرید {grade_label} اجازه TRADE ندارد (فقط WATCH مجاز است).")

    # ۶) چک‌لیست کامل بودن (منشور V3: بدون همراستایی/تأیید هر سه تایم‌فریم
    #    M5/M15/H1، ستاپ کامل محسوب نمی‌شود - برای هر TRADE الزامی است)
    if not td.checklist_complete:
        reasons.append("چک‌لیست همراستایی سه تایم‌فریم (M5/M15/H1) کامل تأیید نشده است.")

    # ۷) تصاویر/داده‌ها جدید و کامل باشند
    if not snapshot.market_open:
        reasons.append("بازار در حال حاضر بسته است.")
    max_data_age = 5 * 60  # ۵ دقیقه - می‌تواند تنظیم‌پذیر شود
    age = (utc_now() - snapshot.market_time_utc.replace(tzinfo=timezone.utc)
           if snapshot.market_time_utc.tzinfo is None
           else utc_now() - snapshot.market_time_utc).total_seconds()
    if age > max_data_age:
        reasons.append("داده‌های بازار قدیمی هستند (بیش از ۵ دقیقه).")
    if age < -60:
        reasons.append("زمان داده بازار بیش از یک دقیقه در آینده است؛ ساعت سیستم/بروکر ناسازگار است.")

    # ۸) سفارش منقضی نشده باشد
    # (بررسی دقیق‌تر expiration در watch_manager انجام می‌شود چون فرمت زمانی آزاد است)
    if not td.expiration:
        reasons.append("زمان انقضا مشخص نشده است.")
    else:
        try:
            expiration = datetime.fromisoformat(td.expiration.replace("Z", "+00:00"))
            if expiration.tzinfo is None:
                expiration = expiration.replace(tzinfo=timezone.utc)
            if expiration <= utc_now():
                reasons.append("زمان انقضای سفارش گذشته است.")
        except ValueError:
            reasons.append("فرمت زمان انقضای سفارش معتبر نیست؛ ISO-8601 همراه timezone لازم است.")

    if reasons:
        return ValidationOutcome(
            is_valid=False,
            downgraded_status=AnalysisStatus.NO_TRADE,
            reasons=reasons,
        )
    return ValidationOutcome(is_valid=True)
