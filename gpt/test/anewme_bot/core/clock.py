"""One UTC reference, anchored to advancing MT5 ticks in live operation.

The broker adapter converts its configured raw data encoding to UTC once.
The monotonic timer advances the reference even if quotes stop arriving.
"""
from datetime import datetime, timezone, timedelta
from threading import RLock, local
from contextlib import contextmanager
import time

_lock = RLock()
_anchor = None
_monotonic = None
_audit_clock = local()


def utc_now():
    replay = getattr(_audit_clock, 'replay', None)
    if replay is not None:
        return datetime.fromisoformat(next(replay))
    with _lock:
        if _anchor is None:  # offline tests / before broker connection
            value = datetime.now(timezone.utc)
        else:
            value = _anchor + timedelta(seconds=time.monotonic() - _monotonic)
    record = getattr(_audit_clock, 'record', None)
    if record is not None:
        record.append(value.isoformat())
    return value


@contextmanager
def audit_timeline(record=None, replay=None):
    """Thread-local instrumentation; never changes the live broker anchor."""
    previous = (_audit_clock.__dict__).copy()
    _audit_clock.record, _audit_clock.replay = record, replay
    try:
        yield
    finally:
        _audit_clock.__dict__.clear()
        _audit_clock.__dict__.update(previous)


def reference_status():
    with _lock:
        anchored = _anchor is not None
    now = utc_now()
    return {'source': 'broker-monotonic' if anchored else 'system-fallback',
            'utc': now.isoformat(),
            'system_difference_seconds': round((now - datetime.now(timezone.utc)).total_seconds(), 1)}


def anchor_to_broker(timestamp):
    global _anchor, _monotonic
    value = datetime.fromtimestamp(float(timestamp), timezone.utc)
    with _lock:
        _anchor, _monotonic = value, time.monotonic()


def reset():
    global _anchor, _monotonic
    with _lock:
        _anchor = _monotonic = None


def tehran_time(value):
    """Presentation only. All persisted values remain normalized UTC."""
    time_only = False
    if isinstance(value, str):
        original = value
        try:
            if value.endswith(' UTC'):
                time_only = len(value) <= 12
                fmt = '%H:%M UTC' if time_only else '%Y-%m-%d %H:%M UTC'
                value = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            else:
                value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return original
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone(timedelta(hours=3, minutes=30))).strftime('%H:%M' if time_only else '%Y-%m-%d %H:%M') + ' به وقت تهران'
