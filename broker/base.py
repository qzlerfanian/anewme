"""
broker/base.py
---------------
اینترفیس انتزاعی منبع داده بازار. این جداسازی به این دلیل مهم است که:
  1) MetaTrader5 فقط روی ویندوز کار می‌کند.
  2) کارفرما ممکن است بعداً بروکر/API دیگری (مثلاً cTrader یا یک بروکر
     دیگر با REST API) جایگزین کند - در این صورت فقط یک کلاس جدید با همین
     اینترفیس نوشته می‌شود و بقیه کد (تحلیل، تلگرام، Watch) دست‌نخورده می‌ماند.

هر پیاده‌سازی بروکر باید این متدها را فراهم کند.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from core.models import MarketSnapshot


class BrokerBase(ABC):

    @abstractmethod
    def connect(self) -> None:
        ...

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        """بند ۴: نماد، Bid/Ask/Spread، زمان بازار/سرور، کندل‌های M5/M15/H1، وضعیت بازار، حساب."""
        ...

    @abstractmethod
    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        """هر رکورد: {"time": datetime, "open":..., "high":..., "low":..., "close":...}"""
        ...

    @abstractmethod
    def get_current_price(self, symbol: str) -> tuple[float, float]:
        """برمی‌گرداند (bid, ask) - برای مانیتور لحظه‌ای Watch (بند ۱۲/۱۳)."""
        ...

    @abstractmethod
    def is_market_open(self, symbol: str) -> bool:
        ...

    @abstractmethod
    def get_open_positions(self, symbol: str) -> list[dict]:
        """
        پوزیشن‌های باز واقعی روی این نماد - مستقیم از حساب MT5 خوانده
        می‌شود، پس معاملات دستی از موبایل/دسکتاپ هم شناسایی می‌شوند.
        هر رکورد حداقل باید شامل: {"ticket":..., "volume":..., "price_open":...}
        """
        ...

    @abstractmethod
    def get_pending_orders(self, symbol: str) -> list[dict]:
        """
        سفارش‌های Pending باز واقعی روی این نماد - مستقیم از حساب MT5.
        هر رکورد حداقل باید شامل: {"ticket":..., "type":..., "price_open":...}
        """
        ...
