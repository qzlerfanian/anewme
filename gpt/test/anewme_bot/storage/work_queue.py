"""Durable local work and per-recipient notifications. One runtime owns dispatch."""
import time
from core.clock import utc_now
from datetime import datetime, timezone
from storage import db


def now():
    return utc_now().isoformat()


def enqueue(job_id, symbol, chat_id):
    with db.transaction(), db.get_connection() as conn:
        if conn.execute("SELECT 1 FROM analysis_jobs WHERE symbol=? AND status IN ('PENDING','RUNNING')", (symbol,)).fetchone():
            return False
        return conn.execute('INSERT OR IGNORE INTO analysis_jobs(id,symbol,chat_id,created_at) VALUES(?,?,?,?)',
                            (job_id, symbol, chat_id, now())).rowcount == 1


def busy(symbol):
    with db.get_connection() as conn:
        return conn.execute("SELECT 1 FROM analysis_jobs WHERE symbol=? AND status IN ('PENDING','RUNNING')", (symbol,)).fetchone() is not None


def claim():
    with db.transaction(), db.get_connection() as conn:
        row = conn.execute("SELECT * FROM analysis_jobs WHERE status='PENDING' ORDER BY created_at LIMIT 1").fetchone()
        if row:
            conn.execute("UPDATE analysis_jobs SET status='RUNNING',started_at=? WHERE id=?", (now(), row['id']))
        return row


def queue_message(key, chat_id, text, watch_id=None):
    from core.runtime import redact
    text = redact(text)
    # 1800 Unicode code points <= 3600 UTF-16 code units, even for emoji.
    with db.transaction(), db.get_connection() as conn:
        for index, start in enumerate(range(0, len(text), 1800)):
            inserted = conn.execute('INSERT OR IGNORE INTO outbox(id,chat_id,text,watch_id) VALUES(?,?,?,?)',
                         (f'{key}:{chat_id}:{index:05d}', chat_id, text[start:start+1800], watch_id))
            if inserted.rowcount == 0:
                db.log_event('NOTIFICATION_DUPLICATE_SUPPRESSED', f'{key}:{chat_id}:{index}', watch_id=watch_id)


def finish(job, text, recipients, result_status='NO_TRADE'):
    with db.transaction(), db.get_connection() as conn:
        existing = conn.execute('SELECT status FROM analysis_jobs WHERE id=?', (job['id'],)).fetchone()
        if existing and existing['status'] == 'DONE':
            return
        conn.execute("UPDATE analysis_jobs SET status='DONE',completed_at=?,result_text=? WHERE id=?", (now(), text, job['id']))
        for chat_id in recipients:
            queue_message('result:' + job['id'], chat_id, text, job['watch_id'])
        if job['watch_id']:
            db.mark_reanalysis_completed(job['watch_id'], result_status)


def pending_messages():
    with db.get_connection() as conn:
        return conn.execute("""SELECT o.* FROM outbox o WHERE status='PENDING' AND next_attempt<=?
            AND NOT EXISTS (SELECT 1 FROM outbox earlier WHERE earlier.chat_id=o.chat_id
                            AND earlier.status='PENDING' AND earlier.rowid<o.rowid)
            ORDER BY o.rowid LIMIT 30""", (time.time(),)).fetchall()


def delivered(message):
    with db.transaction(), db.get_connection() as conn:
        conn.execute("UPDATE outbox SET status='SENT',sent_at=? WHERE id=?", (now(), message['id']))
        if message['watch_id']:
            db.record_watch_notification(message['watch_id'], suppressed=False, note=f"تحویل تأیید شد: {message['id']}")


def delivery_failed(message, error, permanent=False, delay=None):
    from core.runtime import redact
    error = redact(error)
    with db.get_connection() as conn:
        conn.execute('UPDATE outbox SET status=?,attempts=attempts+1,last_error=?,next_attempt=? WHERE id=?',
                     ('FAILED' if permanent else 'PENDING', str(error),
                      time.time() + (delay if delay is not None else min(300, 2 ** min(message['attempts']+1, 8))), message['id']))
    db.log_event('NOTIFICATION_FAILED', f"id={message['id']}; permanent={permanent}; {error}", watch_id=message['watch_id'])


def cache_get(key):
    with db.get_connection() as conn:
        row = conn.execute('SELECT description FROM chart_cache WHERE fingerprint=?', (key,)).fetchone()
        return row['description'] if row else None


def cache_put(key, value):
    with db.get_connection() as conn:
        conn.execute('INSERT OR REPLACE INTO chart_cache VALUES(?,?,?)', (key, value, now()))
