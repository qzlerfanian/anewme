"""Offline regression: feed failures must never be labelled market closed."""
import unittest
import os
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from broker.mt5_broker import MT5Broker
from core import clock


class FeedStatusTests(unittest.TestCase):
    def setUp(self):
        clock.reset()
        self.addCleanup(clock.reset)
        setting = patch.dict(os.environ, {'MT5_DATA_UTC_OFFSET_MINUTES': '0'})
        setting.start()
        self.addCleanup(setting.stop)
        self.api = MagicMock()
        self.api.SYMBOL_TRADE_MODE_DISABLED = 0
        self.api.terminal_info.return_value = SimpleNamespace(connected=True)
        self.api.symbol_info.return_value = SimpleNamespace(trade_mode=4)
        self.api.symbol_info_tick.return_value = SimpleNamespace(time=datetime.now(timezone.utc).timestamp()-1)
        self.patcher = patch('broker.mt5_broker.mt5', self.api)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.broker = MT5Broker()

    def test_fresh_tick_accepted(self):
        self.assertTrue(self.broker.is_market_open('EURUSD'))

    def test_future_tick_reports_clock_not_closed(self):
        self.api.symbol_info_tick.return_value.time += 7200
        with self.assertRaisesRegex(RuntimeError, 'age_seconds=-'):
            self.broker.is_market_open('EURUSD')

    def test_broker_clock_ahead_of_windows_is_accepted(self):
        self.api.symbol_info_tick.return_value.time += 11313
        clock.anchor_to_broker(self.api.symbol_info_tick.return_value.time)
        self.assertTrue(self.broker.is_market_open('EURUSD'))

    def test_broker_clock_behind_windows_is_accepted(self):
        self.api.symbol_info_tick.return_value.time -= 11313
        clock.anchor_to_broker(self.api.symbol_info_tick.return_value.time)
        self.assertTrue(self.broker.is_market_open('EURUSD'))

    def test_stopped_feed_ages_even_with_broker_clock(self):
        with patch('core.clock.time.monotonic', return_value=100):
            clock.anchor_to_broker(self.api.symbol_info_tick.return_value.time)
        with patch('core.clock.time.monotonic', return_value=401):
            with self.assertRaisesRegex(RuntimeError, 'به‌روز نشده'):
                self.broker.is_market_open('EURUSD')

    def test_sync_requires_advance_and_anchors_new_tick(self):
        self.api.symbol_select.side_effect = lambda symbol, selected: symbol == 'EURUSD'
        raw = datetime.now(timezone.utc).timestamp() + 11313
        self.api.symbol_info_tick.side_effect = [SimpleNamespace(time=raw), SimpleNamespace(time=raw+1)]
        self.broker._synchronize_clock()
        self.assertLess(abs(clock.utc_now().timestamp() - raw - 1), 1)

    def test_cached_tick_cannot_initialize_clock(self):
        with patch('broker.mt5_broker.time.monotonic', side_effect=[0, 16]):
            with self.assertRaisesRegex(RuntimeError, 'تیک جدید'):
                self.broker._synchronize_clock()

    def test_candle_zero_excluded_with_broker_clock(self):
        raw = datetime.now(timezone.utc).timestamp() + 11313
        clock.anchor_to_broker(raw)
        self.api.TIMEFRAME_M5 = 5
        self.api.copy_rates_from_pos.return_value = [
            dict(time=raw-600, open=1.1, high=1.2, low=1.0, close=1.15),
            dict(time=raw-300, open=1.1, high=1.2, low=1.0, close=1.15),
            dict(time=raw, open=1.1, high=1.3, low=1.0, close=1.25)]
        candles = self.broker.get_candles('EURUSD', 'M5', 2)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[-1]['source_index'], 1)
        self.assertEqual(candles[-1]['close'], 1.15)
        self.assertEqual(candles[-1]['close_time'].timestamp(), raw)

    def test_verified_broker_raw_time_converts_to_utc_and_tehran(self):
        self.broker.data_utc_offset_minutes = 180
        raw = datetime(2026, 8, 28, 18, 19, tzinfo=timezone.utc).timestamp()
        normalized = self.broker._data_time_utc(raw)
        self.assertEqual(normalized.hour, 15)
        self.assertEqual(clock.tehran_time(normalized), '2026-08-28 18:49 به وقت تهران')

    def test_verified_offset_applied_to_anchor_and_candles_once(self):
        self.broker.data_utc_offset_minutes = 180
        raw = datetime(2026, 8, 28, 18, 25, tzinfo=timezone.utc).timestamp()
        self.api.symbol_select.side_effect = lambda symbol, selected: symbol == 'EURUSD'
        self.api.symbol_info_tick.side_effect = [SimpleNamespace(time=raw), SimpleNamespace(time=raw+1)]
        self.broker._synchronize_clock()
        self.assertEqual(clock.utc_now().hour, 15)
        self.api.copy_rates_from_pos.return_value = [
            dict(time=raw-300, open=1.1, high=1.2, low=1.0, close=1.15),
            dict(time=raw, open=1.1, high=1.3, low=1.0, close=1.25)]
        candle = self.broker.get_candles('EURUSD', 'M5', 1)[0]
        self.assertEqual(candle['close_time'].timestamp(), raw-10800)
        self.assertEqual(candle['raw_broker_open_seconds'], raw-300)
        self.assertEqual(clock.tehran_time(candle['close_time']), '2026-08-28 18:55 به وقت تهران')

    def test_tehran_display_handles_date_rollover(self):
        self.assertEqual(clock.tehran_time('2026-08-28T22:00:00+00:00'), '2026-08-29 01:30 به وقت تهران')
        self.assertEqual(clock.tehran_time('15:19 UTC'), '18:49 به وقت تهران')

    def test_message_converts_analysis_and_candle_without_mutating(self):
        from core.models import AnalysisResult, AnalysisStatus
        from telegram_bot.notifier import format_analysis_message
        result = AnalysisResult(datetime(2026,8,28,15,19,tzinfo=timezone.utc), 'EURUSD',
                                AnalysisStatus.NO_TRADE, None, None, 'test', [],
                                last_closed_m5_time='2026-08-28 15:15 UTC')
        message = format_analysis_message(result)
        self.assertIn('18:49 به وقت تهران', message)
        self.assertIn('18:45 به وقت تهران', message)
        self.assertEqual(result.analysis_time.hour, 15)
        self.assertEqual(result.last_closed_m5_time, '2026-08-28 15:15 UTC')

    def test_stale_tick_reports_data_not_closed(self):
        self.api.symbol_info_tick.return_value.time -= 600
        with self.assertRaisesRegex(RuntimeError, 'به‌روز نشده'):
            self.broker.is_market_open('EURUSD')

    def test_disconnection_reported(self):
        self.api.terminal_info.return_value.connected = False
        with self.assertRaisesRegex(RuntimeError, 'اتصال ترمینال'):
            self.broker.is_market_open('EURUSD')

    def test_disabled_symbol_reported(self):
        self.api.symbol_info.return_value.trade_mode = 0
        with self.assertRaisesRegex(RuntimeError, 'غیرفعال'):
            self.broker.is_market_open('EURUSD')

    def test_missing_tick_reported(self):
        self.api.symbol_info_tick.return_value = None
        with self.assertRaisesRegex(RuntimeError, 'تیک قیمت دریافت نشد'):
            self.broker.is_market_open('EURUSD')
