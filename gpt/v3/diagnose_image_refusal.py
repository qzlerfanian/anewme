"""
diagnose_image_refusal.py
----------------------------
تست می‌کند که آیا خودِ تصویر باعث رد شدن مدل می‌شود، یا ترکیب تصویر + قوانین کامل.
"""

from openai import OpenAI
from config import config
from core.rules_loader import load_anewme_rules
from broker.mock_broker import MockBroker
from charts.chart_generator import generate_clean_chart
import base64

client = OpenAI(api_key=config.openai_api_key)


def _encode(path):
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return f"data:image/png;base64,{data}"


def make_test_chart():
    broker = MockBroker()
    snap = broker.get_market_snapshot("EURUSD")
    return generate_clean_chart("EURUSD", "M15", snap.candles_m15)


def test_3_image_no_rules():
    print("\n--- تست ۳: فقط تصویر چارت + دستور ساده (بدون قوانین ANEWME) ---")
    chart = make_test_chart()
    r = client.chat.completions.create(
        model=config.ai_model,
        max_tokens=300,
        messages=[
            {"role": "system", "content": "You are a technical analysis assistant."},
            {"role": "user", "content": [
                {"type": "text", "text": "Look at this EURUSD M15 chart and describe the price action pattern."},
                {"type": "image_url", "image_url": {"url": _encode(chart)}},
            ]},
        ],
    )
    print("finish_reason:", r.choices[0].finish_reason)
    print("content:", r.choices[0].message.content)


def test_4_image_with_rules():
    print("\n--- تست ۴: تصویر چارت + قوانین کامل ANEWME (دقیقاً مثل production) ---")
    chart = make_test_chart()
    rules = load_anewme_rules()
    r = client.chat.completions.create(
        model=config.ai_model,
        max_tokens=500,
        messages=[
            {"role": "system", "content": "You must issue TRADE, WATCH or NO_TRADE based on the rules below."},
            {"role": "user", "content": [
                {"type": "text", "text": f"ANEWME RULES:\n{rules}"},
                {"type": "text", "text": "EURUSD Bid=1.1750 Ask=1.1752. Analyze the attached chart."},
                {"type": "image_url", "image_url": {"url": _encode(chart)}},
            ]},
        ],
    )
    print("finish_reason:", r.choices[0].finish_reason)
    print("content:", r.choices[0].message.content)


def test_5_exact_production_path():
    print("\n--- تست ۵: دقیقاً مسیر واقعی کد (AIClient.request_analysis) با ۳ تصویر ---")
    from core.ai_client import AIClient
    from charts.chart_generator import generate_required_charts

    broker = MockBroker()
    snap = broker.get_market_snapshot("EURUSD")
    chart_paths = generate_required_charts(
        "EURUSD",
        {"M5": snap.candles_m5, "M15": snap.candles_m15, "H1": snap.candles_h1},
        include_correlated=False,
    )
    print(f"تعداد تصاویر ارسالی: {len(chart_paths)}")

    client = AIClient()
    raw = client.request_analysis("EURUSD", chart_paths, snap, previous_watch=None)
    print("content:", raw)


if __name__ == "__main__":
    test_3_image_no_rules()
    test_4_image_with_rules()
    test_5_exact_production_path()
