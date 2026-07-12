from datetime import datetime

import pytest

from scrobble_recs.models.profile_window import ListeningWindow, resolve_listening_window


def test_resolve_listening_window_defaults_to_twelve_calendar_months_before_as_of() -> None:
    as_of = datetime(2026, 6, 15, 12)

    window = resolve_listening_window(
        as_of=as_of,
        default_months=12,
        since=None,
        until=None,
    )

    assert window == ListeningWindow(start=datetime(2025, 6, 15, 12), end=as_of)


def test_resolve_listening_window_explicit_bounds_override_defaults() -> None:
    window = resolve_listening_window(
        as_of=datetime(2026, 6, 15, 12),
        default_months=12,
        since=datetime(2024, 1, 1),
        until=datetime(2024, 7, 1),
    )

    assert window == ListeningWindow(start=datetime(2024, 1, 1), end=datetime(2024, 7, 1))


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2024, 1, 1), datetime(2024, 1, 1)),
        (datetime(2024, 7, 1), datetime(2024, 1, 1)),
    ],
)
def test_listening_window_rejects_equal_or_reversed_bounds(
    start: datetime,
    end: datetime,
) -> None:
    with pytest.raises(ValueError, match="before --until"):
        ListeningWindow(start=start, end=end)


def test_resolve_listening_window_clamps_calendar_subtraction_to_month_end() -> None:
    window = resolve_listening_window(
        as_of=datetime(2024, 3, 31, 9, 30),
        default_months=1,
        since=None,
        until=None,
    )

    assert window.start == datetime(2024, 2, 29, 9, 30)


def test_listening_window_contains_its_start_but_not_its_end() -> None:
    window = ListeningWindow(start=datetime(2024, 1, 1), end=datetime(2024, 7, 1))

    assert window.contains(datetime(2024, 1, 1)) is True
    assert window.contains(datetime(2024, 6, 30, 23, 59, 59)) is True
    assert window.contains(datetime(2024, 7, 1)) is False


def test_resolve_listening_window_rejects_nonpositive_default_months() -> None:
    with pytest.raises(ValueError, match="profile_window_months must be at least 1"):
        resolve_listening_window(
            as_of=datetime(2026, 6, 15, 12),
            default_months=0,
            since=None,
            until=None,
        )
