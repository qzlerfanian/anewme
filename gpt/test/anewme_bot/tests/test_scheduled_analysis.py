import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config import config
from core.models import MarketSnapshot, AnalysisResult, AnalysisStatus, Grade
from core.scheduled_analysis import (ScheduledAnalysisLoop, SYMBOLS, expected_close,
                                     slots_due, validate_candles)
from storage import db


class ScheduledAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.old=db.DB_PATH; db.DB_PATH=Path(self.tmp.name)/'x.db'; db.init_db()
        self.scheduled=datetime(2026,9,14,7,5,tzinfo=timezone.utc)  # 10:35 Tehran
        self.patches=[patch.object(config,'telegram_allowed_user_ids',(123,)),
                      patch.object(config.timeframes,'m5_candle_count',3),
                      patch.object(config.timeframes,'m15_candle_count',3),
                      patch.object(config.timeframes,'h1_candle_count',3)]
        for p in self.patches:p.start()
        self.broker=MagicMock(); self.ai=MagicMock(); self.ai._request_local=SimpleNamespace(last_attempts=1)
        self.service=MagicMock(); self.service.ai_client=self.ai; self.service._build_charts.return_value=[]
        self.ai.acknowledge_scheduled_chart.side_effect=lambda paths,meta,snap: ({'batch_id':meta['batch_id'],'chart_id':meta['chart_id'],'symbol':meta['symbol'],'accepted':True,'reason':'ok','description':'read'},1)
        self.service.run_prepared_analysis.side_effect=lambda symbol,*a: AnalysisResult(self.scheduled,symbol,AnalysisStatus.NO_TRADE,None,Grade.C,'none',[])
        self.broker.get_market_snapshot.side_effect=lambda symbol: MarketSnapshot(symbol,1,1.1,.1,self.scheduled,self.scheduled,True)
        self.broker.get_candles.side_effect=lambda symbol,tf,count:self.bars(tf,count)
        self.loop=ScheduledAnalysisLoop(self.broker,self.service)

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        db.DB_PATH=self.old; self.tmp.cleanup()

    def bars(self,tf,count,close=1.0):
        end=expected_close(self.scheduled,tf); minutes={'M5':5,'M15':15,'H1':60}[tf]
        return [{'time':end-timedelta(minutes=minutes*(count-i)), 'open':close,'high':close+1,'low':close-0.5,'close':close} for i in range(count)]

    def rows(self,table):
        with db.get_connection() as c:return c.execute(f'SELECT * FROM {table}').fetchall()

    def test_01_fixed_tehran_slot_is_due(self):
        self.assertEqual(slots_due(datetime(2026,9,14,7,5,30,tzinfo=timezone.utc))[0][1],'10:35')

    def test_02_outside_grace_not_due(self): self.assertEqual(slots_due(self.scheduled+timedelta(minutes=4)),[])
    def test_03_m5_exact_boundary_uses_previous_close(self): self.assertEqual(expected_close(self.scheduled,'M5').astimezone(timezone(timedelta(hours=3,minutes=30))).minute,30)
    def test_04_m15_non_boundary_uses_1030(self): self.assertEqual(expected_close(self.scheduled,'M15').astimezone(timezone(timedelta(hours=3,minutes=30))).minute,30)
    def test_05_h1_uses_1000_iran(self): self.assertEqual(expected_close(self.scheduled,'H1').astimezone(timezone(timedelta(hours=3,minutes=30))).hour,10)

    def test_06_valid_candles_exact_count(self): self.assertEqual(len(validate_candles(self.bars('M5',3),'M5',3,self.scheduled)[0]),3)
    def test_07_forming_boundary_is_excluded(self):
        bars=self.bars('M5',3)+[{'time':self.scheduled,'open':1,'high':2,'low':.5,'close':1}]
        valid,error=validate_candles(bars,'M5',3,self.scheduled); self.assertIsNone(error); self.assertLess(valid[-1]['close_time'],self.scheduled)
    def test_08_insufficient_rejected(self): self.assertIn('کافی نیست',validate_candles(self.bars('M5',2),'M5',3,self.scheduled)[1])
    def test_09_stale_rejected(self):
        bars=self.bars('M5',3); bars.pop(); self.assertIn('قدیمی',validate_candles(bars,'M5',2,self.scheduled)[1])
    def test_10_bad_ohlc_rejected(self):
        bars=self.bars('M5',3); bars[-1]['high']=.2; self.assertIn('OHLC',validate_candles(bars,'M5',3,self.scheduled)[1])
    def test_11_nan_rejected(self):
        bars=self.bars('M5',3); bars[-1]['close']=float('nan'); self.assertIn('OHLC',validate_candles(bars,'M5',3,self.scheduled)[1])
    def test_12_gap_rejected(self):
        bars=self.bars('M5',4); bars.pop(1); self.assertIn('شکاف',validate_candles(bars,'M5',3,self.scheduled)[1])

    def test_13_h1_session_gap_is_allowed(self):
        bars=self.bars('H1',4); bars[0]['time'] -= timedelta(hours=2)
        valid,error=validate_candles(bars,'H1',3,self.scheduled)
        self.assertIsNone(error); self.assertEqual(len(valid),3)

    def test_14_claim_is_idempotent(self):
        first=self.loop.claim('London','10:35',self.scheduled); second=self.loop.claim('London','10:35',self.scheduled)
        self.assertIsNotNone(first); self.assertIsNone(second)

    def test_15_success_requires_four_ack_then_four_analysis(self):
        run=self.loop.claim('London','10:35',self.scheduled); self.loop.process(run,'London','10:35',self.scheduled)
        self.assertEqual(self.ai.acknowledge_scheduled_chart.call_count,4)
        self.assertEqual(self.service.run_prepared_analysis.call_count,4)
        self.assertEqual(self.rows('scheduled_runs')[0]['status'],'COMPLETED')
        self.assertEqual(sum(r['status']=='ACCEPTED' for r in self.rows('scheduled_charts')),4)

    def test_16_metadata_keeps_four_isolated_identities(self):
        run=self.loop.claim('London','10:35',self.scheduled); self.loop.process(run,'London','10:35',self.scheduled)
        metas=[c.args[1] for c in self.ai.acknowledge_scheduled_chart.call_args_list]
        self.assertEqual({m['symbol'] for m in metas},set(SYMBOLS)); self.assertEqual(len({m['chart_id'] for m in metas}),4)
        self.assertTrue(all(m['batch_id']==run and m['strategy_version']=='ANEWME-V3' for m in metas))

    def test_17_one_data_failure_cancels_before_service(self):
        self.broker.get_candles.side_effect=lambda s,tf,n: [] if s=='GBPUSD' else self.bars(tf,n)
        run=self.loop.claim('London','10:35',self.scheduled); self.loop.process(run,'London','10:35',self.scheduled)
        self.ai.acknowledge_scheduled_chart.assert_not_called(); self.service.run_prepared_analysis.assert_not_called()
        self.assertEqual(self.rows('scheduled_runs')[0]['status'],'CANCELLED')

    def test_18_three_of_four_ack_cancels_all_final_analysis(self):
        def ack(paths,meta,snap):
            if meta['symbol']=='GBPUSD': raise RuntimeError('API timeout')
            return ({'batch_id':meta['batch_id'],'chart_id':meta['chart_id'],'symbol':meta['symbol'],'description':'ok'},1)
        self.ai.acknowledge_scheduled_chart.side_effect=ack
        run=self.loop.claim('London','10:35',self.scheduled); self.loop.process(run,'London','10:35',self.scheduled)
        self.service.run_prepared_analysis.assert_not_called(); self.assertEqual(self.rows('scheduled_runs')[0]['status'],'CANCELLED')

    def test_19_retry_count_and_error_stage_are_stored(self):
        self.ai._request_local.last_attempts=3; self.ai.acknowledge_scheduled_chart.side_effect=RuntimeError('timeout')
        run=self.loop.claim('London','10:35',self.scheduled); self.loop.process(run,'London','10:35',self.scheduled)
        row=self.rows('scheduled_charts')[0]; self.assertEqual(row['attempts'],3); self.assertEqual(row['error_stage'],'SERVICE_RECEIPT')

    def test_20_notifications_are_deduplicated(self):
        run=self.loop.claim('London','10:35',self.scheduled); self.loop.process(run,'London','10:35',self.scheduled)
        before=len(self.rows('outbox')); self.loop.notify('scheduled-ready:'+run,'duplicate'); self.assertEqual(len(self.rows('outbox')),before)

    def test_20_service_breaker_transitions_down_and_up(self):
        self.loop.service_state(False,'x'); self.loop.service_state(False,'x')
        self.assertEqual(self.rows('scheduled_health')[0]['status'],'DOWN')
        self.loop.service_state(True); self.assertEqual(self.rows('scheduled_health')[0]['status'],'UP')

    def test_21_market_health_transition(self):
        self.broker.get_health_status.return_value={'connected':False}; self.loop.health_tick()
        self.assertEqual([r for r in self.rows('scheduled_health') if r['component']=='market_data'][0]['status'],'DOWN')

    def test_22_daily_report_is_once_via_outbox_key(self):
        self.loop.daily_report(self.scheduled); self.loop.daily_report(self.scheduled)
        self.assertEqual(len([r for r in self.rows('outbox') if r['id'].startswith('daily-report:')]),1)

    def test_23_final_analysis_failure_marks_run(self):
        self.service.run_prepared_analysis.side_effect=RuntimeError('bad final response')
        run=self.loop.claim('London','10:35',self.scheduled); self.loop.process(run,'London','10:35',self.scheduled)
        self.assertEqual(self.rows('scheduled_runs')[0]['status'],'ANALYSIS_FAILED')

    def test_24_all_four_snapshots_share_cutoff(self):
        run=self.loop.claim('London','10:35',self.scheduled); self.loop.process(run,'London','10:35',self.scheduled)
        values=[json.loads(r['last_candles_json']) for r in self.rows('scheduled_charts')]
        self.assertEqual(len({v['M5']['close_time'] for v in values}),1)

    def test_25_final_timing_is_stored(self):
        run=self.loop.claim('London','10:35',self.scheduled); self.loop.process(run,'London','10:35',self.scheduled)
        row=self.rows('scheduled_charts')[0]
        self.assertIsNotNone(row['final_started_at']); self.assertIsNotNone(row['final_received_at'])
        self.assertEqual(row['final_status'],'NO_TRADE'); self.assertGreaterEqual(row['final_response_seconds'],0)

    def test_26_circuit_breaker_skips_service_requests(self):
        self.loop.service_state(False,'x'); self.loop.service_state(False,'x')
        run=self.loop.claim('London','10:35',self.scheduled); self.loop.process(run,'London','10:35',self.scheduled)
        self.ai.acknowledge_scheduled_chart.assert_not_called(); self.service.run_prepared_analysis.assert_not_called()
        self.assertEqual(self.rows('scheduled_runs')[0]['status'],'CANCELLED')


if __name__=='__main__': unittest.main()
