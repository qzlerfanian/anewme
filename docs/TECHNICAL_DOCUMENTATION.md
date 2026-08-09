# مستند فنی — ربات تحلیل ANEWME V3

نسخه مستند: مطابق کد نهایی تحویل‌شده (بازنویسی ai_client با معماری دو مرحله‌ای)

---

## ۱. این پروژه چیست؟

یک ربات تلگرامی که:
1. با دستور `/analyze SYMBOL` شروع به کار می‌کند.
2. داده‌های بازار (قیمت، کندل‌ها، اطلاعات حساب) را از MetaTrader5 می‌گیرد.
3. چارت تمیز (بدون اندیکاتور) از تایم‌فریم‌های M5/M15/H1 (و DXY/USDJPY در صورت نیاز) می‌سازد.
4. تصاویر + قوانین کامل استراتژی «ANEWME V3» + داده بازار را به یک مدل هوش‌مصنوعی (OpenAI GPT-4o) می‌فرستد.
5. پاسخ مدل را به یکی از سه وضعیت **TRADE / WATCH / NO_TRADE** تبدیل، اعتبارسنجی و در تلگرام اعلام می‌کند.
6. در حالت WATCH، به‌صورت خودکار قیمت را زیر نظر می‌گیرد و در صورت فعال‌شدن شرط، تحلیل را مجدداً انجام می‌دهد.

**محدودیت قطعی و عمدی معماری:** این ربات **هیچ سفارشی ثبت، ویرایش یا مدیریت نمی‌کند**. تمام تصمیم‌ها و اجرای معامله همیشه دستی و توسط کاربر است. این محدودیت در سطح کد اعمال شده — در هیچ فایلی تابعی برای ارسال سفارش به بروکر وجود ندارد.

---

## ۲. اسناد پایه‌ای که این پیاده‌سازی از روی آن‌ها ساخته شده

| سند | نقش |
|---|---|
| سند «۲۱ بند» (کارفرما) | رفتار سیستم: چه زمانی چارت بگیرد، چه چیزی به تلگرام بفرستد، چرخه Watch چگونه باشد |
| منشور «ANEWME V3» | خودِ استراتژی معاملاتی: معیار هر Grade، فیلتر دلار، قوانین ورود، مدیریت ریسک |

نکته مهم: منشور V3 با بخشی از سند ۲۱ بندی تناقض داشت (سند ۲۱ بندی می‌گفت B+ هم می‌تواند TRADE شود؛ منشور V3 صریحاً می‌گوید فقط A و A+ اجازه TRADE دارند). چون منشور V3 «نسخه فعال و اصلاح‌شده» عنوان شده، **این پیاده‌سازی از منشور V3 پیروی می‌کند**، نه از سند ۲۱ بندی، در مواردی که این دو تناقض دارند.

---

## ۳. معماری کلی (لایه‌ها)

```
main.py  (نقطه ورود - wiring همه‌چیز)
   │
   ├── telegram_bot/   → دریافت دستور از کاربر، ارسال نتیجه به کاربر
   ├── core/           → منطق تحلیل: AI، پارس، اعتبارسنجی، ریسک
   ├── broker/         → منبع داده بازار (MT5 واقعی / Mock برای تست)
   ├── charts/         → تولید تصویر چارت
   ├── watch/          → چرخه حیات Watch و مانیتور خودکار
   └── storage/        → دیتابیس SQLite (سوابق، Watchها، رویدادها)
```

اصل طراحی: هر لایه فقط از طریق یک اینترفیس مشخص با لایه‌های دیگر حرف می‌زند، طوری که تعویض هرکدام (مثلاً بروکر یا ارائه‌دهنده AI) بدون تغییر بقیه کد ممکن باشد. این در عمل هم تست شد: وقتی از Anthropic به OpenAI عوض شد، فقط فایل `core/ai_client.py` تغییر کرد.

---

## ۴. جریان کامل یک تحلیل (Sequence)

