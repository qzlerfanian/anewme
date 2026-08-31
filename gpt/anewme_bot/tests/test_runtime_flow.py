"""Offline integration scenarios through actual DB, monitor, queue and delivery."""
import asyncio
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, AsyncMock, patch

from broker.mock_broker import MockBroker
from config import config
from core.analysis_service import AnalysisService
from core.models import AnalysisResult, AnalysisStatus, Direction, Grade, WatchDetails
from core.worker import AnalysisWorker
from storage import db, work_queue
from watch import watch_manager
from watch.conditions import parse_conditions, expiration
from watch.monitor_loop import WatchMonitor
from telegram_bot.delivery import DeliveryWorker


class RuntimeFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.original = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / 'test.db'
        db.init_db()
        self.now = datetime.now(timezone.utc)
        self.allow = patch.object(config, 'telegram_allowed_user_ids', (123,))
        self.allow.start()
        self.broker = MockBroker()
        self.broker.is_market_open = lambda symbol: True
        self.broker.get_candles = MagicMock(return_value=[self.bar(1.1560)])
        self.service = MagicMock()
        self.service.run_watch_recheck.return_value = AnalysisResult(self.now, 'EURUSD', AnalysisStatus.NO_TRADE,
                                                                     Direction.BUY, Grade.A_MINUS, 'M5 confirmation insufficient', [])

    def tearDown(self):
        self.allow.stop()
        db.DB_PATH = self.original
        self.temp.cleanup()

    def bar(self, close, minutes=10, high=None, low=None):
        return dict(time=self.now-timedelta(minutes=minutes), open=close,
                    high=high or close+.0002, low=low or close-.0002, close=close)

    def watch(self, **kwargs):
        row = dict(watch_id='w', symbol='EURUSD', direction='BUY', grade='A-', parent_analysis_id=None,
                   trigger_type='M5 close', zone_or_level='1.1556', timeframes_to_recheck=['M5'],
                   expiration=(self.now+timedelta(hours=1)).isoformat(),
                   invalidation_condition='M5 close below 1.1549',
                   created_at=(self.now-timedelta(hours=1)).isoformat())
        row.update(kwargs)
        db.save_watch(row)
        return db.get_watch(row['watch_id'])

    def query(self, sql):
        with db.get_connection() as conn:
            return conn.execute(sql).fetchall()

    async def monitor(self):
        await WatchMonitor(self.broker, self.service)._tick()

    async def test_01_trigger_queues_without_calling_ai(self):
        self.watch()
        await self.monitor()
        self.service.run_watch_recheck.assert_not_called()
        self.assertEqual(db.get_watch('w')['close_status'], 'TRIGGERED')
        self.assertEqual(len(self.query('SELECT * FROM analysis_jobs')), 1)

    async def test_02_poll_twice_calls_reanalysis_once(self):
        self.watch()
        await self.monitor()
        await self.monitor()
        worker = AnalysisWorker(self.service)
        await worker.tick()
        await worker.tick()
        self.service.run_watch_recheck.assert_called_once()

    async def test_03_buy_wick_only_stays_active(self):
        self.watch()
        self.broker.get_candles.return_value = [self.bar(1.1553, high=1.157)]
        await self.monitor()
        self.assertIsNotNone(db.get_active_watch_for_symbol('EURUSD'))

    async def test_04_forming_bar_never_triggers(self):
        self.watch()
        self.broker.get_candles.return_value = [self.bar(1.157, minutes=1)]
        await self.monitor()
        self.assertIsNotNone(db.get_active_watch_for_symbol('EURUSD'))

    async def test_05_sell_trigger_uses_close(self):
        self.watch(direction='SELL', invalidation_condition='M5 close above 1.1570')
        self.broker.get_candles.return_value = [self.bar(1.1550)]
        await self.monitor()
        self.assertEqual(db.get_watch('w')['close_status'], 'TRIGGERED')

    async def test_06_equal_trigger_stays_active(self):
        self.watch()
        self.broker.get_candles.return_value = [self.bar(1.1556)]
        await self.monitor()
        self.assertIsNotNone(db.get_active_watch_for_symbol('EURUSD'))

    async def test_07_equal_invalidation_closes(self):
        self.watch()
        self.broker.get_candles.return_value = [self.bar(1.1549)]
        await self.monitor()
        self.assertEqual(db.get_watch('w')['close_status'], 'INVALIDATED')
        self.assertEqual(self.query('SELECT * FROM analysis_jobs'), [])

    async def test_08_expiration_does_not_call_ai(self):
        self.watch(expiration=(self.now-timedelta(seconds=1)).isoformat())
        await self.monitor()
        self.assertEqual(db.get_watch('w')['close_status'], 'EXPIRED')
        self.service.run_watch_recheck.assert_not_called()

    async def test_09_bad_symbol_does_not_block_next(self):
        self.watch(watch_id='bad', symbol='BAD')
        self.watch()
        def get(symbol, tf, count):
            if symbol == 'BAD':
                raise RuntimeError('unavailable')
            return [self.bar(1.1560)]
        self.broker.get_candles.side_effect = get
        await self.monitor()
        self.assertEqual(db.get_watch('w')['close_status'], 'TRIGGERED')

    async def test_10_latest_closed_only_ignores_earlier_invalidation(self):
        self.watch()
        self.broker.get_candles.return_value = [self.bar(1.1540, minutes=15), self.bar(1.1560)]
        await self.monitor()
        self.assertEqual(db.get_watch('w')['close_status'], 'TRIGGERED')

    async def test_11_evidence_persists_exact_close_time(self):
        self.watch()
        await self.monitor()
        self.assertEqual(db.get_watch('w')['triggered_at'], (self.now-timedelta(minutes=5)).isoformat())

    async def test_12_notification_failure_is_not_sent(self):
        work_queue.queue_message('x', 123, 'hello')
        bot = MagicMock(send_message=AsyncMock(side_effect=RuntimeError('offline')))
        await DeliveryWorker(bot).tick()
        row = self.query('SELECT * FROM outbox')[0]
        self.assertEqual(row['status'], 'PENDING')
        self.assertIsNone(row['sent_at'])
        self.assertEqual(row['attempts'], 1)

    async def test_13_delivery_success_is_not_repeated(self):
        work_queue.queue_message('x', 123, 'hello')
        bot = MagicMock(send_message=AsyncMock())
        worker = DeliveryWorker(bot)
        await worker.tick()
        await worker.tick()
        bot.send_message.assert_awaited_once()
        self.assertEqual(self.query('SELECT * FROM outbox')[0]['status'], 'SENT')

    async def test_14_dedup_and_safe_unicode_chunks(self):
        for _ in range(2):
            work_queue.queue_message('x', 123, '😀' * 4000)
        rows = self.query('SELECT * FROM outbox')
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(len(r['text'].encode('utf-16-le'))//2 <= 4096 for r in rows))

    async def test_15_restart_running_job_is_not_replayed(self):
        work_queue.enqueue('x', 'EURUSD', 123)
        work_queue.claim()
        worker = AnalysisWorker(self.service)
        worker.recover()
        await worker.tick()
        self.service.run_initial_analysis.assert_not_called()
        self.assertIn('NO_TRADE', self.query('SELECT * FROM outbox')[0]['text'])

    async def test_16_same_symbol_queue_deduplicates(self):
        self.assertTrue(work_queue.enqueue('a', 'EURUSD', 123))
        self.assertFalse(work_queue.enqueue('b', 'EURUSD', 123))
        self.assertTrue(work_queue.enqueue('c', 'GBPUSD', 123))

    async def test_17_transaction_rolls_back_watch_and_analysis(self):
        with self.assertRaises(RuntimeError):
            with db.transaction():
                self.watch()
                raise RuntimeError('crash')
        self.assertIsNone(db.get_watch('w'))

    async def test_18_expired_callback_does_not_start_analysis(self):
        from telegram.error import BadRequest
        from telegram_bot.handlers import analyze_symbol_callback
        update = MagicMock()
        update.update_id = 17
        update.effective_user.id = 123
        update.effective_chat.id = 123
        update.callback_query.answer = AsyncMock(side_effect=BadRequest('Query is too old and response timeout expired'))
        await analyze_symbol_callback(update, MagicMock())
        self.assertEqual(self.query('SELECT * FROM analysis_jobs'), [])
        self.assertIn('منقضی', self.query('SELECT * FROM outbox')[0]['text'])

    async def test_19_callback_returns_without_waiting_for_analysis(self):
        from telegram_bot.handlers import analyze_symbol_callback
        update = MagicMock()
        update.update_id = 18
        update.effective_user.id = 123
        update.effective_chat.id = 123
        update.callback_query.data = 'analyze:EURUSD'
        update.callback_query.answer = AsyncMock()
        await analyze_symbol_callback(update, MagicMock())
        self.assertEqual(len(self.query('SELECT * FROM analysis_jobs')), 1)
        self.assertEqual(self.query('SELECT * FROM analysis_jobs')[0]['status'], 'PENDING')

    async def test_20_quota_error_is_not_retried(self):
        import httpx
        from openai import RateLimitError
        from core.ai_client import AIClient
        client = AIClient.__new__(AIClient)
        client.client = MagicMock()
        exc = RateLimitError('no credits', response=httpx.Response(429, request=httpx.Request('POST', 'https://example.invalid')),
                             body={'code': 'credit_balance_exhausted'})
        client.client.chat.completions.create.side_effect = exc
        with self.assertRaises(RuntimeError):
            client._call_with_retry([], 10, 'test')
        self.assertEqual(client.client.chat.completions.create.call_count, 1)

    async def test_21_chart_description_reuses_identical_input(self):
        from core.ai_client import AIClient
        client = AIClient.__new__(AIClient)
        client.describe_chart_image = MagicMock(return_value='H1 stable')
        chart = Path(self.temp.name) / 'EURUSD_H1_test.png'
        chart.write_bytes(b'offline image fixture')
        client.describe_all_charts([chart], 'EURUSD')
        client.describe_all_charts([chart], 'EURUSD')
        client.describe_chart_image.assert_called_once()

    async def test_22_existing_watch_needs_no_snapshot(self):
        self.watch()
        self.broker.get_market_snapshot = MagicMock(side_effect=AssertionError('not needed'))
        result = AnalysisService(self.broker, self.service).run_initial_analysis('EURUSD')
        self.assertEqual(result.status, AnalysisStatus.WATCH)

    async def test_23_invalid_expiration_is_not_guessed(self):
        for text in ('later', '2000-01-01T00:00:00Z', '99:99'):
            with self.assertRaises(ValueError):
                expiration(text)

    async def test_24_trailing_timeframe_not_price(self):
        trig, inv = parse_conditions('BUY', 'M5 close', '1.1556', 'close below 1.1549 on M5')
        self.assertEqual(inv.level, 1.1549)
        self.assertEqual(trig.timeframe, 'M5')

    async def test_25_overlap_rejected(self):
        with self.assertRaises(ValueError):
            parse_conditions('BUY', 'M5 close', '1.1556', 'below 1.1560')

    async def test_26_unsupported_time_trigger_rejected(self):
        with self.assertRaises(ValueError):
            parse_conditions('BUY', 'specific time', '12:30', 'below 1.1549')

    async def test_27_redaction_removes_token(self):
        from core.runtime import redact
        self.assertNotIn('123456:ABC-secret', redact('https://api.telegram.org/bot123456:ABC-secret/getUpdates'))

    async def test_28_claim_is_atomic_under_parallel_callers(self):
        self.watch()
        outcomes = await asyncio.gather(*(asyncio.to_thread(db.claim_watch_trigger, 'w', 'confirmed') for _ in range(8)))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(len(self.query('SELECT * FROM analysis_jobs')), 1)


    async def test_29_account_position_blocks_trigger(self):
        self.watch()
        self.broker.get_open_positions = lambda symbol: [{'ticket': 1}]
        await self.monitor()
        self.assertEqual(db.get_watch('w')['close_status'], 'INVALIDATED')
        self.assertEqual(len(self.query('SELECT * FROM analysis_jobs')), 0)

    async def test_30_outbox_preserves_recipient_order_during_retry(self):
        work_queue.queue_message('first', 123, 'first')
        work_queue.queue_message('second', 123, 'second')
        work_queue.queue_message('other', 456, 'other')
        first = work_queue.pending_messages()[0]
        work_queue.delivery_failed(first, 'offline', delay=60)
        self.assertEqual([r['chat_id'] for r in work_queue.pending_messages()], [456])

    async def test_31_duplicate_message_logs_without_db_lock(self):
        work_queue.queue_message('same', 123, 'text')
        work_queue.queue_message('same', 123, 'text')
        self.assertEqual(len(self.query('SELECT * FROM outbox')), 1)

    async def test_broker_clock_offset_keeps_monitor_trigger_consistent(self):
        from core import clock
        clock.anchor_to_broker(self.now.timestamp() + 11313)
        try:
            self.now = clock.utc_now()
            self.broker.get_candles.return_value = [self.bar(1.1560)]
            self.watch()
            await self.monitor()
            self.assertEqual(db.get_watch('w')['close_status'], 'TRIGGERED')
            self.assertEqual(len(self.query('SELECT * FROM analysis_jobs')), 1)
        finally:
            clock.reset()

    async def test_broker_clock_offset_still_rejects_forming_candle(self):
        from core import clock
        clock.anchor_to_broker(self.now.timestamp() + 11313)
        try:
            self.now = clock.utc_now()
            self.broker.get_candles.return_value = [self.bar(1.1560, minutes=1)]
            self.watch()
            await self.monitor()
            self.assertIsNotNone(db.get_active_watch_for_symbol('EURUSD'))
        finally:
            clock.reset()

    def jpy_watch(self, **kwargs):
        return self.watch(symbol='USDJPY', zone_or_level='159.900',
                          invalidation_condition='M5 close below 159.800', **kwargs)

    async def test_jpy_159899_is_not_triggered_even_with_high_and_live_above(self):
        self.jpy_watch()
        self.broker.get_candles.return_value = [self.bar(159.899, high=160.1)]
        self.broker.get_current_price = lambda symbol: (160.1, 160.2)
        await self.monitor()
        self.assertIsNotNone(db.get_active_watch_for_symbol('USDJPY'))
        self.assertEqual(len(self.query('SELECT * FROM analysis_jobs')), 0)
        messages = self.query("SELECT * FROM outbox WHERE id LIKE 'watch-check:%'")
        self.assertEqual(len(messages), 1)
        self.assertIn('159.899', messages[0]['text'])
        self.assertIn('159.900', messages[0]['text'])
        self.assertIn('تریگر فعال نشد', messages[0]['text'])

    async def test_jpy_equality_not_triggered(self):
        self.jpy_watch()
        self.broker.get_candles.return_value = [self.bar(159.900)]
        await self.monitor()
        self.assertIsNotNone(db.get_active_watch_for_symbol('USDJPY'))

    async def test_jpy_strictly_above_triggers_once(self):
        self.jpy_watch()
        self.broker.get_candles.return_value = [self.bar(159.901)]
        await self.monitor()
        await self.monitor()
        self.assertEqual(db.get_watch('w')['close_status'], 'TRIGGERED')
        self.assertEqual(len(self.query('SELECT * FROM analysis_jobs')), 1)
        self.assertEqual(len(self.query("SELECT * FROM outbox WHERE id LIKE 'watch-check:%'")), 0)

    async def test_old_crossing_does_not_trigger_latest_failed_close(self):
        self.jpy_watch()
        self.broker.get_candles.return_value = [self.bar(160.0, minutes=15), self.bar(159.899)]
        await self.monitor()
        self.assertIsNotNone(db.get_active_watch_for_symbol('USDJPY'))
        self.assertEqual(len(self.query('SELECT * FROM analysis_jobs')), 0)

    async def test_failed_close_waits_and_next_close_triggers(self):
        self.jpy_watch()
        self.broker.get_candles.return_value = [self.bar(159.899)]
        await self.monitor()
        self.assertIsNotNone(db.get_active_watch_for_symbol('USDJPY'))
        self.broker.get_candles.return_value = [self.bar(159.901, minutes=5)]
        await self.monitor()
        self.assertEqual(db.get_watch('w')['close_status'], 'TRIGGERED')

    async def test_diagnostic_one_per_candle_and_survives_monitor_recreation(self):
        self.jpy_watch()
        self.broker.get_candles.return_value = [self.bar(159.899)]
        await self.monitor()
        await self.monitor()
        self.assertEqual(len(self.query("SELECT * FROM outbox WHERE id LIKE 'watch-check:%'")), 1)
        self.broker.get_candles.return_value = [self.bar(159.899, minutes=5)]
        await self.monitor()
        self.assertEqual(len(self.query("SELECT * FROM outbox WHERE id LIKE 'watch-check:%'")), 2)

    async def test_forming_close_does_not_trigger_or_make_extra_report(self):
        self.jpy_watch()
        self.broker.get_candles.return_value = [self.bar(159.899), self.bar(160.0, minutes=1)]
        await self.monitor()
        self.assertIsNotNone(db.get_active_watch_for_symbol('USDJPY'))
        self.assertEqual(len(self.query("SELECT * FROM outbox WHERE id LIKE 'watch-check:%'")), 1)

    async def test_closed_watch_pending_active_report_is_suppressed(self):
        self.jpy_watch()
        self.broker.get_candles.return_value = [self.bar(159.899)]
        await self.monitor()
        db.close_watch_lifecycle('w', 'INVALIDATED', 'test')
        bot = MagicMock(send_message=AsyncMock())
        await DeliveryWorker(bot).tick()
        bot.send_message.assert_not_called()
        self.assertEqual(self.query("SELECT * FROM outbox WHERE id LIKE 'watch-check:%'")[0]['status'], 'SUPPRESSED')

    def watch_result(self):
        details = WatchDetails(Direction.BUY, Grade.A_MINUS, 'waiting for close', 'M5 close',
                               '1.1556', ['M5'], (self.now+timedelta(hours=1)).isoformat(),
                               'M5 close below 1.1549')
        return AnalysisResult(self.now, 'EURUSD', AnalysisStatus.WATCH, Direction.BUY,
                              Grade.A_MINUS, 'waiting for confirmation', ['H1', 'M15', 'M5'], watch_details=details)

    async def test_32_creation_rechecks_latest_invalidation(self):
        service = AnalysisService(self.broker, self.service)
        snapshot = service._normalize_snapshot(self.broker.get_market_snapshot('EURUSD'))
        self.broker.get_candles.return_value = [self.bar(1.1549, minutes=6)]
        with patch('core.analysis_service.parse_ai_response', return_value=self.watch_result()):
            result = service._finalize('EURUSD', 'fixture', snapshot, [], None)
        self.assertEqual(result.status, AnalysisStatus.NO_TRADE)
        self.assertIsNone(db.get_active_watch_for_symbol('EURUSD'))

    async def test_33_creation_and_analysis_rollback_together(self):
        service = AnalysisService(self.broker, self.service)
        snapshot = service._normalize_snapshot(self.broker.get_market_snapshot('EURUSD'))
        self.broker.get_candles.return_value = [self.bar(1.1552, minutes=6)]
        with patch('core.analysis_service.parse_ai_response', return_value=self.watch_result()), \
             patch.object(db, 'save_analysis', side_effect=RuntimeError('disk failure')):
            with self.assertRaises(RuntimeError):
                service._finalize('EURUSD', 'fixture', snapshot, [], None)
        self.assertIsNone(db.get_active_watch_for_symbol('EURUSD'))

    async def test_34_valid_creation_persists_one_watch(self):
        service = AnalysisService(self.broker, self.service)
        snapshot = service._normalize_snapshot(self.broker.get_market_snapshot('EURUSD'))
        self.broker.get_candles.return_value = [self.bar(1.1552, minutes=6)]
        with patch('core.analysis_service.parse_ai_response', return_value=self.watch_result()):
            result = service._finalize('EURUSD', 'fixture', snapshot, [], None)
        self.assertEqual(result.status, AnalysisStatus.WATCH)
        self.assertEqual(len(self.query('SELECT * FROM watches')), 1)
        self.assertIsNotNone(db.get_active_watch_for_symbol('EURUSD')['conditions_json'])


if __name__ == '__main__':
    unittest.main()
