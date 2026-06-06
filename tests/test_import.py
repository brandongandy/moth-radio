import json
from pathlib import Path

import duckdb
import pytest
from duckdb import DuckDBPyConnection

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.importers.lastfm_csv import EXPECTED_COLUMNS, import_lastfm_csv


def write_csv(path: Path, rows: list[str]) -> Path:
    path.write_text(
        "\n".join([",".join(EXPECTED_COLUMNS), *rows]) + "\n",
        encoding="utf-8",
    )
    return path


def write_config(tmp_path: Path, csv_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "scrobbles.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": str(csv_path),
                    "timezone": "UTC",
                    "timestamp_column": "uts",
                    "csv_columns": list(EXPECTED_COLUMNS),
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_import_lastfm_csv_persists_raw_clean_artists_and_facts(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/tiny_lastfm_export.csv")
    config = load_config(write_config(tmp_path, fixture))
    conn = initialize_database(config)

    stats = import_lastfm_csv(conn, fixture)

    assert stats.raw_scrobbles == 6
    assert stats.scrobbles == 6
    assert stats.duplicate_scrobbles == 1
    assert stats.distinct_artists == 4
    assert stats.distinct_tracks == 5
    assert stats.null_artist_mbid_scrobbles == 2

    fact_rows = conn.execute(
        """
        SELECT a.canonical_name, f.play_count, f.track_count, f.active_days
        FROM artist_listening_fact f
        JOIN artist a ON a.artist_id = f.artist_id
        ORDER BY a.canonical_name
        """
    ).fetchall()

    assert fact_rows == [
        ("Cocteau Twins", 1, 1, 1),
        ("Pixies", 1, 1, 1),
        ("Slowdive", 1, 1, 1),
        ("The Cure", 2, 2, 2),
    ]

    conn.close()


def test_import_rejects_wrong_columns(tmp_path: Path) -> None:
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("uts,artist,track\n1704067200,The Cure,Plainsong\n", encoding="utf-8")
    conn = duckdb.connect(":memory:")

    with pytest.raises(ValueError, match="CSV columns must exactly match"):
        import_lastfm_csv(conn, bad_csv)

    conn.close()


def test_import_rejects_rows_with_too_few_columns(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "too_few_columns.csv",
        ["1704067200,2024-01-01 00:00:00,The Cure,mbid-cure,Disintegration"],
    )
    conn = duckdb.connect(":memory:")

    with pytest.raises(ValueError, match="CSV row 2 has the wrong number of columns"):
        import_lastfm_csv(conn, csv_path)

    conn.close()


def test_import_rejects_malformed_csv_syntax(tmp_path: Path) -> None:
    csv_path = tmp_path / "malformed.csv"
    csv_path.write_text(
        ",".join(EXPECTED_COLUMNS)
        + "\n"
        + '1704067200,2024-01-01 00:00:00,The Cure,mbid-cure,Disintegration,,Pictures of You,"\n',
        encoding="utf-8",
    )
    conn = duckdb.connect(":memory:")

    with pytest.raises(ValueError, match="Malformed CSV at line 2"):
        import_lastfm_csv(conn, csv_path)

    conn.close()


def test_import_replaces_previous_import_tables_on_rerun(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/tiny_lastfm_export.csv")
    config = load_config(write_config(tmp_path, fixture))
    conn = initialize_database(config)

    import_lastfm_csv(conn, fixture)
    stats = import_lastfm_csv(conn, fixture)

    assert stats.raw_scrobbles == 6
    assert stats.scrobbles == 6
    assert stats.duplicate_scrobbles == 1
    assert stats.distinct_artists == 4
    assert stats.distinct_tracks == 5
    assert stats.null_artist_mbid_scrobbles == 2
    assert conn.execute("SELECT count(*) FROM artist").fetchone() == (4,)

    conn.close()


def test_import_warns_on_utc_time_mismatch_without_dropping_row(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "timestamp_mismatch.csv",
        ["1704067200,2024-01-02 00:00:00,The Cure,mbid-cure,Disintegration,,Pictures of You,"],
    )
    config = load_config(write_config(tmp_path, csv_path))
    conn = initialize_database(config)

    stats = import_lastfm_csv(conn, csv_path)

    assert stats.scrobbles == 1
    assert stats.import_warnings == 1
    warning_rows = conn.execute(
        """
        SELECT import_row_number, warning_code, severity, message, CAST(evidence AS VARCHAR)
        FROM import_warning
        ORDER BY warning_id
        """
    ).fetchall()
    assert warning_rows == [
        (
            1,
            "timestamp_mismatch",
            "warning",
            "utc_time does not match uts",
            '{"uts":1704067200,"utc_time":"2024-01-02 00:00:00","played_at":"2024-01-01 00:00:00"}',
        )
    ]

    conn.close()


def test_import_does_not_mark_near_duplicates_when_window_is_disabled(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "near_duplicates_disabled.csv",
        [
            "1704067200,2024-01-01 00:00:00,The Cure,,Disintegration,,Pictures of You,",
            "1704067225,2024-01-01 00:00:25,The Cure,,Disintegration,,Pictures of You,",
        ],
    )
    config = load_config(write_config(tmp_path, csv_path))
    conn = initialize_database(config)

    stats = import_lastfm_csv(conn, csv_path)

    assert stats.scrobbles == 2
    assert stats.duplicate_scrobbles == 0
    assert stats.near_duplicate_scrobbles == 0
    assert conn.execute("SELECT count(*) FROM import_warning").fetchone() == (0,)

    conn.close()


def test_import_marks_configured_near_duplicates_without_deleting_rows(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "near_duplicates_enabled.csv",
        [
            "1704067200,2024-01-01 00:00:00,The Cure,,Disintegration,,Pictures of You,",
            "1704067225,2024-01-01 00:00:25,The Cure,,Disintegration,,Pictures of You,",
            "1704067265,2024-01-01 00:01:05,The Cure,,Disintegration,,Pictures of You,",
        ],
    )
    config = load_config(write_config(tmp_path, csv_path))
    conn = initialize_database(config)

    stats = import_lastfm_csv(conn, csv_path, near_duplicate_window_seconds=30)

    assert stats.scrobbles == 3
    assert conn.execute("SELECT count(*) FROM scrobble").fetchone() == (3,)
    assert conn.execute(
        """
        SELECT CAST(played_at AS VARCHAR), is_duplicate
        FROM scrobble
        ORDER BY scrobble_id
        """
    ).fetchall() == [
        ("2024-01-01 00:00:00", False),
        ("2024-01-01 00:00:25", True),
        ("2024-01-01 00:01:05", False),
    ]
    assert stats.duplicate_scrobbles == 1
    assert stats.near_duplicate_scrobbles == 1
    warning_rows = conn.execute(
        """
        SELECT import_row_number, warning_code, message, CAST(evidence AS VARCHAR)
        FROM import_warning
        ORDER BY warning_id
        """
    ).fetchall()
    assert warning_rows == [
        (
            2,
            "near_duplicate_scrobble",
            "same artist and track within configured near-duplicate window",
            '{"uts":1704067225,"artist_key":"the cure",'
            '"track_key":"pictures of you","window_seconds":30}',
        )
    ]

    conn.close()


def test_import_does_not_mark_near_duplicates_with_empty_normalized_keys(
    tmp_path: Path,
) -> None:
    csv_path = write_csv(
        tmp_path / "near_duplicates_empty_keys.csv",
        [
            "1704067200,2024-01-01 00:00:00,!!!,,Disintegration,,???,",
            "1704067225,2024-01-01 00:00:25,***,,Disintegration,,###,",
        ],
    )
    config = load_config(write_config(tmp_path, csv_path))
    conn = initialize_database(config)

    stats = import_lastfm_csv(conn, csv_path, near_duplicate_window_seconds=30)

    assert stats.scrobbles == 2
    assert stats.duplicate_scrobbles == 0
    assert stats.near_duplicate_scrobbles == 0
    assert conn.execute("SELECT count(*) FROM import_warning").fetchone() == (0,)

    conn.close()


def test_import_marks_near_duplicates_in_csv_order_not_timestamp_order(
    tmp_path: Path,
) -> None:
    csv_path = write_csv(
        tmp_path / "near_duplicates_unsorted.csv",
        [
            "1704067225,2024-01-01 00:00:25,The Cure,,Disintegration,,Pictures of You,",
            "1704067200,2024-01-01 00:00:00,The Cure,,Disintegration,,Pictures of You,",
        ],
    )
    config = load_config(write_config(tmp_path, csv_path))
    conn = initialize_database(config)

    stats = import_lastfm_csv(conn, csv_path, near_duplicate_window_seconds=30)

    assert stats.scrobbles == 2
    assert stats.duplicate_scrobbles == 1
    assert stats.near_duplicate_scrobbles == 1
    assert conn.execute(
        """
        SELECT CAST(played_at AS VARCHAR), is_duplicate
        FROM scrobble
        ORDER BY scrobble_id
        """
    ).fetchall() == [
        ("2024-01-01 00:00:25", False),
        ("2024-01-01 00:00:00", True),
    ]
    warning_rows = conn.execute(
        """
        SELECT import_row_number, warning_code, message
        FROM import_warning
        ORDER BY warning_id
        """
    ).fetchall()
    assert warning_rows == [
        (
            2,
            "near_duplicate_scrobble",
            "same artist and track within configured near-duplicate window",
        )
    ]

    conn.close()


def test_import_database_statement_count_is_bounded_for_many_rows(tmp_path: Path) -> None:
    rows = [
        ",".join(
            [
                str(1704067200 + index),
                "2024-01-01 00:00:00",
                f"Artist {index % 10}",
                "",
                f"Album {index % 5}",
                "",
                f"Track {index}",
                "",
            ]
        )
        for index in range(100)
    ]
    csv_path = write_csv(tmp_path / "many.csv", rows)
    config = load_config(write_config(tmp_path, csv_path))
    conn = initialize_database(config)
    counting_conn = CountingConnection(conn)

    try:
        stats = import_lastfm_csv(counting_conn, csv_path)  # type: ignore[arg-type]
    finally:
        conn.close()

    assert stats.scrobbles == 100
    assert stats.distinct_artists == 10
    assert counting_conn.execute_calls <= 30


@pytest.mark.parametrize("column", ["uts", "artist", "track"])
def test_import_rejects_missing_required_fields(tmp_path: Path, column: str) -> None:
    values = {
        "uts": "1704067200",
        "utc_time": "2024-01-01 00:00:00",
        "artist": "The Cure",
        "artist_mbid": "mbid-cure",
        "album": "Disintegration",
        "album_mbid": "",
        "track": "Pictures of You",
        "track_mbid": "",
    }
    values[column] = ""
    csv_path = write_csv(tmp_path / f"missing_{column}.csv", [",".join(values.values())])
    conn = duckdb.connect(":memory:")

    with pytest.raises(ValueError, match=f"missing required field '{column}'"):
        import_lastfm_csv(conn, csv_path)

    conn.close()


class CountingConnection:
    def __init__(self, conn: DuckDBPyConnection) -> None:
        self._conn = conn
        self.execute_calls = 0

    def execute(self, *args: object, **kwargs: object) -> DuckDBPyConnection:
        self.execute_calls += 1
        return self._conn.execute(*args, **kwargs)

    def executemany(self, *args: object, **kwargs: object) -> DuckDBPyConnection:
        self.execute_calls += 1
        return self._conn.executemany(*args, **kwargs)


@pytest.mark.parametrize("uts", ["not-an-int", "999999999999999999999999999999999999999999"])
def test_import_rejects_invalid_uts_with_value_error(tmp_path: Path, uts: str) -> None:
    csv_path = write_csv(
        tmp_path / "invalid_uts.csv",
        [f"{uts},2024-01-01 00:00:00,The Cure,mbid-cure,Disintegration,,Pictures of You,"],
    )
    conn = duckdb.connect(":memory:")

    with pytest.raises(ValueError, match="CSV row 2 has invalid uts"):
        import_lastfm_csv(conn, csv_path)

    conn.close()


def test_import_rejects_duplicate_header_rows_inside_file(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "duplicate_header.csv",
        [
            ",".join(EXPECTED_COLUMNS),
            "1704067200,2024-01-01 00:00:00,The Cure,mbid-cure,Disintegration,,Pictures of You,",
        ],
    )
    conn = duckdb.connect(":memory:")

    with pytest.raises(ValueError, match="duplicate header row at line 2"):
        import_lastfm_csv(conn, csv_path)

    conn.close()


def test_import_preserves_untrimmed_raw_csv_fields(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "untrimmed.csv",
        [
            "1704067200,2024-01-01 00:00:00, The Cure , mbid-cure ,"
            " Disintegration ,, Pictures of You ,"
        ],
    )
    config = load_config(write_config(tmp_path, csv_path))
    conn = initialize_database(config)

    import_lastfm_csv(conn, csv_path)

    raw_row = conn.execute(
        """
        SELECT artist, artist_mbid, album, track
        FROM raw_scrobble
        """
    ).fetchone()
    scrobble_row = conn.execute(
        """
        SELECT artist_raw, artist_mbid_raw, album_raw, track_raw, artist_key, track_key
        FROM scrobble
        """
    ).fetchone()

    assert raw_row == (" The Cure ", " mbid-cure ", " Disintegration ", " Pictures of You ")
    assert scrobble_row == (
        "The Cure",
        "mbid-cure",
        "Disintegration",
        "Pictures of You",
        "the cure",
        "pictures of you",
    )

    conn.close()
