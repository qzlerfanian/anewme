"""
telegram_bot/notifier.py
---------------------------
بند ۶: پیام هر سه نتیجه (TRADE, WATCH, NO_TRADE) باید به تلگرام ارسال شود.
این ماژول فقط مسئول قالب‌بندی خوانا و یکنواخت پیام‌هاست - هیچ منطق
تصمیم‌گیری ندارد.
"""

from __future__ import annotations

from core.models import AnalysisResult, AnalysisStatus, Grade

STATUS_EMOJI = {
    AnalysisStatus.TRADE: "✅",
    AnalysisStatus.WATCH: "👀",
    AnalysisStatus.NO_TRADE: "🚫",
}


ACCOUNT_STATE_EMOJI = {
    "OPEN_POSITION": "🔒",
    "PENDING_ORDER": "🕓",
    "OPEN_POSITION_AND_PENDING_ORDER": "🔒",
    "ACCOUNT_STATE_UNKNOWN": "⚠️",
}
ACCOUNT_STATE_LABEL = {
    "OPEN_POSITION": "پوزیشن باز موجود (فقط مانیتور)",
    "PENDING_ORDER": "سفارش Pending موجود (فقط مانیتور)",
    "OPEN_POSITION_AND_PENDING_ORDER": "پوزیشن و سفارش Pending موجود (فقط نمایش وضعیت)",
    "ACCOUNT_STATE_UNKNOWN": "وضعیت حساب MT5 قابل بررسی نیست",
}


def format_analysis_message(result: AnalysisResult) -> str:
    # اگر پوزیشن/سفارش باز واقعی روی این نماد در MT5 پیدا شده، Status
    # اصلی (TRADE/WATCH) دیگر معنی «سیگنال ورود جدید» ندارد - این حالت
    # جداگانه و واضح نمایش داده می‌شود تا کاربر اشتباه نکند.
    if result.account_state:
        emoji = ACCOUNT_STATE_EMOJI.get(result.account_state, "ℹ️")
        label = ACCOUNT_STATE_LABEL.get(result.account_state, result.account_state)
        header = f"{emoji} {result.symbol} | {label}"
        lines = [header, ""]
        lines.append(f"🕒 Analysis Time: {result.analysis_time.strftime('%Y-%m-%d %H:%M UTC')}")
        if result.last_closed_m5_time:
            lines.append(f"🕔 Last Closed M5: {result.last_closed_m5_time}")
        if result.grade:
            lines.append(f"وضعیت تحلیلی فعلی بازار (فقط اطلاعاتی): Grade {result.grade.value}")
        lines.append(f"📝 {result.reason}")
        if result.account_state_details:
            lines += ["", "— جزئیات حساب MT5 برای همین نماد —"]
            for index, item in enumerate(result.account_state_details, 1):
                if item.get("record_type") == "POSITION":
                    kind = "پوزیشن باز"
                elif item.get("record_type") == "PENDING_ORDER":
                    kind = "سفارش Pending"
                else:
                    kind = "خطای بررسی حساب"
                details = [f"{index}) {kind}"]
                if item.get("ticket") is not None:
                    details.append(f"Ticket: {item['ticket']}")
                if item.get("type") is not None:
                    details.append(f"Type: {item['type']}")
                if item.get("volume") is not None:
                    details.append(f"Volume: {item['volume']}")
                if item.get("price_open") is not None:
                    details.append(f"Open Price: {item['price_open']}")
                if item.get("profit") is not None:
                    details.append(f"Profit: {item['profit']}")
                if item.get("error"):
                    details.append(f"Error: {item['error']}")
                lines.append(" | ".join(details))
        lines.append("")
        if result.account_state == "ACCOUNT_STATE_UNKNOWN":
            lines.append("⚠️ تا زمان بازیابی ارتباط و تأیید وضعیت حساب MT5، سیگنال جدید صادر نمی‌شود.")
        else:
            lines.append(
                "⚠️ چون روی این نماد از قبل معامله/سفارش فعال وجود دارد، ربات "
                "سیگنال ورود جدیدی صادر نمی‌کند - فقط وضعیت حساب را نمایش می‌دهد."
            )
        return "\n".join(lines)

    emoji = STATUS_EMOJI.get(result.status, "ℹ️")
    header = f"{emoji} {result.symbol} | {result.status.value}"
    if result.grade:
        header += f" | {result.grade.value}"

    lines = [header, ""]
    lines.append(f"🕒 Analysis Time: {result.analysis_time.strftime('%Y-%m-%d %H:%M UTC')}")
    if result.last_closed_m5_time:
        lines.append(f"🕔 Last Closed M5: {result.last_closed_m5_time}")
    if result.direction:
        lines.append(f"↕️ Direction: {result.direction.value}")
    lines.append(f"📝 Reason: {result.reason}")
    if result.timeframes_checked:
        lines.append(f"📊 Timeframes: {', '.join(result.timeframes_checked)}")

    if result.status == AnalysisStatus.TRADE and result.trade_details:
        td = result.trade_details
        lines += [
            "",
            "— جزئیات معامله (ثبت دستی توسط شما) —",
            f"Order Type: {td.order_type.value}",
            f"Entry: {td.entry}",
            f"Stop Loss: {td.stop_loss}",
            f"Take Profit: {td.take_profit}",
            f"Risk: {td.risk_percent}%",
            f"Suggested Volume: {td.suggested_volume if td.suggested_volume is not None else 'محاسبه نشد - به دلیل زیر توجه کنید'}",
            f"Reward/Risk: {td.reward_risk_ratio}",
            f"Expiration: {td.expiration}",
            f"Invalidation: {td.invalidation}",
        ]

    elif result.status == AnalysisStatus.WATCH and result.watch_details:
        wd = result.watch_details
        lines += [
            "",
            "— جزئیات Watch —",
            f"Preferred Direction: {wd.preferred_direction.value}",
            f"Trigger Type: {wd.trigger_type}",
            f"Zone/Level: {wd.exact_zone_or_level}",
            f"Recheck Timeframes: {', '.join(wd.timeframes_to_recheck)}",
            f"Expiration: {wd.expiration}",
            f"Invalidation: {wd.invalidation}",
            "وضعیت فعلی: در انتظار تریگر",
        ]

    elif result.status == AnalysisStatus.NO_TRADE:
        lines += ["", "این تحلیل بدون ستاپ معتبر بسته شد."]

    lines.append("")
    lines.append("⚠️ یادآوری: ثبت/مدیریت معامله همیشه دستی است. این ربات هیچ سفارشی ثبت نمی‌کند.")
    return "\n".join(lines)


def format_error_message(context: str, error: str, symbol: str | None = None) -> str:
    prefix = f" ({symbol})" if symbol else ""
    return f"❌ خطا در {context}{prefix}:\n{error}"
