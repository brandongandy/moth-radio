import json
import math
from pathlib import Path

import pytest

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
from scrobble_recs.models.modes import resolve_recommendation_mode
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


def test_select_seed_artists_combines_profile_regions(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))
    mode = resolve_recommendation_mode("balanced", config)
    conn = initialize_database(config)
    _insert_profile_artist(
        conn,
        artist_id=10,
        name="Lifetime Anchor",
        play_count=90,
        lifetime_weight=9.0,
        recent_weight=2.0,
        knownness=0.95,
        rediscovery_score=0.0,
        last_played_at="2025-01-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=11,
        name="Recent Anchor",
        play_count=12,
        lifetime_weight=3.0,
        recent_weight=8.0,
        knownness=0.50,
        rediscovery_score=0.0,
        last_played_at="2026-05-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=12,
        name="Old Obsession",
        play_count=80,
        lifetime_weight=8.0,
        recent_weight=1.0,
        knownness=0.35,
        rediscovery_score=0.60,
        last_played_at="2019-01-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=13,
        name="Underexplored Spark",
        play_count=5,
        lifetime_weight=4.0,
        recent_weight=3.5,
        knownness=0.10,
        rediscovery_score=0.30,
        last_played_at="2024-06-01",
    )

    seeds = select_seed_artists(
        conn,
        limit=4,
        min_scrobbles=3,
        regions=mode.seed_regions,
    )

    assert [seed.name for seed in seeds] == [
        "Old Obsession",
        "Lifetime Anchor",
        "Recent Anchor",
        "Underexplored Spark",
    ]
    assert seeds[0].regions == ("lifetime", "old_obsession")
    assert seeds[1].regions == ("lifetime",)
    assert seeds[2].regions == ("recent",)
    assert seeds[3].regions == ("underexplored",)
    assert seeds[0].seed_weight > seeds[1].seed_weight
    conn.close()


def test_select_seed_artists_supports_recent_mode_region_bias(
    tmp_path: Path,
) -> None:
    config = load_config(_config_path(tmp_path))
    mode = resolve_recommendation_mode("recent", config)
    conn = initialize_database(config)
    _insert_profile_artist(
        conn,
        artist_id=20,
        name="Huge Old Artist",
        play_count=200,
        lifetime_weight=10.0,
        recent_weight=1.0,
        knownness=0.90,
        rediscovery_score=0.0,
        last_played_at="2018-01-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=21,
        name="Smaller Recent Artist",
        play_count=20,
        lifetime_weight=4.0,
        recent_weight=8.0,
        knownness=0.60,
        rediscovery_score=0.0,
        last_played_at="2026-05-01",
    )

    seeds = select_seed_artists(
        conn,
        limit=2,
        min_scrobbles=3,
        regions=mode.seed_regions,
    )

    assert [seed.name for seed in seeds] == [
        "Smaller Recent Artist",
        "Huge Old Artist",
    ]
    assert seeds[0].regions == ("recent",)
    conn.close()


def test_select_seed_artists_backfills_duplicate_region_picks(
    tmp_path: Path,
) -> None:
    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    _insert_profile_artist(
        conn,
        artist_id=30,
        name="Overlap Alpha",
        play_count=100,
        lifetime_weight=10.0,
        recent_weight=9.0,
        knownness=0.90,
        rediscovery_score=0.0,
        last_played_at="2026-05-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=31,
        name="Overlap Beta",
        play_count=90,
        lifetime_weight=8.0,
        recent_weight=7.0,
        knownness=0.80,
        rediscovery_score=0.0,
        last_played_at="2026-04-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=32,
        name="Recent Backfill",
        play_count=40,
        lifetime_weight=3.0,
        recent_weight=6.0,
        knownness=0.40,
        rediscovery_score=0.0,
        last_played_at="2026-03-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=33,
        name="Second Backfill",
        play_count=35,
        lifetime_weight=2.0,
        recent_weight=5.0,
        knownness=0.35,
        rediscovery_score=0.0,
        last_played_at="2026-02-01",
    )

    seeds = select_seed_artists(
        conn,
        limit=4,
        min_scrobbles=3,
        regions=(("lifetime", 2, 1.0), ("recent", 2, 1.0)),
    )

    assert [seed.name for seed in seeds] == [
        "Overlap Alpha",
        "Overlap Beta",
        "Recent Backfill",
        "Second Backfill",
    ]
    assert [seed.regions for seed in seeds] == [
        ("lifetime", "recent"),
        ("lifetime", "recent"),
        ("recent",),
        ("recent",),
    ]
    conn.close()


