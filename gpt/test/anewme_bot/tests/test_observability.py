"""Deterministic offline tests: no AI, MT5 or Telegram network access."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from broker.candle_utils import normalize_candle
from config import config
from core import clock
from core.analysis_service import AnalysisService
from core.audit import decode, encode, digest, RecordedBroker
from core.health import health_report
from core.models import MarketSnapshot, AnalysisStatus
from replay_analysis import load_capsule, replay
from storage import db, work_queue
from telegram_bot.notifier import format_analysis_message


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.original = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / 'live.db'
        db.init_db()
        self.now = datetime(2026, 8, 27, 12, 17, tzinfo=timezone.utc)
        clock.anchor_to_broker(self.now.timestamp())
        self.bars = {}
        for tf, minutes in (('M5', 5), ('M15', 15), ('H1', 60)):
            boundary = self.now.replace(minute=(self.now.minute // minutes) * minutes if minutes < 60 else 0)
            self.bars[tf] = [normalize_candle({'time': boundary - timedelta(minutes=minutes),
                'open': 1.1, 'high': 1.102, 'low': 1.099, 'close': 1.101}, tf)]
        self.snapshot = MarketSnapshot('EURUSD', 1.101, 1.1012, .0002, self.now, self.now, True,
                                      self.bars['M5'], self.bars['M15'], self.bars['H1'])
        self.broker = MagicMock()
        self.broker.get_open_positions.return_value = []
        self.broker.get_pending_orders.return_value = []
        self.broker.get_market_snapshot.return_value = self.snapshot
        self.broker.get_candles.side_effect = lambda symbol, tf, count: self.bars[tf]
        self.service = AnalysisService(self.broker, ai_client=object())

    def tearDown(self):
        db._local.job = None
        db.DB_PATH = self.original
        clock.reset()
        self.temp.cleanup()

    def raw(self, status='NO_TRADE'):
        return f'''Analysis Time: {self.now.isoformat()}
Symbol: EURUSD
Status: {status}
Direction: --
Grade: {'A-' if status == 'WATCH' else 'C'}
Reason: H1=صعودی; M15=صعودی; M5=انتظار; دلیل اصلی=انتظار تایید; شرط بعدی=بسته شدن کندل
Timeframes Checked: H1, M15, M5
Preferred Direction: BUY
Trigger Type: M5 close
Zone Or Level: 1.105
Timeframes To Recheck: M5
Expiration: {(self.now + timedelta(hours=1)).isoformat()}
Invalidation: M5 close below 1.095
'''

    def finalize(self, raw=None, parent=None):
        return self.service._finalize('EURUSD', raw or self.raw(), self.snapshot, [], parent)

    def capsule(self, result):
        return load_capsule(db.DB_PATH, result.analysis_id)

    def test_01_no_trade_replay_matches(self):
        result = self.finalize()
        self.assertEqual(result.status, AnalysisStatus.NO_TRADE)
        self.assertTrue(replay(self.capsule(result))['matched'])

    def test_02_watch_replay_matches_without_duplicate_live_watch(self):
        result = self.finalize(self.raw('WATCH'))
        self.assertEqual(result.status, AnalysisStatus.WATCH, result.reason)
        original = db.get_active_watch_for_symbol('EURUSD')['watch_id']
        report = replay(self.capsule(result))
        self.assertTrue(report['matched'], report)
        self.assertEqual(db.get_active_watch_for_symbol('EURUSD')['watch_id'], original)

    def test_03_parse_error_is_reproducible(self):
        result = self.finalize('invalid response')
        self.assertTrue(replay(self.capsule(result))['matched'])

    def test_04_account_guard_is_reproducible(self):
        self.broker.get_open_positions.return_value = [{'ticket': 123, 'type': 0, 'volume': .1}]
        result = self.finalize()
        self.assertIsNotNone(result.account_state)
        self.assertTrue(replay(self.capsule(result))['matched'])

    def test_05_failed_broker_read_is_reproducible(self):
        self.broker.get_open_positions.side_effect = RuntimeError('disconnected')
        result = self.finalize()
        self.assertTrue(replay(self.capsule(result))['matched'])

    def test_06_replay_twice_is_identical(self):
        capsule = self.capsule(self.finalize())
        self.assertEqual(replay(capsule), replay(capsule))

    def test_07_live_database_unchanged(self):
        capsule = self.capsule(self.finalize())
        before = Path(db.DB_PATH).read_bytes()
        replay(capsule)
        self.assertEqual(before, Path(db.DB_PATH).read_bytes())

    def test_08_tampered_capsule_rejected(self):
        result = self.finalize()
        with db.get_connection() as conn:
            conn.execute("UPDATE replay_capsules SET sha256='bad'")
        with self.assertRaises(ValueError):
            self.capsule(result)

    def test_09_wrong_build_rejected(self):
        capsule = self.capsule(self.finalize())
        capsule['build']['source_sha256'] = 'wrong'
        with self.assertRaises(ValueError):
            replay(capsule)

    def test_10_missing_record_not_silently_replayed(self):
        with self.assertRaises(ValueError):
            load_capsule(db.DB_PATH, 'absent')

    def test_11_passport_and_message_have_identity(self):
        result = self.finalize()
        with db.get_connection() as conn:
            row = conn.execute('SELECT payload FROM decision_passports WHERE analysis_id=?', (result.analysis_id,)).fetchone()
        passport = json.loads(row[0])
        self.assertEqual(passport['result']['status'], result.status.value)
        self.assertEqual(passport['last_candles']['M5']['close'], 1.101)
        self.assertIn(result.analysis_id, format_analysis_message(result))

    def test_12_saved_capsule_contains_no_config_credentials(self):
        with patch.object(config, 'openai_api_key', 'DO_NOT_STORE_THIS'), patch.object(config, 'telegram_token', 'NEITHER_THIS'):
            capsule = self.capsule(self.finalize())
        text = json.dumps(capsule)
        self.assertNotIn('DO_NOT_STORE_THIS', text)
        self.assertNotIn('NEITHER_THIS', text)

    def test_13_worker_job_outbox_replay_stays_offline(self):
        work_queue.enqueue('job1', 'EURUSD', 123)
        db._local.job = work_queue.claim()
        result = self.finalize()
        report = replay(self.capsule(result))
        self.assertTrue(report['matched'], report)
        with db.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

    def test_14_health_reports_disconnection_and_queues(self):
        self.broker.get_health_status.return_value = {'connected': False, 'data_utc_offset_minutes': 180}
        text = health_report(self.broker)
        self.assertIn('MT5 connected: False', text)
        self.assertIn('broker-monotonic', text)
        self.broker.get_market_snapshot.assert_not_called()

    def test_15_health_failure_does_not_crash(self):
        self.broker.get_health_status.side_effect = RuntimeError('failed')
        self.assertIn('MT5 health unavailable: RuntimeError', health_report(self.broker))

    def test_16_codec_roundtrip(self):
        self.assertEqual(decode(encode(self.snapshot)), self.snapshot)

    def test_17_replay_forbids_unrecorded_broker_read(self):
        with self.assertRaises(AssertionError):
            RecordedBroker([]).get_market_snapshot('EURUSD')

    def test_18_clock_context_restored_after_replay(self):
        capsule = self.capsule(self.finalize())
        before = clock.utc_now()
        replay(capsule)
        self.assertGreaterEqual(clock.utc_now(), before)
        self.assertEqual(clock.reference_status()['source'], 'broker-monotonic')

    def test_19_post_trigger_watch_output_closes_without_new_watch(self):
        initial = self.finalize(self.raw('WATCH'))
        db.close_watch_lifecycle(initial.watch_id, 'TRIGGERED', 'test trigger')
        parent = self.service._row_to_watch_state(db.get_watch(initial.watch_id))
        result = self.finalize(self.raw('WATCH'), parent)
        self.assertEqual(result.status, AnalysisStatus.NO_TRADE)
        self.assertEqual(result.analysis_phase, 'post_trigger')
        report = replay(self.capsule(result))
        self.assertTrue(report['matched'], report)
        self.assertIsNone(db.get_active_watch_for_symbol('EURUSD'))

    def trade_raw(self, rr=2):
        return f'''Analysis Time: {self.now.isoformat()}
Symbol: EURUSD
Status: TRADE
Direction: BUY
Grade: A
Reason: H1=صعودی; M15=صعودی; M5=تایید شده; دلیل اصلی=تایید کامل; شرط بعدی=هیچ
Timeframes Checked: H1, M15, M5
Order Type: BUY_LIMIT
Entry: 1.1000
Stop Loss: 1.0990
Take Profit: 1.1020
Risk Percent: 0.5
Suggested Volume: 0.5
Reward Risk Ratio: {rr}
Expiration: {(self.now + timedelta(hours=1)).isoformat()}
Invalidation: M5 close below 1.0990
Short Reason: confirmed
Checklist Complete: true
'''

    def test_20_trade_validation_rejection_replays(self):
        result = self.finalize(self.trade_raw(rr=9))
        self.assertEqual(result.status, AnalysisStatus.NO_TRADE)
        self.assertIn('RR mismatch', result.reason)
        self.assertTrue(replay(self.capsule(result))['matched'])

    def test_21_valid_trade_risk_calculation_replays(self):
        for key, value in dict(account_balance=10000, account_currency='USD',
                symbol_tick_size=.00001, symbol_tick_value=1, symbol_min_lot=.01,
                symbol_lot_step=.01, symbol_max_lot=100, symbol_contract_size=100000).items():
            setattr(self.snapshot, key, value)
        self.broker.get_account_open_risk_amount.return_value = 0.0
        result = self.finalize(self.trade_raw())
        self.assertEqual(result.status, AnalysisStatus.TRADE, result.reason)
        report = replay(self.capsule(result))
        self.assertTrue(report['matched'], report)

    def test_22_changed_expected_result_reports_mismatch(self):
        capsule = self.capsule(self.finalize())
        capsule['expected']['fields']['reason'] = 'not the recorded decision'
        self.assertFalse(replay(capsule)['matched'])

    def test_23_replay_clock_is_thread_local(self):
        from concurrent.futures import ThreadPoolExecutor
        with clock.audit_timeline(replay=iter(['2001-01-01T00:00:00+00:00'])):
            with ThreadPoolExecutor(max_workers=1) as pool:
                other = pool.submit(clock.utc_now).result()
            self.assertEqual(clock.utc_now().year, 2001)
            self.assertEqual(other.year, 2026)

    def test_24_mt5_health_has_no_connect_or_order_side_effect(self):
        from broker.mt5_broker import MT5Broker
        from types import SimpleNamespace
        terminal = MagicMock()
        terminal.terminal_info.return_value = SimpleNamespace(connected=True)
        terminal.symbol_info_tick.return_value = SimpleNamespace(time=self.now.timestamp() + 10800)
        with patch('broker.mt5_broker.mt5', terminal):
            broker = MT5Broker()
            broker.get_candles = MagicMock(side_effect=lambda s, tf, n: self.bars[tf])
            report = broker.get_health_status()
        self.assertTrue(report['connected'])
        self.assertEqual(len(report['symbols']), 4)
        terminal.initialize.assert_not_called()
        terminal.symbol_select.assert_not_called()
        terminal.order_send.assert_not_called()

    def test_25_repeated_database_init_releases_file_handles(self):
        # tearDown must be able to clean this temporary DB on Windows.
        for _ in range(3):
            db.init_db()
        self.assertIsNone(db.get_latest_analysis())


class HealthAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_health_never_reads_broker(self):
        from telegram_bot.handlers import health_command
        update, context = MagicMock(), MagicMock()
        update.message.reply_text = AsyncMock()
        with patch('telegram_bot.handlers._is_authorized', return_value=False), patch('core.health.health_report') as report:
            await health_command(update, context)
        report.assert_not_called()


if __name__ == '__main__':
    unittest.main()
