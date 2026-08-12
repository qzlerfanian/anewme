"""
core/models.py
--------------
مدل‌های داده مشترک بین تمام لایه‌ها.
این مدل‌ها مستقیماً بازتاب فرمت ثابت پاسخ AI هستند (بند ۷ سند) تا هیچ‌جای
کد مجبور به «حدس زدن» ساختار پاسخ نباشد.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class AnalysisStatus(str, Enum):
    TRADE = "TRADE"
    WATCH = "WATCH"
    NO_TRADE = "NO_TRADE"


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    BUY_LIMIT = "BUY_LIMIT"
    SELL_LIMIT = "SELL_LIMIT"
    BUY_STOP = "BUY_STOP"
    SELL_STOP = "SELL_STOP"


class Grade(str, Enum):
    """
    طبق منشور رسمی ANEWME V3 بند ۲ (درجه‌بندی ستاپ)، از بهترین به بدترین:
      A+  -> ستاپ بسیار تمیز، هم‌جهت و کامل -> TRADE
      A   -> ستاپ معتبر با تمام تأییدهای لازم -> TRADE
      A-  -> یک نقص مؤثر دارد، ورود ممنوع -> WATCH
      B+  -> نزدیک به ستاپ اما کافی نیست -> WATCH
      B   -> صرفاً قابل تحلیل، بدون مزیت اجرایی -> NO_TRADE
      C   -> ساختار بی‌کیفیت یا نامعتبر -> NO_TRADE
    """
    A_PLUS = "A+"
    A = "A"
    A_MINUS = "A-"
    B_PLUS = "B+"
    B = "B"
    C = "C"
    WEAK = "WEAK"  # fallback داخلی وقتی گرید از پاسخ AI قابل تشخیص نیست


# طبق «قانون غیرقابل مذاکره» منشور V3: فقط A+ و A اجازه TRADE دارند.
# B+ و A- حتی اگر جذاب باشند فقط WATCH هستند - این تفاوت میان تحلیل خوب
# و معامله مجاز است (نقل مستقیم از منشور، بند «قاعده قطعی درجه‌بندی»).
GRADES_ALLOWING_TRADE = {Grade.A_PLUS, Grade.A}


@dataclass
class MarketSnapshot:
    """اطلاعات عددی همراه تصاویر - بند ۴."""
    symbol: str
    bid: float
    ask: float
    spread: float
    market_time_utc: datetime
    broker_server_time: datetime
    market_open: bool
    candles_m5: list = field(default_factory=list)   # هرکدام dict با open/high/low/close/time
    candles_m15: list = field(default_factory=list)
    candles_h1: list = field(default_factory=list)
    account_balance: Optional[float] = None
    account_currency: Optional[str] = None
    symbol_contract_size: Optional[float] = None
    symbol_min_lot: Optional[float] = None
    symbol_lot_step: Optional[float] = None
    symbol_pip_value: Optional[float] = None
    # مشخصات خام بروکر برای محاسبه ریسک بدون حدس‌زدن اندازه pip.
    symbol_tick_size: Optional[float] = None
    symbol_tick_value: Optional[float] = None
    symbol_max_lot: Optional[float] = None


@dataclass
class TradeDetails:
    """بند ۸ - خروجی TRADE."""
    order_type: OrderType
    entry: float
    stop_loss: float
    take_profit: float
    risk_percent: float
    suggested_volume: Optional[float]
    reward_risk_ratio: float
    expiration: str
    invalidation: str
    short_reason: str
    checklist_complete: bool


@dataclass
class WatchDetails:
    """بند ۱۱ - خروجی WATCH."""
    preferred_direction: Direction
    current_or_potential_grade: Grade
    watch_reason: str
    trigger_type: str          # یکی از موارد بند ۱۳
    exact_zone_or_level: str
    timeframes_to_recheck: list
    expiration: str
    invalidation: str


@dataclass
class AnalysisResult:
    """
    نتیجه یکپارچه‌شده و پارس‌شده تحلیل AI - انعکاس دقیق بند ۷.
    """
    analysis_time: datetime
    symbol: str
    status: AnalysisStatus
    direction: Optional[Direction]
    grade: Optional[Grade]
    reason: str
    timeframes_checked: list
    trade_details: Optional[TradeDetails] = None
    watch_details: Optional[WatchDetails] = None
    raw_ai_text: str = ""       # متن خام پاسخ AI برای ثبت در سابقه (بند ۲۰)
    # اگر True باشد، این نتیجه هنوز در دیتابیس ذخیره و لاگ می‌شود (بند ۲۰)
    # اما به تلگرام ارسال نمی‌شود - برای وقتی که تحلیل مجدد نشان می‌دهد
    # ستاپ واقعاً تغییری نکرده (رفع باگ ارسال مکرر Watch یکسان)
    suppress_notification: bool = False
    # اگر پوزیشن باز یا سفارش Pending واقعی روی این نماد در MT5 پیدا شود،
    # این فیلد به "OPEN_POSITION" یا "PENDING_ORDER" ست می‌شود و یعنی
    # Status/Grade فقط جنبه‌ی مانیتور دارند، نه سیگنال ورود جدید.
    account_state: Optional[str] = None
    account_state_details: list = field(default_factory=list)
    # ساعت آخرین کندل M5 بسته‌شده - برای شفافیت در پیام خروجی
    last_closed_m5_time: Optional[str] = None


@dataclass
class WatchState:
    """
    وضعیت نگهداری‌شده یک Watch فعال در دیتابیس/حافظه - بند ۱۲ تا ۱۵ و ۱۹.
    """
    watch_id: str
    symbol: str
    parent_analysis_id: Optional[str]     # برای Watch چندمرحله‌ای (بند ۱۵)
    direction: Direction
    grade: Grade
    trigger_type: str
    zone_or_level: str
    timeframes_to_recheck: list
    expiration: datetime
    invalidation_condition: str
    created_at: datetime
    is_locked: bool = False               # قفل هنگام بررسی مجدد (بند ۱۹)
    is_triggered: bool = False            # جلوگیری از Trigger تکراری (بند ۱۹)
    is_closed: bool = False
    close_status: Optional[str] = None
    closed_at: Optional[datetime] = None
