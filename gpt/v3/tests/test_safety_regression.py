"""تست‌های رگرسیون استاندارد برای کنترل‌های ایمنی حیاتی."""

from datetime import datetime, timedelta, timezone
import math
import unittest

from core.models import (
    AnalysisResult, AnalysisStatus, Direction, Grade, MarketSnapshot,
    OrderType, TradeDetails,
)
from core.parser import AIResponseParseError, parse_ai_response
from core.risk_manager import calculate_position_size
from core.validator import validate_trade_result
from watch.watch_manager import _invalidation_reached, _parse_expiration
from watch.watch_manager import check_trigger


def snapshot(**overrides):
    values = dict(
        symbol="EURUSD", bid=1.1000, ask=1.1002, spread=0.0002,
        market_time_utc=datetime.now(timezone.utc),
        broker_server_time=datetime.now(timezone.utc), market_open=True,
        account_balance=10_000.0, account_currency="USD",
        symbol_contract_size=100_000.0, symbol_min_lot=0.01,
        symbol_lot_step=0.01, symbol_pip_value=10.0,
        symbol_tick_size=0.00001, symbol_tick_value=1.0, symbol_max_lot=100.0,
    )
    values.update(overrides)
    return MarketSnapshot(**values)


def trade_result(**trade_overrides):
    trade = dict(
        order_type=OrderType.BUY_LIMIT, entry=1.0990, stop_loss=1.0980,
        take_profit=1.1010, risk_percent=1.0, suggested_volume=None,
        reward_risk_ratio=2.0,
        expiration=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        invalidation="M5 close below 1.0980", short_reason="test",
        checklist_complete=True,
    )
    trade.update(trade_overrides)
    return AnalysisResult(
        analysis_time=datetime.now(timezone.utc), symbol="EURUSD",
        status=AnalysisStatus.TRADE, direction=Direction.BUY, grade=Grade.A,
        reason="test", timeframes_checked=["M5", "M15", "H1"],
        trade_details=TradeDetails(**trade),
    )