```
کاربر در تلگرام: /analyze EURUSD
        │
        ▼
telegram_bot/handlers.py :: analyze_command
        │  (تأیید فوری دریافت دستور به کاربر)
        ▼
core/analysis_service.py :: AnalysisService.run_initial_analysis
        │
        ├─► broker.get_market_snapshot()          [قیمت، کندل، حساب]
        ├─► charts/chart_generator.py              [تولید PNG چارت‌ها]
        │
        ├─► core/ai_client.py :: describe_all_charts       (مرحله ۱ - توصیف بصری هر تصویر)
        ├─► core/ai_client.py :: request_analysis           (مرحله ۲ - تصمیم‌گیری متنی نهایی)
        │
        ├─► core/parser.py :: parse_ai_response             [متن خام → AnalysisResult]
        ├─► core/validator.py :: validate_trade_result      [فقط اگر TRADE بود]
        ├─► core/risk_manager.py :: calculate_position_size [فقط اگر TRADE معتبر بود]
        ├─► watch/watch_manager.py                          [فقط اگر WATCH بود]
        ├─► storage/db.py :: save_analysis, log_event
        │
        ▼
telegram_bot/notifier.py :: format_analysis_message
        │
        ▼
پیام نهایی در تلگرام (TRADE / WATCH / NO_TRADE)
```

اگر نتیجه WATCH بود، `watch/monitor_loop.py` هر چند ثانیه (تنظیم‌پذیر) قیمت را چک می‌کند؛ با فعال‌شدن Trigger، دوباره `AnalysisService.run_watch_recheck` صدا زده می‌شود که همین مسیر بالا را (با تصاویر تازه) تکرار می‌کند.

---

## ۵. معماری دو مرحله‌ای هوش مصنوعی — چرا این‌طور طراحی شد

در توسعه مشخص شد که وقتی تصویر چارت + دستور قطعی «باید TRADE/WATCH/NO_TRADE با Entry/SL/TP دقیق بدهی» با هم در یک درخواست به مدل فرستاده می‌شوند، مدل گاهی (به‌صورت غیرقطعی و تصادفی، نه همیشه) درخواست را رد می‌کند. با تست جداگانه مشخص شد:
- فقط تصویر + دستور خنثی توصیفی → همیشه پایدار
- فقط متن (بدون تصویر) + قوانین کامل + دستور تصمیم‌گیری → همیشه پایدار
- تصویر + قوانین + دستور تصمیم‌گیری با هم → گاه‌به‌گاه رد می‌شود

راه‌حل: این دو کار از هم جدا شدند.

- **مرحله ۱ (`AIClient.describe_chart_image`)**: هر تصویر را جدا با پرامپت کاملاً خنثی (`IMAGE_DESCRIPTION_PROMPT`) می‌فرستد — بدون هیچ کلمه TRADE/BUY/SELL/Entry — و فقط توصیف بصری عینی می‌گیرد.
- **مرحله ۲ (`AIClient.request_analysis`)**: توصیف‌های متنی حاصل از مرحله ۱ را (نه تصویر مستقیم) به‌همراه داده بازار و متن کامل قوانین ANEWME V3 می‌فرستد و تصمیم نهایی را در قالب ثابت می‌گیرد.

علاوه بر این، `AIClient._call_with_retry` هر تماس (چه توصیف تصویر، چه تصمیم‌گیری) را در صورت تشخیص «رد شدن» تا ۳ بار با ۱.۵ ثانیه فاصله دوباره امتحان می‌کند، چون این رفتار در عمل تصادفی بود نه قطعی.

---

## ۶. شرح فایل‌به‌فایل

### `config.py`
تنظیمات مرکزی و تنها منبع اعداد قابل‌تغییر (ریسک، تایم‌فریم‌ها، مسیرها).

