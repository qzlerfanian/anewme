"""
broker/mt5_broker.py
----------------------
پیاده‌سازی BrokerBase با استفاده از پکیج رسمی MetaTrader5.
توجه: این پکیج فقط روی ویندوز و با ترمینال MT5 نصب‌شده در دسترس است.
اگر سرویس روی لینوکس (مثلاً VPS لینوکسی یا Docker) اجرا می‌شود، باید:
  - از Wine برای اجرای MT5 استفاده کرد، یا
  - یک broker/rest_broker.py جایگزین نوشت که با یک API واسط (مثلاً یک
    اکسپرت مشاور MT5 که روی یک VPS ویندوزی جدا REST سرور بالا می‌آورد)
    ارتباط برقرار کند.
این فایل فرض می‌کند ترمینال MT5 از قبل روی سیستم لاگین شده است.
"""

from __future__ import annotations

import logging
import os
import math
import time
from core import clock
from threading import RLock
from functools import wraps
from datetime import datetime, timezone

from broker.base import BrokerBase
from broker.candle_utils import closed_only
from config import config
from core.models import MarketSnapshot

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
except ImportError:  # روی لینوکس/مک این پکیج قابل نصب نیست
    mt5 = None


TIMEFRAME_MAP = {
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "H1": "TIMEFRAME_H1",
}

_terminal_lock = RLock()

def serialized(method):
    @wraps(method)
    def call(*args, **kwargs):
        with _terminal_lock:
            return method(*args, **kwargs)
    return call


