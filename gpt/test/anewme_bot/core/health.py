"""Read-only operational diagnostics. No AI requests or trading actions."""
from pathlib import Path
from core.clock import reference_status
from core.version import build_identity
from storage import db


def health_report(broker):
    build = build_identity()
    clock = reference_status()
    lines = [f"Build: {build['version']}", f"Source: {build['source_sha256'][:16]}",
             f"Database: {Path(db.DB_PATH).resolve()}",
             f"Clock source: {clock['source']}", f"UTC: {clock['utc']}",
             f"Clock minus system: {clock['system_difference_seconds']} seconds"]
    try:
        with db.get_connection() as conn:
            for table in ('analysis_jobs', 'outbox'):
                counts = conn.execute(f'SELECT status,COUNT(*) FROM {table} GROUP BY status').fetchall()
                lines.append(table + ': ' + (', '.join(f'{r[0]}={r[1]}' for r in counts) or 'empty'))
            active = conn.execute('SELECT COUNT(*) FROM watches WHERE is_closed=0 AND is_triggered=0').fetchone()[0]
            capsules = conn.execute('SELECT COUNT(*) FROM replay_capsules').fetchone()[0]
            missing = conn.execute('SELECT COUNT(*) FROM decision_passports p LEFT JOIN replay_capsules r ON p.analysis_id=r.analysis_id WHERE r.analysis_id IS NULL').fetchone()[0]
            lines.extend([f'Active watches: {active}', f'Replay capsules: {capsules}',
                          f'Passports without replay (early guard or capture failure): {missing}'])
            latest=conn.execute('SELECT run_id,status,actual_started_at FROM scheduled_runs ORDER BY actual_started_at DESC LIMIT 1').fetchone()
            if latest: lines.append(f'Scheduled batch: {latest[0]} | {latest[1]} | {latest[2]}')
            for row in conn.execute('SELECT component,status,consecutive_failures,last_error FROM scheduled_health'):
                lines.append(f'Health {row[0]}: {row[1]} failures={row[2]}' + (f' error={row[3]}' if row[3] else ''))
    except Exception as exc:
        lines.append(f'Database check failed: {type(exc).__name__}')
    try:
        status = broker.get_health_status()
        lines.append(f"MT5 connected: {status['connected']}")
        lines.append(f"MT5 data UTC offset: {status['data_utc_offset_minutes']} minutes")
        for symbol, item in status.get('symbols', {}).items():
            lines.append(f"{symbol}: tick age={item.get('tick_age_seconds', 'unknown')} seconds")
            for tf, candle in item.get('candles', {}).items():
                lines.append(f"  {tf}: {candle}")
            if item.get('error'):
                lines.append(f"  read failed: {item['error']}")
    except Exception as exc:
        lines.append(f'MT5 health unavailable: {type(exc).__name__}')
    lines.append('OpenAI/Telegram connectivity and quota: not probed; no paid request made.')
    return '\n'.join(lines)
