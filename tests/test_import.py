import json
from pathlib import Path

import duckdb
import pytest

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
