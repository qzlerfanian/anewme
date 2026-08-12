"""
tests/test_rules_regression.py
---------------------------------
تست رگرسیون خودکار برای زنجیره parser -> validator -> consistency_checker.

هدف: هر بار که rules/anewme_rules.txt یا کد core/*.py تغییر می‌کند، این
اسکریپت به‌جای تست دستی (که تا الان بارها انجام شد)، در چند ثانیه تأیید
می‌کند که سناریوهای شناخته‌شده هنوز درست کار می‌کنند. این اسکریپت به AI
واقعی وصل نمی‌شود - فقط زنجیره پردازش پاسخ (که همان چیزی است که واقعاً
در کد اجرا می‌شود) را با پاسخ‌های شبیه‌سازی‌شده AI تست می‌کند.

اجرا:
    python tests/test_rules_regression.py

خروجی: لیست PASS/FAIL هر سناریو + کد خروج ۰ (همه موفق) یا ۱ (حداقل یک شکست).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.mock_broker import MockBroker
from core.consistency_checker import check_watch_consistency
from core.models import AnalysisStatus, Grade
from core.parser import AIResponseParseError, parse_ai_response
from core.validator import validate_trade_result

RESULTS: list[tuple[str, bool, str]] = []


def check(description: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((description, condition, detail))


def run_all_tests() -> None:
    broker = MockBroker(base_prices={"EURUSD": 1.1750})
    # تست‌های عمومی نباید به روز واقعی هفته (شنبه/یکشنبه) وابسته باشند -
    # فقط سناریوی اختصاصی «بازار بسته» (پایین همین فایل) این را جدا تست می‌کند.
    broker.is_market_open = lambda symbol: True
    snap = broker.get_market_snapshot("EURUSD")
    bid, ask = snap.bid, snap.ask

    # ------------------------------------------------------------------
    # سناریو ۱: H1/M15 هم‌جهت، M5 در انتظار -> WATCH | A-  (بند ۲.۱)
    text = f"""Analysis Time: 2026-08-06T09:00:00Z
Symbol: EURUSD
Status: WATCH
Direction: --
Grade: A-
Reason: H1=صعودی تمیز; M15=هم‌جهت با H1; M5=هنوز کندل تاییدی بسته نشده; دلیل اصلی=نبود تریگر M5; شرط بعدی=بسته‌شدن کندل M5 بالای سطح
Timeframes Checked: H1, M15
Preferred Direction: BUY
Trigger Type: بسته‌شدن کندل M5
Zone Or Level: {bid + 0.001}
Timeframes To Recheck: M5
Expiration: 2026-08-06T20:00:00Z
Invalidation: بسته‌شدن M15 زیر {bid - 0.002}
"""
    try:
        r = parse_ai_response(text, "EURUSD")
        ok = r.status == AnalysisStatus.WATCH and r.grade == Grade.A_MINUS
        check("سناریو ۱: H1/M15 هم‌جهت + M5 در انتظار -> WATCH|A-", ok, f"status={r.status}, grade={r.grade}")
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۱: H1/M15 هم‌جهت + M5 در انتظار -> WATCH|A-", False, f"استثنا: {exc}")

    # ------------------------------------------------------------------
    # سناریو ۲: H1 مشخص، M15 در حال تشکیل با سطح دقیق -> WATCH | B+
    text = f"""Analysis Time: 2026-08-06T09:00:00Z
Symbol: EURUSD
Status: WATCH
Direction: --
Grade: B+
Reason: H1=صعودی; M15=در حال تشکیل ساختار; M5=نامشخص; دلیل اصلی=ساختار M15 هنوز کامل نشده; شرط بعدی=تکمیل ساختار M15
Timeframes Checked: H1
Preferred Direction: BUY
Trigger Type: سطح مشخص
Zone Or Level: {bid + 0.002}
Timeframes To Recheck: M15, M5
Expiration: 2026-08-06T20:00:00Z
Invalidation: شکست سطح {bid - 0.003}
"""
    try:
        r = parse_ai_response(text, "EURUSD")
        ok = r.status == AnalysisStatus.WATCH and r.grade == Grade.B_PLUS
        check("سناریو ۲: H1 مشخص + M15 در حال تشکیل -> WATCH|B+", ok, f"status={r.status}, grade={r.grade}")
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۲: H1 مشخص + M15 در حال تشکیل -> WATCH|B+", False, f"استثنا: {exc}")

    # ------------------------------------------------------------------
    # سناریو ۳: تضاد واقعی H1/M15 -> NO_TRADE | B
    text = """Analysis Time: 2026-08-06T09:00:00Z
