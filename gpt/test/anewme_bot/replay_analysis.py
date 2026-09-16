"""Offline post-AI finalization replay. Reads a source DB in SQLite read-only mode.

Usage: python replay_analysis.py --db data_utc_v2/anewme.db --analysis-id UUID
Never imports MT5 or constructs an AI client; all writes go to a temporary DB.
"""
import argparse
import copy
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from config import config
from core.audit import RecordedBroker, decode, decision, digest, TABLES
from core.clock import audit_timeline
from core.version import build_identity
from storage import db


def load_capsule(path, analysis_id):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        row = conn.execute('SELECT payload,sha256 FROM replay_capsules WHERE analysis_id=?', (analysis_id,)).fetchone()
    if row is None:
        raise ValueError('No replay capsule: old record, early guard, or incomplete capture')
    capsule = json.loads(row[0])
    if capsule.get('schema') != 1 or digest(capsule) != row[1]:
        raise ValueError('Replay capsule schema or integrity check failed')
    return capsule


def replay(capsule):
    from core.analysis_service import AnalysisService
    if capsule['build']['source_sha256'] != build_identity()['source_sha256']:
        raise ValueError('Different code/rules version. Use the original release for exact replay.')
    if getattr(db._local, 'connection', None) is not None:
        raise RuntimeError('Replay must run outside a live transaction in a separate process')
    previous_path, previous_job = db.DB_PATH, getattr(db._local, 'job', None)
    previous_config = copy.deepcopy(config.__dict__)
    broker = RecordedBroker(capsule['calls'])
    timeline = iter(capsule['clock'])
    try:
        with tempfile.TemporaryDirectory(prefix='anewme-replay-') as folder:
            db.DB_PATH = Path(folder) / 'replay.db'
            db.init_db()
            with db.get_connection() as conn:
                for table in TABLES:
                    columns_allowed = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
                    for row in capsule['prestate'][table]:
                        if not set(row) <= columns_allowed:
                            raise ValueError('Unknown replay database columns')
                        names = ','.join('"' + key + '"' for key in row)
                        placeholders = ','.join('?' for _ in row)
                        conn.execute(f'INSERT INTO {table} ({names}) VALUES ({placeholders})', tuple(row.values()))
                job = capsule.get('job')
                if job:
                    keys = ('id', 'symbol', 'watch_id', 'chat_id', 'status', 'created_at', 'started_at', 'completed_at', 'result_text')
                    conn.execute('INSERT INTO analysis_jobs VALUES(?,?,?,?,?,?,?,?,?)', tuple(job[k] for k in keys))
            for key, value in capsule['risk'].items():
                if not hasattr(config.risk, key):
                    raise ValueError('Unknown risk field')
                setattr(config.risk, key, value)
            for key, value in capsule['timeframes'].items():
                if not hasattr(config.timeframes, key):
                    raise ValueError('Unknown timeframe field')
                setattr(config.timeframes, key, value)
            config.telegram_allowed_user_ids = tuple(capsule.get('recipients', ()))
            db._local.job = capsule.get('job')
            # object() is an explicit inert AI dependency; no paid client initialization.
            service = AnalysisService(broker, ai_client=object())
            with audit_timeline(replay=timeline):
                actual = service._finalize.__wrapped__(service, **decode(capsule['inputs']))
            remaining = list(timeline)
            matched = (decision(actual) == capsule['expected'] and not remaining
                       and broker.index == len(broker.calls))
            return {'matched': matched, 'scope': capsule['scope'],
                    'analysis_id': capsule['analysis_id'], 'expected': capsule['expected'],
                    'actual': decision(actual), 'unused_clock_reads': len(remaining),
                    'unused_broker_reads': len(broker.calls) - broker.index,
                    'network_calls': 0, 'live_database_writes': 0}
    finally:
        db.DB_PATH, db._local.job = previous_path, previous_job
        config.__dict__.clear()
        config.__dict__.update(previous_config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--analysis-id', required=True)
    args = parser.parse_args()
    try:
        result = replay(load_capsule(args.db, args.analysis_id))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['matched'] else 1
    except Exception as exc:
        print(json.dumps({'matched': False, 'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
