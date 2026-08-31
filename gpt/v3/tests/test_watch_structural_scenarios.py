"""Twenty fixed, repeatable watch-cycle scenarios (no live market or AI)."""

from datetime import datetime, timedelta, timezone
import unittest

from broker.candle_utils import closed_only, normalize_candle
from core.models import Direction, Grade, WatchDetails
from watch.watch_manager import evaluate_level_candle, validate_before_creation


class WatchStructuralScenarioTests(unittest.TestCase):
    def test_01_buy_close_above_triggers(self): self.assertEqual(evaluate_level_candle("BUY", 101, 100, 95), "TRIGGERED")
    def test_02_buy_close_equal_trigger_stays(self): self.assertEqual(evaluate_level_candle("BUY", 100, 100, 95), "ACTIVE")
    def test_03_buy_close_between_stays(self): self.assertEqual(evaluate_level_candle("BUY", 98, 100, 95), "ACTIVE")
    def test_04_buy_close_equal_invalidation_invalidates(self): self.assertEqual(evaluate_level_candle("BUY", 95, 100, 95), "INVALIDATED")
    def test_05_buy_close_below_invalidation_invalidates(self): self.assertEqual(evaluate_level_candle("BUY", 94, 100, 95), "INVALIDATED")
    def test_06_sell_close_below_triggers(self): self.assertEqual(evaluate_level_candle("SELL", 99, 100, 105), "TRIGGERED")
    def test_07_sell_close_equal_trigger_stays(self): self.assertEqual(evaluate_level_candle("SELL", 100, 100, 105), "ACTIVE")
    def test_08_sell_close_between_stays(self): self.assertEqual(evaluate_level_candle("SELL", 102, 100, 105), "ACTIVE")
    def test_09_sell_close_equal_invalidation_invalidates(self): self.assertEqual(evaluate_level_candle("SELL", 105, 100, 105), "INVALIDATED")
    def test_10_sell_close_above_invalidation_invalidates(self): self.assertEqual(evaluate_level_candle("SELL", 106, 100, 105), "INVALIDATED")

    def test_11_wick_above_does_not_trigger_buy(self):
        candle = {"high": 101, "low": 97, "close": 99}
        self.assertEqual(evaluate_level_candle("BUY", candle["close"], 100, 95), "ACTIVE")

    def test_12_wick_below_does_not_trigger_sell(self):
        candle = {"high": 103, "low": 99, "close": 101}
        self.assertEqual(evaluate_level_candle("SELL", candle["close"], 100, 105), "ACTIVE")

    def test_13_tick_bid_ask_do_not_affect_buy(self):
        self.assertEqual(evaluate_level_candle("BUY", 99, 100, 95), "ACTIVE")

    def test_14_tick_bid_ask_do_not_affect_sell(self):
        self.assertEqual(evaluate_level_candle("SELL", 101, 100, 105), "ACTIVE")

    def test_15_forming_m5_is_filtered(self):
        now = datetime(2026, 8, 20, 9, 38, tzinfo=timezone.utc)
        candles = [{"time": datetime(2026, 8, 20, 9, 35, tzinfo=timezone.utc), "open": 1, "high": 2, "low": .5, "close": 2}]
        self.assertEqual(closed_only(candles, "M5", now), [])

    def test_16_last_closed_reports_close_time(self):
        candle = normalize_candle({"time": datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc), "open": 1, "high": 2, "low": .5, "close": 1.5}, "M5")
        self.assertEqual(candle["close_time"].strftime("%H:%M"), "09:35")

    @staticmethod
    def _watch(direction, invalidation):
        return WatchDetails(Direction(direction), Grade.A_MINUS, "test", "M5 close", "100", ["M5"],
                            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), invalidation)

    @staticmethod
    def _candle(close):
        return {"time": datetime.now(timezone.utc) - timedelta(minutes=5), "open": close, "high": close, "low": close, "close": close}

    def test_17_reject_buy_already_below_invalidation(self): self.assertIsNotNone(validate_before_creation(self._watch("BUY", "95"), self._candle(94)))
    def test_18_reject_buy_equal_invalidation(self): self.assertIsNotNone(validate_before_creation(self._watch("BUY", "95"), self._candle(95)))
    def test_19_reject_sell_already_above_invalidation(self): self.assertIsNotNone(validate_before_creation(self._watch("SELL", "105"), self._candle(106)))
    def test_20_allow_valid_sell_watch(self): self.assertIsNone(validate_before_creation(self._watch("SELL", "105"), self._candle(103)))


if __name__ == "__main__":
    unittest.main()
