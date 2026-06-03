import json
import math
from pathlib import Path

import pytest

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
from scrobble_recs.models.profile import select_seed_artists


def _config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": "tests/fixtures/tiny_lastfm_export.csv",
                    "timezone": "UTC",
                    "timestamp_column": "uts",
                    "csv_columns": [
                        "uts",
                        "utc_time",
                        "artist",
                        "artist_mbid",
                        "album",
                        "album_mbid",
                        "track",
                        "track_mbid",
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_select_seed_artists_orders_by_weight_then_name(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))

    seeds = select_seed_artists(conn, limit=3, min_scrobbles=1)

    assert [(seed.artist_id, seed.name, seed.play_count) for seed in seeds] == [
        (4, "The Cure", 2),
        (1, "Cocteau Twins", 1),
        (2, "Pixies", 1),
    ]
    conn.close()


def test_select_seed_artists_respects_min_scrobbles(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))

    seeds = select_seed_artists(conn, limit=10, min_scrobbles=2)

    assert [seed.name for seed in seeds] == ["The Cure"]
    conn.close()


def test_select_seed_artists_uses_lastfm_name_and_weight_fallbacks(
    tmp_path: Path,
) -> None:
    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    conn.execute(
        """
        INSERT INTO artist (
            artist_id,
            canonical_name,
            artist_key,
            lastfm_name
        )
        VALUES
            (1, 'Recent Source', 'recent-source', NULL),
            (2, 'Lifetime Source', 'lifetime-source', ''),
            (3, 'Play Count Source', 'play-count-source', 'Last.fm Alias')
        """
    )
    conn.execute(
        """
        INSERT INTO artist_listening_fact (
            artist_id,
            play_count,
            lifetime_weight,
            recent_weight
        )
        VALUES
            (1, 9, 8.0, 1.0),
            (2, 9, 2.0, NULL),
            (3, 20, NULL, NULL)
        """
    )

    seeds = select_seed_artists(conn, limit=10, min_scrobbles=1)

    assert [(seed.name, seed.lastfm_name) for seed in seeds] == [
        ("Play Count Source", "Last.fm Alias"),
        ("Lifetime Source", "Lifetime Source"),
        ("Recent Source", "Recent Source"),
    ]
    assert [seed.seed_weight for seed in seeds] == pytest.approx(
        [
            math.log(21),
            2.0,
            1.0,
        ]
    )
    conn.close()
