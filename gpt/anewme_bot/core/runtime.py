"""Local process ownership and secret-safe rotating logs."""
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import time
from config import DATA_DIR, LOG_DIR


def redact(value):
    text = str(value)
    text = re.sub(r'bot\d+:[A-Za-z0-9_-]+', 'bot[REDACTED]', text)
    text = re.sub(r'sk-[A-Za-z0-9_-]+', '[REDACTED]', text)
    for key in ('TELEGRAM_BOT_TOKEN', 'OPENAI_API_KEY', 'MT5_PASSWORD'):
        secret = os.getenv(key)
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return text


class SafeFormatter(logging.Formatter):
    converter = time.gmtime
    def format(self, record):
        return redact(super().format(record))


def configure_logging():
    file = RotatingFileHandler(LOG_DIR / 'anewme_bot.log', maxBytes=5_000_000, backupCount=3, encoding='utf-8')
    console = logging.StreamHandler()
    for handler in (file, console):
        handler.setFormatter(SafeFormatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[file, console], force=True)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)


class ProcessLock:
    def __enter__(self):
        self.file = open(DATA_DIR / 'runtime.lock', 'a+b')
        if self.file.tell() == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('نسخه دیگری از همین ربات روی این دیتابیس فعال است.')
        return self

    def __exit__(self, *args):
        self.file.close()
