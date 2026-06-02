from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_INTEGER_STRING = re.compile(r"[+-]?\d+")


def parse_uts(value: str | int) -> datetime:
    if isinstance(value, bool):
        raise ValueError("uts must be an integer")

    if isinstance(value, int):
        timestamp = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not _INTEGER_STRING.fullmatch(candidate):
            raise ValueError("uts must be an integer")
        timestamp = int(candidate)
    else:
        raise ValueError("uts must be an integer")

    if timestamp < 0:
        raise ValueError("uts is before 1970-01-01")

    played_at = datetime.fromtimestamp(timestamp, tz=UTC)
    if played_at > datetime.now(tz=UTC) + timedelta(days=1):
        raise ValueError("uts is more than one day in the future")

    return played_at.replace(tzinfo=None)