Symbol: EURUSD
Status: NO_TRADE
Direction: --
Grade: B
Reason: H1=صعودی; M15=نزولی و مخالف H1; M5=نامرتبط; دلیل اصلی=تضاد واقعی میان H1 و M15; شرط بعدی=هم‌جهت شدن H1 و M15
Timeframes Checked: H1, M15
"""
    try:
        r = parse_ai_response(text, "EURUSD")
        ok = r.status == AnalysisStatus.NO_TRADE and r.grade == Grade.B
        check("سناریو ۳: تضاد واقعی H1/M15 -> NO_TRADE|B", ok, f"status={r.status}, grade={r.grade}")
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۳: تضاد واقعی H1/M15 -> NO_TRADE|B", False, f"استثنا: {exc}")

    # ------------------------------------------------------------------
    # سناریو ۴: TRADE معتبر با گرید A -> باید validator قبول کند
    text = f"""Analysis Time: 2026-08-06T09:00:00Z
Symbol: EURUSD
Status: TRADE
Direction: SELL
Grade: A
Reason: ساختار کامل و تمیز با تریگر معتبر M5
Timeframes Checked: H1, M15, M5
Order Type: SELL_LIMIT
Entry: {bid + 0.0015}
Stop Loss: {bid + 0.0030}
Take Profit: {bid - 0.0030}
Risk Percent: 1.0
Reward Risk Ratio: 3.0
Expiration: 2099-08-06T18:00:00Z
Invalidation: بسته‌شدن بالای Stop Loss
Checklist Complete: true
"""
    try:
        r = parse_ai_response(text, "EURUSD")
        outcome = validate_trade_result(r, snap)
        check("سناریو ۴: TRADE معتبر گرید A -> باید تأیید شود", outcome.is_valid, str(outcome.reasons))
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۴: TRADE معتبر گرید A -> باید تأیید شود", False, f"استثنا: {exc}")

    # ------------------------------------------------------------------
    # سناریو ۵: TRADE با گرید B+ -> باید validator رد کند (طبق منشور فقط A/A+ مجازند)
    text = f"""Analysis Time: 2026-08-06T09:00:00Z
Symbol: EURUSD
Status: TRADE
Direction: BUY
Grade: B+
Reason: نزدیک ولی ناقص
Timeframes Checked: H1
Order Type: BUY_LIMIT
Entry: {ask - 0.001}
Stop Loss: {ask - 0.002}
Take Profit: {ask + 0.002}
Risk Percent: 1.0
Reward Risk Ratio: 2.0
Expiration: 2099-08-06T18:00:00Z
Invalidation: شکست سطح {ask - 0.002}
Checklist Complete: true
"""
    try:
        r = parse_ai_response(text, "EURUSD")
        outcome = validate_trade_result(r, snap)
        ok = not outcome.is_valid
        check("سناریو ۵: TRADE با گرید B+ -> باید رد شود", ok, str(outcome.reasons))
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۵: TRADE با گرید B+ -> باید رد شود", False, f"استثنا: {exc}")

    # ------------------------------------------------------------------
    # سناریو ۶: TRADE با ریسک بیش از سقف -> باید validator رد کند
    text = f"""Analysis Time: 2026-08-06T09:00:00Z
