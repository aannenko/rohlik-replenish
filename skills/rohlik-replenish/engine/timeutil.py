"""Time helpers. Dates are local calendar dates in Europe/Prague (spec conventions).

Europe/Prague follows the EU rule: CEST (UTC+2) from the last Sunday of March 01:00 UTC to the
last Sunday of October 01:00 UTC, CET (UTC+1) otherwise. It is implemented here so that Windows,
which has no IANA database, needs no `tzdata` package.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo

_HOUR = timedelta(hours=1)


def _last_sunday(year: int, month: int) -> date:
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 6) % 7)


def _dst_utc_bounds(year: int) -> tuple[datetime, datetime]:
    return (datetime.combine(_last_sunday(year, 3), time(1)), datetime.combine(_last_sunday(year, 10), time(1)))


class _Prague(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return _HOUR
        wall = dt.replace(tzinfo=None, fold=0)
        start = datetime.combine(_last_sunday(wall.year, 3), time(2))
        end = datetime.combine(_last_sunday(wall.year, 10), time(2))
        if start + _HOUR <= wall < end:
            return 2 * _HOUR
        if end <= wall < end + _HOUR:  # ambiguous hour: fold 0 = first (CEST)
            return 2 * _HOUR if dt.fold == 0 else _HOUR
        if start <= wall < start + _HOUR:  # nonexistent hour (PEP 495: fold 0 = offset before)
            return _HOUR if dt.fold == 0 else 2 * _HOUR
        return _HOUR

    def dst(self, dt: datetime | None) -> timedelta:
        return self.utcoffset(dt) - _HOUR

    def tzname(self, dt: datetime | None) -> str:
        return "CEST" if self.dst(dt) else "CET"

    def fromutc(self, dt: datetime) -> datetime:
        naive = dt.replace(tzinfo=None)
        start, end = _dst_utc_bounds(naive.year)
        if start <= naive < end:
            return (naive + 2 * _HOUR).replace(tzinfo=self, fold=0)
        fold = 1 if end <= naive < end + _HOUR else 0
        return (naive + _HOUR).replace(tzinfo=self, fold=fold)

    def __repr__(self) -> str:
        return "Europe/Prague"


PRAGUE = _Prague()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(PRAGUE)


def local_date(dt: datetime) -> date:
    return dt.astimezone(PRAGUE).date()


def local_midnight(d: date) -> datetime:
    """The instant local day `d` starts."""
    return datetime.combine(d, time(0), tzinfo=PRAGUE).astimezone(timezone.utc)


def end_of_day(d: date) -> datetime:
    """The end of local day `d` (= the start of the next day)."""
    return local_midnight(d + timedelta(days=1))


def iso(dt: datetime) -> str:
    """Stored timestamp format: UTC, fixed width, so text order is time order."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(_normalise_offset(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PRAGUE)
    return dt.astimezone(timezone.utc)


_OFFSET = re.compile(r"([+-])(\d{2})(\d{2})$")
_FRACTION = re.compile(r"\.(\d+)")


def _normalise_offset(value: str) -> str:
    value = value.strip().replace("Z", "+00:00")
    value = _OFFSET.sub(r"\1\2:\3", value)
    # Python 3.11 accepts 3- or 6-digit fractions only.
    m = _FRACTION.search(value)
    if m:
        frac = (m.group(1) + "000000")[:6]
        value = value[: m.start()] + "." + frac + value[m.end():]
    return value


def parse_rohlik_time(value: str | None) -> datetime | None:
    """E.g. `2026-07-06T15:09:36.000+0200` (the offset has no colon and follows DST)."""
    if not value:
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


_SLOT = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_slot(time_range: str | None) -> tuple[date, datetime | None] | None:
    """`YYYY-MM-DD HH:MM-HH:MM` -> (delivery date, slot end instant)."""
    if not time_range:
        return None
    m = _SLOT.match(time_range)
    if not m:
        try:
            return date.fromisoformat(time_range.strip()[:10]), None
        except ValueError:
            return None
    d = date.fromisoformat(m.group(1))
    sh, sm, eh, em = (int(m.group(i)) for i in (2, 3, 4, 5))
    end_day = d
    if eh == 24:
        eh, end_day = 0, d + timedelta(days=1)
    elif (eh, em) <= (sh, sm):
        end_day = d + timedelta(days=1)
    end = datetime.combine(end_day, time(eh, em), tzinfo=PRAGUE).astimezone(timezone.utc)
    return d, end


def parse_date_loose(value) -> date | None:
    """A date from `YYYY-MM-DD...` text, or the local date of an ISO timestamp."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if len(text) > 10 and ("T" in text or " " in text[10:11]):
        try:
            return local_date(parse_iso(text.replace(" ", "T", 1)))
        except ValueError:
            pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def fmt_day(d: date) -> str:
    """`8 Oct` (English and locale-independent, as the summary is English)."""
    return f"{d.day} {_MONTHS[d.month - 1]}"


def fmt_local(dt: datetime) -> str:
    loc = to_local(dt)
    return f"{loc.day} {_MONTHS[loc.month - 1]} {loc.hour:02d}:{loc.minute:02d}"


def day_num(d: date) -> float:
    return float(d.toordinal())


def from_day_num(x: float) -> date:
    return date.fromordinal(int(x))
