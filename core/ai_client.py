"""
core/ai_client.py
------------------
معماری دو مرحله‌ای (بر اساس مشاهده عملی حین توسعه):

  مرحله ۱ - "دید" (Vision, خنثی): هر تصویر چارت جداگانه با یک دستور کاملاً
  خنثی و توصیفی (بدون کلمه TRADE/BUY/SELL/Entry) به مدل داده می‌شود و فقط
  توصیف عینی از آنچه در تصویر دیده می‌شود گرفته می‌شود.

  مرحله ۲ - "تصمیم" (فقط متن، بدون تصویر): توصیف‌های متنی مرحله ۱ + داده‌های
  بازار + متن کامل قوانین ANEWME (بند ۵) به‌صورت یک درخواست کاملاً متنی
  فرستاده می‌شود و از مدل خواسته می‌شود در قالب ثابت (بند ۷) نتیجه
  TRADE/WATCH/NO_TRADE را صادر کند.

چرا این تغییر لازم شد: در تست عملی مشخص شد که ترکیب «تصویر + دستور قطعی
صدور TRADE با Entry/SL/TP دقیق» گاهی به‌صورت غیرقطعی توسط مدل رد می‌شود
(همان درخواست دقیق، یک‌بار جواب می‌دهد یک‌بار رد می‌کند)، در حالی که هرکدام
از این دو کار به‌تنهایی (فقط توصیف تصویر، یا فقط تصمیم‌گیری متنی) در تست
کاملاً پایدار بود. جدا کردن «دیدن» از «تصمیم‌گیری» این ناپایداری را دور
می‌زند بدون این‌که به محتوای قوانین ANEWME یا فرمت ثابت پاسخ (بند ۷)
دست بزنیم.

این ماژول هیچ تصمیمی نمی‌گیرد و هیچ اعتباری به پاسخ نمی‌دهد - فقط رابط
بین برنامه و مدل است. اعتبارسنجی و پارس در core/parser.py و core/validator.py
انجام می‌شود (جداسازی مسئولیت‌ها).
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Optional

from openai import OpenAI

from config import config
from core.models import MarketSnapshot, WatchState
from core.rules_loader import load_anewme_rules

logger = logging.getLogger(__name__)

# --- مرحله ۱: پرامپت کاملاً خنثی برای توصیف تصویر (بدون هیچ کلمه معاملاتی) ---
IMAGE_DESCRIPTION_PROMPT = """
لطفاً فقط آنچه در این تصویر چارت قیمت به‌صورت عینی می‌بینید را توصیف کنید:
- روند کلی (صعودی/نزولی/خنثی) در بازه زمانی نمایش داده‌شده
- سقف‌ها و کف‌های قابل‌توجه و ترتیب آن‌ها
- رفتار چند کندل آخر (رنگ، اندازه بدنه، سایه‌ها، آیا کندل بسته شده یا نه)
- هر گونه الگوی قیمتی قابل مشاهده صرفاً از روی شکل کندل‌ها (مثلاً engulfing،
  doji، پین‌بار) - فقط اگر واقعاً واضح است
- زمان آخرین کندل و بازه قیمتی نمایش داده‌شده در تصویر

این فقط یک توصیف بصری خنثی است، نه توصیه یا تحلیل معاملاتی. لطفاً هیچ
پیشنهاد خرید/فروش، نقطه ورود، حد ضرر یا حد سود ندهید - فقط آنچه در تصویر
دیده می‌شود را با دقت توصیف کنید.
"""

# --- مرحله ۲: پرامپت تصمیم‌گیری - فقط متن، بدون تصویر ---
DECISION_INSTRUCTIONS = """
شما در حال کمک به یک معامله‌گر باتجربه هستید که خودش، شخصاً و با تشخیص
و مسئولیت خودش، تصمیم نهایی معامله را می‌گیرد. خروجی شما فقط یک تحلیل
تکنیکال ساختاریافته بر اساس یک متدولوژی از پیش‌تعریف‌شده (ANEWME) است که
توسط خودِ کاربر نوشته شده و پیوست شده - نه توصیه سرمایه‌گذاری عمومی و نه
پیشنهاد به اشخاص ثالث. کاربر این خروجی را صرفاً به‌عنوان ورودی به فرآیند
تصمیم‌گیری دستی خودش در نظر می‌گیرد و هیچ سفارشی به‌صورت خودکار ثبت نمی‌شود.

