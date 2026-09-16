"""
core/consistency_checker.py
------------------------------
مکمل core/validator.py: آن ماژول فقط خروجی TRADE را از نظر منطقی چک
می‌کند (بند ۱۷ سند رفتاری). این ماژول برای WATCH یک لایه دوم و
برنامه‌نویسی‌شده (نه فقط دستور به AI) اضافه می‌کند تا تناقض‌هایی که
AI ممکن است علی‌رغم دستور صریح بند ۱۵ رعایت نکند، شناسایی و لاگ شوند.

طراحی عمدی: این چک‌ها فقط **هشدار و لاگ** تولید می‌کنند، نتیجه را رد یا
تغییر نمی‌دهند - چون تشخیص این موارد از روی متن آزاد (Reason) قطعی
نیست و ریسک false-positive دارد. هدف قابلیت ردیابی (auditability) است:
اگر یک WATCH مشکوک به تناقض بود، در دیتابیس قابل پیدا کردن باشد.
"""

from __future__ import annotations

import re

from core.models import AnalysisResult, AnalysisStatus, Grade, MarketSnapshot

# کلیدواژه‌های فارسی/انگلیسی که نشان‌دهنده ارزیابی مثبت یا منفی کیفیت ستاپ هستند
POSITIVE_QUALITY_WORDS = ("کامل", "تمیز", "قوی", "معتبر", "دقیق", "clean", "strong", "valid")
NEGATIVE_QUALITY_WORDS = ("ضعیف", "مبهم", "نامعتبر", "بی‌کیفیت", "باطل", "weak", "invalid", "unclear")

RESISTANCE_WORDS = ("مقاومت", "resistance")
SUPPORT_WORDS = ("حمایت", "support")


def _extract_first_number(text: str) -> float | None:
    match = re.search(r"\d+\.\d+|\d+", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def check_watch_consistency(result: AnalysisResult, snapshot: MarketSnapshot) -> list[str]:
    """
    بند ۱۵ فایل قوانین را به‌صورت برنامه‌نویسی‌شده (نه فقط دستور به AI)
    بازبینی می‌کند. فقط برای Status=WATCH فراخوانی می‌شود.
    خروجی: لیستی از هشدارهای متنی (خالی یعنی چیزی پیدا نشد).
    """
    warnings: list[str] = []
    if result.status != AnalysisStatus.WATCH or result.watch_details is None:
        return warnings

    reason_lower = result.reason.lower()
    wd = result.watch_details

    # ۱) تناقض Grade با کلیدواژه‌های کیفیت در Reason
    if result.grade in (Grade.A_PLUS, Grade.A):
        # این گریدها اصلاً نباید در WATCH ظاهر شوند - خودش یک تناقض جدی‌تر است
        warnings.append(
            f"گرید {result.grade.value} برای WATCH نامعتبر است - طبق منشور فقط A- و B+ مجازند."
        )
    has_negative = any(w in reason_lower for w in NEGATIVE_QUALITY_WORDS)
    has_positive = any(w in reason_lower for w in POSITIVE_QUALITY_WORDS)
    if result.grade == Grade.A_MINUS and has_negative and not has_positive:
        warnings.append(
            f"Grade=A- ولی Reason فقط کلمات منفی دارد ('{result.reason}') - احتمال تناقض کیفیت ستاپ."
        )
    if result.grade == Grade.B_PLUS and has_positive and not has_negative:
        # B+ یعنی نزدیک ولی ناقص - اگر Reason کاملاً مثبت و بدون هیچ نقصی باشد، مشکوک است
        pass  # این حالت شایع و طبیعی است (نزدیک به کامل)، هشدار لازم نیست

    # ۲) تناقض نقش سطح (Resistance/Support) نسبت به قیمت فعلی
    level = _extract_first_number(wd.exact_zone_or_level)
    if level is not None and snapshot.bid and snapshot.ask:
        mid_price = (snapshot.bid + snapshot.ask) / 2
        mentions_resistance = any(w in reason_lower for w in RESISTANCE_WORDS)
        mentions_support = any(w in reason_lower for w in SUPPORT_WORDS)
        if mentions_resistance and level < mid_price:
            warnings.append(
                f"سطح {level} پایین‌تر از قیمت فعلی ({mid_price:.5f}) است ولی در Reason "
                f"«مقاومت/Resistance» نامیده شده - نقش سطح باید حمایت (Support) باشد."
            )
        if mentions_support and level > mid_price:
            warnings.append(
                f"سطح {level} بالاتر از قیمت فعلی ({mid_price:.5f}) است ولی در Reason "
                f"«حمایت/Support» نامیده شده - نقش سطح باید مقاومت (Resistance) باشد."
            )

    # ۳) تناقض ساده جهت: هم BUY و هم SELL هر دو در Reason به‌عنوان جهت نهایی ادعا شده باشند
    mentions_buy = "buy" in reason_lower or "خرید" in reason_lower
    mentions_sell = "sell" in reason_lower or "فروش" in reason_lower
    if mentions_buy and mentions_sell and wd.preferred_direction:
        # هر دو کلمه آمدن لزوماً تناقض نیست (ممکن است یکی برای توصیف Pullback باشد)
        # ولی اگر Preferred Direction با آخرین جهت ذکرشده در متن هم‌خوان نباشد، لاگ شود
        pass  # تشخیص دقیق این مورد بدون NLP واقعی قابل اعتماد نیست - عمداً محافظه‌کارانه رد شد

    return warnings
