"""
core/rules_loader.py
---------------------
بند ۵ سند: "هوش مصنوعی از قبل ANEWME را نمی‌شناسد و نباید به حافظه چت قبلی
متکی باشد. در هر تحلیل اولیه و هر بررسی مجدد، متن کامل قوانین ANEWME باید
همراه درخواست فرستاده شود."

این ماژول فقط مسئول خواندن آن متن از فایل rules/anewme_rules.txt است.
خود متنِ استراتژی معاملاتی (قوانین ANEWME) در این سند ۲۱ بندی نیامده -
آن سند رفتاری سیستم است، نه استراتژی. کارفرما باید متن کامل استراتژی را
در فایل rules/anewme_rules.txt قرار دهد.
"""

from functools import lru_cache
from config import RULES_FILE


class RulesNotConfiguredError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def load_anewme_rules() -> str:
    """
    متن کامل قوانین ANEWME را برمی‌گرداند.
    lru_cache باعث می‌شود فایل فقط یک‌بار از دیسک خوانده شود (فایل تغییر
    نمی‌کند در حین اجرا؛ در صورت نیاز به ری‌لود، از invalidate_cache استفاده کنید).
    """
    if not RULES_FILE.exists():
        raise RulesNotConfiguredError(
            f"فایل قوانین ANEWME یافت نشد: {RULES_FILE}\n"
            "لطفاً متن کامل استراتژی معاملاتی ANEWME را در این فایل قرار دهید."
        )
    text = RULES_FILE.read_text(encoding="utf-8").strip()
    if not text:
        raise RulesNotConfiguredError("فایل قوانین ANEWME خالی است.")
    return text


def invalidate_cache() -> None:
    load_anewme_rules.cache_clear()
