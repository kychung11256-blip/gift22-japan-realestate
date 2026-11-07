"""Precision-preserving Japanese dates. Callers retain their raw source fields."""
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

ERA_BASE = {'令和': 2018, '平成': 1988, '昭和': 1925}
ERA_BOUNDS = {'令和': (date(2019, 5, 1), date.max),
              '平成': (date(1989, 1, 8), date(2019, 4, 30)),
              '昭和': (date(1926, 12, 25), date(1989, 1, 7))}
JST = timezone(timedelta(hours=9))


def parse_japanese_date(value):
    """Return YYYY, YYYY-MM or YYYY-MM-DD; invalid input returns empty string."""
    text = unicodedata.normalize('NFKC', str(value or '')).strip()
    text = re.sub(r'\s+', '', text)
    era = None
    m = re.fullmatch(r'(令和|平成|昭和)(元|\d+)年(?:(\d{1,2})月(?:(\d{1,2})日)?)?', text)
    if m:
        era, yr, month, day = m.groups()
        n = 1 if yr == '元' else int(yr)
        if n < 1:
            return ''
        year = ERA_BASE[era] + n
    else:
        m = re.fullmatch(r'(\d{4})(?:年(?:(\d{1,2})月(?:(\d{1,2})日)?)?|[-/](\d{1,2})(?:[-/](\d{1,2}))?)?', text)
        if not m:
            return ''
        year, jm, jd, month, day = m.groups()
        year = int(year)
        month, day = jm or month, jd or day
    try:
        lo = date(year, int(month or 1), int(day or 1))
        if day:
            hi = lo
        elif month:
            hi = date(year + (int(month) == 12), int(month) % 12 + 1, 1) - timedelta(days=1) if year < 9999 or int(month) < 12 else date.max
        else:
            hi = date(year, 12, 31)
        if era:
            start, end = ERA_BOUNDS[era]
            if hi < start or lo > end:
                return ''
    except (ValueError, OverflowError):
        return ''
    out = f'{year:04d}'
    if month:
        out += f'-{int(month):02d}'
    if day:
        out += f'-{int(day):02d}'
    return out


def _timestamp(value):
    text = str(value or '').strip()
    if not re.search(r'[T ]\d{2}:\d{2}', text):
        return None
    try:
        dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        return None


def iso_z(value):
    dt = _timestamp(value)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if dt else parse_japanese_date(value)


def display_jst(value):
    dt = _timestamp(value)
    return dt.astimezone(JST).strftime('%Y-%m-%d %H:%M JST') if dt else (parse_japanese_date(value) or '待確認')
