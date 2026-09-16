"""Strict parsing at the boundary; numeric, immutable conditions thereafter."""
from dataclasses import dataclass, asdict
from core.clock import utc_now
from datetime import datetime, timezone
import math
import re
from decimal import Decimal


def clean(text):
    return str(text).translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫', '01234567890123456789.')).lower()


def timeframe(text, default='M5'):
    value = clean(text)
    if any(tf not in ('m5', 'm15', 'h1') for tf in re.findall(r'\b[mh]\d+\b', value)):
        raise ValueError('تایم‌فریم شرط پشتیبانی نمی‌شود.')
    matches = re.findall(r'\b(m5|m15|h1)\b', value)
    for pattern, tf in ((r'(?:۵|5|پنج)\s*دقیقه', 'm5'),
                        (r'(?:۱۵|15|پانزده)\s*دقیقه', 'm15'),
                        (r'(?:یک|1)\s*ساعت', 'h1')):
        if re.search(pattern, value):
            matches.append(tf)
    if len(set(matches)) > 1:
        raise ValueError('تایم‌فریم شرط مبهم است.')
    return matches[0].upper() if matches else default


def levels(text):
    value = clean(text)
    if value.strip().startswith('-'):
        raise ValueError('سطح قیمت منفی مجاز نیست.')
    value = re.sub(r'\b(?:m5|m15|h1)\b', '', value)
    value = re.sub(r'(?:15|5|1)\s*(?:دقیقه|ساعت)(?:‌ای|ای)?', '', value)
    if re.search(r'\d\s*[,،]\s*\d|\d\s*:\s*\d', value):
        raise ValueError('سطح قیمت باید بدون جداکننده هزارگان یا ساعت باشد.')
    nums = [float(n) for n in re.findall(r'(?<![\w.])\d+(?:\.\d+)?(?![\w.])', value)]
    if not nums or not all(math.isfinite(n) and n > 0 for n in nums):
        raise ValueError('سطح عددی مثبت و دقیق پیدا نشد.')
    return nums


@dataclass(frozen=True)
class Condition:
    timeframe: str
    operator: str
    level: float
    upper: float | None = None

    def matches(self, close):
        if not math.isfinite(float(close)):
            raise ValueError('Close نامعتبر است.')
        value = Decimal(str(close))
        level = Decimal(str(self.level))
        # No rounding, epsilon, tolerance, tick price or OHLC high/low.
        return {'GT': lambda: value > level,
                'LT': lambda: value < level,
                'LE': lambda: value <= level,
                'GE': lambda: value >= level,
                'ZONE': lambda: level <= value <= Decimal(str(self.upper))}[self.operator]()


def parse_conditions(direction, trigger_type, zone, invalidation):
    if direction not in ('BUY', 'SELL'):
        raise ValueError('جهت واچ نامعتبر است.')
    kind = clean(trigger_type)
    if any(k in kind for k in ('زمان مشخص', 'specific time', 'expiration', 'زمان انقضا', 'شرط ابطال', 'invalidation')):
        raise ValueError('این نوع شرط تریگر کندلی نیست؛ واچ قابل اجرا نیست.')
    nums = levels(zone)
    if len(nums) not in (1, 2):
        raise ValueError('تریگر باید یک سطح یا یک محدوده دقیق باشد.')
    tf = timeframe(trigger_type + ' ' + zone)
    op = 'GT' if direction == 'BUY' else 'LT'
    trigger = Condition(tf, op, nums[0]) if len(nums) == 1 else Condition(tf, 'ZONE', min(nums), max(nums))
    inv = levels(invalidation)
    if len(inv) != 1:
        raise ValueError('شرط ابطال باید دقیقاً یک سطح داشته باشد.')
    inv_op = 'LE' if direction == 'BUY' else 'GE'
    text = clean(invalidation)
    if (direction == 'BUY' and any(k in text for k in ('above', 'بالاتر', 'بالای', '>'))) or (
            direction == 'SELL' and any(k in text for k in ('below', 'زیر', 'پایین', '<'))):
        raise ValueError('جهت شرط ابطال با سناریو سازگار نیست.')
    invalid = Condition(timeframe(invalidation), inv_op, inv[0])
    boundary = trigger.level if direction == 'BUY' else (trigger.upper or trigger.level)
    if (direction == 'BUY' and invalid.level >= boundary) or (direction == 'SELL' and invalid.level <= boundary):
        raise ValueError('سطوح تریگر و ابطال هم‌پوشانی یا ترتیب نامعتبر دارند.')
    return trigger, invalid


def expiration(text, now=None):
    now = now or utc_now()
    value = clean(text).strip().replace('z', '+00:00')
    if re.fullmatch(r'\d{2}:\d{2}', value):
        h, m = map(int, value.split(':'))
        parsed = now.replace(hour=h, minute=m, second=0, microsecond=0)
    else:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= now:
        raise ValueError('زمان انقضای واچ گذشته است.')
    return parsed
