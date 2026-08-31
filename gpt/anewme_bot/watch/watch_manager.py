"""Immutable watch conditions and chronological closed-candle checks."""
from core.clock import utc_now, tehran_time
from datetime import datetime, timezone
from dataclasses import asdict
import json
import uuid
from broker.candle_utils import closed_only, normalize_candle, utc, TIMEFRAME_MINUTES
from core.models import WatchState
from storage import db, work_queue
from config import config
from watch.conditions import Condition, parse_conditions, levels, expiration

def _parse_expiration(text):
    return expiration(text)

def _extract_levels(text):
    try:
        return levels(text)
    except ValueError:
        return []

def validate_before_creation(details, candle):
    try:
        trigger, invalid = parse_conditions(details.preferred_direction.value, details.trigger_type,
                                            details.exact_zone_or_level, details.invalidation)
        expiration(details.expiration)
        if candle is None:
            raise ValueError('کندل بسته‌شده شرط ابطال در دسترس نیست.')
        normalized = normalize_candle(candle, invalid.timeframe)
        if normalized['close_time'] > utc_now():
            raise ValueError('کندل ابطال هنوز بسته نشده است.')
        if invalid.matches(float(normalized['close'])):
            raise ValueError(f"سناریو پیش از ثبت باطل است: {invalid.timeframe} close={normalized['close']}; "
                             f"level={invalid.level}; close_time={normalized['close_time'].isoformat()}")
    except (ValueError, TypeError, KeyError) as exc:
        return f'واچ ساخته نشد: {exc}'
    return None

def create_watch_from_details(symbol, watch_details, parent_analysis_id, baseline_candle=None):
    trigger, invalid = parse_conditions(watch_details.preferred_direction.value, watch_details.trigger_type,
                                        watch_details.exact_zone_or_level, watch_details.invalidation)
    watch = WatchState(str(uuid.uuid4()), symbol, parent_analysis_id, watch_details.preferred_direction,
                       watch_details.current_or_potential_grade, watch_details.trigger_type,
                       watch_details.exact_zone_or_level, watch_details.timeframes_to_recheck,
                       expiration(watch_details.expiration), watch_details.invalidation, utc_now())
    with db.transaction():
        db.save_watch(dict(watch_id=watch.watch_id, symbol=symbol, parent_analysis_id=parent_analysis_id,
                           direction=watch.direction.value, grade=watch.grade.value, trigger_type=watch.trigger_type,
                           zone_or_level=watch.zone_or_level, timeframes_to_recheck=watch.timeframes_to_recheck,
                           expiration=watch.expiration.isoformat(), invalidation_condition=watch.invalidation_condition,
                           created_at=watch.created_at.isoformat()))
        with db.get_connection() as conn:
            conn.execute('UPDATE watches SET conditions_json=? WHERE watch_id=?',
                         (json.dumps([asdict(trigger), asdict(invalid)]), watch.watch_id))
        db.log_event('WATCH_CREATED', f'trigger={asdict(trigger)}; invalidation={asdict(invalid)}', symbol, watch.watch_id)
    return watch

def _conditions(row):
    if 'conditions_json' in row.keys() and row['conditions_json']:
        return tuple(Condition(**v) for v in json.loads(row['conditions_json']))
    return parse_conditions(row['direction'], row['trigger_type'], row['zone_or_level'], row['invalidation_condition'])