شما تحلیل‌گر ستاپ‌های معاملاتی بر اساس قوانین ANEWME هستید. ورودی شما شامل
توصیف متنیِ از پیش‌آماده‌شده از تصاویر چارت (توسط سیستم دیگری تهیه شده)
به‌همراه داده‌های عددی بازار است - شما تصویر مستقیم نمی‌بینید، فقط توصیف
متنی آن را دارید. قوانین کامل ANEWME (تنها منبع معتبر تحلیل شما) پیوست
شده است. شما فقط اجازه دارید یکی از سه نتیجه TRADE / WATCH / NO_TRADE را
صادر کنید.

پاسخ شما باید دقیقاً در قالب فیلدهای ثابت زیر باشد - بدون توضیح اضافه،
بدون مقدمه، بدون Markdown decoration:

Analysis Time: <UTC ISO8601>
Symbol: <SYMBOL>
Status: <TRADE|WATCH|NO_TRADE>
Direction: (فقط یکی از این دو کلمه: BUY یا SELL - اگر غیرقابل‌اعمال است دقیقاً بنویسید دو خط تیره: --)
Grade: <A+|A|A-|B+|B|C>
Reason: <متن کوتاه>
Timeframes Checked: <لیست با کاما>

اگر Status=TRADE، این فیلدهای اضافه را هم بنویسید:
Order Type: <BUY_LIMIT|SELL_LIMIT|BUY_STOP|SELL_STOP>
Entry: <عدد>
Stop Loss: <عدد>
Take Profit: <عدد>
Risk Percent: <عدد>
Reward Risk Ratio: <عدد>
Expiration: <زمان>
Invalidation: <شرط>
Checklist Complete: <true|false>

اگر Status=WATCH، این فیلدهای اضافه را هم بنویسید:
Preferred Direction: <BUY|SELL>
Trigger Type: <یکی از: زون ورود/خروج، سطح مشخص، بسته‌شدن کندل M5،
              بسته‌شدن کندل M15، زمان مشخص، شرط ابطال، زمان انقضا>
Zone Or Level: <محدوده یا سطح دقیق - عدد، نه توصیف مبهم>
Timeframes To Recheck: <لیست>
Expiration: <زمان>
Invalidation: <شرط عددی/زمانی/وابسته به کندل - نه عبارت مبهم>

