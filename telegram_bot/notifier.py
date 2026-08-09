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


def format_analysis_message(result: AnalysisResult) -> str:
    emoji = STATUS_EMOJI.get(result.status, "ℹ️")
    header = f"{emoji} {result.symbol} | {result.status.value}"
    if result.grade:
        header += f" | {result.grade.value}"

    lines = [header, ""]
    lines.append(f"🕒 Analysis Time: {result.analysis_time.strftime('%Y-%m-%d %H:%M UTC')}")
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
        ]

    elif result.status == AnalysisStatus.NO_TRADE:
        lines += ["", "این تحلیل بدون ستاپ معتبر بسته شد."]

    lines.append("")
    lines.append("⚠️ یادآوری: ثبت/مدیریت معامله همیشه دستی است. این ربات هیچ سفارشی ثبت نمی‌کند.")
    return "\n".join(lines)


def format_error_message(context: str, error: str, symbol: str | None = None) -> str:
    prefix = f" ({symbol})" if symbol else ""
    return f"❌ خطا در {context}{prefix}:\n{error}"
