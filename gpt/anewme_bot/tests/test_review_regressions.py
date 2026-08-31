"""Offline review probes: assertions describe required behavior, not current bugs.

Run: python -X utf8 -m unittest review_probes -v
No network, credentials, terminal connection, or production database is used.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from broker import mt5_broker
from broker.mock_broker import MockBroker
from core.analysis_service import AnalysisService
from core.models import AnalysisStatus
from core.validator import validate_trade_result
from storage import db
from tests.test_safety_regression import snapshot, trade_result
from watch import watch_manager
from watch.monitor_loop import WatchMonitor
from watch.trade_tracker import TradeTracker


class ReviewProbes(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.original = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / 'review.db'
        db.init_db()
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        db.DB_PATH = self.original
        self.temp.cleanup()

    def make_watch(self, **changes):
        row = dict(watch_id='w', symbol='EURUSD', parent_analysis_id=None,
                   direction='BUY', grade='A-', trigger_type='M5 candle close',
                   zone_or_level='1.1556', timeframes_to_recheck=['M5'],
                   expiration=(self.now + timedelta(hours=1)).isoformat(),
                   invalidation_condition='M5 close below 1.1549',
                   created_at=(self.now - timedelta(hours=1)).isoformat())
        row.update(changes)
        db.save_watch(row)
        return db.get_watch('w')

    def candle(self, close):
        return dict(time=self.now - timedelta(minutes=10), open=close,
                    high=close + .0002, low=close - .0002, close=close)

    def test_account_api_error_must_not_mean_empty_account(self):
        fake = MagicMock()
        fake.positions_get.return_value = None
        fake.orders_get.return_value = None
        with patch.object(mt5_broker, 'mt5', fake):
            state, _ = AnalysisService(mt5_broker.MT5Broker(), MagicMock())._get_account_status('EURUSD')
        self.assertEqual(state, 'ACCOUNT_STATE_UNKNOWN')

    def test_trade_requires_actual_timeframe_data(self):
        self.assertFalse(validate_trade_result(trade_result(), snapshot()).is_valid)

    def test_invalidation_price_is_not_trailing_timeframe_number(self):
        self.assertFalse(watch_manager._invalidation_reached(
            'close below 1.1549 on M5', self.candle(1.1552), 'BUY'))

    def test_m15_invalidation_does_not_use_m5_close(self):
        row = self.make_watch(invalidation_condition='M15 close below 1.1549')
        broker = MagicMock()
        broker.get_open_positions.return_value = []
        broker.get_pending_orders.return_value = []
        broker.get_candles.side_effect = lambda symbol, tf, count: [
            self.candle(1.1540 if tf == 'M5' else 1.1552)]
        self.assertEqual(watch_manager.check_trigger(row, broker), (False, ''))

    def test_consumed_candle_cannot_move_backwards(self):
        self.make_watch()
        self.assertTrue(db.claim_watch_candle('w', '2026-08-27T10:10:00+00:00'))
        self.assertFalse(db.claim_watch_candle('w', '2026-08-27T10:05:00+00:00'))

    def test_closed_market_after_trigger_is_final_no_trade(self):
        self.make_watch()
        db.claim_watch_trigger('w', 'closed candle confirmed')
        broker = MockBroker()
        broker.is_market_open = lambda symbol: False
        result = AnalysisService(broker, MagicMock()).run_watch_recheck(db.get_watch('w'))
        self.assertEqual(result.status, AnalysisStatus.NO_TRADE)

    def test_old_bar_cannot_fill_new_trade(self):
        db.create_trade_tracking('a', 'EURUSD', 'BUY', 'BUY_LIMIT', 1.1000,
                                 1.0990, 1.1020, 1, 2,
                                 (self.now + timedelta(hours=1)).isoformat())
        broker = MagicMock()
        broker.get_current_price.return_value = (1.1008, 1.1010)
        broker.get_candles.return_value = [self.candle(1.1000)]
        TradeTracker(broker)._check_one(db.get_open_trade_trackings()[0])
        self.assertEqual(db.get_open_trade_trackings()[0]['status'], 'PENDING')

    def test_restart_recovers_triggered_but_unstarted_analysis(self):
        self.make_watch()
        db.claim_watch_trigger('w', 'process stopped before reanalysis')
        service = MagicMock()
        async def notify(text):
            pass
        from core.worker import AnalysisWorker
        from core.models import AnalysisResult
        service.run_watch_recheck.return_value = AnalysisResult(self.now, 'EURUSD', AnalysisStatus.NO_TRADE,
                                                               None, None, 'test', [])
        worker = AnalysisWorker(service)
        worker.recover()
        asyncio.run(worker.tick())
        service.run_watch_recheck.assert_called_once()


if __name__ == '__main__':
    unittest.main()
