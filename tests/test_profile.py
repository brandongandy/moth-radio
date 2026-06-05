import json
import math
from datetime import datetime
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


def test_refresh_artist_listening_scores_updates_weights_for_build_profile(
    tmp_path: Path,
) -> None:
    from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
    from scrobble_recs.models.listening import refresh_artist_listening_scores

    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    try:
        import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
        stats = refresh_artist_listening_scores(conn, as_of=datetime(2026, 6, 5, 12, 0, 0))

        assert stats.artists_updated == 4
        missing_scores = conn.execute(
            """
            SELECT count(*)
            FROM artist_listening_fact
            WHERE lifetime_weight IS NULL
               OR recent_weight IS NULL
               OR knownness IS NULL
               OR rediscovery_score IS NULL
            """
        ).fetchone()
        assert missing_scores == (0,)
    finally:
        conn.close()


def test_build_profile_persists_tag_profile_and_genre_clusters(tmp_path: Path) -> None:
    from scrobble_recs.models.profile_builder import build_profile

    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    try:
        _insert_profile_artist(
            conn,
            artist_id=10,
            name="Post Punk Anchor",
            play_count=100,
            lifetime_weight=0.0,
            recent_weight=0.0,
            knownness=0.0,
            rediscovery_score=0.0,
            last_played_at="2026-05-01",
        )
        _insert_profile_artist(
            conn,
            artist_id=11,
            name="Shoegaze Anchor",
            play_count=40,
            lifetime_weight=0.0,
            recent_weight=0.0,
            knownness=0.0,
            rediscovery_score=0.0,
            last_played_at="2026-04-01",
        )
        _insert_profile_artist(
            conn,
            artist_id=12,
            name="Below Threshold Artist",
            play_count=2,
            lifetime_weight=0.0,
            recent_weight=0.0,
            knownness=0.0,
            rediscovery_score=0.0,
            last_played_at="2026-03-01",
        )
        _insert_artist_tag(conn, artist_id=10, tag="Post-Punk", weight=0.90)
        _insert_artist_tag(conn, artist_id=10, tag="Gothic Rock", weight=0.60)
        _insert_artist_tag(conn, artist_id=11, tag="Shoegaze", weight=0.80)
        _insert_artist_tag(conn, artist_id=11, tag="Dream Pop", weight=0.50)
        _insert_artist_tag(conn, artist_id=12, tag="Ignored", weight=1.00)

        stats = build_profile(conn, config, as_of=datetime(2026, 6, 5, 12, 0, 0))

        assert stats.artists_updated == 3
        assert stats.tag_weights == 4
        assert stats.genre_clusters == 4
        score_rows = conn.execute(
            """
            SELECT lifetime_weight, recent_weight, knownness, rediscovery_score
            FROM artist_listening_fact
            ORDER BY artist_id
            """
        ).fetchall()
        assert len(score_rows) == 3
        assert all(value is not None for row in score_rows for value in row)
        tag_rows = conn.execute(
            """
            SELECT tag, source, user_weight, artist_count, top_seed_artists
            FROM tag_profile
            ORDER BY user_weight DESC, tag
            """
        ).fetchall()
        assert [row[0] for row in tag_rows] == [
            "post-punk",
            "shoegaze",
            "gothic rock",
            "dream pop",
        ]
        assert [row[1] for row in tag_rows] == ["lastfm"] * 4
        assert tag_rows[0][2] == pytest.approx(1.0)
        assert all(0.0 < float(row[2]) <= 1.0 for row in tag_rows)
        assert all(row[3] == 1 for row in tag_rows)
        assert json.loads(tag_rows[0][4])[0]["artist"] == "Post Punk Anchor"
        assert "Ignored" not in {row[0] for row in tag_rows}

        cluster_rows = conn.execute(
            """
            SELECT cluster_key, cluster_name, tag_weights, seed_artists, artist_count
            FROM profile_cluster
            ORDER BY cluster_key
            """
        ).fetchall()
        assert [row[1] for row in cluster_rows] == [
            "dream pop",
            "gothic rock",
            "post-punk",
            "shoegaze",
        ]
        assert all(
            str(row[0]).startswith(f"lastfm:{row[1].replace(' ', '-')}:") for row in cluster_rows
        )
        post_punk = next(row for row in cluster_rows if row[1] == "post-punk")
        assert post_punk[1] == "post-punk"
        assert json.loads(post_punk[2]) == {"post-punk": 1.0}
        assert json.loads(post_punk[3])[0]["artist"] == "Post Punk Anchor"
        assert post_punk[4] == 1
    finally:
        conn.close()


def test_build_profile_persists_clusters_for_slug_colliding_tags(tmp_path: Path) -> None:
    from scrobble_recs.models.profile_builder import build_profile

    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    try:
        _insert_profile_artist(
            conn,
            artist_id=20,
            name="Collision Anchor",
            play_count=100,
            lifetime_weight=0.0,
            recent_weight=0.0,
            knownness=0.0,
            rediscovery_score=0.0,
            last_played_at="2026-05-01",
        )
        _insert_artist_tag(conn, artist_id=20, tag="post punk", weight=1.00)
        _insert_artist_tag(conn, artist_id=20, tag="post-punk", weight=0.90)
        _insert_artist_tag(conn, artist_id=20, tag="post_punk", weight=0.80)

        stats = build_profile(conn, config, as_of=datetime(2026, 6, 5, 12, 0, 0))

        assert stats.tag_weights == 3
        assert stats.genre_clusters == 3
        assert conn.execute("SELECT count(*) FROM tag_profile").fetchone() == (3,)
        cluster_rows = conn.execute(
            """
            SELECT cluster_key, cluster_name
            FROM profile_cluster
            ORDER BY cluster_name
            """
        ).fetchall()
        assert [row[1] for row in cluster_rows] == ["post punk", "post-punk", "post_punk"]
        assert len({row[0] for row in cluster_rows}) == 3
        assert all(str(row[0]).startswith("lastfm:post-punk") for row in cluster_rows)
    finally:
        conn.close()


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


def _insert_artist_tag(conn, *, artist_id: int, tag: str, weight: float) -> None:
    conn.execute(
        """
        INSERT INTO artist_tag (
            artist_id,
            tag,
            source,
            tag_count,
            normalized_weight,
            fetched_at
        )
        VALUES (?, ?, 'lastfm', ?, ?, current_timestamp)
        """,
        [artist_id, tag, weight * 100.0, weight],
    )
