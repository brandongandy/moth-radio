"""Validated listening-history windows for recommendation seed selection."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ListeningWindow:
    """A half-open interval of listening history."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError("--since must be before --until")

    def contains(self, played_at: datetime) -> bool:
        return self.start <= played_at < self.end


def resolve_listening_window(
    *,
    as_of: datetime,
    default_months: int,
    since: datetime | None,
    until: datetime | None,
) -> ListeningWindow:
    """Resolve explicit or calendar-month history bounds."""
    if default_months < 1:
        raise ValueError("profile_window_months must be at least 1")

    end = until or as_of
    start = since or _subtract_calendar_months(end, default_months)
    return ListeningWindow(start=start, end=end)


def _subtract_calendar_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_offset = divmod(month_index, 12)
    month = month_offset + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)
