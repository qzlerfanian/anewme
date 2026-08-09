"""
diagnose_refusal.py
----------------------
اسکریپت تشخیصی موقت - چرا GPT درخواست را رد می‌کند؟
سه سناریو را جدا تست می‌کند تا مشخص شود مشکل از کجاست:
  1. فقط پرامپت متنی ساده (بدون تصویر، بدون قوانین ANEWME)
  2. پرامپت متنی + قوانین ANEWME واقعی شما
  3. پرامپت کامل + تصویر چارت

این فایل را بعد از پیدا کردن علت حذف کنید.
"""

from openai import OpenAI
from config import config
from core.rules_loader import load_anewme_rules

client = OpenAI(api_key=config.openai_api_key)


def test_1_minimal():
    print("\n--- تست ۱: حداقلی، بدون قوانین ANEWME و بدون تصویر ---")
    r = client.chat.completions.create(
        model=config.ai_model,
        max_tokens=200,
        messages=[
            {"role": "system", "content": "You are a technical analysis assistant."},
            {"role": "user", "content": "EURUSD is at 1.1750. Based on a bullish engulfing candle on M15, suggest a BUY_LIMIT order with entry, stop loss and take profit for the user's own manual, discretionary execution."},
        ],
    )
    print("finish_reason:", r.choices[0].finish_reason)
    print("content:", r.choices[0].message.content)


def test_2_with_rules():
    print("\n--- تست ۲: با متن کامل قوانین ANEWME شما (بدون تصویر) ---")
    rules = load_anewme_rules()
    r = client.chat.completions.create(
        model=config.ai_model,
        max_tokens=300,
        messages=[
            {"role": "system", "content": f"ANEWME RULES:\n{rules}\n\nYou must issue TRADE, WATCH or NO_TRADE."},
            {"role": "user", "content": "EURUSD Bid=1.1750 Ask=1.1752. M15 candles show a bullish structure. Analyze."},
        ],
    )
    print("finish_reason:", r.choices[0].finish_reason)
    print("content:", r.choices[0].message.content)


if __name__ == "__main__":
    test_1_minimal()
    test_2_with_rules()
