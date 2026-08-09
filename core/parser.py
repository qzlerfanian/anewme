"""
core/parser.py
---------------
بند ۷: "پاسخ مدل نباید متن آزاد و متغیر باشد... ربات بدون حدس‌زدن آن را
پردازش کند."

این ماژول متن خام خروجی AI را به AnalysisResult تبدیل می‌کند.
هرگونه فیلد گمشده یا مقدار خارج از دامنه مجاز => خطای صریح (نه حدس زدن).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from core.models import (
    AnalysisResult,
    AnalysisStatus,
    Direction,
    Grade,
    OrderType,
    TradeDetails,
    WatchDetails,
)


class AIResponseParseError(ValueError):
    """پاسخ AI با فرمت ثابت مطابقت ندارد."""


def _normalize_ai_text(text: str) -> str:
    """
    مدل‌های GPT با وجود دستور صریح، گاهی نام فیلدها را با **Bold**،
    بولت (- یا •) یا داخل بلاک ```...``` برمی‌گردانند. این تابع این
    تزئینات رایج را حذف می‌کند تا پارسر سخت‌گیر (که عمداً regex دقیق
    دارد - بند ۷) دچار «شکست کاذب» نشود، بدون این‌که خودِ داده تغییر کند.
    """
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        line = re.sub(r"^```[a-zA-Z]*$", "", line)   # خط باز/بسته‌شدن code block
        line = re.sub(r"^[-•*]\s+", "", line)         # بولت ابتدای خط
        line = line.replace("**", "").replace("__", "")  # بولد/ایتالیک مارک‌داون
        lines.append(line)
    return "\n".join(lines)


def _extract_field(text: str, field_name: str, required: bool = True) -> Optional[str]:
    pattern = rf"^{re.escape(field_name)}\s*:\s*(.+)$"
    match = re.search(pattern, text, flags=re.MULTILINE)
    if match:
        value = match.group(1).strip()
        return None if value in ("--", "-", "") else value
    if required:
        raise AIResponseParseError(f"فیلد الزامی '{field_name}' در پاسخ AI یافت نشد.")
    return None


def _parse_bool(value: Optional[str]) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "بله")


def _parse_list(value: Optional[str]) -> list:
    if not value:
        return []
    return [v.strip() for v in re.split(r"[,،]", value) if v.strip()]


def _parse_float(value: Optional[str], field_name: str) -> float:
    if value is None:
        raise AIResponseParseError(f"فیلد عددی '{field_name}' خالی است.")
    cleaned = value.replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError as exc:
        raise AIResponseParseError(f"فیلد '{field_name}' عدد معتبر نیست: {value}") from exc


def parse_ai_response(raw_text: str, expected_symbol: str) -> AnalysisResult:
    """
    ورودی: متن خام پاسخ AI
    خروجی: AnalysisResult ساختاریافته
    استثنا: AIResponseParseError در صورت هر گونه ناسازگاری با فرمت ثابت
    """
    if not raw_text or not raw_text.strip():
        raise AIResponseParseError("پاسخ AI خالی است.")

    original_raw_text = raw_text          # برای ثبت دقیق در سابقه (بند ۲۰)
    raw_text = _normalize_ai_text(raw_text)  # فقط برای پارس ساده‌تر

    status_raw = _extract_field(raw_text, "Status")
    if status_raw not in AnalysisStatus._value2member_map_:
        raise AIResponseParseError(
            f"Status نامعتبر: '{status_raw}'. فقط TRADE/WATCH/NO_TRADE مجاز است."
        )
    status = AnalysisStatus(status_raw)

    symbol = _extract_field(raw_text, "Symbol")
    if symbol.upper() != expected_symbol.upper():
        raise AIResponseParseError(
            f"نماد پاسخ ({symbol}) با نماد درخواستی ({expected_symbol}) مطابقت ندارد."
        )

    analysis_time_raw = _extract_field(raw_text, "Analysis Time")
    try:
        analysis_time = datetime.fromisoformat(analysis_time_raw.replace("Z", "+00:00"))
    except Exception:
        analysis_time = datetime.now(timezone.utc)

    direction_raw = _extract_field(raw_text, "Direction", required=False)
    direction = Direction(direction_raw) if direction_raw in Direction._value2member_map_ else None

    grade_raw = _extract_field(raw_text, "Grade", required=False)
    grade = Grade(grade_raw) if grade_raw in Grade._value2member_map_ else None

    reason = _extract_field(raw_text, "Reason")
    timeframes_checked = _parse_list(_extract_field(raw_text, "Timeframes Checked", required=False))

    trade_details = None
    watch_details = None

    if status == AnalysisStatus.TRADE:
        order_type_raw = _extract_field(raw_text, "Order Type")
        if order_type_raw not in OrderType._value2member_map_:
            raise AIResponseParseError(
                f"Order Type نامعتبر: '{order_type_raw}'. فقط سفارش‌های Pending مجاز است "
                f"({', '.join(o.value for o in OrderType)})."
            )
        trade_details = TradeDetails(
            order_type=OrderType(order_type_raw),
            entry=_parse_float(_extract_field(raw_text, "Entry"), "Entry"),
            stop_loss=_parse_float(_extract_field(raw_text, "Stop Loss"), "Stop Loss"),
            take_profit=_parse_float(_extract_field(raw_text, "Take Profit"), "Take Profit"),
            risk_percent=_parse_float(_extract_field(raw_text, "Risk Percent"), "Risk Percent"),
            suggested_volume=None,  # توسط risk_manager محاسبه می‌شود، نه AI
            reward_risk_ratio=_parse_float(
                _extract_field(raw_text, "Reward Risk Ratio"), "Reward Risk Ratio"
            ),
            expiration=_extract_field(raw_text, "Expiration"),
            invalidation=_extract_field(raw_text, "Invalidation"),
            short_reason=reason,
            checklist_complete=_parse_bool(_extract_field(raw_text, "Checklist Complete", required=False)),
        )

    elif status == AnalysisStatus.WATCH:
        pref_dir_raw = _extract_field(raw_text, "Preferred Direction")
        if pref_dir_raw not in Direction._value2member_map_:
            raise AIResponseParseError(f"Preferred Direction نامعتبر: '{pref_dir_raw}'")
        zone = _extract_field(raw_text, "Zone Or Level")
        # بند ۱۱: شرط باید عددی/زمانی/وابسته به کندل باشد - عبارت مبهم رد شود
        vague_phrases = ("بعدا", "بعداً", "دوباره بررسی شود", "later", "check again")
        if any(p in zone.lower() for p in vague_phrases):
            raise AIResponseParseError(
                f"Zone Or Level مبهم است و طبق بند ۱۱ سند مجاز نیست: '{zone}'"
            )
        watch_details = WatchDetails(
            preferred_direction=Direction(pref_dir_raw),
            current_or_potential_grade=grade or Grade.WEAK,
            watch_reason=reason,
            trigger_type=_extract_field(raw_text, "Trigger Type"),
            exact_zone_or_level=zone,
            timeframes_to_recheck=_parse_list(_extract_field(raw_text, "Timeframes To Recheck")),
            expiration=_extract_field(raw_text, "Expiration"),
            invalidation=_extract_field(raw_text, "Invalidation"),
        )

    return AnalysisResult(
        analysis_time=analysis_time,
        symbol=symbol,
        status=status,
        direction=direction,
        grade=grade,
        reason=reason,
        timeframes_checked=timeframes_checked,
        trade_details=trade_details,
        watch_details=watch_details,
        raw_ai_text=original_raw_text,
    )
