"""Durable six-slot Tehran scheduler and four-symbol all-or-nothing batches."""
import asyncio
import dataclasses
import json
import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from broker.candle_utils import closed_only, normalize_candle, TIMEFRAME_MINUTES
from config import config
from core.audit import encode
from core.clock import utc_now
from core.models import AnalysisResult, AnalysisStatus, Direction, Grade, WatchDetails
from core.version import VERSION, STRATEGY_VERSION
from storage import db, work_queue
from telegram_bot.notifier import format_analysis_message

TEHRAN = ZoneInfo('Asia/Tehran')
SYMBOLS = ('XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY')
SLOTS = (('London','10:35'), ('London','11:15'), ('London','12:25'),
         ('New York','15:35'), ('New York','16:15'), ('New York','17:05'))


def expected_close(scheduled_utc, timeframe):
    seconds = TIMEFRAME_MINUTES[timeframe] * 60
    stamp = int(scheduled_utc.timestamp())
    boundary = stamp - stamp % seconds
    if boundary >= stamp:
        boundary -= seconds
    return datetime.fromtimestamp(boundary, timezone.utc)


def validate_candles(candles, timeframe, required, scheduled_utc):
    normalized = []
    for raw in candles:
        try:
            c = normalize_candle(raw, timeframe)
        except Exception as exc:
            return None, f'قالب زمان کندل نامعتبر است: {exc}'
        values = [c.get(k) for k in ('open','high','low','close')]
        if not all(isinstance(v, (int,float)) and math.isfinite(v) and v > 0 for v in values):
            return None, 'مقدار OHLC خالی، غیرعددی یا نامعتبر است.'
        if not c['low'] <= min(c['open'],c['close']) <= max(c['open'],c['close']) <= c['high']:
            return None, 'رابطه قیمت‌های OHLC نامعتبر است.'
        normalized.append(c)
    limit = expected_close(scheduled_utc, timeframe)
    usable = [c for c in closed_only(normalized, timeframe, limit) if c['close_time'] <= limit]
    usable.sort(key=lambda c: c['open_time'])
    if len(usable) < required:
        return None, f'تعداد کندل کافی نیست: {len(usable)}/{required}'
    usable = usable[-required:]
    times = [c['open_time'] for c in usable]
    if times != sorted(times) or len(set(times)) != len(times):
        return None, 'ترتیب یا یکتایی زمان کندل‌ها نامعتبر است.'
    if usable[-1]['close_time'] != limit:
        return None, f"داده قدیمی/ناهماهنگ است؛ انتظار={limit.isoformat()} دریافت={usable[-1]['close_time'].isoformat()}"
    step = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    gaps = [(a, b) for a, b in zip(times, times[1:]) if b-a != step]
    # H1 feeds commonly omit the weekend/rollover session (and some brokers
    # publish a shortened daily break). This is a valid market gap, not a
    # corrupt candle. M5/M15 remain strict because they are the trigger data.
    if gaps and timeframe != 'H1':
        return None, 'بین کندل‌ها شکاف زمانی وجود دارد.'
    return usable, None


def slots_due(now=None):
    now = (now or utc_now()).astimezone(TEHRAN)
    result = []
    for session, label in SLOTS:
        hour, minute = map(int, label.split(':'))
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delay = (now-scheduled).total_seconds()
        if 0 <= delay <= config.scheduled_start_grace_seconds:
            result.append((session, label, scheduled.astimezone(timezone.utc)))
    return result


