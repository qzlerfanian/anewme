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
            self.assertEqual(fake.start_pos, 1)
        finally:
            mt5_broker.mt5 = original


if __name__ == "__main__":
    unittest.main()
