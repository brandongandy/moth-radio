from datetime import datetime

import pytest

from scrobble_recs.normalize.strings import normalize_key
from scrobble_recs.normalize.timestamps import parse_uts


def test_normalize_key_preserves_meaningful_words() -> None:
    assert normalize_key("  The   Cure  ") == "the cure"


def test_normalize_key_collapses_punctuation_and_zero_width_characters() -> None:
    assert normalize_key("Boys\u200b Don't--Cry!") == "boys don t cry"


def test_normalize_key_treats_underscores_as_separators() -> None:
    assert normalize_key("AC_DC") == "ac dc"


def test_parse_uts_returns_naive_utc_datetime() -> None:
    assert parse_uts("1704067200") == datetime(2024, 1, 1, 0, 0, 0)


def test_parse_uts_rejects_non_integer_values() -> None:
    with pytest.raises(ValueError, match="uts must be an integer"):
        parse_uts("not-a-timestamp")


def test_parse_uts_rejects_boolean_values() -> None:
    with pytest.raises(ValueError, match="uts must be an integer"):
        parse_uts(True)


def test_parse_uts_rejects_non_integral_numeric_values() -> None:
    with pytest.raises(ValueError, match="uts must be an integer"):
        parse_uts(1.5)


def test_parse_uts_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="uts is before 1970-01-01"):
        parse_uts("-1")


def test_parse_uts_rejects_timestamps_more_than_one_day_in_the_future(monkeypatch) -> None:
    from scrobble_recs.normalize import timestamps

    class FakeDatetime:
        @staticmethod
        def fromtimestamp(value: int, tz) -> datetime:
            assert value == 1704412800
            return datetime(2024, 1, 5, 0, 0, 0, tzinfo=tz)

        @staticmethod
        def now(tz) -> datetime:
            return datetime(2024, 1, 1, 0, 0, 0, tzinfo=tz)

    monkeypatch.setattr(timestamps, "datetime", FakeDatetime)

    with pytest.raises(ValueError, match="uts is more than one day in the future"):
        parse_uts("1704412800")
