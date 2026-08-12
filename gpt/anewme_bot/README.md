# ANEWME Analysis Bot

پیاده‌سازی کامل سند «۲۱ بند» — ربات تحلیل‌گر (نه معامله‌گر). این پروژه
هیچ سفارشی ثبت/مدیریت نمی‌کند؛ فقط تحلیل می‌کند و در تلگرام اعلام می‌کند.

## معماری و نگاشت به بندهای سند

| لایه | فایل | بند(های) مرتبط |
|---|---|---|
| تنظیمات مرکزی و قابل تغییر | `config.py` | ۹, ۱۸ |
| مدل‌های داده / فرمت ثابت | `core/models.py` | ۷ |
| دستورات تلگرام (`/analyze`, `/status`, `/history`) | `telegram_bot/handlers.py` | ۲ |
| دریافت تصاویر و داده بازار | `broker/*`, `charts/chart_generator.py` | ۳, ۴ |
| ارسال قوانین + قالب + داده + تصویر به AI | `core/ai_client.py` | ۵ |
| بارگذاری متن قوانین ANEWME | `core/rules_loader.py`, `rules/anewme_rules.txt` | ۵ |
| پارس فرمت ثابت پاسخ AI | `core/parser.py` | ۶, ۷ |
| خروجی TRADE و درجه‌بندی | `core/models.py`, `core/parser.py` | ۸, ۹, ۱۰ |
| خروجی WATCH | `core/parser.py`, `watch/watch_manager.py` | ۱۱ |
| مانیتور بدون تماس مداوم به AI | `watch/monitor_loop.py` | ۱۲, ۱۳ |
| تحلیل مجدد بعد از Trigger | `core/analysis_service.py` | ۱۴ |
| Watch چندمرحله‌ای (جایگزینی) | `core/analysis_service.py` | ۱۵ |
| خروجی NO_TRADE | `core/parser.py`, `core/analysis_service.py` | ۱۶ |
| اعتبارسنجی نهایی قبل از ارسال TRADE | `core/validator.py` | ۱۷ |
| محاسبه ریسک و حجم | `core/risk_manager.py` | ۱۸ |
| جلوگیری از Trigger/ارسال تکراری | `watch/watch_manager.py` (lock/triggered flags) | ۱۹ |
| انقضا، ابطال، لاگ رویدادها، سابقه | `storage/db.py` | ۲۰ |
| هماهنگ‌کننده کل جریان + محدودیت اجرای دستی | `core/analysis_service.py`, `main.py` | ۲۱ |

نکته معماری مهم: **هیچ تابعی برای ثبت/بستن/تغییر سفارش در کل پروژه وجود
ندارد** — نه در broker، نه در core، نه در telegram_bot. این یک تصمیم
عمدی برای انطباق قطعی با بند ۲۱ است، نه صرفاً یک قرارداد مستندسازی.

## پیش‌نیاز مهم قبل از اجرا

سند ۲۱ بندی، **رفتار سیستم** را توصیف می‌کند، نه **استراتژی معاملاتی
ANEWME** (چه چیزی سیگنال است، معیار هر Grade، ساختار بازار و ...).
طبق بند ۵، متن کامل آن استراتژی باید در فایل زیر قرار بگیرد:

```
rules/anewme_rules.txt
```

بدون این فایل، برنامه در ابتدای هر تحلیل خطای صریح `RulesNotConfiguredError`
می‌دهد (به‌جای این‌که خاموش با قوانین ناقص کار کند).

## نصب

```bash
python -m venv venv
source venv/bin/activate   # ویندوز: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# سپس .env را با مقادیر واقعی پر کنید
```

`TELEGRAM_ALLOWED_USER_IDS` در اجرای واقعی الزامی است. ربات در نبود
whitelist به‌صورت پیش‌فرض اجرا نمی‌شود؛ `ALLOW_UNRESTRICTED_TELEGRAM=true`
فقط برای تست محلی در نظر گرفته شده است.

## اجرا

```bash
python main.py
```

در تلگرام، دستور `/symbols` و پیام `/start` چهار دکمه آماده برای
`/analyze EURUSD`، `/analyze GBPUSD`، `/analyze XAUUSD` و
`/analyze USDJPY` نمایش می‌دهند. کلیک روی هر دکمه همان مسیر امن دستور
`/analyze SYMBOL` را اجرا می‌کند.

## کنترل‌های ایمنی اجرایی

