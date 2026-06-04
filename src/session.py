from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

# CME equity-index futures regular trading hours (the US cash session), in
# Eastern wall-clock minutes from midnight: 09:30 to 16:00 ET.
RTH_OPEN_MINUTES = 9 * 60 + 30
RTH_CLOSE_MINUTES = 16 * 60

# Scheduled times MNQ reacts to, in ET minutes from midnight:
# 08:30 (CPI/NFP/etc.), 10:00 (ISM/sentiment), 14:00 (FOMC).
KEY_EVENT_MINUTES = (8 * 60 + 30, 10 * 60, 14 * 60)

SUNDAY = 6  # datetime.weekday(): Monday=0 ... Sunday=6


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def eastern_offset_hours(dt_utc: datetime) -> int:
    """US Eastern UTC offset (-4 EDT or -5 EST) for an instant, no tz database.

    DST rule (2007+): from 07:00 UTC on the 2nd Sunday of March to 06:00 UTC on
    the 1st Sunday of November the offset is -4 (EDT); otherwise -5 (EST).
    """
    dt_utc = _as_utc(dt_utc)
    year = dt_utc.year
    spring = nth_weekday(year, 3, SUNDAY, 2)
    fall = nth_weekday(year, 11, SUNDAY, 1)
    dst_start = datetime(spring.year, spring.month, spring.day, 7, tzinfo=UTC)
    dst_end = datetime(fall.year, fall.month, fall.day, 6, tzinfo=UTC)
    return -4 if dst_start <= dt_utc < dst_end else -5


def to_eastern(dt_utc: datetime) -> datetime:
    """Return the Eastern wall-clock time (naive) for a UTC instant."""
    dt_utc = _as_utc(dt_utc)
    return (dt_utc + timedelta(hours=eastern_offset_hours(dt_utc))).replace(tzinfo=None)


def eastern_minutes(dt_utc: datetime) -> int:
    eastern = to_eastern(dt_utc)
    return eastern.hour * 60 + eastern.minute


def is_weekday(dt_utc: datetime) -> bool:
    return to_eastern(dt_utc).weekday() < 5


def is_rth(dt_utc: datetime) -> bool:
    if not is_weekday(dt_utc):
        return False
    minutes = eastern_minutes(dt_utc)
    return RTH_OPEN_MINUTES <= minutes < RTH_CLOSE_MINUTES


def minutes_since_open(dt_utc: datetime) -> int:
    """Minutes since the 09:30 ET cash open (negative before the open)."""
    return eastern_minutes(dt_utc) - RTH_OPEN_MINUTES


def session_bucket(dt_utc: datetime) -> str:
    """Coarse intraday regime label."""
    if not is_weekday(dt_utc):
        return "weekend"
    minutes = eastern_minutes(dt_utc)
    if minutes < RTH_OPEN_MINUTES or minutes >= RTH_CLOSE_MINUTES:
        return "overnight"
    if minutes < RTH_OPEN_MINUTES + 60:
        return "open_hour"
    if minutes >= RTH_CLOSE_MINUTES - 60:
        return "close_hour"
    if 12 * 60 <= minutes < 13 * 60:
        return "lunch"
    return "midday"


def session_day(dt_utc: datetime) -> date:
    """Trading day a bar belongs to, anchored at the 09:30 ET cash open.

    Bars before 09:30 ET roll into the prior calendar day's session, so VWAP and
    prior-day reference levels are anchored to the cash open the way intraday
    traders watch them.
    """
    eastern = to_eastern(dt_utc)
    day = eastern.date()
    if eastern.hour * 60 + eastern.minute < RTH_OPEN_MINUTES:
        day = day - timedelta(days=1)
    return day


def minutes_to_nearest_event(dt_utc: datetime) -> int:
    minutes = eastern_minutes(dt_utc)
    return min(abs(minutes - event) for event in KEY_EVENT_MINUTES)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
