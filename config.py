"""
config.py
---------
تنظیمات مرکزی پروژه ANEWME Bot.
تمام مقادیر قابل تغییر (ریسک، تایمفریم‌ها، مسیرها) اینجا متمرکز شده‌اند
تا هیچ عدد ثابتی (magic number) داخل منطق برنامه پنهان نشود.
مطابق بند ۹ و ۱۸ سند: درصدهای ریسک باید قابل تغییر باشند.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

# مسیر .env صریحاً مشخص شده تا مستقل از Working Directory تنظیم‌شده در
# Run Configuration (پایچارم و غیره) همیشه درست پیدا شود.
load_dotenv(dotenv_path=ENV_FILE)

if not ENV_FILE.exists():
    import warnings
    warnings.warn(
        f"فایل .env پیدا نشد: {ENV_FILE}\n"
        "لطفاً فایل .env.example را کپی و به نام .env در همین مسیر ذخیره کنید، "
        "سپس مقادیر واقعی (OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, ...) را در آن قرار دهید."
    )

DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
RULES_FILE = BASE_DIR / "rules" / "anewme_rules.txt"
DB_PATH = DATA_DIR / "anewme.db"
CHART_TMP_DIR = DATA_DIR / "charts_tmp"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
CHART_TMP_DIR.mkdir(exist_ok=True)


@dataclass
class RiskConfig:
    """
    سقف‌های ریسک طبق منشور رسمی ANEWME V3 (بند ۷: مدیریت ریسک).
    نکته مهم: طبق V3، فقط گریدهای A+ و A اجازه TRADE دارند - B+ هرگز به
    TRADE نمی‌رسد (فقط WATCH). بنابراین سقف ریسک B+ عملاً هرگز استفاده
    نمی‌شود؛ این فیلد صرفاً برای مستندسازی نگه داشته شده.
    این مقادیر «سقف اولیه» هستند و باید در زمان اجرا (مثلاً از طریق دستور
    تلگرام یا فایل تنظیمات) قابل تغییر باشند -> از این رو در دیتابیس هم
    نگهداری می‌شوند (جدول settings) نه فقط این فایل.
    """
    risk_percent_A_plus: float = 1.0     # A+  -> حداکثر ۱٪ (منشور V3 بند ۷)
    risk_percent_A: float = 1.0          # A   -> حداکثر ۱٪ (منشور V3 بند ۷)
    risk_percent_B_plus: float = 0.0     # B+ هرگز TRADE نمی‌شود؛ این مقدار عملاً بی‌اثر است
    risk_percent_friday: float = 0.5     # جمعه -> حداکثر ۰.۵٪ (منشور V3 بند ۷)
    max_risk_percent_hard_cap: float = 1.0  # سقف مطلق ایمنی - هرگز از ۱٪ عبور نکند

    # کنترل ریسک باز کل حساب (منشور V3 بند ۷) - جمع ریسک همزمان باز
    max_daily_open_risk_usd_per_5000: float = 250.0   # حساب ۵٬۰۰۰ دلاری
    max_daily_open_risk_usd_per_10000: float = 500.0  # حساب ۱۰٬۰۰۰ دلاری


@dataclass
class TimeframeConfig:
    """تایمفریم‌های موردنیاز طبق بند ۳."""
    primary_symbol_timeframes: tuple = ("M5", "M15", "H1")
    correlated_symbols: tuple = ("DXY", "USDJPY")  # فقط برای تأیید جانبی، نه فیلتر اجباری

    # تعداد کندل ارسالی به هر تایم‌فریم. برای M5/M15 عدد کم کافی است چون
    # فقط تریگر/تثبیت اخیر مهم است. برای H1 عدد باید بزرگ‌تر باشد تا مدل
    # واقعاً بتواند «ساختار کلی سقف‌ها و کف‌ها» را ببیند (بند ۳.۲ فایل
    # قوانین) نه این‌که جهت را فقط از چند کندل آخر حدس بزند. ۶۰ کندل H1
    # تقریباً معادل ۲.۵ روز معاملاتی است.
    m5_candle_count: int = field(default_factory=lambda: int(os.getenv("M5_CANDLE_COUNT", "20")))
    m15_candle_count: int = field(default_factory=lambda: int(os.getenv("M15_CANDLE_COUNT", "20")))
    h1_candle_count: int = field(default_factory=lambda: int(os.getenv("H1_CANDLE_COUNT", "60")))

    # سقف تعداد کندلی که در لیست متنی (نه تصویر) فرستاده می‌شود - مستقل
    # از h1/m15/m5_candle_count بالا. حتی اگر تعداد کندل تصویر خیلی زیاد
    # باشد، لیست متنی خام همیشه به همین مقدار محدود می‌ماند (کنترل هزینه
    # و توجه مدل - نگاه کنید به core/ai_client.py::_format_market_snapshot)
    max_text_candles: int = field(default_factory=lambda: int(os.getenv("MAX_TEXT_CANDLES", "30")))


@dataclass
class AppConfig:
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_allowed_user_ids: tuple = field(
        default_factory=lambda: tuple(
            int(x) for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip()
        )
    )
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    ai_model: str = os.getenv("AI_MODEL", "gpt-4o")  # باید مدل vision-capable باشد

    # فرکانس مانیتور Watch (بند ۱۲ و ۱۳) - فقط قیمت/کندل چک می‌شود، نه فراخوانی مداوم AI
    watch_poll_interval_seconds: int = int(os.getenv("WATCH_POLL_INTERVAL_SECONDS", "15"))

    risk: RiskConfig = field(default_factory=RiskConfig)
    timeframes: TimeframeConfig = field(default_factory=TimeframeConfig)


config = AppConfig()

# --- ثابت‌های فرمت پاسخ AI (بند ۷ سند رفتاری / بند ۲ منشور V3) ---
ALLOWED_STATUSES = ("TRADE", "WATCH", "NO_TRADE")
ALLOWED_ORDER_TYPES = ("BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP")
# طبق منشور رسمی ANEWME V3 بند ۲: ترتیب کامل گریدها از بهترین به بدترین
ALLOWED_GRADES = ("A+", "A", "A-", "B+", "B", "C")
# طبق قانون غیرقابل مذاکره منشور V3: فقط A+ و A اجازه TRADE دارند.
# B+ حتی اگر جذاب باشد فقط WATCH است؛ A- هم فقط WATCH (یک نقص مؤثر دارد).
GRADES_ALLOWING_TRADE = ("A+", "A")
