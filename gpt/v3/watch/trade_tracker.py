"""
watch/trade_tracker.py
-------------------------
پاسخ به این سؤال کلیدی: «آیا این TRADEهایی که ربات پیشنهاد داده واقعاً
سودآور بوده‌اند؟» - بدون این ماژول، تنها راه دونستن جواب، حدس زدن بود.

منطق: بعد از هر TRADE معتبر، یک رکورد در جدول trade_tracking ثبت می‌شود
(توسط core/analysis_service.py). این حلقه به‌صورت دوره‌ای قیمت فعلی را
چک می‌کند:
  ۱. اگر هنوز PENDING است و قیمت به سطح Entry رسیده -> FILLED
  ۲. اگر FILLED است و قیمت به TP یا SL رسیده (هرکدام زودتر) -> WIN/LOSS
  ۳. اگر منقضی شده و هنوز پر نشده -> EXPIRED (خنثی، در آمار برد/باخت شمرده نمی‌شود)

محدودیت صادقانه: این فقط بر اساس قیمت Bid/Ask لحظه‌ای (نه دقت کامل
درون‌کندلی) کار می‌کند - چون کاربر معامله را دستی و در بروکر واقعی خودش
اجرا می‌کند، این حلقه صرفاً یک **تخمین معقول** از نتیجه واقعی است، نه
مرجع رسمی حساب کاربر.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from broker.base import BrokerBase
from core.models import Direction
from storage import db

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30


class TradeTracker:
    def __init__(self, broker: BrokerBase):
        self.broker = broker
        self._running = False

    async def start(self) -> None:
        self._running = True
        logger.info("حلقه ردیابی نتیجه واقعی TRADEها شروع شد.")
        while self._running:
            try:
                await asyncio.to_thread(self._tick)
            except Exception as exc:  # noqa: BLE001
                logger.exception("خطا در حلقه ردیابی TRADE: %s", exc)
                db.log_error("trade_tracker_loop", str(exc))
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._running = False

    def _tick(self) -> None:
        open_trades = db.get_open_trade_trackings()
        for trade in open_trades:
            try:
                self._check_one(trade)
            except Exception as exc:  # noqa: BLE001
                logger.exception("خطا در بررسی ردیابی %s: %s", trade["analysis_id"], exc)
                db.log_error("trade_tracker_check", str(exc), symbol=trade["symbol"])

    def _check_one(self, trade) -> None:
        symbol = trade["symbol"]
        direction = trade["direction"]
        entry, sl, tp = trade["entry"], trade["stop_loss"], trade["take_profit"]
        now = datetime.now(timezone.utc)

        try:
            bid, ask = self.broker.get_current_price(symbol)
            closed_candles = self.broker.get_candles(symbol, "M5", 1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("دریافت قیمت %s برای ردیابی ناموفق بود: %s", symbol, exc)
            return

        # نکته مهم: برای دقت واقعی باید سمت درست قیمت استفاده شود، نه
        # میانگین. خرید با Ask انجام می‌شود و با Bid بسته می‌شود (و برعکس
        # برای فروش) - وگرنه اسپرد باعث تشخیص نادرست پر شدن/برخورد می‌شود.
        entry_check_price = ask if direction == Direction.BUY.value else bid
        exit_check_price = bid if direction == Direction.BUY.value else ask
        candle = closed_candles[-1] if closed_candles else None

        # --- انقضا (فقط اگر هنوز پر نشده) ---
        if trade["status"] == "PENDING" and trade["expiration"]:
            try:
                expiration_dt = datetime.fromisoformat(trade["expiration"].replace("Z", "+00:00"))
                if expiration_dt.tzinfo is None:
                    expiration_dt = expiration_dt.replace(tzinfo=timezone.utc)
                if now >= expiration_dt:
                    db.update_trade_tracking(trade["analysis_id"], status="EXPIRED", closed_at=now.isoformat())
                    db.log_event("TRADE_TRACKING_EXPIRED", f"{symbol} منقضی شد بدون پر شدن.", symbol=symbol)
                    return
            except Exception:  # noqa: BLE001
                pass  # فرمت زمان قابل‌پارس نبود - نادیده گرفته می‌شود

        # --- بررسی پر شدن سفارش Pending ---
        if trade["status"] == "PENDING":
            filled = False
            if direction == Direction.BUY.value:
                # BUY_LIMIT: قیمت باید پایین بیاید تا Entry -> BUY_STOP: قیمت باید بالا برود
                if ("LIMIT" in trade["order_type"] and entry_check_price <= entry) or \
                   ("STOP" in trade["order_type"] and entry_check_price >= entry):
                    filled = True
            else:
                if ("LIMIT" in trade["order_type"] and entry_check_price >= entry) or \
                   ("STOP" in trade["order_type"] and entry_check_price <= entry):
                    filled = True
            # Poll ممکن است تماس Entry را از دست بدهد؛ بازه کندل بسته نیز بررسی می‌شود.
            if candle and candle["low"] <= entry <= candle["high"]:
                filled = True

            if filled:
                db.update_trade_tracking(trade["analysis_id"], status="FILLED", filled_at=now.isoformat())
                db.log_event("TRADE_TRACKING_FILLED", f"{symbol} در {entry} پر شد.", symbol=symbol)
            return  # همین تیک فقط پر شدن چک می‌شود؛ SL/TP از تیک بعد بررسی می‌شود

        # --- بررسی رسیدن به TP یا SL (فقط برای معاملات پرشده) ---
        if trade["status"] == "FILLED":
            hit_tp = (exit_check_price >= tp) if direction == Direction.BUY.value else (exit_check_price <= tp)
            hit_sl = (exit_check_price <= sl) if direction == Direction.BUY.value else (exit_check_price >= sl)
            if candle:
                hit_tp = hit_tp or (candle["high"] >= tp if direction == Direction.BUY.value else candle["low"] <= tp)
                hit_sl = hit_sl or (candle["low"] <= sl if direction == Direction.BUY.value else candle["high"] >= sl)

            if hit_tp and hit_sl:
                # از OHLC نمی‌توان ترتیب برخورد TP و SL را فهمید؛ حدس‌زدن آمار را خراب می‌کند.
                db.update_trade_tracking(
                    trade["analysis_id"], status="AMBIGUOUS", closed_at=now.isoformat()
                )
                db.log_event("TRADE_TRACKING_AMBIGUOUS", f"{symbol}: TP و SL در یک کندل لمس شدند.", symbol=symbol)
            elif hit_tp:
                r_multiple = self._calc_r_multiple(direction, entry, sl, tp)
                db.update_trade_tracking(
                    trade["analysis_id"], status="WIN", closed_at=now.isoformat(),
                    exit_price=tp, actual_r_multiple=r_multiple,
                )
                db.log_event("TRADE_TRACKING_WIN", f"{symbol} به TP رسید.", symbol=symbol)
            elif hit_sl:
                db.update_trade_tracking(
                    trade["analysis_id"], status="LOSS", closed_at=now.isoformat(),
                    exit_price=sl, actual_r_multiple=-1.0,
                )
                db.log_event("TRADE_TRACKING_LOSS", f"{symbol} به SL خورد.", symbol=symbol)

    @staticmethod
    def _calc_r_multiple(direction: str, entry: float, sl: float, tp: float) -> float:
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk == 0:
            return 0.0
        return round(reward / risk, 2)
