"""Single bounded analysis worker; monitoring and Telegram remain responsive."""
import asyncio
from datetime import datetime, timezone
from config import config
from storage import db, work_queue
from telegram_bot.notifier import format_analysis_message
from core.models import AnalysisResult, AnalysisStatus


class AnalysisWorker:
    def __init__(self, service):
        self.service = service
        self.running = True

    def recipients(self, job):
        return [job['chat_id']] if job['chat_id'] is not None else config.telegram_allowed_user_ids

    def recover(self):
        # Called once under runtime's exclusive process lock, never by each poll.
        with db.transaction(), db.get_connection() as conn:
            rows = conn.execute("SELECT * FROM watches WHERE close_status='TRIGGERED' AND reanalysis_completed_at IS NULL").fetchall()
            for row in rows:
                conn.execute('INSERT OR IGNORE INTO analysis_jobs(id,symbol,watch_id,created_at,status) VALUES(?,?,?,?,?)',
                             ('watch:' + row['watch_id'], row['symbol'], row['watch_id'], work_queue.now(),
                              'RUNNING' if row['reanalysis_started_at'] else 'PENDING'))
        with db.get_connection() as conn:
            running = conn.execute("SELECT * FROM analysis_jobs WHERE status='RUNNING'").fetchall()
        for job in running:
            text = f"{job['symbol']} | NO_TRADE\nتحلیل با توقف برنامه قطع شد؛ برای جلوگیری از تکرار، همان تماس دوباره اجرا نشد."
            work_queue.finish(job, text, self.recipients(job))

    async def tick(self):
        job = work_queue.claim()
        if not job:
            return False
        try:
            if job['watch_id']:
                db.mark_reanalysis_started(job['watch_id'])
                result = await self._run_thread(job)
            else:
                result = await self._run_thread(job)
            text = format_analysis_message(result)
            work_queue.finish(job, text, self.recipients(job), result.status.value)
        except Exception as exc:
            db.log_error('analysis_worker', str(exc), job['symbol'])
            text = f"{job['symbol']} | NO_TRADE\nتحلیل تکمیل نشد: {exc}\nهیچ سیگنال اجرایی صادر نشد."
            work_queue.finish(job, text, self.recipients(job))
        return True

    async def _run_thread(self, job):
        task = asyncio.create_task(asyncio.to_thread(self._execute, job))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not disconnect MT5 while a thread is still using it.
            await task
            raise

    def _execute(self, job):
        db._local.job = job
        try:
            if job['watch_id']:
                return self.service.run_watch_recheck(db.get_watch(job['watch_id']))
            return self.service.run_initial_analysis(job['symbol'])
        finally:
            db._local.job = None

    async def start(self):
        while self.running:
            if not await self.tick():
                await asyncio.sleep(.5)

    def stop(self):
        self.running = False