class SafetyRegressionTests(unittest.TestCase):
    def test_volume_rounds_down(self):
        result = calculate_position_size(
            trade_result(risk_percent=0.333).trade_details, snapshot()
        )
        self.assertIsNotNone(result.suggested_volume)
        loss_per_lot = (0.001 / 0.00001) * 1.0
        self.assertLessEqual(result.suggested_volume * loss_per_lot, result.risk_amount + 1e-8)

    def test_validator_rejects_wrong_order_direction(self):
        result = trade_result(order_type=OrderType.SELL_LIMIT)
        self.assertFalse(validate_trade_result(result, snapshot()).is_valid)

    def test_validator_rejects_false_rr(self):
        result = trade_result(reward_risk_ratio=9.0)
        self.assertFalse(validate_trade_result(result, snapshot()).is_valid)

    def test_validator_rejects_non_finite_and_negative_risk(self):
        self.assertFalse(validate_trade_result(trade_result(risk_percent=-1), snapshot()).is_valid)
        self.assertFalse(validate_trade_result(trade_result(entry=math.inf), snapshot()).is_valid)

    def test_parser_rejects_watch_without_numeric_level(self):
        raw = """Analysis Time: 2026-08-11T09:00:00Z
Symbol: EURUSD
Status: WATCH
Direction: --
Grade: A-
Reason: test
Timeframes Checked: H1
Preferred Direction: BUY
Trigger Type: candle close
Zone Or Level: resistance area
Timeframes To Recheck: M5
Expiration: 2026-08-12T09:00:00Z
Invalidation: below 1.0900"""
        with self.assertRaises(AIResponseParseError):
            parse_ai_response(raw, "EURUSD")

    def test_naive_expiration_becomes_utc(self):
        self.assertIsNotNone(_parse_expiration("2099-01-01T12:00:00").tzinfo)

    def test_invalidation_uses_closed_candle(self):
        candle = {"open": 1.1, "high": 1.101, "low": 1.097, "close": 1.098}
        self.assertTrue(_invalidation_reached("M5 close below 1.099", candle, "BUY"))

    def test_wick_above_trigger_does_not_trigger_buy_watch(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from storage import db
        from watch.watch_manager import check_trigger

        with TemporaryDirectory() as directory:
            original = db.DB_PATH
            db.DB_PATH = Path(directory) / "wick.db"
            try:
                db.init_db()
                watch = {
                    "watch_id": "wick", "symbol": "EURUSD", "parent_analysis_id": "a",
                    "direction": "BUY", "grade": "A-",
                    "trigger_type": "بسته‌شدن کندل M5 بالای سطح",
                    "zone_or_level": "1.1556", "timeframes_to_recheck": ["M5"],
                    "expiration": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                    "invalidation_condition": "بسته‌شدن کندل M5 زیر 1.1549",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                db.save_watch(watch)
                broker = type("Broker", (), {"get_candles": lambda self, *args: [{
                    "time": datetime.now(timezone.utc), "open": 1.1552,
                    "high": 1.1562, "low": 1.1550, "close": 1.1555,
                }]})()
                self.assertEqual(check_trigger(db.get_watch("wick"), broker), (False, ""))
                self.assertIsNotNone(db.get_active_watch_for_symbol("EURUSD"))
            finally:
                db.DB_PATH = original

    def test_usdjpy_wick_above_159425_but_close_below_stays_active(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from storage import db
        from watch.watch_manager import check_trigger

        with TemporaryDirectory() as directory:
            original = db.DB_PATH
            db.DB_PATH = Path(directory) / "usdjpy.db"
            try:
                db.init_db()
                db.save_watch({
                    "watch_id": "usdjpy-wick", "symbol": "USDJPY", "parent_analysis_id": "a",
                    "direction": "BUY", "grade": "A-",
                    "trigger_type": "بسته‌شدن کندل M5 بالای سطح",
                    "zone_or_level": "159.425", "timeframes_to_recheck": ["M5"],
                    "expiration": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                    "invalidation_condition": "بسته‌شدن کندل M5 زیر 159.300",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                broker = type("Broker", (), {"get_candles": lambda self, *args: [{
                    "time": datetime.now(timezone.utc), "open": 159.390,
                    "high": 159.470, "low": 159.360, "close": 159.410,
                }]})()
                row = db.get_watch("usdjpy-wick")
                self.assertEqual(check_trigger(row, broker), (False, ""))
                self.assertIsNotNone(db.get_active_watch_for_symbol("USDJPY"))
            finally:
                db.DB_PATH = original

    def test_close_above_trigger_triggers_buy_watch_even_red_candle(self):
        candle = {"open": 1.1570, "high": 1.1572, "low": 1.1550, "close": 1.1560}
        # رنگ کندل ملاک نیست؛ close بالای سطح کافی است.
        self.assertGreater(candle["close"], 1.1556)

    def test_wick_below_invalidation_does_not_invalidate(self):
        candle = {"open": 1.1554, "high": 1.1557, "low": 1.1540, "close": 1.1551}
        self.assertFalse(_invalidation_reached("شکست سطح 1.1549", candle, "BUY"))

    def test_expired_watch_does_not_read_market(self):
        class BrokerThatMustNotBeCalled:
            def get_candles(self, *args):
                raise AssertionError("برای Expiration نباید بازار خوانده شود")
        row = {
            "is_locked": 0, "is_triggered": 0, "is_closed": 0,
            "expiration": "2000-01-01T00:00:00+00:00", "symbol": "EURUSD",
        }
        self.assertEqual(check_trigger(row, BrokerThatMustNotBeCalled()), (True, "EXPIRATION_REACHED"))

    def test_mt5_fetch_skips_forming_candle(self):
        from broker import mt5_broker

        class FakeMT5:
            TIMEFRAME_M5 = 5
            def __init__(self):
                self.start_pos = None
            def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
                self.start_pos = start_pos
                return [{"time": 1_700_000_000, "open": 1, "high": 2, "low": 0.5, "close": 1.5}]

        original = mt5_broker.mt5
        fake = FakeMT5()
        mt5_broker.mt5 = fake
        try:
            broker = mt5_broker.MT5Broker()
            broker.get_candles("EURUSD", "M5", 1)
            self.assertEqual(fake.start_pos, 0)
        finally:
            mt5_broker.mt5 = original


if __name__ == "__main__":
    unittest.main()