class MT5Broker(BrokerBase):
    def __init__(self, login: int | None = None, password: str | None = None, server: str | None = None):
        if mt5 is None:
            raise RuntimeError(
                "پکیج MetaTrader5 در این سیستم قابل استفاده نیست (فقط ویندوز). "
                "برای اجرا روی لینوکس از یک بروکر جایگزین (broker/rest_broker.py) استفاده کنید."
            )
        self.login = login
        self.password = password
        self.server = server
        # Verified for this deployment: API encodes broker wall time (UTC+3).
        # For an API returning real UTC set this explicitly to 0.
        self.data_utc_offset_minutes = int(os.getenv('MT5_DATA_UTC_OFFSET_MINUTES', '180'))
        if not -840 <= self.data_utc_offset_minutes <= 840:
            raise ValueError('MT5_DATA_UTC_OFFSET_MINUTES خارج از محدوده معتبر است.')

    def _data_time_utc(self, raw_seconds):
        return datetime.fromtimestamp(float(raw_seconds) - self.data_utc_offset_minutes * 60, timezone.utc)

    @serialized
    def get_health_status(self):
        terminal = mt5.terminal_info()
        report = {'connected': bool(terminal and terminal.connected),
                  'data_utc_offset_minutes': self.data_utc_offset_minutes, 'symbols': {}}
        if not report['connected']:
            return report
        for symbol in ('EURUSD', 'GBPUSD', 'XAUUSD', 'USDJPY'):
            item = {'candles': {}}
            report['symbols'][symbol] = item
            try:
                tick = mt5.symbol_info_tick(symbol)
                if tick is not None:
                    item['tick_age_seconds'] = round((clock.utc_now() - self._data_time_utc(tick.time)).total_seconds(), 1)
                for tf in ('M5', 'M15', 'H1'):
                    try:
                        bars = self.get_candles(symbol, tf, 2)
                        item['candles'][tf] = (f"close_time={bars[-1]['close_time'].isoformat()} close={bars[-1]['close']}"
                                               if bars else 'no closed candle')
                    except Exception as exc:
                        item['candles'][tf] = type(exc).__name__
            except Exception as exc:
                item['error'] = type(exc).__name__
        return report

    @serialized
    def connect(self) -> None:
        kwargs = {k: v for k, v in dict(login=self.login, password=self.password, server=self.server).items() if v is not None}
        kwargs['timeout'] = 15000
        path = os.getenv('MT5_TERMINAL_PATH')
        ok = mt5.initialize(path, **kwargs) if path else mt5.initialize(**kwargs)
        if not ok:
            raise RuntimeError(f"اتصال به MT5 ناموفق بود: {mt5.last_error()}")
        self._synchronize_clock()
        logger.info("اتصال به MT5 برقرار شد.")

    def _synchronize_clock(self):
        # Observe an actual new tick; a cached weekend quote is not 'now'.
        previous = {}
        for symbol in ('EURUSD', 'GBPUSD', 'USDJPY', 'XAUUSD'):
            if mt5.symbol_select(symbol, True):
                tick = mt5.symbol_info_tick(symbol)
                if tick is not None:
                    previous[symbol] = self._tick_seconds(tick)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for symbol, before in previous.items():
                tick = mt5.symbol_info_tick(symbol)
                if tick is not None and self._tick_seconds(tick) > before:
                    timestamp = self._tick_seconds(tick)
                    clock.anchor_to_broker(self._data_time_utc(timestamp).timestamp())
                    logger.info('CLOCK_ANCHORED broker_utc=%s system_utc=%s symbol=%s',
                                clock.utc_now().isoformat(), datetime.now(timezone.utc).isoformat(), symbol)
                    return
            time.sleep(.2)
        raise RuntimeError('برای تعیین زمان مرجع، طی ۱۵ ثانیه تیک جدید دریافت نشد؛ اتصال و نام نمادهای Market Watch را بررسی کنید. تعطیلی بازار نتیجه‌گیری نشده است.')

    @staticmethod
    def _tick_seconds(tick):
        milliseconds = getattr(tick, 'time_msc', None)
        return milliseconds / 1000 if isinstance(milliseconds, (int, float)) and milliseconds > 0 else float(tick.time)

    @serialized
    def disconnect(self) -> None:
        mt5.shutdown()
        clock.reset()

    @serialized
    def _fetch_candles(self, symbol: str, timeframe_key: str, count: int) -> list[dict]:
        tf = getattr(mt5, TIMEFRAME_MAP[timeframe_key])
        # position=0 را هم می‌خوانیم اما سپس با close_time واقعی فیلتر
        # می‌کنیم. این روش در مرز بسته‌شدن کندل از اتکا به شماره shift امن‌تر است.
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count + 1)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"دریافت کندل {timeframe_key} برای {symbol} ناموفق بود.")
        result = []
        for source_index, r in enumerate(rates[:-1]):  # shift zero is always excluded
            result.append({
                "time": self._data_time_utc(r["time"]),
                "raw_broker_open_seconds": int(r["time"]),
                "data_utc_offset_minutes": self.data_utc_offset_minutes,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "source_index": len(rates) - 1 - source_index,
                # MT5 position 0 (the forming bar) was removed above; this
                # flag records that the remaining bar came from a finalized feed row.
                "closed_confirmed": True,
                "timestamp_semantics": "open_time",
            })
        return closed_only(result, timeframe_key, clock.utc_now())[-count:]

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        return self._fetch_candles(symbol, timeframe, count)

    @serialized
    def get_current_price(self, symbol: str) -> tuple[float, float]:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"دریافت قیمت لحظه‌ای {symbol} ناموفق بود.")
        return tick.bid, tick.ask

    # Freshness is NOT proof of trading-session status. Report data failures
    # separately instead of labelling a disconnected/stale terminal closed.
    STALE_TICK_THRESHOLD_SECONDS = 300  # ۵ دقیقه

    @serialized
    def is_market_open(self, symbol: str) -> bool:
        terminal = mt5.terminal_info()
        if terminal is None or not terminal.connected:
            raise RuntimeError(f"{symbol}: اتصال ترمینال MT5 برقرار نیست؛ وضعیت بازار قابل تشخیص نیست.")
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"{symbol}: نماد در ترمینال موجود نیست؛ نام و پسوند نماد بروکر را بررسی کنید.")
        if info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
            raise RuntimeError(f"{symbol}: معامله این نماد در ترمینال غیرفعال است؛ این وضعیت به‌تنهایی به معنی بسته‌بودن بازار نیست.")
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"{symbol}: تیک قیمت دریافت نشد؛ وضعیت بازار قابل تشخیص نیست.")
        last_tick_time = self._data_time_utc(self._tick_seconds(tick))
        now = clock.utc_now()
        age_seconds = (now - last_tick_time).total_seconds()
        evidence = (f"symbol={symbol}; reference_utc={now.isoformat()}; "
                    f"tick_utc={last_tick_time.isoformat()}; age_seconds={age_seconds:.3f}; "
                    f"connected={terminal.connected}; trade_mode={info.trade_mode}; "
                    f"raw_tick={self._tick_seconds(tick)}; offset_minutes={self.data_utc_offset_minutes}")
        logger.info("MT5_FEED_STATUS %s", evidence)
        if age_seconds < -5:
            raise RuntimeError("زمان تیک با مرجع تثبیت‌شده بروکر سازگار نیست؛ جریان داده باید بررسی شود. " + evidence)
        if age_seconds >= self.STALE_TICK_THRESHOLD_SECONDS:
            raise RuntimeError("قیمت MT5 بیش از ۵ دقیقه به‌روز نشده است؛ اتصال/دریافت قیمت را بررسی کنید. تعطیلی بازار تأیید نشده است. " + evidence)
        return True

    @serialized
    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        account = mt5.account_info()
        if info is None or tick is None:
            raise RuntimeError(f"اطلاعات نماد {symbol} در دسترس نیست.")

        return MarketSnapshot(
            symbol=symbol,
            bid=tick.bid,
            ask=tick.ask,
            spread=round((tick.ask - tick.bid), info.digits),
            # زمان واقعی آخرین تیک؛ validator با این مقدار stale بودن داده را می‌سنجد.
            market_time_utc=self._data_time_utc(self._tick_seconds(tick)),
            broker_server_time=self._data_time_utc(self._tick_seconds(tick)),
            market_open=self.is_market_open(symbol),
            candles_m5=self._fetch_candles(symbol, "M5", config.timeframes.m5_candle_count),
            candles_m15=self._fetch_candles(symbol, "M15", config.timeframes.m15_candle_count),
            candles_h1=self._fetch_candles(symbol, "H1", config.timeframes.h1_candle_count),
            account_balance=account.balance if account else None,
            account_currency=account.currency if account else None,
            symbol_contract_size=info.trade_contract_size,
            symbol_min_lot=info.volume_min,
            symbol_lot_step=info.volume_step,
            symbol_pip_value=info.trade_tick_value * (10 if info.digits in (3, 5) else 1),
            symbol_tick_size=info.trade_tick_size,
            symbol_tick_value=(getattr(info, "trade_tick_value_loss", 0) or info.trade_tick_value),
            symbol_max_lot=info.volume_max,
            symbol_digits=info.digits,
        )

    @serialized
    def get_open_positions(self, symbol: str) -> list[dict]:
        """
        پوزیشن‌های باز واقعی - مستقیم از حساب MT5. مهم: این شامل معاملاتی
        هم می‌شود که کاربر دستی از موبایل یا دسکتاپ MT5 باز کرده، نه فقط
        معاملاتی که از طریق این ربات پیشنهاد شده‌اند.
        """
        positions = mt5.positions_get(symbol=symbol)
        if positions is None:
            raise RuntimeError(f'خواندن پوزیشن‌های MT5 ناموفق بود: {mt5.last_error()}')
        if not positions:
            return []
        return [
            {
                "ticket": p.ticket,
                "volume": p.volume,
                "price_open": p.price_open,
                "type": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                "profit": p.profit,
            }
            for p in positions
        ]

    @serialized
    def get_pending_orders(self, symbol: str) -> list[dict]:
        """سفارش‌های Pending باز واقعی - مستقیم از حساب MT5 (شامل ثبت دستی)."""
        orders = mt5.orders_get(symbol=symbol)
        if orders is None:
            raise RuntimeError(f'خواندن سفارش‌های MT5 ناموفق بود: {mt5.last_error()}')
        if not orders:
            return []
        return [
            {
                "ticket": o.ticket,
                "volume": o.volume_current,
                "price_open": o.price_open,
                "type": o.type,
            }
            for o in orders
        ]

    @serialized
    def get_account_open_risk_amount(self):
        positions, orders = mt5.positions_get(), mt5.orders_get()
        if positions is None or orders is None:
            raise RuntimeError('ریسک باز حساب قابل خواندن نیست.')
        total = 0.0
        for item in list(positions) + list(orders):
            if item.sl <= 0:
                raise RuntimeError(f'پوزیشن/سفارش {item.ticket} حد ضرر ندارد؛ ریسک کل حساب نامعلوم است.')
            buy = item.type in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP,
                               getattr(mt5, 'ORDER_TYPE_BUY_STOP_LIMIT', 6))
            side = mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL
            volume = getattr(item, 'volume', None) or getattr(item, 'volume_current', None)
            loss = mt5.order_calc_profit(side, item.symbol, volume, item.price_open, item.sl)
            if loss is None or not math.isfinite(loss):
                raise RuntimeError(f'محاسبه ریسک {item.symbol} ناموفق بود.')
            total += max(0.0, -loss)
        return total
