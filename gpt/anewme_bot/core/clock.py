"""One UTC reference, anchored to advancing MT5 ticks in live operation.

The broker adapter converts its configured raw data encoding to UTC once.
The monotonic timer advances the reference even if quotes stop arriving.
"""
from datetime import datetime, timezone, timedelta
from threading import RLock
import time

_lock = RLock()
_anchor = None
_monotonic = None


def utc_now():
    with _lock:
        if _anchor is None:  # offline tests / before broker connection
            return datetime.now(timezone.utc)
        return _anchor + timedelta(seconds=time.monotonic() - _monotonic)


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