def check_trigger(watch_row, broker):
    if watch_row['is_closed'] or watch_row['is_triggered'] or watch_row['is_locked']:
        return False, ''
    now = utc_now()
    expiry = utc(datetime.fromisoformat(watch_row['expiration'].replace('Z', '+00:00')))
    if now >= expiry:
        return True, 'EXPIRATION_REACHED'
    row = db.get_watch(watch_row['watch_id'])
    if row is None or row['is_closed'] or row['is_triggered'] or row['is_locked']:
        return False, ''
    if row['pending_event']:
        event = json.loads(row['pending_event'])
        return True, event['reason']
    if hasattr(broker, 'get_open_positions'):
        positions = broker.get_open_positions(row['symbol'])
        orders = broker.get_pending_orders(row['symbol'])
        if positions is None or orders is None:
            raise RuntimeError('وضعیت حساب قابل بررسی نیست.')
        if positions or orders:
            return True, 'ACCOUNT_ACTIVE'
    try:
        trigger, invalid = _conditions(row)
    except (ValueError, TypeError, KeyError) as exc:
        db.log_event('WATCH_INVALID_CONDITION', str(exc), row['symbol'], row['watch_id'])
        return True, 'INVALIDATION_REACHED'
    created = utc(datetime.fromisoformat(row['created_at']))
    cursors = json.loads(row['candle_cursors'] or '{}')
    series = {}
    for tf in {trigger.timeframe, invalid.timeframe, 'M5'}:
        after = utc(datetime.fromisoformat(cursors[tf])) if tf in cursors else created
        bars = closed_only(broker.get_candles(row['symbol'], tf, 3), tf, now)
        # Never replay a historical crossing when the latest closed bar differs.
        series[tf] = [b for b in bars[-1:] if after < b['close_time'] <= expiry]
    timeline = sorted({b['close_time'] for bars in series.values() for b in bars})
    try:
        bid, ask = broker.get_current_price(row['symbol'])
    except Exception:
        bid, ask = None, None
    for stamp in timeline:
        batch = {tf: next((b for b in bars if b['close_time'] == stamp), None) for tf, bars in series.items()}
        batch = {tf: b for tf, b in batch.items() if b is not None}
        with db.transaction(), db.get_connection() as conn:
            current = conn.execute('SELECT * FROM watches WHERE watch_id=?', (row['watch_id'],)).fetchone()
            if current['is_closed'] or current['pending_event']:
                return False, ''
            actual = json.loads(current['candle_cursors'] or '{}')
            batch = {tf: b for tf, b in batch.items() if tf not in actual or b['close_time'] > utc(datetime.fromisoformat(actual[tf]))}
            if not batch:
                continue
            evidence = dict(candles={tf: {k: str(v) if isinstance(v, datetime) else v for k, v in b.items()} for tf, b in batch.items()},
                            bid=bid, ask=ask, trigger=asdict(trigger), invalidation=asdict(invalid))
            db.log_event('WATCH_CANDLE_EVIDENCE', json.dumps(evidence, default=str), row['symbol'], row['watch_id'])
            reason = ''
            inv_bar = batch.get(invalid.timeframe)
            trig_bar = batch.get(trigger.timeframe)
            invalidated = bool(inv_bar and invalid.matches(inv_bar['close']))
            triggered = bool(trig_bar and trigger.matches(trig_bar['close']))
            db.log_event('WATCH_COMPARISON', json.dumps({
                'trigger_close': str(trig_bar['close']) if trig_bar else None,
                'trigger_level': str(trigger.level), 'operator': trigger.operator,
                'trigger_matched': triggered, 'invalidation_matched': invalidated,
                'rounding': False, 'tolerance': None}, ensure_ascii=False), row['symbol'], row['watch_id'])
            if invalidated:
                reason = 'INVALIDATION_REACHED'
            elif triggered:
                reason = f"CANDLE_{trigger.timeframe}_TRIGGER; close={trig_bar['close']}; operator={trigger.operator}; level={trigger.level}; close_time={stamp.isoformat()}"
            actual.update({tf: b['close_time'].isoformat() for tf, b in batch.items()})
            event = json.dumps(dict(reason=reason, candle_time=stamp.isoformat(), evidence=evidence), default=str) if reason else None
            conn.execute('UPDATE watches SET candle_cursors=?,last_checked_candle_time=?,pending_event=? WHERE watch_id=?',
                         (json.dumps(actual), stamp.isoformat(), event, row['watch_id']))
            db.log_event('WATCH_CHECKED', stamp.isoformat(), row['symbol'], row['watch_id'])
            if reason:
                return True, reason
            if 'M5' in batch:
                candle = batch['M5']
                remaining = max(0, int((expiry-now).total_seconds()))
                result_text = ('تریگر فعال نشد' if trigger.timeframe == 'M5' else
                               f'در انتظار شرط بسته‌شدن {trigger.timeframe}؛ این پیام گزارش M5 است')
                message = (f"{row['symbol']} | واچ فعال\nکندل ۵ دقیقه‌ای بسته شد\n"
                           f"ساعت بسته‌شدن کندل: {candle['close_time'].strftime('%Y-%m-%d %H:%M UTC')}\n"
                           f"{tehran_time(candle['close_time'])}\n"
                           f"قیمت بسته‌شدن: {candle['close']}\nسطح تریگر: {row['zone_or_level']}\n"
                           f"نتیجه: {result_text}\n"
                           f"زمان باقی‌مانده تا انقضا در زمان بررسی: {remaining//60} دقیقه و {remaining%60} ثانیه")
                for recipient in config.telegram_allowed_user_ids:
                    work_queue.queue_message(f"watch-check:{row['watch_id']}:M5:{stamp.isoformat()}",
                                             recipient, message, row['watch_id'])
    return False, ''

def claim_trigger(watch_id, reason):
    row = db.get_watch(watch_id)
    event = json.loads(row['pending_event']) if row and row['pending_event'] else {}
    return db.claim_watch_trigger(watch_id, reason, event.get('candle_time'))

def close_watch(watch_id, status, reason):
    changed = db.close_watch_lifecycle(watch_id, status, reason)
    if changed:
        db.log_event('WATCH_' + status, reason, watch_id=watch_id)
    return changed

def lock_watch(watch_id):
    db.update_watch_flags(watch_id, is_locked=True)

def unlock_watch(watch_id):
    db.update_watch_flags(watch_id, is_locked=False)

def evaluate_level_candle(direction, close, trigger_level, invalidation_level):
    invalid = Condition('M5', 'LE' if direction == 'BUY' else 'GE', invalidation_level)
    trigger = Condition('M5', 'GT' if direction == 'BUY' else 'LT', trigger_level)
    return 'INVALIDATED' if invalid.matches(close) else 'TRIGGERED' if trigger.matches(close) else 'ACTIVE'

def _invalidation_reached(condition, candle, direction):
    from watch.conditions import clean
    ns = levels(condition)
    if len(ns) != 1:
        raise ValueError('سطح ابطال مبهم است.')
    text = clean(condition)
    op = 'GE' if any(k in text for k in ('above', 'بالا', '>')) else 'LE' if any(k in text for k in ('below', 'زیر', 'پایین', '<')) else 'LE' if direction == 'BUY' else 'GE'
    return Condition('M5', op, ns[0]).matches(candle['close'])

def is_materially_same_scenario(previous, new_details):
    try:
        old = parse_conditions(previous.direction.value, previous.trigger_type, previous.zone_or_level, previous.invalidation_condition)
        new = parse_conditions(new_details.preferred_direction.value, new_details.trigger_type, new_details.exact_zone_or_level, new_details.invalidation)
        return previous.direction == new_details.preferred_direction and all(
            a.timeframe == b.timeframe and a.operator == b.operator and
            abs(a.level-b.level) <= max(1e-8, abs(a.level)*1e-4) and
            abs((a.upper or a.level)-(b.upper or b.level)) <= max(1e-8, abs(a.upper or a.level)*1e-4)
            for a,b in zip(old,new))
    except ValueError:
        return True
