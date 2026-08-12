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
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from broker.base import BrokerBase
from config import config
from core.models import AnalysisResult, AnalysisStatus, Direction, Grade, MarketSnapshot, WatchDetails, WatchState
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
        if ai_client is None:
            from core.ai_client import AIClient
            ai_client = AIClient()
        self.ai_client = ai_client

    # ------------------------------------------------------------------
    def run_initial_analysis(self, symbol: str, needs_correlated_symbols: bool = True) -> AnalysisResult:
        """تحلیل اولیه از طریق دستور /analyze (بند ۲)."""
        # این بررسی باید قبل از snapshot/چارت/AI انجام شود.
        account_state, account_details = self._get_account_status(symbol)
        if account_state is not None:
            existing = db.get_active_watch_for_symbol(symbol)
            active_watch_invalidated = False
            if existing is not None and account_state != "ACCOUNT_STATE_UNKNOWN":
                watch_manager.close_watch(
                    existing["watch_id"], "INVALIDATED",
                    "وجود پوزیشن یا سفارش واقعی روی نماد شناسایی شد.",
                )
                active_watch_invalidated = True
            reason = (
                "ارتباط با حساب MT5 برای بررسی پوزیشن/سفارش ناموفق بود؛ برای ایمنی تحلیل جدید اجرا نشد."
                if account_state == "ACCOUNT_STATE_UNKNOWN" else
                "روی این نماد از قبل پوزیشن یا سفارش فعال وجود دارد؛ تحلیل جدید اجرا نشد."
            )
            if active_watch_invalidated:
                reason += " واچ فعال قبلی با وضعیت «باطل‌شده» بسته شد."
            result = AnalysisResult(
                analysis_time=datetime.now(timezone.utc), symbol=symbol,
                status=AnalysisStatus.NO_TRADE, direction=None, grade=None,
                reason=reason,
                timeframes_checked=[], account_state=account_state,
                account_state_details=account_details,
            )
            db.log_event(f"ACCOUNT_STATE_{account_state}", result.reason, symbol=symbol)
            return result

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
                reason="واچ فعال قبلی بدون اجرای تحلیل جدید نمایش داده شده است. وضعیت فعلی: در انتظار تریگر.",
                timeframes_checked=[],
                watch_details=WatchDetails(
                    preferred_direction=Direction(existing_watch["direction"]),
                    current_or_potential_grade=Grade(existing_watch["grade"]),
                    watch_reason="واچ فعال موجود",
                    trigger_type=existing_watch["trigger_type"],
                    exact_zone_or_level=existing_watch["zone_or_level"],
                    timeframes_to_recheck=json.loads(existing_watch["timeframes_to_recheck"] or "[]"),
                    expiration=existing_watch["expiration"],
                    invalidation=existing_watch["invalidation_condition"],
                ),
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
        watch_state = self._row_to_watch_state(watch_row)

        account_state, account_details = self._get_account_status(symbol)
        if account_state is not None:
            reason = (
                "وضعیت حساب MT5 قابل بررسی نبود؛ تحلیل مجدد برای ایمنی اجرا نشد."
                if account_state == "ACCOUNT_STATE_UNKNOWN" else
                "پوزیشن یا سفارش واقعی روی نماد فعال است؛ سیگنال جدید صادر نشد."
            )
            return AnalysisResult(
                analysis_time=datetime.now(timezone.utc), symbol=symbol,
                status=AnalysisStatus.NO_TRADE, direction=None, grade=None,
                reason=reason,
                timeframes_checked=[], account_state=account_state,
                account_state_details=account_details,
            )

        snapshot = self.broker.get_market_snapshot(symbol)

        if not snapshot.market_open:
            # بازار بسته است - تحلیل مجدد به‌جای مصرف بی‌فایده AI، به تعویق
            # می‌افتد. Watch بسته یا جایگزین نمی‌شود، فقط برای بررسی بعدی
            # (وقتی بازار باز شد و کندل جدید بسته شد) آزاد می‌شود.
            logger.info("بازار %s بسته است - تحلیل مجدد Watch به تعویق افتاد.", symbol)
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

        from charts.chart_generator import generate_required_charts
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
        account_state, account_details = self._get_account_status(symbol)
        if account_state is not None:
            result.account_state = account_state
            result.account_state_details = account_details
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
                    result = AnalysisResult(
                        analysis_time=result.analysis_time, symbol=result.symbol,
                        status=AnalysisStatus.NO_TRADE, direction=result.direction, grade=result.grade,
                        reason=f"نتیجه TRADE به‌علت محاسبه‌نشدن حجم ایمن رد شد: {vol_result.warning}",
                        timeframes_checked=result.timeframes_checked, raw_ai_text=result.raw_ai_text,
                    )
                else:
                    balance = snapshot.account_balance or 0.0
                    current_open_risk = db.get_estimated_open_risk_amount(balance)
                    proposed_risk = balance * result.trade_details.risk_percent / 100.0
                    max_open_risk = balance / 5000.0 * config.risk.max_daily_open_risk_usd_per_5000
                    if current_open_risk + proposed_risk > max_open_risk:
                        result = AnalysisResult(
                            analysis_time=result.analysis_time, symbol=result.symbol,
                            status=AnalysisStatus.NO_TRADE, direction=result.direction, grade=result.grade,
                            reason=(f"سقف ریسک باز حساب رد می‌شود: ریسک باز {current_open_risk:.2f} + "
                                    f"ریسک جدید {proposed_risk:.2f} > سقف {max_open_risk:.2f}."),
                            timeframes_checked=result.timeframes_checked, raw_ai_text=result.raw_ai_text,
                        )

                # ثبت ردیابی برای سنجش عملکرد واقعی بعداً (/performance) -
                # این تنها راه سنجش عینی «آیا این استراتژی سودآور است؟» است
                if result.status == AnalysisStatus.TRADE:
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

        # parent_watch پیش از ورود به تحلیل مجدد با TRIGGERED بسته شده است.
        # بنابراین هر خروجی WATCH در این مرحله همیشه رکورد تازه‌ای می‌سازد.
        new_watch_id = None
        if result.status == AnalysisStatus.WATCH and result.watch_details is not None:
            if parent_watch is not None and watch_manager.is_same_triggered_setup(parent_watch, result.watch_details):
                result.suppress_notification = True
                db.log_event(
                    "WATCH_RECREATION_SUPPRESSED",
                    "خروجی reanalysis همان Watch تریگرشده قبلی بود؛ Watch جدید ساخته نشد.",
                    symbol=symbol, watch_id=parent_watch.watch_id,
                )
            else:
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

    def _get_account_status(self, symbol: str) -> tuple[str | None, list[dict]]:
        """
        بند جدید (مورد ۳): تشخیص پوزیشن باز یا سفارش Pending واقعی روی این
        نماد، مستقیم از حساب MT5 - تا معاملات دستی موبایل/دسکتاپ هم دیده
        شوند. اگر خطایی در ارتباط با بروکر رخ دهد، محافظه‌کارانه None
        برگردانده می‌شود (یعنی تحلیل عادی ادامه پیدا می‌کند) تا یک خطای
        موقت شبکه کل تحلیل را متوقف نکند.
        """
        try:
            positions = self.broker.get_open_positions(symbol)
            orders = self.broker.get_pending_orders(symbol)
            details = [dict(item, record_type="POSITION") for item in positions]
            details += [dict(item, record_type="PENDING_ORDER") for item in orders]
            if positions and orders:
                return "OPEN_POSITION_AND_PENDING_ORDER", details
            if positions:
                return "OPEN_POSITION", details
            if orders:
                return "PENDING_ORDER", details
        except Exception as exc:  # noqa: BLE001
            logger.warning("بررسی پوزیشن/سفارش باز %s ناموفق بود: %s", symbol, exc)
            # Fail closed: وقتی حساب MT5 قابل بررسی نیست، صدور سیگنال جدید امن نیست.
            return "ACCOUNT_STATE_UNKNOWN", [{"record_type": "ERROR", "error": str(exc)}]
        return None, []

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
            close_status=row["close_status"] if "close_status" in row.keys() else None,
            closed_at=(datetime.fromisoformat(row["closed_at"]) if "closed_at" in row.keys() and row["closed_at"] else None),
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
