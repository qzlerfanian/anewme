"""Canonical closed-candle/time handling shared by analysis and watch flows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math


TIMEFRAME_MINUTES = {"M5": 5, "M15": 15, "H1": 60}


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_candle(candle: dict, timeframe: str, *, source_index: int | None = None) -> dict:
    """Return a candle with explicit UTC open/close times.

    MT5's ``time`` is the bar *open* time.  Keeping ``close_time`` explicit
    prevents a 13:20--13:25 bar from being reported as "closed at 13:20".
    """
    open_time = utc(candle.get("open_time") or candle["time"])
    if not all(math.isfinite(float(candle[k])) and float(candle[k]) > 0 for k in ('open', 'high', 'low', 'close')):
        raise ValueError('OHLC کندل نامعتبر است.')
    if not (candle['low'] <= min(candle['open'], candle['close']) <= max(candle['open'], candle['close']) <= candle['high']):
        raise ValueError('ترتیب OHLC کندل ناسازگار است.')
    close_time = utc(candle.get("close_time") or (open_time + timedelta(minutes=TIMEFRAME_MINUTES[timeframe])))
    result = dict(candle)
    result.update({"time": open_time, "open_time": open_time, "close_time": close_time, "timeframe": timeframe})
    if source_index is not None:
        result["source_index"] = source_index
    return result


def closed_only(candles: list[dict], timeframe: str, reference_time: datetime) -> list[dict]:
    reference = utc(reference_time)
    normalized = [normalize_candle(c, timeframe, source_index=c.get("source_index")) for c in candles]
    # Future bars and the currently forming bar are never allowed downstream.
    return sorted((c for c in normalized if c["close_time"] <= reference), key=lambda c: c["open_time"])


def latest_closed(candles: list[dict], timeframe: str, reference_time: datetime) -> dict | None:
    items = closed_only(candles, timeframe, reference_time)
    return items[-1] if items else None
