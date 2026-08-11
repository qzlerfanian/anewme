"""
core/analysis_service.py
---------------------------
این ماژول «مغز هماهنگ‌کننده» است که دقیقاً جریان بند ۲۱ سند را پیاده می‌کند:

  درخواست نماد در تلگرام
  -> دریافت تصاویر و داده بازار
  -> ارسال قوانین ANEWME و قالب پاسخ
  -> دریافت TRADE / WATCH / NO_TRADE
  -> ارسال نتیجه در تلگرام
  -> در صورت WATCH: مانیتور و تحلیل مجدد
  -> ثبت دستی معامله توسط کاربر (خارج از این سیستم)

هیچ تابعی در این فایل سفارش واقعی ثبت/مدیریت نمی‌کند - طبق بند ۲۱،
این محدودیت قطعی است و عمداً هیچ متد place_order مشابهی این‌جا وجود ندارد.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from broker.base import BrokerBase
from charts.chart_generator import generate_required_charts
from config import config
from core.ai_client import AIClient
from core.models import AnalysisResult, AnalysisStatus, Direction, Grade, MarketSnapshot, WatchState
from core.parser import AIResponseParseError, parse_ai_response
from core.risk_manager import calculate_position_size
from core.consistency_checker import check_watch_consistency
from core.validator import validate_trade_result
from storage import db
from watch import watch_manager

logger = logging.getLogger(__name__)


class AnalysisService:
    def __init__(self, broker: BrokerBase, ai_client: Optional[AIClient] = None):
        self.broker = broker
        self.ai_client = ai_client or AIClient()

    # ------------------------------------------------------------------
    def run_initial_analysis(self, symbol: str, needs_correlated_symbols: bool = True) -> AnalysisResult:
        """تحلیل اولیه از طریق دستور /analyze (بند ۲)."""
        snapshot = self.broker.get_market_snapshot(symbol)

        if not snapshot.market_open:
            # قبل از هر هزینه‌ای (ساخت چارت، تماس AI)، اگر بازار بسته است
            # مستقیم و بدون حدس زدن اعلام می‌شود - نیازی به تحلیل نیست.
            logger.info("بازار %s بسته است - تحلیل بدون فراخوانی AI رد شد.", symbol)
            result = AnalysisResult(
                analysis_time=datetime.now(timezone.utc),
                symbol=symbol,
                status=AnalysisStatus.NO_TRADE,
                direction=None,
                grade=None,
                reason="بازار برای این نماد در حال حاضر بسته است.",
                timeframes_checked=[],
            )
            db.save_analysis(
                analysis_id=str(uuid.uuid4()), symbol=symbol, status=result.status.value,
                direction=None, grade=None, reason=result.reason, raw_ai_text="",
                chart_paths=[], market_snapshot_dict=_snapshot_to_dict(snapshot),
                trade_details_dict=None, watch_details_dict=None, parent_watch_id=None,
            )
            db.log_event("MARKET_CLOSED", result.reason, symbol=symbol)
            return result

        # --- مورد ۲: جلوگیری از ساخت Watch تکراری روی همین نماد ---
        # تا زمانی که یک Watch فعال (هنوز بسته‌نشده) برای این نماد وجود
        # دارد، تحلیل تازه‌ای که دوباره منجر به Watch شود اجرا نمی‌شود؛
        # باید اول تکلیف Watch موجود (TRADE/NO_TRADE/انقضا/ابطال) روشن شود.
        existing_watch = db.get_active_watch_for_symbol(symbol)
        if existing_watch is not None:
            logger.info("Watch فعال از قبل روی %s وجود دارد - تحلیل جدید رد شد.", symbol)
            result = AnalysisResult(
                analysis_time=datetime.now(timezone.utc),
                symbol=symbol,
                status=AnalysisStatus.WATCH,
                direction=Direction(existing_watch["direction"]),
                grade=Grade(existing_watch["grade"]),
                reason=(
                    f"یک Watch فعال از قبل روی {symbol} وجود دارد (سطح: "
                    f"{existing_watch['zone_or_level']}، انقضا: {existing_watch['expiration']}). "
                    "تا مشخص‌شدن تکلیف آن (TRADE/NO_TRADE/انقضا)، Watch جدیدی صادر نمی‌شود."
                ),
                timeframes_checked=[],
            )
            db.log_event("DUPLICATE_WATCH_PREVENTED", result.reason, symbol=symbol, watch_id=existing_watch["watch_id"])
            return result

        chart_paths = self._build_charts(symbol, snapshot, needs_correlated_symbols)
        raw_text = self.ai_client.request_analysis(symbol, chart_paths, snapshot, previous_watch=None)
        return self._finalize(
            symbol, raw_text, snapshot, chart_paths, parent_watch=None,
            chart_descriptions_text=self.ai_client.last_chart_descriptions,
        )

    def run_watch_recheck(self, watch_row) -> AnalysisResult:
        """
        تحلیل مجدد بعد از فعال‌شدن Trigger یک Watch (بند ۱۴).
        watch_row: ردیف دیتابیس Watch (sqlite3.Row)
        """
        symbol = watch_row["symbol"]
        snapshot = self.broker.get_market_snapshot(symbol)
        watch_state = self._row_to_watch_state(watch_row)

        if not snapshot.market_open:
            # بازار بسته است - تحلیل مجدد به‌جای مصرف بی‌فایده AI، به تعویق
            # می‌افتد. Watch بسته یا جایگزین نمی‌شود، فقط برای بررسی بعدی
            # (وقتی بازار باز شد و کندل جدید بسته شد) آزاد می‌شود.
            logger.info("بازار %s بسته است - تحلیل مجدد Watch به تعویق افتاد.", symbol)
            watch_manager.reset_for_continued_monitoring(watch_state.watch_id)
            db.log_event(
                "WATCH_RECHECK_DEFERRED_MARKET_CLOSED",
                "بازار بسته است - بررسی به بازگشایی بازار موکول شد.",
                symbol=symbol, watch_id=watch_state.watch_id,
            )
            return AnalysisResult(
                analysis_time=datetime.now(timezone.utc),
                symbol=symbol,
                status=AnalysisStatus.WATCH,
                direction=watch_state.direction,
                grade=watch_state.grade,
                reason="بازار بسته است - بررسی به بازگشایی بازار موکول شد.",
                timeframes_checked=[],
                suppress_notification=True,  # کاربر پیام غیرضروری دریافت نمی‌کند
            )

        needed_tfs = watch_state.timeframes_to_recheck or ["M5", "M15", "H1"]
        chart_paths = self._build_charts(symbol, snapshot, needs_correlated_symbols=True,
                                          only_timeframes=needed_tfs)

        raw_text = self.ai_client.request_analysis(symbol, chart_paths, snapshot, previous_watch=watch_state)
        return self._finalize(
            symbol, raw_text, snapshot, chart_paths, parent_watch=watch_state,
            chart_descriptions_text=self.ai_client.last_chart_descriptions,
        )

    # ------------------------------------------------------------------
    def _build_charts(self, symbol: str, snapshot: MarketSnapshot, needs_correlated_symbols: bool,
                       only_timeframes: list[str] | None = None) -> list[Path]:
        snapshot_candles = {
            "M5": snapshot.candles_m5,
            "M15": snapshot.candles_m15,
            "H1": snapshot.candles_h1,
        }
        if only_timeframes:
            snapshot_candles = {k: v for k, v in snapshot_candles.items() if k in only_timeframes}

        correlated_candles = None
        if needs_correlated_symbols:
            correlated_candles = {}
            for corr_symbol in ("DXY", "USDJPY"):
                try:
                    correlated_candles[corr_symbol] = {
                        "M5": self.broker.get_candles(corr_symbol, "M5", config.timeframes.m5_candle_count),
                        "M15": self.broker.get_candles(corr_symbol, "M15", config.timeframes.m15_candle_count),
                        "H1": self.broker.get_candles(corr_symbol, "H1", config.timeframes.h1_candle_count),
                    }
                except Exception as exc:
                    logger.warning("دریافت داده %s ناموفق بود: %s", corr_symbol, exc)

        return generate_required_charts(
            symbol, snapshot_candles, include_correlated=needs_correlated_symbols,
            correlated_candles=correlated_candles,
        )

    def _finalize(
        self,
        symbol: str,
        raw_text: str,
        snapshot: MarketSnapshot,
        chart_paths: list[Path],
        parent_watch: Optional[WatchState],
        chart_descriptions_text: str = "",
    ) -> AnalysisResult:
        """
        پارس، اعتبارسنجی (بند ۱۷)، محاسبه حجم (بند ۱۸)، ذخیره‌سازی (بند ۲۰)
        و به‌روزرسانی وضعیت Watch (بند ۱۵/۱۹).
        در صورت هر خطای پارس/اعتبارسنجی، به‌جای شکست خاموش، به NO_TRADE
        ایمن تبدیل می‌شود و دلیل دقیق ثبت/اعلام می‌شود.
        """
        analysis_id = str(uuid.uuid4())
        parent_watch_id = parent_watch.watch_id if parent_watch else None

        # مورد ۴: ساعت آخرین کندل M5 بسته‌شده - برای شفافیت خروجی
        last_closed_m5_time = None
        if snapshot.candles_m5:
            last_closed_m5_time = snapshot.candles_m5[-1]["time"].strftime("%H:%M UTC")

        try:
            result = parse_ai_response(raw_text, expected_symbol=symbol)
        except AIResponseParseError as exc:
            logger.error("پارس پاسخ AI شکست خورد: %s", exc)
            db.log_error("ai_response_parse", str(exc), symbol=symbol)
            result = AnalysisResult(
                analysis_time=datetime.now(timezone.utc),
                symbol=symbol,
                status=AnalysisStatus.NO_TRADE,
                direction=None,
                grade=None,
                reason=f"خطای پردازش پاسخ هوش مصنوعی: {exc}",
                timeframes_checked=[],
                raw_ai_text=raw_text,
            )
        result.last_closed_m5_time = last_closed_m5_time

        # --- مورد ۳: تشخیص پوزیشن باز/سفارش Pending واقعی روی این نماد ---
        # این چک مستقیم از حساب MT5 خوانده می‌شود (نه از دیتابیس خودمان)
        # تا معاملات دستی از موبایل/دسکتاپ هم شناسایی شوند. اگر پوزیشن یا
        # سفارش باز پیدا شود، Grade/Reason همچنان (برای مانیتور) نمایش داده
        # می‌شود ولی هیچ Watch/TRADE جدیدی ساخته یا ردیابی نمی‌شود - طبق
        # قانون «تا وقتی روی نماد معامله فعال هست، سیگنال ورود جدید صادر نشود».
        account_state = self._get_account_state(symbol)
        if account_state is not None:
            result.account_state = account_state
            logger.info(
                "پوزیشن/سفارش باز روی %s پیدا شد (%s) - سیگنال جدید صادر نمی‌شود.",
                symbol, account_state,
            )
            db.save_analysis(
                analysis_id=analysis_id,
                symbol=symbol,
                status=result.status.value,
                direction=result.direction.value if result.direction else None,
                grade=result.grade.value if result.grade else None,
                reason=result.reason,
                raw_ai_text=result.raw_ai_text,
                chart_descriptions_text=chart_descriptions_text,
                chart_paths=[str(p) for p in chart_paths],
                market_snapshot_dict=_snapshot_to_dict(snapshot),
                trade_details_dict=_dataclass_or_none(result.trade_details),
                watch_details_dict=_dataclass_or_none(result.watch_details),
                parent_watch_id=parent_watch_id,
            )
            db.log_event(
                f"ACCOUNT_STATE_{account_state}",
                f"{account_state} روی {symbol} فعال است - سیگنال جدید سرکوب شد.",
                symbol=symbol,
            )
            return result

        if result.status == AnalysisStatus.TRADE:
            outcome = validate_trade_result(result, snapshot)
            if not outcome.is_valid:
                logger.warning("نتیجه TRADE رد شد: %s", outcome.reasons)
                result = AnalysisResult(
                    analysis_time=result.analysis_time,
                    symbol=result.symbol,
                    status=AnalysisStatus.NO_TRADE,
                    direction=result.direction,
                    grade=result.grade,
                    reason="نتیجه TRADE توسط کنترل ایمنی رد شد: " + "؛ ".join(outcome.reasons),
                    timeframes_checked=result.timeframes_checked,
                    raw_ai_text=result.raw_ai_text,
                )
            else:
                vol_result = calculate_position_size(result.trade_details, snapshot)
                result.trade_details.suggested_volume = vol_result.suggested_volume
                if vol_result.warning:
                    result.reason += f" | هشدار حجم: {vol_result.warning}"

                # ثبت ردیابی برای سنجش عملکرد واقعی بعداً (/performance) -
                # این تنها راه سنجش عینی «آیا این استراتژی سودآور است؟» است
                db.create_trade_tracking(
                    analysis_id=analysis_id,
                    symbol=symbol,
                    direction=result.direction.value,
                    order_type=result.trade_details.order_type.value,
                    entry=result.trade_details.entry,
                    stop_loss=result.trade_details.stop_loss,
                    take_profit=result.trade_details.take_profit,
                    risk_percent=result.trade_details.risk_percent,
                    reward_risk_ratio=result.trade_details.reward_risk_ratio,
                    expiration=result.trade_details.expiration,
                )

        elif result.status == AnalysisStatus.WATCH:
            # بند ۱۵ فایل قوانین: چک برنامه‌نویسی‌شده تناقض (مکمل دستور به AI).
            # فقط لاگ می‌شود - نتیجه به کاربر تغییر نمی‌کند (طراحی محافظه‌کارانه
            # چون تشخیص این موارد از روی متن آزاد قطعی نیست).
            consistency_warnings = check_watch_consistency(result, snapshot)
            for warning_text in consistency_warnings:
                logger.warning("تناقض احتمالی در WATCH %s: %s", symbol, warning_text)
                db.log_event("WATCH_CONSISTENCY_WARNING", warning_text, symbol=symbol)

        # --- بند ۲/۴/۵: تشخیص «بدون تغییر واقعی» قبل از هر تصمیمی درباره Watch ---
        # اگر این یک تحلیل مجدد است و نتیجه بازهم WATCH با همان مشخصات قبلی
        # (جهت/سطح/Trigger/Invalidation) است، نباید Watch جدید ساخته شود،
        # Watch قبلی نباید بسته شود، Expiration نباید تغییر کند، و هیچ پیام
        # جدیدی هم نباید به تلگرام ارسال شود - فقط برای ادامه مانیتور باز می‌شود.
        if (
            parent_watch is not None
            and result.status == AnalysisStatus.WATCH
            and result.watch_details is not None
            and watch_manager.is_same_setup(parent_watch, result.watch_details)
        ):
            watch_manager.reset_for_continued_monitoring(parent_watch.watch_id)
            result.suppress_notification = True

            db.save_analysis(
                analysis_id=analysis_id,
                symbol=symbol,
                status=result.status.value,
                direction=result.direction.value if result.direction else None,
                grade=result.grade.value if result.grade else None,
                reason=result.reason,
                raw_ai_text=result.raw_ai_text,
                chart_descriptions_text=chart_descriptions_text,
                chart_paths=[str(p) for p in chart_paths],
                market_snapshot_dict=_snapshot_to_dict(snapshot),
                trade_details_dict=None,
                watch_details_dict=_dataclass_or_none(result.watch_details),
                parent_watch_id=parent_watch_id,
            )
            db.log_event(
                "ANALYSIS_WATCH_UNCHANGED", result.reason, symbol=symbol, watch_id=parent_watch.watch_id
            )
            return result

        # اگر تحلیل مجدد بود و نتیجه TRADE/NO_TRADE شد، Watch قبلی بسته می‌شود
        if parent_watch is not None and result.status != AnalysisStatus.WATCH:
            watch_manager.close_watch(
                parent_watch.watch_id,
                reason=f"با نتیجه {result.status.value} بسته شد.",
            )

        # اگر نتیجه جدید هم WATCH بود ولی واقعاً متفاوت (Watch چندمرحله‌ای -
        # بند ۱۵)، Watch جدید جایگزین قبلی می‌شود
        new_watch_id = None
        if result.status == AnalysisStatus.WATCH and result.watch_details is not None:
            if parent_watch is not None:
                watch_manager.close_watch(parent_watch.watch_id, reason="جایگزین شد با Watch جدید (تغییر واقعی ستاپ).")
            new_watch = watch_manager.create_watch_from_details(
                symbol, result.watch_details, parent_analysis_id=analysis_id
            )
            new_watch_id = new_watch.watch_id

        db.save_analysis(
            analysis_id=analysis_id,
            symbol=symbol,
            status=result.status.value,
            direction=result.direction.value if result.direction else None,
            grade=result.grade.value if result.grade else None,
            reason=result.reason,
            raw_ai_text=result.raw_ai_text,
            chart_descriptions_text=chart_descriptions_text,
            chart_paths=[str(p) for p in chart_paths],
            market_snapshot_dict=_snapshot_to_dict(snapshot),
            trade_details_dict=_dataclass_or_none(result.trade_details),
            watch_details_dict=_dataclass_or_none(result.watch_details),
            parent_watch_id=parent_watch_id,
        )

        db.log_event(
            f"ANALYSIS_{result.status.value}",
            result.reason,
            symbol=symbol,
            watch_id=new_watch_id or parent_watch_id,
        )
        return result

    def _get_account_state(self, symbol: str) -> str | None:
        """
        بند جدید (مورد ۳): تشخیص پوزیشن باز یا سفارش Pending واقعی روی این
        نماد، مستقیم از حساب MT5 - تا معاملات دستی موبایل/دسکتاپ هم دیده
        شوند. اگر خطایی در ارتباط با بروکر رخ دهد، محافظه‌کارانه None
        برگردانده می‌شود (یعنی تحلیل عادی ادامه پیدا می‌کند) تا یک خطای
        موقت شبکه کل تحلیل را متوقف نکند.
        """
        try:
            if self.broker.get_open_positions(symbol):
                return "OPEN_POSITION"
            if self.broker.get_pending_orders(symbol):
                return "PENDING_ORDER"
        except Exception as exc:  # noqa: BLE001
            logger.warning("بررسی پوزیشن/سفارش باز %s ناموفق بود: %s", symbol, exc)
        return None

    @staticmethod
    def _row_to_watch_state(row) -> WatchState:
        import json
        from core.models import Direction, Grade
        return WatchState(
            watch_id=row["watch_id"],
            symbol=row["symbol"],
            parent_analysis_id=row["parent_analysis_id"],
            direction=Direction(row["direction"]),
            grade=Grade(row["grade"]),
            trigger_type=row["trigger_type"],
            zone_or_level=row["zone_or_level"],
            timeframes_to_recheck=json.loads(row["timeframes_to_recheck"]),
            expiration=datetime.fromisoformat(row["expiration"]),
            invalidation_condition=row["invalidation_condition"],
            created_at=datetime.fromisoformat(row["created_at"]),
            is_locked=bool(row["is_locked"]),
            is_triggered=bool(row["is_triggered"]),
            is_closed=bool(row["is_closed"]),
        )


def _dataclass_or_none(obj):
    if obj is None:
        return None
    from dataclasses import asdict
    d = asdict(obj)
    # تبدیل Enum ها به مقدار قابل serialize
    for k, v in d.items():
        if hasattr(v, "value"):
            d[k] = v.value
    return d


def _snapshot_to_dict(snapshot: MarketSnapshot) -> dict:
    from dataclasses import asdict
    return asdict(snapshot)
