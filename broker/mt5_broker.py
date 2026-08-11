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
from datetime import datetime, timezone

from broker.base import BrokerBase
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

    def connect(self) -> None:
        if not mt5.initialize(login=self.login, password=self.password, server=self.server):
            raise RuntimeError(f"اتصال به MT5 ناموفق بود: {mt5.last_error()}")
        logger.info("اتصال به MT5 برقرار شد.")

    def disconnect(self) -> None:
        mt5.shutdown()

    def _fetch_candles(self, symbol: str, timeframe_key: str, count: int) -> list[dict]:
        tf = getattr(mt5, TIMEFRAME_MAP[timeframe_key])
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None:
            raise RuntimeError(f"دریافت کندل {timeframe_key} برای {symbol} ناموفق بود.")
        result = []
        for r in rates:
            result.append({
                "time": datetime.fromtimestamp(r["time"], tz=timezone.utc),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            })
        return result

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        return self._fetch_candles(symbol, timeframe, count)

    def get_current_price(self, symbol: str) -> tuple[float, float]:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"دریافت قیمت لحظه‌ای {symbol} ناموفق بود.")
        return tick.bid, tick.ask

    # اگر آخرین تیک قیمت قدیمی‌تر از این مقدار باشد، بازار بسته در نظر
    # گرفته می‌شود. این روش قابل‌اعتمادتر از session_deals است چون روی
    # همه نمادها (فارکس، طلا، کریپتو) یکسان کار می‌کند.
    STALE_TICK_THRESHOLD_SECONDS = 300  # ۵ دقیقه

    def is_market_open(self, symbol: str) -> bool:
        info = mt5.symbol_info(symbol)
        if info is None or info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
            return False
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return False
        last_tick_time = datetime.fromtimestamp(tick.time, tz=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - last_tick_time).total_seconds()
        return age_seconds < self.STALE_TICK_THRESHOLD_SECONDS

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
            market_time_utc=datetime.now(timezone.utc),
            broker_server_time=datetime.fromtimestamp(tick.time, tz=timezone.utc),
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
        )

    def get_open_positions(self, symbol: str) -> list[dict]:
        """
        پوزیشن‌های باز واقعی - مستقیم از حساب MT5. مهم: این شامل معاملاتی
        هم می‌شود که کاربر دستی از موبایل یا دسکتاپ MT5 باز کرده، نه فقط
        معاملاتی که از طریق این ربات پیشنهاد شده‌اند.
        """
        positions = mt5.positions_get(symbol=symbol)
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

    def get_pending_orders(self, symbol: str) -> list[dict]:
        """سفارش‌های Pending باز واقعی - مستقیم از حساب MT5 (شامل ثبت دستی)."""
        orders = mt5.orders_get(symbol=symbol)
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
