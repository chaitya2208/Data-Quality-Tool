"""
Next-run computation for schedules — pure Python, server-local time.

All datetimes are naive (no tzinfo) to match Snowflake TIMESTAMP_NTZ and the
server-local-time decision. `compute_next_run` is used both when a schedule is
created/updated (initial NEXT_RUN_AT) and after each fire.
"""
from datetime import datetime, timedelta
from typing import Any

from dateutil.relativedelta import relativedelta


def _parse_hhmm(time_of_day: Any) -> tuple[int, int]:
    """Parse 'HH:MM' → (hour, minute). Defaults to 00:00 on bad/missing input."""
    if not time_of_day:
        return 0, 0
    try:
        h, m = str(time_of_day).split(":")
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except Exception:
        return 0, 0


def _at_time(base: datetime, hour: int, minute: int) -> datetime:
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def compute_next_run(schedule: Any, after: datetime) -> datetime:
    """
    Return the next fire time strictly after `after` for the given schedule.

    `schedule` is any object with cadence / time_of_day / day_of_week /
    day_of_month / month_of_year / interval_value / interval_unit attributes
    (a SimpleNamespace from storage, or a pydantic model).
    """
    cadence = (getattr(schedule, "cadence", None) or "daily").lower()
    hour, minute = _parse_hhmm(getattr(schedule, "time_of_day", None))

    if cadence == "custom":
        value = int(getattr(schedule, "interval_value", None) or 1)
        unit = (getattr(schedule, "interval_unit", None) or "days").lower()
        delta = timedelta(hours=value) if unit == "hours" else timedelta(days=value)
        return after + delta

    if cadence == "daily":
        candidate = _at_time(after, hour, minute)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    if cadence == "weekly":
        # day_of_week: 0=Mon..6=Sun (matches Python datetime.weekday())
        target_dow = int(getattr(schedule, "day_of_week", None) or 0) % 7
        candidate = _at_time(after, hour, minute)
        days_ahead = (target_dow - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate

    if cadence == "monthly":
        target_dom = int(getattr(schedule, "day_of_month", None) or 1)
        return _next_monthly(after, target_dom, hour, minute)

    if cadence == "yearly":
        target_month = int(getattr(schedule, "month_of_year", None) or 1)
        target_dom = int(getattr(schedule, "day_of_month", None) or 1)
        return _next_yearly(after, target_month, target_dom, hour, minute)

    # Unknown cadence — fall back to daily so a schedule never stalls silently.
    candidate = _at_time(after, hour, minute)
    return candidate if candidate > after else candidate + timedelta(days=1)


def _clamp_day(year: int, month: int, day: int) -> int:
    """Clamp a target day-of-month to the last valid day of that month."""
    # First of next month minus one day = last day of `month`.
    first_next = datetime(year, month, 1) + relativedelta(months=1)
    last_day = (first_next - timedelta(days=1)).day
    return min(day, last_day)


def _next_monthly(after: datetime, target_dom: int, hour: int, minute: int) -> datetime:
    dom = _clamp_day(after.year, after.month, target_dom)
    candidate = _at_time(after.replace(day=dom), hour, minute)
    if candidate <= after:
        nxt = after + relativedelta(months=1)
        dom = _clamp_day(nxt.year, nxt.month, target_dom)
        candidate = _at_time(nxt.replace(day=dom), hour, minute)
    return candidate


def _next_yearly(after: datetime, target_month: int, target_dom: int, hour: int, minute: int) -> datetime:
    dom = _clamp_day(after.year, target_month, target_dom)
    candidate = _at_time(after.replace(month=target_month, day=dom), hour, minute)
    if candidate <= after:
        dom = _clamp_day(after.year + 1, target_month, target_dom)
        candidate = _at_time(
            after.replace(year=after.year + 1, month=target_month, day=dom), hour, minute
        )
    return candidate
