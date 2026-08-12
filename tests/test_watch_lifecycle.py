"""سناریوهای چرخه‌عمر Watch بدون نیاز به Telegram/OpenAI واقعی."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

from broker.mock_broker import MockBroker
from core.analysis_service import AnalysisService
from core.models import AnalysisStatus
from core.models import AnalysisResult
from storage import db
from watch import watch_manager
from watch.monitor_loop import WatchMonitor


class WatchLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.temp_dir.name) / "test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def watch_dict(self, watch_id="w1", symbol="EURUSD"):
        return {
            "watch_id": watch_id, "symbol": symbol, "parent_analysis_id": "a1",
            "direction": "BUY", "grade": "A-", "trigger_type": "بسته‌شدن کندل M5 بالای سطح",
            "zone_or_level": "1.1556", "timeframes_to_recheck": ["M5"],
            "expiration": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
            "invalidation_condition": "بسته‌شدن کندل M5 زیر 1.1549",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def test_only_one_active_watch_per_symbol_is_atomic(self):
        db.save_watch(self.watch_dict("w1"))
        with self.assertRaises(db.ActiveWatchExistsError):
            db.save_watch(self.watch_dict("w2"))
        self.assertEqual(len(db.get_active_watches()), 1)

    def test_watch_has_only_three_terminal_states(self):
        db.save_watch(self.watch_dict())
        with self.assertRaises(ValueError):
            watch_manager.close_watch("w1", "OTHER", "invalid")
        watch_manager.close_watch("w1", "TRIGGERED", "trigger met")
        row = db.get_watch("w1")
        self.assertEqual(row["close_status"], "TRIGGERED")
        self.assertIsNotNone(row["triggered_at"])
        self.assertIsNotNone(row["closed_at"])

    def test_analyze_existing_watch_returns_exact_original_conditions(self):
        db.save_watch(self.watch_dict())
        broker = MockBroker()
        broker.is_market_open = lambda symbol: True
        fake_ai = MagicMock()
        service = AnalysisService(broker, fake_ai)
        result = service.run_initial_analysis("EURUSD")
        self.assertEqual(result.status, AnalysisStatus.WATCH)
        self.assertEqual(result.watch_details.exact_zone_or_level, "1.1556")
        self.assertEqual(result.watch_details.invalidation, "بسته‌شدن کندل M5 زیر 1.1549")
        fake_ai.request_analysis.assert_not_called()

    def test_account_state_is_checked_before_snapshot_or_ai(self):
        class AccountBroker(MockBroker):
            def get_market_snapshot(self, symbol):
                raise AssertionError("snapshot نباید خوانده شود")

        broker = AccountBroker()
        broker._mock_open_positions["EURUSD"] = [{
            "ticket": 10, "volume": 0.1, "price_open": 1.15,
            "type": "BUY", "profit": 12.0,
        }]
        fake_ai = MagicMock()
        result = AnalysisService(broker, fake_ai).run_initial_analysis("EURUSD")
        self.assertEqual(result.account_state, "OPEN_POSITION")
        self.assertEqual(result.account_state_details[0]["ticket"], 10)
        fake_ai.request_analysis.assert_not_called()

    def test_triggered_parent_stays_closed_and_watch_result_creates_new_row(self):
        db.save_watch(self.watch_dict("old"))
        watch_manager.close_watch("old", "TRIGGERED", "trigger met")
        old_row = db.get_watch("old")
        parent = AnalysisService._row_to_watch_state(old_row)
        broker = MockBroker()
        broker.is_market_open = lambda symbol: True
        service = AnalysisService(broker, MagicMock())
        snap = broker.get_market_snapshot("EURUSD")
        future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        raw = f"""Analysis Time: {datetime.now(timezone.utc).isoformat()}
Symbol: EURUSD
Status: WATCH
Direction: --
Grade: A-
Reason: شرایط مرحله بعد هنوز نیاز به تریگر دارد
Timeframes Checked: H1, M15, M5
Preferred Direction: BUY
Trigger Type: بسته شدن M5 Candle بالای سطح
Zone Or Level: 1.1565
Timeframes To Recheck: M5
Expiration: {future}
Invalidation: بسته‌شدن کندل M5 زیر 1.1550"""
        result = service._finalize("EURUSD", raw, snap, [], parent)
        self.assertEqual(result.status, AnalysisStatus.WATCH)
        self.assertEqual(db.get_watch("old")["close_status"], "TRIGGERED")
        active = db.get_active_watch_for_symbol("EURUSD")
        self.assertIsNotNone(active)
        self.assertNotEqual(active["watch_id"], "old")
        self.assertEqual(active["zone_or_level"], "1.1565")

    def test_same_triggered_setup_is_not_recreated(self):
        db.save_watch(self.watch_dict("old-same"))
        self.assertTrue(watch_manager.claim_trigger("old-same", "trigger met"))
        parent = AnalysisService._row_to_watch_state(db.get_watch("old-same"))
        broker = MockBroker()
        broker.is_market_open = lambda symbol: True
        service = AnalysisService(broker, MagicMock())
        snap = broker.get_market_snapshot("EURUSD")
        future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        raw = f"""Analysis Time: {datetime.now(timezone.utc).isoformat()}
