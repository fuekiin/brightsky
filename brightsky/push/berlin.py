"""Europe/Berlin date arithmetic, exactly as the app does it.

The app's `CalendarDate.startOfDay.addingTimeInterval(h * 3600)` adds absolute
seconds to local midnight. On DST-change days that is not the wall-clock hour
(22:00 "night start" lands at 21:00 or 23:00 local). The server must agree with
the app, so everything here works in UTC after resolving local midnight —
never `aware_local_datetime + timedelta`, which Python evaluates in wall time.
"""

import datetime
from zoneinfo import ZoneInfo


TZ = ZoneInfo('Europe/Berlin')
UTC = datetime.timezone.utc


def local_date(dt):
    return dt.astimezone(TZ).date()


def start_of_day(date):
    """Local midnight of `date` as an aware UTC datetime."""
    return datetime.datetime.combine(
        date, datetime.time(), tzinfo=TZ).astimezone(UTC)


def at_hour(date, hours):
    """`startOfDay + hours * 3600` — may run past the date (night: 30)."""
    return start_of_day(date) + datetime.timedelta(hours=hours)


def weekday(date):
    """1 = Monday … 7 = Sunday, as the app's `Weekday`."""
    return date.isoweekday()


def local_hour(dt):
    return dt.astimezone(TZ).hour
