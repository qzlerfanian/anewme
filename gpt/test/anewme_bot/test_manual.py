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

این تست اتصال MT5 یا تلگرام را تأیید نمی‌کند. تماس هزینه‌دار فقط با
گزینه صریح --live-api انجام می‌شود. بدون آن هیچ درخواست API ارسال نمی‌شود.
"""

import os
from tempfile import TemporaryDirectory

# Separate data root BEFORE importing config or service. No runtime contamination.
_test_directory = TemporaryDirectory(prefix='anewme-manual-')
os.environ['ANEWME_DATA_DIR'] = _test_directory.name
os.environ['ANEWME_LOG_DIR'] = _test_directory.name

from broker.mock_broker import MockBroker
from core.analysis_service import AnalysisService
from storage.db import init_db
from telegram_bot.notifier import format_analysis_message

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Optional paid API smoke test with a mock broker')
    parser.add_argument('--live-api', action='store_true', help='Allow real, billable OpenAI requests')
    if not parser.parse_args().live_api:
        print('No API call made. Offline tests: python -m unittest discover -s tests -q')
        return
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
    print("این خروجی فقط تست API با داده ساختگی است؛ MT5 و تلگرام تست نشده‌اند.")


if __name__ == "__main__":
    main()