Symbol: EURUSD
Status: TRADE
Direction: BUY
Grade: A
Reason: ستاپ کامل ولی ریسک درخواستی بیش از سقف
Timeframes Checked: H1, M15, M5
Order Type: BUY_LIMIT
Entry: {ask - 0.001}
Stop Loss: {ask - 0.002}
Take Profit: {ask + 0.003}
Risk Percent: 3.0
Reward Risk Ratio: 3.0
Expiration: 2099-08-06T18:00:00Z
Invalidation: شکست سطح {bid + 0.010}
Checklist Complete: true
"""
    try:
        r = parse_ai_response(text, "EURUSD")
        outcome = validate_trade_result(r, snap)
        ok = not outcome.is_valid
        check("سناریو ۶: TRADE با ریسک ۳٪ (بیش از سقف ۱٪) -> باید رد شود", ok, str(outcome.reasons))
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۶: TRADE با ریسک ۳٪ (بیش از سقف ۱٪) -> باید رد شود", False, f"استثنا: {exc}")

    # ------------------------------------------------------------------
    # سناریو ۷: WATCH با Zone Or Level مبهم -> باید پارسر رد کند (بند ۱۱)
    text = """Analysis Time: 2026-08-06T09:00:00Z
Symbol: EURUSD
Status: WATCH
Direction: --
Grade: B+
Reason: نیاز به بررسی بیشتر
Timeframes Checked: H1
Preferred Direction: BUY
Trigger Type: نامشخص
Zone Or Level: بعداً دوباره بررسی شود
Timeframes To Recheck: M15
Expiration: 2026-08-06T18:00:00Z
Invalidation: شکست سطح {bid - 0.010}
"""
    try:
        parse_ai_response(text, "EURUSD")
        check("سناریو ۷: Zone Or Level مبهم -> باید AIResponseParseError بدهد", False, "استثنا داده نشد!")
    except AIResponseParseError:
        check("سناریو ۷: Zone Or Level مبهم -> باید AIResponseParseError بدهد", True)
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۷: Zone Or Level مبهم -> باید AIResponseParseError بدهد", False, f"استثنای غلط: {exc}")

    # ------------------------------------------------------------------
    # سناریو ۸: پاسخ با مارک‌داون (Bold) هم باید درست پارس شود
    text = """**Analysis Time:** 2026-08-06T09:00:00Z
**Symbol:** EURUSD
**Status:** NO_TRADE
**Direction:** --
**Grade:** C
**Reason:** ساختار بی‌کیفیت
**Timeframes Checked:** M5
"""
    try:
        r = parse_ai_response(text, "EURUSD")
        ok = r.status == AnalysisStatus.NO_TRADE and r.grade == Grade.C
        check("سناریو ۸: پاسخ Markdown-دار باید پارس شود", ok, f"status={r.status}, grade={r.grade}")
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۸: پاسخ Markdown-دار باید پارس شود", False, f"استثنا: {exc}")

    # ------------------------------------------------------------------
    # سناریو ۹: WATCH با نقش سطح اشتباه (سطح پایین قیمت ولی «مقاومت» نامیده شده)
    # باید توسط consistency_checker هشدار داده شود (بند ۵.۱ و ۱۵)
    text = f"""Analysis Time: 2026-08-06T09:00:00Z
