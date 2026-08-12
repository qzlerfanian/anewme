"""
broker/mock_broker.py
------------------------
پیاده‌سازی آزمایشی BrokerBase برای توسعه/تست روی لینوکس یا هر سیستمی که
به MT5 واقعی دسترسی ندارد. داده‌های تصادفی اما ساختاریافته تولید می‌کند
تا کل مسیر (چارت -> AI -> پارس -> اعتبارسنجی -> تلگرام) بدون نیاز به
بروکر واقعی قابل تست باشد.
هرگز در محیط Production استفاده نشود.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from broker.base import BrokerBase
from config import config
from core.models import MarketSnapshot


class MockBroker(BrokerBase):
    def __init__(self, base_prices: dict[str, float] | None = None):
        self.base_prices = base_prices or {
            "EURUSD": 1.1750, "GBPUSD": 1.3400, "USDJPY": 156.50, "DXY": 104.20,
        }
        # برای تست دستی: می‌توانید مستقیم روی این دیکشنری‌ها مقدار بذارید
        # تا رفتار «پوزیشن باز وجود دارد» شبیه‌سازی شود.
        self._mock_open_positions: dict[str, list[dict]] = {}
        self._mock_pending_orders: dict[str, list[dict]] = {}

    def get_open_positions(self, symbol: str) -> list[dict]:
        return self._mock_open_positions.get(symbol, [])

    def get_pending_orders(self, symbol: str) -> list[dict]:
        return self._mock_pending_orders.get(symbol, [])

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def _gen_candles(self, symbol: str, count: int, step_minutes: int) -> list[dict]:
        price = self.base_prices.get(symbol, 1.0)
        now = datetime.now(timezone.utc)
        candles = []
        for i in range(count, 0, -1):
            o = price + random.uniform(-0.002, 0.002)
            c = o + random.uniform(-0.001, 0.001)
            h = max(o, c) + random.uniform(0, 0.0008)
            l = min(o, c) - random.uniform(0, 0.0008)
            candles.append({
                "time": now - timedelta(minutes=step_minutes * i),
                "open": round(o, 5), "high": round(h, 5),
                "low": round(l, 5), "close": round(c, 5),
            })
            price = c
        return candles

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        step = {"M5": 5, "M15": 15, "H1": 60}.get(timeframe, 5)
        return self._gen_candles(symbol, count, step)

    def get_current_price(self, symbol: str) -> tuple[float, float]:
        base = self.base_prices.get(symbol, 1.0)
        spread = 0.0002
        return round(base, 5), round(base + spread, 5)

    def is_market_open(self, symbol: str) -> bool:
        # شبیه‌سازی تقریبی بسته‌بودن بازار فارکس در آخر هفته (شنبه کامل و
        # یکشنبه تا حدود ۲۲:۰۰ UTC) - فقط برای تست؛ کریپتو (BTCUSD) همیشه باز است.
        if "BTC" in symbol.upper():
            return True
        now = datetime.now(timezone.utc)
        if now.weekday() == 5:  # شنبه
            return False
        if now.weekday() == 6 and now.hour < 22:  # یکشنبه قبل از ۲۲:۰۰ UTC
            return False
        return True

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        bid, ask = self.get_current_price(symbol)
        return MarketSnapshot(
            symbol=symbol,
            bid=bid, ask=ask, spread=round(ask - bid, 5),
            market_time_utc=datetime.now(timezone.utc),
            broker_server_time=datetime.now(timezone.utc),
            market_open=self.is_market_open(symbol),
            candles_m5=self._gen_candles(symbol, config.timeframes.m5_candle_count, 5),
            candles_m15=self._gen_candles(symbol, config.timeframes.m15_candle_count, 15),
            candles_h1=self._gen_candles(symbol, config.timeframes.h1_candle_count, 60),
            account_balance=10000.0,
            account_currency="USD",
            symbol_contract_size=100000.0,
            symbol_min_lot=0.01,
            symbol_lot_step=0.01,
            symbol_pip_value=10.0,
            symbol_tick_size=0.00001,
            symbol_tick_value=1.0,
            symbol_max_lot=100.0,
        )