Symbol: EURUSD
Status: WATCH
Direction: --
Grade: A-
Reason: همان موقعیت قبلی هنوز بالای همان تریگر دیده می‌شود
Timeframes Checked: H1, M15, M5
Preferred Direction: BUY
Trigger Type: بسته شدن M5 Candle بالای سطح
Zone Or Level: 1.1556
Timeframes To Recheck: M5
Expiration: {future}
Invalidation: بسته‌شدن کندل M5 زیر 1.1548"""
        result = service._finalize("EURUSD", raw, snap, [], parent)
        self.assertTrue(result.suppress_notification)
        self.assertIsNone(db.get_active_watch_for_symbol("EURUSD"))
        self.assertEqual(db.get_watch("old-same")["close_status"], "TRIGGERED")

    def test_trigger_claim_is_atomic_and_only_first_caller_wins(self):
        db.save_watch(self.watch_dict("atomic"))
        self.assertTrue(watch_manager.claim_trigger("atomic", "first"))
        self.assertFalse(watch_manager.claim_trigger("atomic", "second"))
        row = db.get_watch("atomic")
        self.assertEqual(row["close_reason"], "first")
        self.assertEqual(row["close_status"], "TRIGGERED")
        self.assertIsNone(db.get_active_watch_for_symbol("EURUSD"))

    def test_closed_candle_is_claimed_once_per_watch(self):
        db.save_watch(self.watch_dict("candle-once"))
        candle_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        class FixedBroker:
            def get_candles(self, symbol, timeframe, count):
                return [{
                    "time": candle_time, "open": 1.1550,
                    "high": 1.1554, "low": 1.1550, "close": 1.1553,
                }]

        row = db.get_watch("candle-once")
        self.assertEqual(watch_manager.check_trigger(row, FixedBroker()), (False, ""))
        # همان row قدیمی شبیه دو worker هم‌زمان است؛ claim دیتابیس مانع بررسی دوم می‌شود.
        self.assertEqual(watch_manager.check_trigger(row, FixedBroker()), (False, ""))
        events = []
        with db.get_connection() as conn:
            events = conn.execute(
                "SELECT * FROM events_log WHERE watch_id = ? AND event_type = 'WATCH_CHECKED'",
                ("candle-once",),
            ).fetchall()
        self.assertEqual(len(events), 1)

    def test_triggered_watch_is_never_returned_as_active(self):
        db.save_watch(self.watch_dict("not-active"))
        self.assertTrue(watch_manager.claim_trigger("not-active", "done"))
        self.assertEqual(db.get_active_watches(), [])
        self.assertIsNone(db.get_active_watch_for_symbol("EURUSD"))


class WatchMonitorFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.temp_dir.name) / "monitor.db"
        db.init_db()
        db.save_watch({
            "watch_id": "monitor-watch", "symbol": "EURUSD", "parent_analysis_id": "a1",
            "direction": "BUY", "grade": "A-", "trigger_type": "بسته‌شدن کندل M5 بالای سطح",
            "zone_or_level": "1.1556", "timeframes_to_recheck": ["M5"],
            "expiration": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
            "invalidation_condition": "بسته‌شدن کندل M5 زیر 1.1549",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

    async def asyncTearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_trigger_closes_parent_runs_reanalysis_and_reports(self):
        broker = MagicMock()
        broker.get_candles.return_value = [{
            "time": datetime.now(timezone.utc), "open": 1.1550,
            "high": 1.1562, "low": 1.1550, "close": 1.1560,
        }]
        service = MagicMock()
        service.run_watch_recheck.return_value = AnalysisResult(
            analysis_time=datetime.now(timezone.utc), symbol="EURUSD",
            status=AnalysisStatus.NO_TRADE, direction=None, grade=None,
            reason="بعد از تریگر ستاپ معامله کامل نشد.", timeframes_checked=["M5"],
        )
        messages = []

        async def notify(text):
            messages.append(text)

        await WatchMonitor(broker, service, notify)._tick()
        row = db.get_watch("monitor-watch")
        self.assertEqual(row["close_status"], "TRIGGERED")
        self.assertEqual(row["reanalysis_result"], "NO_TRADE")
        self.assertIsNotNone(row["reanalysis_started_at"])
        self.assertIsNotNone(row["reanalysis_completed_at"])
        service.run_watch_recheck.assert_called_once()
        self.assertEqual(len(messages), 2)
        self.assertIn("واچ تریگر شد", messages[0])


if __name__ == "__main__":
    unittest.main()