توجه: نام سفارش فقط باید یکی از BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP
باشد. فقط Pending Order مجاز است؛ Market Order هرگز مجاز نیست.
عبارت‌های مبهم مانند "بعداً دوباره بررسی شود" مجاز نیست.
مهم: فقط همین فیلدهای متنی ساده را برگردانید (بدون Markdown، بدون **، بدون
لیست‌های bullet، بدون JSON).
"""


class AIClient:
    REFUSAL_MARKERS = ("i'm sorry", "i cannot assist", "i can't assist", "i am unable to",
                        "i'm unable to", "i can not assist")
    MAX_ATTEMPTS = 3  # طبق مشاهده عملی: رد شدن مدل روی این نوع درخواست تصادفی است، نه قطعی

    def __init__(self):
        if not config.openai_api_key:
            from config import ENV_FILE
            raise RuntimeError(
                "OPENAI_API_KEY تنظیم نشده است.\n"
                f"بررسی کنید فایل .env در این مسیر وجود دارد و پر شده: {ENV_FILE}\n"
                "محتوای مورد انتظار باید خطی شبیه این داشته باشد (بدون فاصله اضافه، بدون کوتیشن):\n"
                "OPENAI_API_KEY=sk-..."
            )
        self.client = OpenAI(api_key=config.openai_api_key)

    # ------------------------------------------------------------------
    @staticmethod
    def _encode_image_data_url(path: Path) -> str:
        data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
        return f"data:image/png;base64,{data}"

    def _looks_like_refusal(self, text: str, finish_reason: str) -> bool:
        lowered = text.lower().strip()
        return finish_reason == "content_filter" or any(
            lowered.startswith(m) for m in self.REFUSAL_MARKERS
        )

    def _call_with_retry(self, messages: list, max_tokens: int, label: str) -> str:
        """
        فراخوانی عمومی مدل با retry خودکار در صورت رد شدن (رفتار stochastic
        که در عمل مشاهده شد). label فقط برای لاگ خواناتر است.
        """
        last_raw_text = ""
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            logger.info("درخواست '%s' به OpenAI (تلاش %d/%d)", label, attempt, self.MAX_ATTEMPTS)
            response = self.client.chat.completions.create(
                model=config.ai_model,
                max_tokens=max_tokens,
                temperature=0,
                messages=messages,
            )
            raw_text = (response.choices[0].message.content or "").strip()
            finish_reason = response.choices[0].finish_reason
            logger.debug("'%s' تلاش %d | finish_reason=%s | پاسخ:\n%s", label, attempt, finish_reason, raw_text)

            if not self._looks_like_refusal(raw_text, finish_reason):
                return raw_text

            last_raw_text = raw_text
            logger.warning(
                "درخواست '%s' رد شد (تلاش %d/%d, finish_reason=%s). متن: %s",
                label, attempt, self.MAX_ATTEMPTS, finish_reason, raw_text,
            )
            if attempt < self.MAX_ATTEMPTS:
                time.sleep(1.5)

        logger.error("درخواست '%s' بعد از %d تلاش همچنان رد شد.", label, self.MAX_ATTEMPTS)
        return last_raw_text

    # ------------------------------------------------------------------ مرحله ۱
    def describe_chart_image(self, image_path: Path, symbol: str, timeframe_label: str) -> str:
        """
        مرحله ۱: توصیف خنثی و صرفاً بصری یک تصویر چارت - بدون هیچ کلمه
        معاملاتی. خروجی این متد ورودی مرحله تصمیم‌گیری (متنی) می‌شود.
        """
        messages = [
            {"role": "system", "content": "You are a neutral, objective chart-description assistant."},
            {"role": "user", "content": [
                {"type": "text", "text": f"{IMAGE_DESCRIPTION_PROMPT}\n\nSymbol: {symbol} | Timeframe: {timeframe_label}"},
                {"type": "image_url", "image_url": {"url": self._encode_image_data_url(image_path)}},
            ]},
        ]
        return self._call_with_retry(messages, max_tokens=500, label=f"توصیف تصویر {symbol}/{timeframe_label}")

    def describe_all_charts(self, chart_paths: list[Path], symbol: str) -> str:
        """
        توصیف تمام تصاویر (نماد اصلی + DXY/USDJPY در صورت وجود) و ترکیب
        آن‌ها در یک متن واحد قابل استفاده در مرحله تصمیم‌گیری.
        نام فایل به شکل SYMBOL_TIMEFRAME_hash.png است (چارت‌های خودمان)،
        از این‌رو نماد و تایم‌فریم از روی اسم فایل استخراج می‌شود.
        """
        blocks = []
        for path in chart_paths:
            parts = path.stem.split("_")
            chart_symbol = parts[0] if len(parts) > 0 else symbol
            chart_tf = parts[1] if len(parts) > 1 else "?"
            description = self.describe_chart_image(path, chart_symbol, chart_tf)
            blocks.append(f"--- توصیف چارت {chart_symbol} / {chart_tf} ---\n{description}")
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------ متن‌سازی کمکی
    @staticmethod
    def _format_market_snapshot(snapshot: MarketSnapshot) -> str:
        lines = [
            f"Symbol: {snapshot.symbol}",
            f"Bid: {snapshot.bid}",
            f"Ask: {snapshot.ask}",
            f"Spread: {snapshot.spread}",
            f"Market Time (UTC): {snapshot.market_time_utc.isoformat()}",
            f"Broker Server Time: {snapshot.broker_server_time.isoformat()}",
            f"Market Open: {snapshot.market_open}",
        ]
        if snapshot.account_balance is not None:
            lines.append(
                f"Account Balance: {snapshot.account_balance} {snapshot.account_currency or ''}"
            )
        if snapshot.symbol_contract_size is not None:
            lines.append(f"Contract Size: {snapshot.symbol_contract_size}")
        if snapshot.symbol_min_lot is not None:
            lines.append(f"Min Lot: {snapshot.symbol_min_lot}")
        if snapshot.symbol_lot_step is not None:
            lines.append(f"Lot Step: {snapshot.symbol_lot_step}")

        for label, candles in (
            ("M5", snapshot.candles_m5),
            ("M15", snapshot.candles_m15),
            ("H1", snapshot.candles_h1),
        ):
            if candles:
                lines.append(f"\nCandles {label} (Open/High/Low/Close/Time, most recent last):")
                for c in candles:
                    lines.append(
                        f"  {c.get('time')} O:{c.get('open')} H:{c.get('high')} "
                        f"L:{c.get('low')} C:{c.get('close')}"
                    )
        return "\n".join(lines)

    @staticmethod
    def _format_previous_watch(watch: Optional[WatchState]) -> str:
        if watch is None:
            return ""
        return (
            "\n--- Previous WATCH being re-checked ---\n"
            f"Watch ID: {watch.watch_id}\n"
            f"Symbol: {watch.symbol}\n"
            f"Direction: {watch.direction.value}\n"
            f"Grade: {watch.grade.value}\n"
            f"Trigger Type: {watch.trigger_type}\n"
            f"Zone Or Level: {watch.zone_or_level}\n"
            f"Timeframes To Recheck: {', '.join(watch.timeframes_to_recheck)}\n"
            f"Expiration: {watch.expiration.isoformat()}\n"
            f"Invalidation: {watch.invalidation_condition}\n"
        )

    # ------------------------------------------------------------------ مرحله ۲
    def request_analysis(
        self,
        symbol: str,
        chart_paths: list[Path],
        snapshot: MarketSnapshot,
        previous_watch: Optional[WatchState] = None,
    ) -> str:
        """
        نقطه ورود اصلی که analysis_service صدا می‌زند. داخلش دو مرحله انجام
        می‌شود: ابتدا توصیف خنثی هر تصویر (مرحله ۱)، سپس تصمیم‌گیری متنی
        نهایی بر اساس آن توصیف‌ها + قوانین کامل ANEWME (مرحله ۲).
        """
        chart_descriptions = self.describe_all_charts(chart_paths, symbol)

        rules_text = load_anewme_rules()

        text_blocks = [
            f"--- ANEWME RULES (FULL TEXT) ---\n{rules_text}",
            f"\n--- MARKET DATA ---\n{self._format_market_snapshot(snapshot)}",
            f"\n--- CHART DESCRIPTIONS (تهیه‌شده توسط ماژول دید، نه تصویر مستقیم) ---\n{chart_descriptions}",
        ]
        if previous_watch is not None:
            text_blocks.append(self._format_previous_watch(previous_watch))

        messages = [
            {"role": "system", "content": DECISION_INSTRUCTIONS},
            {"role": "user", "content": "\n".join(text_blocks)},
        ]

        return self._call_with_retry(messages, max_tokens=1500, label=f"تصمیم‌گیری {symbol}")