| عنصر | نقش |
|---|---|
| `class RiskConfig` | سقف‌های ریسک: A+/A=۱٪، جمعه=۰.۵٪، سقف مطلق ایمنی=۱٪ (طبق منشور V3 بند ۷) |
| `class TimeframeConfig` | تایم‌فریم‌های موردنیاز نماد اصلی و نمادهای همبسته (DXY/USDJPY) |
| `class AppConfig` | توکن تلگرام، لیست کاربران مجاز، کلید OpenAI، نام مدل، فاصله زمانی مانیتور Watch |
| `ENV_FILE` | مسیر دقیق فایل `.env` (صریح تعریف شده تا مستقل از Working Directory همیشه پیدا شود) |
| `ALLOWED_GRADES` / `GRADES_ALLOWING_TRADE` | فقط `A+` و `A` اجازه TRADE دارند (طبق منشور V3) |

### `core/models.py`
مدل‌های داده (dataclass/Enum) که بازتاب دقیق فرمت ثابت پاسخ AI هستند.

| عنصر | نقش |
|---|---|
| `AnalysisStatus` | Enum: `TRADE`, `WATCH`, `NO_TRADE` |
| `Direction` | Enum: `BUY`, `SELL` |
| `OrderType` | Enum: فقط ۴ سفارش Pending مجاز (`BUY_LIMIT`, `SELL_LIMIT`, `BUY_STOP`, `SELL_STOP`) |
| `Grade` | Enum: `A+, A, A-, B+, B, C` طبق درجه‌بندی منشور V3 |
| `MarketSnapshot` | عکس لحظه‌ای بازار: قیمت، اسپرد، کندل‌ها، اطلاعات حساب و نماد |
| `TradeDetails` | جزئیات کامل یک سفارش پیشنهادی TRADE |
| `WatchDetails` | جزئیات یک وضعیت WATCH (جهت، Trigger، سطح، انقضا) |
| `AnalysisResult` | نتیجه یکپارچه و پارس‌شده یک تحلیل کامل |
| `WatchState` | وضعیت نگهداری‌شده یک Watch فعال (شامل فلگ قفل/تریگرشده/بسته) |

### `core/rules_loader.py`
| تابع | نقش |
|---|---|
| `load_anewme_rules()` | خواندن متن کامل `rules/anewme_rules.txt` (کش‌شده با `lru_cache`)؛ اگر فایل خالی/غایب باشد خطای صریح `RulesNotConfiguredError` می‌دهد |
| `invalidate_cache()` | پاک‌کردن کش برای ری‌لود دستی |

### `core/ai_client.py`
واسط با OpenAI API. تنها فایلی که مستقیماً با ارائه‌دهنده AI حرف می‌زند.

| عنصر | نقش |
|---|---|
| `IMAGE_DESCRIPTION_PROMPT` | پرامپت خنثی مرحله ۱ (توصیف بصری، بدون کلمه معاملاتی) |
| `DECISION_INSTRUCTIONS` | پرامپت مرحله ۲ (تصمیم‌گیری + فرمت ثابت پاسخ) |
| `AIClient.__init__` | ساخت کلاینت OpenAI؛ خطای صریح اگر `OPENAI_API_KEY` نباشد (با نشان‌دادن مسیر `.env` موردانتظار) |
| `_looks_like_refusal(text, finish_reason)` | تشخیص این‌که آیا پاسخ مدل یک نوع رد کردن/امتناع است |
| `_call_with_retry(messages, max_tokens, label)` | فراخوانی عمومی مدل با retry خودکار (حداکثر ۳ بار) روی تشخیص رد شدن |
| `describe_chart_image(image_path, symbol, timeframe_label)` | مرحله ۱ برای یک تصویر: توصیف بصری خنثی |
| `describe_all_charts(chart_paths, symbol)` | مرحله ۱ برای همه تصاویر یک تحلیل؛ ترکیب توصیف‌ها در یک متن |
| `_format_market_snapshot(snapshot)` | تبدیل `MarketSnapshot` به متن خوانا برای پرامپت |
| `_format_previous_watch(watch)` | تبدیل Watch قبلی (در تحلیل مجدد) به متن برای پرامپت |
| `request_analysis(symbol, chart_paths, snapshot, previous_watch)` | نقطه ورود اصلی: مرحله ۱ + مرحله ۲ را پشت‌سرهم اجرا و متن خام پاسخ نهایی را برمی‌گرداند |

