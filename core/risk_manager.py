"""
core/risk_manager.py
----------------------
بند ۱۸: محاسبه حجم پیشنهادی بر اساس Balance، درصد ریسک، فاصله Entry تا
Stop Loss و مشخصات حجم نماد. هوش مصنوعی مسئول محاسبه حجم دقیق نیست
(چون به مشخصات دقیق حساب/بروکر دسترسی امن ندارد)؛ این محاسبه همیشه توسط
کد برنامه انجام می‌شود - این یک تصمیم معماری عمدی برای دقت و امنیت است.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.models import MarketSnapshot, TradeDetails


@dataclass
class VolumeCalculationResult:
    suggested_volume: float | None
    risk_amount: float
    warning: str | None = None


def calculate_position_size(
    trade: TradeDetails,
    snapshot: MarketSnapshot,
) -> VolumeCalculationResult:
    """
    فرمول استاندارد:
        risk_amount = balance * risk_percent / 100
        pip_distance = |entry - stop_loss| / pip_size
        volume = risk_amount / (pip_distance * pip_value_per_lot)

    سپس مقدار به نزدیک‌ترین lot_step گرد می‌شود و به min_lot بروکر محدود می‌شود.
    اگر min_lot بروکر باعث عبور از سقف ریسک شود، طبق بند ۱۸ به‌جای پیشنهاد
    اشتباه، هشدار داده می‌شود و suggested_volume برابر None برمی‌گردد.
    """
    if snapshot.account_balance is None:
        return VolumeCalculationResult(
            suggested_volume=None,
            risk_amount=0.0,
            warning="اطلاعات حساب (Balance) در دسترس نیست؛ حجم قابل محاسبه نیست.",
        )

    if not all([
        snapshot.symbol_contract_size,
        snapshot.symbol_min_lot,
        snapshot.symbol_lot_step,
        snapshot.symbol_pip_value,
    ]):
        return VolumeCalculationResult(
            suggested_volume=None,
            risk_amount=0.0,
            warning="مشخصات نماد (contract size / min lot / lot step / pip value) کامل نیست.",
        )

    risk_amount = snapshot.account_balance * trade.risk_percent / 100.0
    sl_distance = abs(trade.entry - trade.stop_loss)
    if sl_distance <= 0:
        return VolumeCalculationResult(
            suggested_volume=None,
            risk_amount=risk_amount,
            warning="فاصله Entry تا Stop Loss صفر یا نامعتبر است.",
        )

    # فرض: symbol_pip_value = ارزش هر پیپ به ازای ۱ لات استاندارد
    pip_size = _infer_pip_size(snapshot.symbol)
    pip_distance = sl_distance / pip_size
    if pip_distance <= 0:
        return VolumeCalculationResult(
            suggested_volume=None, risk_amount=risk_amount,
            warning="محاسبه فاصله پیپ ناموفق بود.",
        )

    raw_volume = risk_amount / (pip_distance * snapshot.symbol_pip_value)

    # گرد کردن به نزدیک‌ترین lot_step
    step = snapshot.symbol_lot_step
    rounded_volume = round(raw_volume / step) * step
    rounded_volume = round(rounded_volume, 2)

    if rounded_volume < snapshot.symbol_min_lot:
        # بند ۱۸: اگر حداقل حجم بروکر باعث عبور از سقف ریسک شود،
        # حجم اشتباه پیشنهاد نشود و هشدار ارسال شود.
        actual_risk_at_min_lot = (
            snapshot.symbol_min_lot * pip_distance * snapshot.symbol_pip_value
            / snapshot.account_balance * 100
        )
        return VolumeCalculationResult(
            suggested_volume=None,
            risk_amount=risk_amount,
            warning=(
                f"حداقل حجم بروکر ({snapshot.symbol_min_lot}) باعث می‌شود ریسک واقعی "
                f"به {actual_risk_at_min_lot:.2f}% برسد که بیش از سقف مجاز "
                f"({trade.risk_percent}%) است. حجم پیشنهاد نمی‌شود."
            ),
        )

    return VolumeCalculationResult(suggested_volume=rounded_volume, risk_amount=risk_amount)


def _infer_pip_size(symbol: str) -> float:
    """
    استنتاج ساده اندازه پیپ بر اساس نوع نماد. برای دقت کامل در پروژه نهایی
    بهتر است این مقدار مستقیماً از broker (symbol_info.point) خوانده شود
    نه اینکه اینجا حدس زده شود - این تابع صرفاً fallback است.
    """
    s = symbol.upper()
    if "JPY" in s:
        return 0.01
    if s in ("XAUUSD", "GOLD"):
        return 0.1
    if s in ("BTCUSD", "BTCUSDT", "XBTUSD"):
        # قیمت بیت‌کوین در واحد دلار کامل نوسان می‌کند، نه فراکسیون‌های
        # ریز فارکس. این مقدار fallback تقریبی است و بین بروکرها فرق
        # می‌کند - برای دقت کامل باید از symbol_info.point خودِ بروکر
        # خوانده شود.
        return 1.0
    return 0.0001
