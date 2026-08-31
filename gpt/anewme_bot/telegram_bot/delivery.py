import asyncio
import time
from datetime import timedelta
from telegram.error import BadRequest, Forbidden, RetryAfter
from storage import work_queue, db
from core.clock import utc_now
from broker.candle_utils import utc
from datetime import datetime


class DeliveryWorker:
    def __init__(self, bot):
        self.bot = bot
        self.running = True
        self.last_maintenance = 0

    async def tick(self):
        blocked = set()
        for message in work_queue.pending_messages():
            if message['id'].startswith('watch-check:'):
                watch = db.get_watch(message['watch_id'])
                if (watch is None or watch['is_closed'] or watch['is_triggered'] or
                        utc_now() >= utc(datetime.fromisoformat(watch['expiration']))):
                    with db.transaction(), db.get_connection() as conn:
                        conn.execute("UPDATE outbox SET status='SUPPRESSED',last_error=? WHERE id=?",
                                     ('واچ دیگر فعال نیست؛ پیام تشخیصی ارسال نشد.', message['id']))
                        db.log_event('WATCH_CHECK_MESSAGE_SUPPRESSED', message['id'], watch_id=message['watch_id'])
                    continue
            if message['chat_id'] in blocked:
                continue
            try:
                await self.bot.send_message(chat_id=message['chat_id'], text=message['text'])
            except RetryAfter as exc:
                delay = exc.retry_after.total_seconds() if isinstance(exc.retry_after, timedelta) else exc.retry_after
                work_queue.delivery_failed(message, exc, delay=delay)
                blocked.add(message['chat_id'])
            except (Forbidden, BadRequest) as exc:
                work_queue.delivery_failed(message, exc, permanent=True)
                blocked.add(message['chat_id'])
            except Exception as exc:
                work_queue.delivery_failed(message, exc)
                blocked.add(message['chat_id'])
            else:
                work_queue.delivered(message)

    async def start(self):
        while self.running:
            if time.monotonic() - self.last_maintenance > 3600:
                db.prune_operational_logs()
                self.last_maintenance = time.monotonic()
            await self.tick()
            await asyncio.sleep(1)

    def stop(self):
        self.running = False