- داده MT5 فقط از کندل‌های بسته‌شده خوانده می‌شود؛ کندل position صفر وارد تحلیل یا Trigger نمی‌شود.
- Expiration و Invalidation، Watch را مستقیم و بدون تماس دوباره با AI می‌بندند.
- حجم با tick size/value واقعی بروکر و گردکردن رو به پایین محاسبه می‌شود.
- TRADE با RR، ریسک، زمان، جهت سفارش یا حجم نامعتبر به NO_TRADE تبدیل می‌شود.
- چارت‌های موقت پس از `CHART_RETENTION_HOURS` پاک می‌شوند.
- اگر TP و SL در یک کندل لمس شوند، نتیجه `AMBIGUOUS` ثبت می‌شود و برد/باخت حدس زده نمی‌شود.

## چرخه مدیریت Watch

- پیش از snapshot، چارت و AI، پوزیشن‌ها و سفارش‌های Pending همان نماد مستقیماً از MT5 بررسی می‌شوند.
- برای هر نماد فقط یک Watch فعال مجاز است؛ این محدودیت هم در کد و هم با index دیتابیس تضمین می‌شود.
- اجرای دوباره `/analyze SYMBOL` در زمان وجود Watch، تحلیل جدید انجام نمی‌دهد و همان Trigger، Level، Invalidation و Expiration قبلی را نمایش می‌دهد.
- Watch فقط با یکی از وضعیت‌های `TRIGGERED`، `INVALIDATED` یا `EXPIRED` بسته می‌شود.
- پس از Trigger، رکورد قبلی قبل از تحلیل خودکار بسته می‌شود. خروجی WATCH فقط وقتی ستاپ متفاوتی باشد رکورد جدید می‌سازد.
- زمان بررسی کندل، شروع و پایان تحلیل مجدد، نتیجه و وضعیت ارسال یا سرکوب اعلان در دیتابیس و `events_log` ثبت می‌شود.
- انتقال `ACTIVE -> TRIGGERED` با یک UPDATE شرطی اتمیک انجام می‌شود؛ فقط worker برنده اجازه ارسال پیام و اجرای reanalysis دارد.
- هر کندل بسته برای هر Watch با claim دیتابیسی فقط یک‌بار بررسی می‌شود، حتی اگر چند poll یا process هم‌زمان اجرا شوند.
- اگر reanalysis فوری همان جهت، Trigger و Level واچ تریگرشده را بازتولید کند، ساخت Watch جدید و اعلان تکراری سرکوب می‌شود؛ تحلیل‌های مستقل بعدی همچنان می‌توانند ستاپ تازه بسازند.

نکته پلتفرم: پکیج `MetaTrader5` فقط روی **ویندوز** کار می‌کند (محدودیت
خود آن پکیج، نه این پروژه). برای اجرای Production روی یک VPS ویندوزی با
MT5 نصب‌شده اجرا کنید. برای توسعه/تست روی لینوکس، از `broker/mock_broker.py`
استفاده کنید (در `main.py` جایگزین `build_broker()` کنید).

## تست بدون نیاز به تلگرام/AI واقعی

هسته منطقی (parser, validator, risk_manager, watch_manager,
analysis_service) کاملاً از تلگرام و شبکه واقعی جداست و با
`broker/mock_broker.py` و یک `AIClient` ساختگی (`unittest.mock.MagicMock`)
قابل تست است. نمونه:

```python
from unittest.mock import MagicMock
from broker.mock_broker import MockBroker
from core.analysis_service import AnalysisService

fake_ai = MagicMock()
fake_ai.request_analysis.return_value = "Analysis Time: ...\nSymbol: EURUSD\nStatus: NO_TRADE\n..."

svc = AnalysisService(broker=MockBroker(), ai_client=fake_ai)
result = svc.run_initial_analysis("EURUSD")
```

## نقاط توسعه بعدی (پیشنهادی، نه الزامی سند)

- `broker/rest_broker.py`: اگر بروکر روی لینوکس/Docker باید اجرا شود، یک
  اکسپرت مشاور MT5 روی یک VPS ویندوزی REST سرور بالا بیاورد و اینجا فقط
  یک HTTP client نوشته شود؛ بقیه کد دست‌نخورده می‌ماند (به همین دلیل
  `BrokerBase` انتزاعی طراحی شده).
- افزودن دستور تلگرام برای تغییر زنده درصدهای ریسک (`storage.db.set_setting`
  از قبل آماده است، فقط handler آن باقی مانده).
- تست‌های خودکار (`pytest`) بر پایه سناریوهای دستی که در توسعه اجرا شد.