class ScheduledAnalysisLoop:
    def __init__(self, broker, service):
        self.broker, self.service, self.running = broker, service, True

    def recipients(self):
        return config.telegram_allowed_user_ids

    def notify(self, key, text):
        for chat_id in self.recipients():
            work_queue.queue_message(key, chat_id, text)

    def claim(self, session, label, scheduled):
        run_id = f"{scheduled.astimezone(TEHRAN).date()}:{label}:{session.replace(' ','_')}"
        with db.get_connection() as conn:
            inserted = conn.execute('INSERT OR IGNORE INTO scheduled_runs VALUES(?,?,?,?,?,?,?,?,?)',
                (run_id, scheduled.isoformat(), session, label, utc_now().isoformat(), 'COLLECTING', None, None, 0)).rowcount
        return run_id if inserted else None

    def prepare_snapshot(self, symbol, scheduled):
        base = self.broker.get_market_snapshot(symbol)
        if not base.market_open:
            raise RuntimeError('منبع داده بازار را باز/سالم تأیید نکرد.')
        attrs = {}
        last = {}
        for tf, attr, count in (('M5','candles_m5',config.timeframes.m5_candle_count),
                                ('M15','candles_m15',config.timeframes.m15_candle_count),
                                ('H1','candles_h1',config.timeframes.h1_candle_count)):
            raw = self.broker.get_candles(symbol, tf, count + 2)
            bars, error = validate_candles(raw, tf, count, scheduled)
            if error:
                raise RuntimeError(f'{tf}: {error}')
            attrs[attr] = bars
            last[tf] = {'close_time':bars[-1]['close_time'].isoformat(), 'close':bars[-1]['close']}
        return dataclasses.replace(base, **attrs), last

    def process(self, run_id, session, label, scheduled):
        prepared = {}
        errors = []
        for index, symbol in enumerate(SYMBOLS, 1):
            chart_id = f'{run_id}:{index}'
            try:
                snapshot, last = self.prepare_snapshot(symbol, scheduled)
                prepared[symbol] = (chart_id, snapshot, last)
                status, reason = 'READY', None
            except Exception as exc:
                status, reason = 'DATA_FAILED', str(exc)
                errors.append((symbol, 'DATA_VALIDATION', reason, 0))
            with db.get_connection() as conn:
                conn.execute('INSERT OR REPLACE INTO scheduled_charts(run_id,chart_id,symbol,status,snapshot_json,last_candles_json,error_stage,error_reason) VALUES(?,?,?,?,?,?,?,?)',
                    (run_id, chart_id, symbol, status,
                     json.dumps(encode(prepared[symbol][1]), ensure_ascii=False) if symbol in prepared else None,
                     json.dumps(prepared[symbol][2], ensure_ascii=False) if symbol in prepared else None,
                     'DATA_VALIDATION' if reason else None, reason))
        if errors:
            return self.cancel(run_id, session, label, errors)

        with db.get_connection() as conn:
            unavailable=conn.execute("SELECT * FROM scheduled_health WHERE component='analysis_service' AND status='DOWN'").fetchone()
        if unavailable and unavailable['next_probe_at'] and utc_now() < datetime.fromisoformat(unavailable['next_probe_at']):
            errors=[(symbol,'CIRCUIT_BREAKER','سرویس تحلیل موقتاً قطع شناخته شده؛ ارسال تا زمان بررسی بعدی متوقف است.',0) for symbol in SYMBOLS]
            return self.cancel(run_id,session,label,errors)

        accepted = {}
        service_started = time.monotonic()
        for index, symbol in enumerate(SYMBOLS, 1):
            chart_id, snapshot, last = prepared[symbol]
            paths = self.service._build_charts(symbol, snapshot, needs_correlated_symbols=False)
            metadata = {'batch_id':run_id, 'chart_id':chart_id, 'symbol':symbol,
                        'session':session, 'scheduled_time_iran':label,
                        'analysis_time_utc':scheduled.isoformat(), 'last_closed':last,
                        'timeframes':['M5','M15','H1'],
                        'chart_name':f'{index}/4', 'bot_version':VERSION,
                        'strategy_version':STRATEGY_VERSION}
            started = time.monotonic()
            sent = utc_now().isoformat()
            try:
                receipt, attempts = self.service.ai_client.acknowledge_scheduled_chart(paths, metadata, snapshot)
                accepted[symbol] = receipt['description']
                status, stage, reason = 'ACCEPTED', None, None
            except Exception as exc:
                attempts = int(getattr(self.service.ai_client._request_local, 'last_attempts', 3))
                status, stage, reason = 'SERVICE_FAILED', 'SERVICE_RECEIPT', str(exc)
                errors.append((symbol, stage, reason, attempts))
                receipt = None
            elapsed = time.monotonic()-started
            with db.get_connection() as conn:
                conn.execute('''UPDATE scheduled_charts SET status=?,sent_at=?,received_at=?,attempts=?,response_seconds=?,error_stage=?,error_reason=?,receipt_json=? WHERE run_id=? AND chart_id=?''',
                    (status,sent,utc_now().isoformat() if receipt else None,attempts,elapsed,stage,reason,
                     json.dumps(receipt,ensure_ascii=False) if receipt else None,run_id,chart_id))
        if errors:
            self.service_state(False, '; '.join(e[2] for e in errors))
            return self.cancel(run_id, session, label, errors, time.monotonic()-service_started)

        self.service_state(True)
        self.notify('scheduled-ready:'+run_id,
            f'{label} | {session} Session\n\n4/4 Data Received Successfully\n'
            'All chart data was successfully sent and confirmed.\nWaiting for analysis results...\n'
            f'Batch ID: {run_id}')
        results = {}
        for symbol in SYMBOLS:
            chart_id, snapshot, _ = prepared[symbol]
            final_started=utc_now().isoformat(); final_timer=time.monotonic()
            try:
                result = self.service.run_prepared_analysis(symbol, snapshot, accepted[symbol])
                text = f'{label} | {session} Session\nBatch ID: {run_id}\n\n' + format_analysis_message(result)
                results[symbol] = result.status.value
                final_status,analysis_id=result.status.value,result.analysis_id
            except Exception as exc:
                text = f'{label} | {session} Session\nBatch ID: {run_id}\n{symbol} | ANALYSIS_FAILED\nReason: {exc}'
                results[symbol] = 'FAILED'
                final_status,analysis_id='FAILED',None
            with db.get_connection() as conn:
                conn.execute('''UPDATE scheduled_charts SET final_started_at=?,final_received_at=?,final_attempts=?,final_response_seconds=?,final_status=?,final_analysis_id=? WHERE run_id=? AND chart_id=?''',
                    (final_started,utc_now().isoformat(),int(getattr(self.service.ai_client._request_local,'last_attempts',1)),
                     time.monotonic()-final_timer,final_status,analysis_id,run_id,chart_id))
            self.notify(f'scheduled-result:{run_id}:{symbol}', text)
        elapsed = time.monotonic()-service_started
        run_status = 'COMPLETED' if all(v != 'FAILED' for v in results.values()) else 'ANALYSIS_FAILED'
        if run_status != 'COMPLETED':
            self.service_state(False, 'یک یا چند تحلیل نهایی پاسخ معتبر دریافت نکرد.')
        with db.get_connection() as conn:
            conn.execute('UPDATE scheduled_runs SET status=?,completed_at=?,final_result=?,service_seconds=? WHERE run_id=?',
                (run_status,utc_now().isoformat(),json.dumps(results),elapsed,run_id))

    def cancel(self, run_id, session, label, errors, service_seconds=0):
        successful = 4-len(errors)
        details = '\n'.join(f'- {s} | stage={stage} | attempts={attempts} | {reason}' for s,stage,reason,attempts in errors)
        self.notify('scheduled-cancel:'+run_id,
            f'{label} | {session} Session\n\nData Status: {successful}/4\n\nFailed Chart(s):\n{details}\n\nAnalysis Cancelled.\nBatch ID: {run_id}')
        with db.get_connection() as conn:
            conn.execute('UPDATE scheduled_runs SET status=?,completed_at=?,final_result=?,service_seconds=? WHERE run_id=?',
                ('CANCELLED',utc_now().isoformat(),json.dumps({'errors':errors},ensure_ascii=False),service_seconds,run_id))

    def service_state(self, healthy, error=None):
        now = utc_now()
        with db.get_connection() as conn:
            old = conn.execute("SELECT * FROM scheduled_health WHERE component='analysis_service'").fetchone()
            failures = 0 if healthy else (old['consecutive_failures'] if old else 0)+1
            status = 'UP' if healthy else ('DOWN' if failures >= config.service_failure_threshold else 'DEGRADED')
            changed = old is None or old['status'] != status
            conn.execute('INSERT OR REPLACE INTO scheduled_health VALUES(?,?,?,?,?,?)',
                ('analysis_service',status,now.isoformat(),failures,error,
                 (now+timedelta(seconds=config.service_recheck_seconds)).isoformat() if status=='DOWN' else None))
        if changed:
            self.notify('health-transition:analysis_service:'+status+':'+now.strftime('%Y%m%d%H%M'),
                        f"وضعیت سرویس تحلیل: {status}" + (f'\nدلیل: {error}' if error else '\nارتباط دوباره برقرار شد.'))

    def market_state(self, healthy, error=None):
        now=utc_now(); status='UP' if healthy else 'DOWN'
        with db.get_connection() as conn:
            old=conn.execute("SELECT * FROM scheduled_health WHERE component='market_data'").fetchone()
            changed=old is None or old['status']!=status
            failures=0 if healthy else (old['consecutive_failures'] if old else 0)+1
            conn.execute('INSERT OR REPLACE INTO scheduled_health VALUES(?,?,?,?,?,?)',
                         ('market_data',status,now.isoformat(),failures,error,None))
        if changed:
            self.notify('health-transition:market_data:'+status+':'+now.strftime('%Y%m%d%H%M'),
                        f"وضعیت منبع داده بازار: {status}"+(f'\nدلیل: {error}' if error else '\nاتصال دوباره برقرار شد.'))

    def health_tick(self):
        now=utc_now()
        with db.get_connection() as conn:
            conn.execute('INSERT OR REPLACE INTO scheduled_health VALUES(?,?,?,?,?,?)',
                         ('bot_scheduler','UP',now.isoformat(),0,None,None))
        try:
            status=self.broker.get_health_status()
            if not status.get('connected'):
                raise RuntimeError('MT5 disconnected')
            self.market_state(True)
        except Exception as exc:
            self.market_state(False,str(exc))
        with db.get_connection() as conn:
            service=conn.execute("SELECT * FROM scheduled_health WHERE component='analysis_service'").fetchone()
        if service and service['status']=='DOWN' and service['next_probe_at'] and utc_now()>=datetime.fromisoformat(service['next_probe_at']):
            try:
                if not self.service.ai_client.probe_service(): raise RuntimeError('پاسخ health نامعتبر بود.')
                self.service_state(True)
            except Exception as exc:
                self.service_state(False,str(exc))

    def daily_report(self, now):
        day = now.astimezone(TEHRAN).date().isoformat()
        with db.get_connection() as conn:
            rows = conn.execute("SELECT * FROM scheduled_runs WHERE substr(scheduled_at,1,10)=?", (day,)).fetchall()
            charts = conn.execute("SELECT c.* FROM scheduled_charts c JOIN scheduled_runs r ON r.run_id=c.run_id WHERE substr(r.scheduled_at,1,10)=?",(day,)).fetchall()
        successful=sum(r['status']=='COMPLETED' for r in rows); cancelled=sum(r['status']!='COMPLETED' for r in rows)+(6-len(rows))
        retries=sum(max(0,c['attempts']-1) for c in charts); service_errors=sum(c['status']=='SERVICE_FAILED' for c in charts)
        data_errors=sum(c['status']=='DATA_FAILED' for c in charts)
        timings=[v for c in charts for v in (c['response_seconds'],c['final_response_seconds']) if v is not None]
        average=f'{sum(timings)/len(timings):.2f} ثانیه' if timings else '-'
        text=(f'گزارش روزانه {day}\nکل تحلیل‌های برنامه‌ریزی‌شده: 6\nاجراشده: {len(rows)}\nموفق: {successful}\nناموفق/اجرانشده: {cancelled}'
              f'\nRetry: {retries}\nخطای داده: {data_errors}\nخطای سرویس: {service_errors}\n4/4 کامل: {successful}'
              f'\nلغوشده به دلیل داده ناقص: {sum(any(c["run_id"]==r["run_id"] and c["status"]=="DATA_FAILED" for c in charts) for r in rows)}'
              f'\nمیانگین پاسخ سرویس: {average}')
        self.notify('daily-report:'+day, text)

    async def start(self):
        last_report = None
        last_health = 0.0
        while self.running:
            now = utc_now()
            for session,label,scheduled in slots_due(now):
                run_id = self.claim(session,label,scheduled)
                if run_id:
                    task=asyncio.create_task(asyncio.to_thread(self.process,run_id,session,label,scheduled))
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        await task
                        raise
            local=now.astimezone(TEHRAN)
            if time.monotonic()-last_health >= 60:
                await asyncio.to_thread(self.health_tick); last_health=time.monotonic()
            if local.hour==23 and local.minute>=55 and last_report!=local.date():
                await asyncio.to_thread(self.daily_report,now); last_report=local.date()
            await asyncio.sleep(10)

    def stop(self): self.running=False
