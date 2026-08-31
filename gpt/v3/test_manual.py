"""
test_manual.py
----------------
اسکریپت تست دستی - این فایل را مستقیم توی پایچارم Run کنید (راست‌کلیک -> Run).
هیچ اتصال واقعی به MT5 یا تلگرام لازم ندارد؛ فقط از Mock Broker و AI واقعی
(OpenAI) استفاده می‌کند تا مطمئن شوید:
  1. OPENAI_API_KEY درست کار می‌کند
  2. متن rules/anewme_rules.txt به‌درستی خوانده و ارسال می‌شود
  3. چارت‌ها تولید می‌شوند
  4. پاسخ AI پارس و اعتبارسنجی می‌شود

بعد از دیدن پاسخ صحیح اینجا، خیالتان راحت باشد main.py هم کار می‌کند.
این فایل را بعد از تست، از پروژه حذف کنید (جزو معماری اصلی نیست).
"""

from broker.mock_broker import MockBroker
from core.analysis_service import AnalysisService
from storage.db import init_db
from telegram_bot.notifier import format_analysis_message

def main():
    print("در حال آماده‌سازی دیتابیس...")
    init_db()

    print("در حال ساخت Mock Broker (داده‌های تصادفی EURUSD)...")
    broker = MockBroker()

    print("در حال فراخوانی AnalysisService (این یک تماس واقعی به OpenAI API است)...")
    service = AnalysisService(broker=broker)  # از AIClient واقعی استفاده می‌کند

    result = service.run_initial_analysis("EURUSD", needs_correlated_symbols=False)

    print("\n========== متن خام پاسخ AI (برای دیباگ) ==========\n")
    print(result.raw_ai_text)
    print("\n====================================================\n")

    print("\n========== نتیجه ==========\n")
    print(format_analysis_message(result))
    print("\n============================\n")
    print("اگر پیام بالا را بدون خطا دیدید، تنظیمات AI و قوانین درست است.")


if __name__ == "__main__":
    main()