### `core/parser.py`
پارس سخت‌گیر فرمت ثابت پاسخ AI به `AnalysisResult`. هیچ فیلد گمشده‌ای حدس زده نمی‌شود؛ به‌جایش `AIResponseParseError` صادر می‌شود.

| عنصر | نقش |
|---|---|
| `AIResponseParseError` | استثنای مخصوص شکست پارس |
| `_normalize_ai_text(text)` | حذف `**Bold**`، بولت‌ها و بلاک‌های کد که GPT با وجود دستور صریح گاهی اضافه می‌کند |
| `_extract_field(text, field_name, required)` | استخراج مقدار یک فیلد با regex؛ خطا اگر الزامی و غایب باشد |
| `_parse_bool` / `_parse_list` / `_parse_float` | تبدیل مقدار متنی خام به نوع مناسب |
| `parse_ai_response(raw_text, expected_symbol)` | تابع اصلی: خروجی کامل `AnalysisResult`؛ نماد پاسخ را با نماد درخواستی چک می‌کند؛ عبارات مبهم WATCH (مثل «بعداً بررسی شود») را رد می‌کند |

### `core/validator.py`
آخرین خط دفاعی قبل از ارسال TRADE به کاربر (بند ۱۷ سند رفتاری).

| عنصر | نقش |
|---|---|
| `ValidationOutcome` | نتیجه اعتبارسنجی: معتبر/نامعتبر + لیست دلایل رد |
| `_risk_cap_for_grade(grade, is_friday)` | سقف ریسک مجاز بر اساس گرید و روز هفته |
| `validate_trade_result(result, snapshot)` | چک کامل: نوع سفارش Pending، سازگاری Entry/SL/TP با جهت، Reward/Risk معتبر، سقف ریسک، گرید مجاز TRADE (فقط A/A+)، چک‌لیست کامل، تازگی داده (کمتر از ۵ دقیقه)، بازار باز، Expiration موجود. هر شکست → تبدیل به NO_TRADE با دلیل دقیق، نه ارسال اشتباه |

### `core/risk_manager.py`
محاسبه حجم پیشنهادی — همیشه توسط کد، نه AI (چون AI به مشخصات دقیق حساب/بروکر دسترسی امن ندارد).

| عنصر | نقش |
|---|---|
| `VolumeCalculationResult` | حجم پیشنهادی + مبلغ ریسک + هشدار احتمالی |
| `calculate_position_size(trade, snapshot)` | فرمول: `risk_amount = balance × risk% ÷ 100`، تقسیم بر فاصله پیپ SL و ارزش پیپ، گرد کردن به `lot_step` بروکر؛ اگر حداقل حجم بروکر باعث عبور از سقف ریسک شود، حجمی پیشنهاد نمی‌شود (فقط هشدار) |
| `_infer_pip_size(symbol)` | تخمین اندازه پیپ بر اساس نوع نماد (fallback ساده؛ در تولید واقعی بهتر از broker خوانده شود) |

### `core/analysis_service.py`
مغز هماهنگ‌کننده — دقیقاً جریان بند ۲۱ سند رفتاری را پیاده می‌کند. **هیچ متد ثبت سفارشی در این فایل وجود ندارد** (تصمیم معماری عمدی).