Symbol: EURUSD
Status: WATCH
Direction: --
Grade: A-
Reason: قیمت به مقاومت نزدیک می‌شود و منتظر شکست هستیم
Timeframes Checked: H1
Preferred Direction: BUY
Trigger Type: سطح مشخص
Zone Or Level: {bid - 0.005}
Timeframes To Recheck: M15
Expiration: 2026-08-06T18:00:00Z
Invalidation: بسته‌شدن زیر {bid - 0.010}
"""
    try:
        r = parse_ai_response(text, "EURUSD")
        warnings = check_watch_consistency(r, snap)
        ok = len(warnings) > 0
        check("سناریو ۹: نقش سطح اشتباه (Resistance زیر قیمت) -> باید هشدار بدهد", ok, str(warnings))
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۹: نقش سطح اشتباه (Resistance زیر قیمت) -> باید هشدار بدهد", False, f"استثنا: {exc}")

    # ------------------------------------------------------------------
    # سناریو ۱۰: بازار بسته -> نباید AI صدا زده شود و باید NO_TRADE برگردد
    from unittest.mock import MagicMock
    from core.analysis_service import AnalysisService
    from storage.db import init_db

    init_db()
    closed_broker = MockBroker(base_prices={"EURUSD": 1.1750})
    closed_broker.is_market_open = lambda symbol: False
    fake_ai = MagicMock()
    fake_ai.request_analysis.return_value = "این پاسخ هرگز نباید استفاده شود"
    svc = AnalysisService(broker=closed_broker, ai_client=fake_ai)
    try:
        result = svc.run_initial_analysis("EURUSD", needs_correlated_symbols=False)
        ok = result.status == AnalysisStatus.NO_TRADE and not fake_ai.request_analysis.called
        check(
            "سناریو ۱۰: بازار بسته -> بدون تماس AI و NO_TRADE",
            ok,
            f"status={result.status}, ai_called={fake_ai.request_analysis.called}",
        )
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۱۰: بازار بسته -> بدون تماس AI و NO_TRADE", False, f"استثنا: {exc}")

    # ------------------------------------------------------------------
    # سناریو ۱۱: جلوگیری از Watch تکراری روی همان نماد
    from datetime import timedelta

    init_db()
    dup_broker = MockBroker(base_prices={"EURUSD": 1.1750})
    dup_broker.is_market_open = lambda symbol: True
    future_exp = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    fake_ai_dup = MagicMock()
    fake_ai_dup.last_chart_descriptions = "توصیف نمونه"
    fake_ai_dup.request_analysis.return_value = f"""Analysis Time: 2026-08-09T09:00:00Z
Symbol: EURUSD
Status: WATCH
Direction: --
Grade: A-
Reason: تست
Timeframes Checked: H1, M15
Preferred Direction: BUY
Trigger Type: بسته‌شدن کندل M5
Zone Or Level: 1.1760
Timeframes To Recheck: M5
Expiration: {future_exp}
Invalidation: بسته‌شدن زیر 1.1600
"""
    svc_dup = AnalysisService(broker=dup_broker, ai_client=fake_ai_dup)
    try:
        r1 = svc_dup.run_initial_analysis("EURUSD", needs_correlated_symbols=False)
        r2 = svc_dup.run_initial_analysis("EURUSD", needs_correlated_symbols=False)
        ok = (
            r1.status == AnalysisStatus.WATCH
            and r2.status == AnalysisStatus.WATCH
            and fake_ai_dup.request_analysis.call_count == 1  # بار دوم AI صدا زده نشده
        )
        check(
            "سناریو ۱۱: جلوگیری از Watch تکراری روی همان نماد",
            ok,
            f"ai_call_count={fake_ai_dup.request_analysis.call_count}",
        )
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۱۱: جلوگیری از Watch تکراری روی همان نماد", False, f"استثنا: {exc}")

    # ------------------------------------------------------------------
    # سناریو ۱۲: پوزیشن باز واقعی در MT5 -> سیگنال جدید سرکوب شود
    init_db()
    pos_broker = MockBroker(base_prices={"GBPUSD": 1.3400})
    pos_broker.is_market_open = lambda symbol: True
    pos_broker._mock_open_positions["GBPUSD"] = [
        {"ticket": 1, "volume": 0.1, "price_open": 1.3395, "type": "BUY", "profit": 5.0}
    ]
    fake_ai_pos = MagicMock()
    fake_ai_pos.last_chart_descriptions = "توصیف نمونه"
    fake_ai_pos.request_analysis.return_value = f"""Analysis Time: 2026-08-09T09:00:00Z