def test_select_seed_artists_accumulates_shared_backfill_region(
    tmp_path: Path,
) -> None:
    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    _insert_profile_artist(
        conn,
        artist_id=40,
        name="Overlap Alpha",
        play_count=100,
        lifetime_weight=10.0,
        recent_weight=10.0,
        knownness=0.90,
        rediscovery_score=1.0,
        last_played_at="2026-05-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=41,
        name="Overlap Beta",
        play_count=90,
        lifetime_weight=9.0,
        recent_weight=9.0,
        knownness=0.80,
        rediscovery_score=1.0,
        last_played_at="2026-04-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=42,
        name="Shared Backfill",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.40,
        rediscovery_score=1.0,
        last_played_at="2026-03-01",
    )

    seeds = select_seed_artists(
        conn,
        limit=6,
        min_scrobbles=3,
        regions=(
            ("lifetime", 2, 1.0),
            ("recent", 2, 0.5),
            ("old_obsession", 2, 0.25),
        ),
    )

    shared_backfill = next(seed for seed in seeds if seed.name == "Shared Backfill")
    assert shared_backfill.seed_weight == pytest.approx(6.0)
    assert shared_backfill.regions == ("old_obsession", "recent")
    conn.close()


def test_select_seed_artists_limits_backfill_to_missing_region_slots(
    tmp_path: Path,
) -> None:
    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    _insert_profile_artist(
        conn,
        artist_id=50,
        name="Overlap Alpha",
        play_count=100,
        lifetime_weight=10.0,
        recent_weight=10.0,
        knownness=0.90,
        rediscovery_score=0.0,
        last_played_at="2026-05-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=51,
        name="Lifetime Only",
        play_count=90,
        lifetime_weight=9.0,
        recent_weight=7.0,
        knownness=0.80,
        rediscovery_score=0.0,
        last_played_at="2026-04-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=52,
        name="Recent Anchor",
        play_count=40,
        lifetime_weight=1.0,
        recent_weight=9.0,
        knownness=0.40,
        rediscovery_score=0.0,
        last_played_at="2026-03-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=53,
        name="Recent Backfill",
        play_count=35,
        lifetime_weight=1.0,
        recent_weight=8.0,
        knownness=0.35,
        rediscovery_score=0.0,
        last_played_at="2026-02-01",
    )

    seeds = select_seed_artists(
        conn,
        limit=4,
        min_scrobbles=3,
        regions=(("lifetime", 2, 1.0), ("recent", 2, 1.0)),
    )

    lifetime_only = next(seed for seed in seeds if seed.name == "Lifetime Only")
    assert lifetime_only.seed_weight == pytest.approx(9.0)
    assert lifetime_only.regions == ("lifetime",)
    conn.close()


def test_select_seed_artists_prioritizes_unique_backfill_before_shared_region(
    tmp_path: Path,
) -> None:
    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    _insert_profile_artist(
        conn,
        artist_id=60,
        name="Overlap Alpha",
        play_count=100,
        lifetime_weight=10.0,
        recent_weight=10.0,
        knownness=0.90,
        rediscovery_score=0.0,
        last_played_at="2026-05-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=61,
        name="Shared Existing",
        play_count=90,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.80,
        rediscovery_score=0.0,
        last_played_at="2026-04-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=62,
        name="Recent Anchor",
        play_count=40,
        lifetime_weight=1.0,
        recent_weight=9.0,
        knownness=0.40,
        rediscovery_score=0.0,
        last_played_at="2026-03-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=63,
        name="Recent Backfill",
        play_count=35,
        lifetime_weight=1.0,
        recent_weight=7.0,
        knownness=0.35,
        rediscovery_score=0.0,
        last_played_at="2026-02-01",
    )

    seeds = select_seed_artists(
        conn,
        limit=4,
        min_scrobbles=3,
        regions=(("lifetime", 2, 1.0), ("recent", 2, 1.0)),
    )

    assert [seed.name for seed in seeds] == [
        "Overlap Alpha",
        "Recent Anchor",
        "Shared Existing",
        "Recent Backfill",
    ]
    shared_existing = next(seed for seed in seeds if seed.name == "Shared Existing")
    assert shared_existing.seed_weight == pytest.approx(8.0)
    assert shared_existing.regions == ("lifetime",)
    conn.close()


def test_select_seed_artists_empty_regions_selects_no_seeds(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    _insert_profile_artist(
        conn,
        artist_id=50,
        name="Recent Artist",
        play_count=10,
        lifetime_weight=2.0,
        recent_weight=10.0,
        knownness=0.50,
        rediscovery_score=0.0,
        last_played_at="2026-05-01",
    )

    seeds = select_seed_artists(conn, limit=3, min_scrobbles=3, regions=())

    assert seeds == []
    conn.close()


def _insert_profile_artist(
    conn,
    *,
    artist_id: int,
    name: str,
    play_count: int,
    lifetime_weight: float,
    recent_weight: float,
    knownness: float,
    rediscovery_score: float,
    last_played_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO artist (
            artist_id,
            canonical_name,
            artist_key,
            lastfm_name
        )
        VALUES (?, ?, ?, ?)
        """,
        [artist_id, name, name.casefold().replace(" ", "-"), name],
    )
    conn.execute(
        """
        INSERT INTO artist_listening_fact (
            artist_id,
            play_count,
            track_count,
            active_months,
            first_played_at,
            last_played_at,
            lifetime_weight,
            recent_weight,
            knownness,
            rediscovery_score
        )
        VALUES (?, ?, 5, 5, '2018-01-01', ?, ?, ?, ?, ?)
        """,
        [
            artist_id,
            play_count,
            last_played_at,
            lifetime_weight,
            recent_weight,
            knownness,
            rediscovery_score,
        ],
    )
