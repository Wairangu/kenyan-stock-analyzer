"""Shared numeric and NSE session checks; no network or filesystem effects."""

import math
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo


def finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


@lru_cache(maxsize=16)
def _holidays(year):
    import holidays
    return holidays.Kenya(years=year)


def is_session(day):
    return day.weekday() < 5 and day not in _holidays(day.year)


def latest_completed_session(now=None):
    now = now or datetime.now(ZoneInfo("Africa/Nairobi"))
    if now.tzinfo is not None:
        now = now.astimezone(ZoneInfo("Africa/Nairobi"))
    day = now.date()
    # Allow the closing feed time to settle.
    if (now.hour, now.minute) < (15, 30):
        day -= timedelta(days=1)
    while not is_session(day):
        day -= timedelta(days=1)
    return day


def parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def recommendation_expiry(session):
    day = parse_date(session) + timedelta(days=1)
    while not is_session(day):
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, 15, 30,
                    tzinfo=ZoneInfo('Africa/Nairobi')).isoformat()


def extract_report_date(text):
    """Read an explicit date from a feed header; never substitute fetch time."""
    patterns = (
        (r"\b\d{4}-\d{2}-\d{2}\b", ("%Y-%m-%d",)),
        (r"\b\d{1,2}[- /][A-Za-z]{3,9}[- /]\d{2,4}\b", ("%d %b %Y", "%d %B %Y", "%d %b %y")),
        (r"\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}\b", ("%b %d %Y", "%B %d %Y")),
    )
    for pattern, formats in patterns:
        for match in re.findall(pattern, text):
            for fmt in formats:
                value = match if fmt == "%Y-%m-%d" else re.sub(r"[-/,]", " ", match)
                value = " ".join(value.split())
                try:
                    return datetime.strptime(value, fmt).date()
                except ValueError:
                    continue
    return None