| عنصر | نقش |
|---|---|
| `AnalysisService.__init__(broker, ai_client)` | تزریق وابستگی‌ها (برای تست با Mock/MagicMock) |
| `run_initial_analysis(symbol, needs_correlated_symbols)` | تحلیل اولیه از `/analyze`: گرفتن snapshot، ساخت چارت، فراخوانی AI، `_finalize` |
| `run_watch_recheck(watch_row)` | تحلیل مجدد بعد از فعال‌شدن Trigger یک Watch |
| `_build_charts(...)` | تولید چارت‌های نماد اصلی + DXY/USDJPY در صورت نیاز |
| `_finalize(...)` | پارس → اعتبارسنجی (اگر TRADE) → محاسبه حجم → مدیریت باز/بسته‌شدن Watch (شامل منطق چندمرحله‌ای) → ذخیره در دیتابیس → لاگ رویداد |
| `_row_to_watch_state(row)` | تبدیل ردیف دیتابیس Watch به شیء `WatchState` |
| `_dataclass_or_none` / `_snapshot_to_dict` | کمکی برای serialize کردن قبل از ذخیره در DB |

### `broker/base.py`
اینترفیس انتزاعی منبع داده بازار (`connect`, `disconnect`, `get_market_snapshot`, `get_candles`, `get_current_price`, `is_market_open`). هر پیاده‌سازی جدید (مثلاً یک بروکر دیگر) فقط باید این اینترفیس را رعایت کند.

### `broker/mt5_broker.py`
پیاده‌سازی واقعی با پکیج `MetaTrader5` (فقط ویندوز، نیاز به ترمینال MT5 لاگین‌شده). تمام متدهای `BrokerBase` را با فراخوانی توابع `MetaTrader5` پیاده می‌کند.

### `broker/mock_broker.py`
پیاده‌سازی آزمایشی با داده‌های تصادفی ساختاریافته — برای توسعه/تست بدون نیاز به MT5 واقعی. **هرگز در Production استفاده نشود.**

### `charts/chart_generator.py`
تولید تصویر چارت تمیز با `mplfinance` — فقط کندل، بدون هیچ اندیکاتوری (بند ۳ سند / استاندارد تصویر منشور V3).

| تابع | نقش |
|---|---|
| `_candles_to_dataframe(candles)` | تبدیل لیست dict کندل به `DataFrame` سازگار با mplfinance |
| `generate_clean_chart(symbol, timeframe_label, candles, watch_zone, watch_level)` | تولید یک PNG؛ فقط اگر `watch_zone`/`watch_level` داده شود خط افقی (تنها استثنای مجاز) رسم می‌شود |
| `generate_required_charts(...)` | تولید همه چارت‌های موردنیاز یک تحلیل (نماد اصلی M5/M15/H1 + DXY/USDJPY در صورت نیاز) |

### `storage/db.py`
لایه SQLite برای سوابق، Watchها، رویدادها و خطاها (بند ۲۰).

| تابع | نقش |
|---|---|
| `get_connection()` | context manager اتصال SQLite |
| `init_db()` | اجرای schema (جداول analyses، watches، events_log، errors_log، settings) |
| `save_analysis(...)` | ذخیره یک تحلیل کامل (شامل مسیر تصاویر و JSON کامل snapshot/trade/watch) |
| `get_history(symbol, limit)` | آخرین N تحلیل (برای `/history`) |
| `save_watch(watch)` | ثبت یک Watch جدید |
| `get_active_watches()` | همه Watchهای هنوز بسته‌نشده (برای مانیتور و `/status`) |
| `update_watch_flags(...)` | تغییر فلگ‌های is_locked/is_triggered/is_closed |
| `log_event` / `log_error` | ثبت رویداد یا خطا برای ردیابی بعدی |
| `get_setting` / `set_setting` | تنظیمات قابل‌تغییر در زمان اجرا (مثلاً درصد ریسک) بدون نیاز به ری‌استارت |

### `watch/watch_manager.py`
چرخه حیات یک Watch (بند ۱۲ تا ۱۵ و ۱۹) — مستقل از AI/تلگرام برای قابلیت تست جدا.