Symbol: GBPUSD
Status: TRADE
Direction: BUY
Grade: A+
Reason: تست
Timeframes Checked: H1, M15, M5
Order Type: BUY_LIMIT
Entry: 1.3399
Stop Loss: 1.3389
Take Profit: 1.3419
Risk Percent: 1.0
Reward Risk Ratio: 2.0
Expiration: {future_exp}
Invalidation: شکست سطح 1.3389
Checklist Complete: true
"""
    svc_pos = AnalysisService(broker=pos_broker, ai_client=fake_ai_pos)
    try:
        r = svc_pos.run_initial_analysis("GBPUSD", needs_correlated_symbols=False)
        from storage.db import get_open_trade_trackings as _get_open
        ok = r.account_state == "OPEN_POSITION" and len(_get_open()) == 0
        check("سناریو ۱۲: پوزیشن باز MT5 -> سیگنال جدید سرکوب و ردیابی نشود", ok, f"account_state={r.account_state}")
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۱۲: پوزیشن باز MT5 -> سیگنال جدید سرکوب و ردیابی نشود", False, f"استثنا: {exc}")

    # ------------------------------------------------------------------
    # سناریو ۱۳: last_closed_m5_time در نتیجه پر شود
    m5_broker = MockBroker(base_prices={"USDCHF": 0.8800})
    m5_broker.is_market_open = lambda symbol: True
    fake_ai_m5 = MagicMock()
    fake_ai_m5.last_chart_descriptions = "توصیف نمونه"
    fake_ai_m5.request_analysis.return_value = """Analysis Time: 2026-08-09T09:00:00Z
Symbol: USDCHF
Status: NO_TRADE
Direction: --
Grade: C
Reason: تست
Timeframes Checked: M5
"""
    svc_m5 = AnalysisService(broker=m5_broker, ai_client=fake_ai_m5)
    try:
        r = svc_m5.run_initial_analysis("USDCHF", needs_correlated_symbols=False)
        ok = r.last_closed_m5_time is not None and "UTC" in r.last_closed_m5_time
        check("سناریو ۱۳: last_closed_m5_time در نتیجه پر می‌شود", ok, f"last_closed_m5_time={r.last_closed_m5_time}")
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۱۳: last_closed_m5_time در نتیجه پر می‌شود", False, f"استثنا: {exc}")

    # ------------------------------------------------------------------
    # سناریو ۱۴: Trigger Type بدون کلیدواژه شناخته‌شده (مثل "M5 Close > 1.1560")
    # نباید بی‌صدا نادیده گرفته شود - این دقیقاً باگ گزارش‌شده توسط کارفرما بود
    from watch.watch_manager import check_trigger as _check_trigger

    init_db()
    weird_row = {
        "watch_id": "regression-weird", "symbol": "EURUSD", "direction": "BUY",
        "trigger_type": "M5 Close > 1.1560", "zone_or_level": "1.1560",
        "expiration": "2099-01-01T00:00:00+00:00",
        "is_locked": 0, "is_triggered": 0, "is_closed": 0,
        "last_checked_candle_time": None,
    }
    weird_broker = MagicMock()
    weird_broker.get_candles.return_value = [{
        "time": datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc),
        "open": 1.1550, "high": 1.1570, "low": 1.1548, "close": 1.1565,
    }]
    try:
        triggered, reason = _check_trigger(weird_row, weird_broker)
        ok = triggered is True
        check(
            "سناریو ۱۴: Trigger Type غیراستاندارد نباید بی‌صدا نادیده گرفته شود",
            ok, f"triggered={triggered}, reason={reason}",
        )
    except Exception as exc:  # noqa: BLE001
        check("سناریو ۱۴: Trigger Type غیراستاندارد نباید بی‌صدا نادیده گرفته شود", False, f"استثنا: {exc}")


def main() -> int:
    run_all_tests()
    print("\n" + "=" * 70)
    print("نتیجه تست‌های رگرسیون قوانین ANEWME")
    print("=" * 70)
    passed = 0
    for description, ok, detail in RESULTS:
        tag = "✅ PASS" if ok else "❌ FAIL"
        print(f"{tag} | {description}")
        if detail and not ok:
            print(f"        جزئیات: {detail}")
        if ok:
            passed += 1
    print("=" * 70)
    print(f"{passed}/{len(RESULTS)} تست موفق بود.")
    print("=" * 70)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
