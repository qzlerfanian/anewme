"""Fast watch polling; durable analysis jobs execute in a separate worker."""
import asyncio
import json
import logging
from config import config
from storage import db, work_queue
from watch import watch_manager

logger = logging.getLogger(__name__)

class WatchMonitor:
    def __init__(self, broker, analysis_service=None, notify=None):
        self.broker = broker
        self.analysis_service = analysis_service
        self._running = False

    async def start(self):
        self._running = True
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                logger.exception('Watch monitor failed')
                db.log_error('watch_monitor', str(exc))
            await asyncio.sleep(max(1, config.watch_poll_interval_seconds))

    def stop(self):
        self._running = False

    async def _tick(self):
        for row in db.get_active_watches():
            try:
                changed, reason = await asyncio.to_thread(watch_manager.check_trigger, row, self.broker)
                if not changed:
                    continue
                with db.transaction():
                    fresh = db.get_watch(row['watch_id'])
                    event = json.loads(fresh['pending_event']) if fresh['pending_event'] else {}
                    evidence = event.get('evidence', {})
                    if reason in ('INVALIDATION_REACHED', 'EXPIRATION_REACHED', 'ACCOUNT_ACTIVE'):
                        status = 'EXPIRED' if reason == 'EXPIRATION_REACHED' else 'INVALIDATED'
                        detail = ('شرط ابطال: ' + row['invalidation_condition']) if status == 'INVALIDATED' else 'اعتبار واچ پایان یافت؛ تریگر قابل اجرا در مهلت پایش ثبت نشد.'
                        if reason == 'ACCOUNT_ACTIVE':
                            detail = 'معامله یا سفارش فعال در حساب شناسایی شد؛ واچ بسته شد و تریگر جدید صادر نشد.'
                        if evidence:
                            detail += '\n' + json.dumps(evidence, ensure_ascii=False, default=str)
                        won = watch_manager.close_watch(row['watch_id'], status, detail)
                        text = f"{row['symbol']} | {'واچ باطل شد' if status == 'INVALIDATED' else 'واچ منقضی شد'}\n{detail}"
                    else:
                        won = watch_manager.claim_trigger(row['watch_id'], reason)
                        text = f"{row['symbol']} | واچ تریگر شد\n{reason}\nتحلیل نهایی در صف قرار گرفت."
                    if won:
                        text += f"\nWatch ID: {row['watch_id']}"
                        for recipient in config.telegram_allowed_user_ids:
                            work_queue.queue_message('watch-end:' + row['watch_id'], recipient, text, row['watch_id'])
            except Exception as exc:
                logger.exception('Watch check failed for %s', row['symbol'])
                db.log_error('watch_check', str(exc), row['symbol'])