| تابع | نقش |
|---|---|
| `create_watch_from_details(symbol, watch_details, parent_analysis_id)` | ثبت Watch جدید در دیتابیس از خروجی WATCH مدل |
| `_parse_expiration(expiration_text)` | پارس زمان انقضا (ISO یا `HH:MM`)؛ در صورت شکست، ۴ ساعت پیش‌فرض (Watch هرگز بدون انقضا نمی‌ماند) |
| `check_trigger(watch_row, broker)` | چک کردن این‌که آیا Trigger این Watch فعال شده (ورود به محدوده، رسیدن به سطح، بسته‌شدن کندل M5/M15، یا رسیدن انقضا)؛ Watchهای قفل/تریگرشده/بسته را نادیده می‌گیرد |
| `_extract_levels(text)` | استخراج عدد(های) سطح/محدوده از متن آزاد Zone Or Level |
| `lock_watch` / `unlock_watch` | قفل کردن حین بررسی مجدد (بند ۱۹) |
| `mark_triggered(watch_id, reason)` | جلوگیری از پردازش تکراری همان Trigger |
| `close_watch(watch_id, reason)` | بستن نهایی Watch |

### `watch/monitor_loop.py`
حلقه پس‌زمینه‌ای که **بدون تماس مداوم به AI**، فقط قیمت/کندل چک می‌کند (بند ۱۲).

| عنصر | نقش |
|---|---|
| `WatchMonitor.__init__(broker, analysis_service, notify)` | تزریق وابستگی + callback ارسال پیام تلگرام |
| `start()` | حلقه بی‌نهایت با فاصله `config.watch_poll_interval_seconds`؛ خطای هر دور جلوی توقف حلقه را نمی‌گیرد |
| `stop()` | توقف حلقه |
| `_tick()` | یک دور بررسی: برای هر Watch فعال، `check_trigger`؛ اگر فعال شد → قفل + علامت‌گذاری + اطلاع فوری در تلگرام + `analysis_service.run_watch_recheck` + ارسال نتیجه نهایی |
| `_get_watch_row(watch_id)` | خواندن مجدد یک ردیف Watch بعد از قفل شدن |

### `telegram_bot/handlers.py`
دستورات تلگرام (بند ۲).

| تابع | نقش |
|---|---|
| `_is_authorized(update)` | چک لیست سفید کاربران مجاز |
| `start_command` | پیام خوش‌آمد و راهنمای دستورات |
| `analyze_command` | دریافت `/analyze SYMBOL`؛ تأیید فوری + اجرای `AnalysisService.run_initial_analysis` + ارسال نتیجه |
| `status_command` | نمایش Watchهای فعال فعلی |
| `history_command` | نمایش آخرین تحلیل‌ها (کل یا برای یک نماد خاص) |
| `unknown_command` | راهنما برای دستور نامعتبر |

### `telegram_bot/notifier.py`
قالب‌بندی پیام‌های خروجی — بدون هیچ منطق تصمیم‌گیری.

| تابع | نقش |
|---|---|
| `format_analysis_message(result)` | تبدیل `AnalysisResult` به پیام تلگرام خوانا (شامل جزئیات TRADE یا WATCH و یادآوری «ثبت دستی») |
| `format_error_message(context, error, symbol)` | قالب پیام خطا |

### `main.py`
نقطه ورود؛ wiring همه سرویس‌ها.

| عنصر | نقش |
|---|---|
| `build_broker()` | انتخاب بروکر بر اساس پلتفرم (فقط ویندوز → `MT5Broker` واقعی؛ در غیر این‌صورت خطای صریح) |
| `run()` | راه‌اندازی دیتابیس، بروکر، `AIClient`، `AnalysisService`، اپلیکیشن تلگرام (ثبت هندلرها)، و اجرای هم‌زمان `WatchMonitor.start()` تا وقتی برنامه متوقف شود |

---

## ۷. فایل‌های کمکی/تشخیصی (غیر از معماری اصلی)

این‌ها بخشی از معماری تولید نیستند، فقط برای دیباگ در زمان توسعه ساخته شدند:

