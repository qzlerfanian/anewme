"""Record finalization inputs; replay uses no AI, MT5 or live database.

This deliberately covers the post-AI finalization boundary, not AI generation
or the monitor loop. JSON only: replay never unpickles or executes saved code.
"""
import copy
import dataclasses
import enum
import functools
import hashlib
import inspect
import json
import logging
import math
from datetime import datetime
from pathlib import Path

from core import models
from core.clock import audit_timeline
from core.version import build_identity
from config import config
from storage import db

logger = logging.getLogger(__name__)
TABLES = ('watches', 'analyses', 'trade_tracking')


def encode(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {'$float': str(value)}
    if isinstance(value, enum.Enum):
        return {'$enum': type(value).__name__, 'value': value.value}
    if isinstance(value, datetime):
        return {'$datetime': value.isoformat()}
    if isinstance(value, Path):
        return {'$path': str(value)}
    if dataclasses.is_dataclass(value):
        return {'$model': type(value).__name__, 'fields':
                {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f'Unsupported audit value: {type(value).__name__}')


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if '$datetime' in value:
        return datetime.fromisoformat(value['$datetime'])
    if '$float' in value:
        if value['$float'] not in ('nan', 'inf', '-inf'):
            raise ValueError('Invalid non-finite float encoding')
        return float(value['$float'])
    if '$path' in value:
        return Path(value['$path'])
    allowed = {name: getattr(models, name) for name in
               ('MarketSnapshot', 'WatchState', 'WatchDetails', 'TradeDetails',
                'AnalysisResult', 'AnalysisStatus', 'Direction', 'Grade', 'OrderType')}
    if '$model' in value:
        return allowed[value['$model']](**decode(value['fields']))
    if '$enum' in value:
        return allowed[value['$enum']](value['value'])
    return {k: decode(v) for k, v in value.items()}


def digest(value):
    return hashlib.sha256(json.dumps(encode(value), ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode('utf-8')).hexdigest()


class RecordingBroker:
    ALLOWED = {'get_open_positions', 'get_pending_orders', 'get_market_snapshot',
               'get_candles', 'get_account_open_risk_amount'}

    def __init__(self, broker, calls):
        self.broker, self.calls = broker, calls

    def __getattr__(self, name):
        if name not in self.ALLOWED:
            raise AttributeError(name)

        def invoke(*args, **kwargs):
            call = {'name': name, 'args': encode(list(args)), 'kwargs': encode(kwargs)}
            self.calls.append(call)
            try:
                # Broker-internal clock calls do not execute during replay.
                with audit_timeline():
                    result = getattr(self.broker, name)(*args, **kwargs)
            except Exception as exc:
                call['error'] = str(exc)
                raise
            try:
                call['result'] = encode(result)
            except Exception as exc:
                call['capture_error'] = type(exc).__name__
            return result
        return invoke


class RecordedBroker:
    def __init__(self, calls):
        self.calls, self.index = calls, 0

    def __getattr__(self, name):
        if name not in RecordingBroker.ALLOWED:
            raise AttributeError(name)

        def invoke(*args, **kwargs):
            if self.index >= len(self.calls):
                raise AssertionError('Replay requested an unrecorded broker read')
            item = self.calls[self.index]
            self.index += 1
            if (item['name'], item['args'], item['kwargs']) != (name, encode(list(args)), encode(kwargs)):
                raise AssertionError('Replay broker call sequence differs')
            if 'error' in item:
                raise RuntimeError(item['error'])
            return decode(item['result'])
        return invoke


def prestate(symbol):
    with db.get_connection() as conn:
        # Short consistent read snapshot; no database lock across broker calls.
        conn.execute('BEGIN') if not conn.in_transaction else None
        watches = [dict(r) for r in conn.execute('SELECT * FROM watches WHERE symbol=?', (symbol,))]
        analyses = [dict(r) for r in conn.execute('SELECT * FROM analyses WHERE id IN (SELECT parent_analysis_id FROM watches WHERE symbol=?)', (symbol,))]
        tracking = [dict(r) for r in conn.execute("SELECT * FROM trade_tracking WHERE status IN ('PENDING','FILLED')")]
    return {'watches': watches, 'analyses': analyses, 'trade_tracking': tracking}


def decision(result):
    value = encode(result)
    for key in ('analysis_id', 'watch_id'):
        value['fields'].pop(key, None)
    return value


def audited_finalization(method):
    @functools.wraps(method)
    def invoke(self, *args, **kwargs):
        bound = inspect.signature(method).bind(self, *args, **kwargs)
        bound.apply_defaults()
        inputs = {k: v for k, v in bound.arguments.items() if k != 'self'}
        capsule = {'schema': 1, 'build': build_identity(), 'inputs': encode(inputs),
                   'prestate': prestate(inputs['symbol']), 'calls': [], 'clock': [],
                   'risk': dataclasses.asdict(config.risk),
                   'timeframes': dataclasses.asdict(config.timeframes),
                   'scope': 'post_ai_finalization', 'ai_model': config.ai_model}
        job = getattr(db._local, 'job', None)
        capsule['job'] = dict(job) if job is not None else None
        capsule['recipients'] = list(config.telegram_allowed_user_ids)
        offset = getattr(self.broker, 'data_utc_offset_minutes', None)
        capsule['feed'] = {'adapter': type(self.broker).__name__,
                           'data_utc_offset_minutes': offset if isinstance(offset, int) else None}
        # Copy service so monitoring/concurrent reads never see the proxy.
        service = copy.copy(self)
        service.broker = RecordingBroker(self.broker, capsule['calls'])
        with audit_timeline(record=capsule['clock']):
            result = method(service, *args, **kwargs)
        capsule['expected'] = decision(result)
        capsule['analysis_id'] = result.analysis_id
        try:
            if any('capture_error' in item for item in capsule['calls']):
                raise ValueError('Broker output could not be serialized; capsule incomplete')
            payload = json.dumps(capsule, ensure_ascii=False, allow_nan=False)
            with db.get_connection() as conn:
                conn.execute('INSERT INTO replay_capsules(analysis_id,payload,sha256) VALUES(?,?,?)',
                             (result.analysis_id, payload, digest(capsule)))
        except Exception:
            # Never turn a committed signal into a second conflicting decision.
            logger.exception('Replay capsule persistence failed: %s', result.analysis_id)
        return result
    return invoke