- `test_manual.py` — تست دستی end-to-end با Mock Broker + AI واقعی
- `diagnose_refusal.py` — تشخیص رد شدن مدل: با/بدون قوانین، با/بدون تصویر
- `diagnose_image_refusal.py` — تشخیص دقیق‌تر نقش تصویر در رد شدن

می‌توانید این سه فایل را نگه دارید (برای دیباگ آینده) یا حذف کنید؛ در مسیر اصلی برنامه (`main.py`) استفاده نمی‌شوند.

---

## ۸. محدودیت‌های شناخته‌شده (هنوز پیاده‌سازی نشده)

این‌ها آگاهانه پیاده نشده‌اند چون نیاز به منبع داده/API اضافه دارند که هنوز تصمیم‌گیری نشده:

1. **کنترل مجموع ریسک باز روزانه + هم‌بستگی پوزیشن‌های دلاری** (منشور V3 بند ۷) — نیاز به خواندن پوزیشن‌های واقعی باز از MT5 و جمع‌زدن ریسک هم‌جهت روی جفت‌های دلاری دارد.
2. **فیلتر خبر خودکار** (منشور V3 بند ۹) — نیاز به یک API تقویم اقتصادی (مثلاً ForexFactory) دارد؛ فعلاً مدل بر اساس دانش عمومی خودش تخمین می‌زند.

هر دو به‌عنوان یادداشت فنی داخل خودِ `rules/anewme_rules.txt` هم به مدل گفته شده تا شفاف باشد این کنترل‌ها فعلاً خودکار نیستند.

---

## ۹. تنظیمات محیطی (`.env`)

| متغیر | نقش |
|---|---|
| `TELEGRAM_BOT_TOKEN` | توکن بات از BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | آیدی عددی کاربران مجاز (کاما-جدا) |
| `OPENAI_API_KEY` | کلید OpenAI |
| `AI_MODEL` | باید vision-capable باشد (پیش‌فرض `gpt-4o`) |
| `WATCH_POLL_INTERVAL_SECONDS` | فاصله بررسی قیمت برای Watchهای فعال |
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | فقط برای اتصال واقعی MT5 روی ویندوز |

---

## ۱۰. مسائل نصب/محیط که در طول توسعه حل شدند (برای مرجع آینده)

| مشکل | علت | راه‌حل |
|---|---|---|
| `TypeError: Client.__init__() got an unexpected keyword argument 'proxies'` | ناسازگاری نسخه `openai` و `httpx` | پین کردن `openai==1.57.4` و `httpx==0.27.2` |
| `ImportError ... NumPy 1.x cannot be run in NumPy 2.x` | پکیج `MetaTrader5` با numpy 1.x کامپایل شده | پین کردن `numpy==1.26.4` |
| `No matching distribution found for MetaTrader5==5.0.45` | نسخه از PyPI حذف شده بود | تغییر به `MetaTrader5>=5.0.47` |
| رد شدن گاه‌به‌گاه مدل روی درخواست تحلیل | ترکیب تصویر + دستور قطعی TRADE حساسیت مدل را بالا می‌برد | معماری دو مرحله‌ای (دید جدا از تصمیم) + retry خودکار |
| `فیلد الزامی 'Status' یافت نشد` | مدل خروجی را با `**Bold**`/بولت مارک‌داون برمی‌گرداند | نرمال‌سازی متن قبل از پارس (`_normalize_ai_text`) |
| مدل با «قوانین ANEWME را کامل نفرستادی» جواب می‌داد | فایل `rules/anewme_rules.txt` هنوز placeholder بود | جایگزینی با متن واقعی منشور V3 |

---

## ۱۱. راه‌اندازی سریع

```bash
python -m venv venv
source venv/bin/activate        # ویندوز: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # سپس مقادیر واقعی را پر کنید
```

فایل `rules/anewme_rules.txt` باید حاوی متن کامل منشور ANEWME V3 باشد (در تحویل فعلی، از قبل پر شده).

```bash
python main.py
```

برای تست بدون تلگرام واقعی:
```bash
python test_manual.py
```
