import json
from datetime import datetime
from pathlib import Path

import pytest
from duckdb import ConstraintException

from scrobble_recs.apis.lastfm import LastFmArtistTag, LastFmSimilarArtist, LastFmTagTopArtist
from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.enrichment.lastfm import run_lastfm_enrichment
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
from scrobble_recs.models.feedback import record_feedback_event
from scrobble_recs.models.recommend import generate_recommendations


def write_config(tmp_path: Path) -> Path:
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
                "recommendation": {
                    "seed_artist_limit": 10,
                    "similar_artists_per_seed": 10,
                    "min_seed_scrobbles": 2,
                    "obviousness": {"tag_top_artists_limit": 100},
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


def write_config_with_obviousness_disabled(tmp_path: Path) -> Path:
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
                "recommendation": {
                    "seed_artist_limit": 10,
                    "similar_artists_per_seed": 10,
                    "min_seed_scrobbles": 2,
                    "obviousness": {
                        "enabled": False,
                        "tag_top_artists_limit": 100,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_recommendation_migration_creates_run_and_result_tables(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main'
                """
            ).fetchall()
        }

        assert "recommendation_run" in tables
        assert "recommendation" in tables
        assert _column_types(conn, "recommendation_run") == {
            "run_id": "UUID",
            "created_at": "TIMESTAMP",
            "config_hash": "VARCHAR",
            "mode": "VARCHAR",
            "seed_summary": "JSON",
            "scoring_weights": "JSON",
            "notes": "VARCHAR",
        }
        assert _column_types(conn, "recommendation") == {
            "run_id": "UUID",
            "rank": "INTEGER",
            "artist_id": "UBIGINT",
            "final_score": "DOUBLE",
            "raw_score": "DOUBLE",
            "lastfm_similarity_score": "DOUBLE",
            "tag_similarity_score": "DOUBLE",
            "cluster_fit_score": "DOUBLE",
            "rediscovery_score": "DOUBLE",
            "metadata_graph_score": "DOUBLE",
            "feedback_score": "DOUBLE",
            "knownness": "DOUBLE",
            "obviousness": "DOUBLE",
            "diversity_penalty": "DOUBLE",
            "explanation": "JSON",
            "created_at": "TIMESTAMP",
            "score_band": "VARCHAR",
            "evidence_source_count": "INTEGER",
            "local_scrobble_count": "INTEGER",
            "listened_day_count": "INTEGER",
            "first_listened_at": "TIMESTAMP",
            "last_listened_at": "TIMESTAMP",
            "years_since_last_listen": "DOUBLE",
            "rediscovery_eligible": "BOOLEAN",
            "trial_penalty": "DOUBLE",
            "candidate_artist_name": "VARCHAR",
            "canonical_artist_name": "VARCHAR",
            "musicbrainz_artist_mbid": "VARCHAR",
            "lastfm_url": "VARCHAR",
            "musicbrainz_url": "VARCHAR",
            "identity_confidence": "DOUBLE",
            "identity_warnings": "VARCHAR",
        }

        conn.execute(
            """
            INSERT INTO recommendation_run (
                run_id,
                config_hash,
                mode,
                seed_summary,
                scoring_weights
            )
            VALUES (
                '00000000-0000-0000-0000-000000000001',
                'hash-1',
                'balanced',
                '{}',
                '{}'
            )
            """
        )
        with pytest.raises(ConstraintException):
            conn.execute(
                """
                INSERT INTO recommendation_run (
                    run_id,
                    config_hash,
                    mode,
                    seed_summary,
                    scoring_weights
                )
                VALUES (
                    '00000000-0000-0000-0000-000000000001',
                    'hash-2',
                    'balanced',
                    '{}',
                    '{}'
                )
                """
            )

        conn.execute(
            """
            INSERT INTO recommendation (run_id, rank, artist_id)
            VALUES ('00000000-0000-0000-0000-000000000001', 1, 100)
            """
        )
        with pytest.raises(ConstraintException):
            conn.execute(
                """
                INSERT INTO recommendation (run_id, rank, artist_id)
                VALUES ('00000000-0000-0000-0000-000000000001', 1, 101)
                """
            )
        with pytest.raises(ConstraintException):
            conn.execute(
                """
                INSERT INTO recommendation (run_id, rank, artist_id)
                VALUES ('00000000-0000-0000-0000-000000000001', 2, 100)
                """
            )
    finally:
        conn.close()


def _column_types(conn, table_name: str) -> dict[str, str]:
    return {
        str(column_name): str(data_type)
        for column_name, data_type in conn.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'main'
              AND table_name = ?
            ORDER BY ordinal_position
            """,
            [table_name],
        ).fetchall()
    }


class RecommendationFakeLastFmClient:
    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        return [
            LastFmSimilarArtist("Siouxsie and the Banshees", None, 0.91),
            LastFmSimilarArtist("Bauhaus", None, 0.72),
            LastFmSimilarArtist("The Cure", "mbid-cure", 1.0),
            LastFmSimilarArtist("Joy Division", None, 0.60),
        ][:limit]

    def get_artist_tags(self, artist: str) -> list[LastFmArtistTag]:
        return [
            LastFmArtistTag("post-punk", 100.0),
            LastFmArtistTag("gothic rock", 75.0),
        ]

    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        rows = {
            "post-punk": [
                LastFmTagTopArtist(1, "The Cure", "mbid-cure", 1000.0),
                LastFmTagTopArtist(2, "Joy Division", None, 900.0),
                LastFmTagTopArtist(42, "Bauhaus", None, 250.0),
                LastFmTagTopArtist(55, "Siouxsie and the Banshees", None, 200.0),
            ],
            "gothic rock": [
                LastFmTagTopArtist(1, "The Cure", "mbid-cure", 1000.0),
                LastFmTagTopArtist(4, "Bauhaus", None, 700.0),
                LastFmTagTopArtist(30, "Siouxsie and the Banshees", None, 300.0),
            ],
        }
        return rows[tag][:limit]


def test_generate_recommendations_persists_ranked_run_with_explanations(
    tmp_path: Path,
) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, config, client=RecommendationFakeLastFmClient())

    stats = generate_recommendations(
        conn,
        config,
        limit=3,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert stats.recommendations == 2
    assert stats.mode == "balanced"

    rows = conn.execute(
        """
        SELECT r.rank, a.canonical_name, r.final_score, r.knownness, r.obviousness, r.explanation
        FROM recommendation r
        JOIN artist a ON a.artist_id = r.artist_id
        WHERE r.run_id = ?
        ORDER BY r.rank
        """,
        [stats.run_id],
    ).fetchall()

    assert [row[1] for row in rows] == [
        "Siouxsie and the Banshees",
        "Bauhaus",
    ]
    assert rows[0][2] > rows[1][2]
    assert rows[0][3] == 0.0
    assert rows[1][4] > 0.0

    explanation = json.loads(rows[0][5])
    assert explanation["classification"] == {
        "is_new": True,
        "is_rediscovery": False,
        "is_obvious": False,
    }
    assert explanation["seed_artists"][0]["artist"] == "The Cure"
    assert explanation["tags"][0]["tag"] == "post-punk"

    conn.close()


def test_generate_recommendations_reports_phase_progress(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, config, client=RecommendationFakeLastFmClient())
    events = []

    generate_recommendations(
        conn,
        config,
        limit=3,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
        progress_callback=events.append,
    )

    assert [
        (event.phase, event.current, event.total, event.subject, event.status) for event in events
    ] == [
        ("refresh_listening", 0, 1, "artist listening scores", "processing"),
        ("refresh_listening", 1, 1, "artist listening scores", "completed"),
        ("select_seeds", 0, 1, "balanced", "processing"),
        ("select_seeds", 1, 1, "1 seed artists", "completed"),
        ("build_profiles", 0, 1, "tags and feedback", "processing"),
        ("build_profiles", 1, 1, "2 tag weights", "completed"),
        ("load_candidates", 0, 1, "local graph", "processing"),
        ("load_candidates", 1, 1, "3 candidates", "completed"),
        ("score_candidates", 0, 3, "candidate artists", "processing"),
        ("score_candidates", 1, 3, "1 positive so far", "processing"),
        ("score_candidates", 2, 3, "2 positive so far", "processing"),
        ("score_candidates", 3, 3, "2 positive scores", "completed"),
        ("rerank_diversity", 0, 3, "ranked candidates", "processing"),
        ("rerank_diversity", 1, 3, "1 selected", "processing"),
        ("rerank_diversity", 2, 3, "2 selected", "processing"),
        ("rerank_diversity", 2, 3, "2 selected", "completed"),
        ("build_explanations", 0, 2, "recommendations", "processing"),
        ("build_explanations", 2, 2, "recommendations", "completed"),
        ("persist_run", 0, 2, "recommendation rows", "processing"),
        ("persist_run", 2, 2, "recommendation rows", "completed"),
    ]
    conn.close()


def test_generate_recommendations_reports_incremental_rerank_progress(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    config.recommendation.diversity.max_same_primary_tag_in_top_20 = 30
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=1200,
        name="Seed Source",
        play_count=80,
        lifetime_weight=10.0,
        recent_weight=10.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=1200, tag="post-punk", weight=100.0)
    for offset in range(1, 31):
        _insert_candidate_from_seed(
            conn,
            seed_id=1200,
            candidate_id=1200 + offset,
            candidate_name=f"Candidate {offset:02d}",
            tag="post-punk" if offset % 2 else "dream pop",
            tag_rank=20 + offset,
            similarity=0.95 - offset * 0.01,
        )
    events = []

    generate_recommendations(
        conn,
        config,
        limit=25,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
        progress_callback=events.append,
    )

    rerank_processing_counts = [
        event.current
        for event in events
        if event.phase == "rerank_diversity" and event.status == "processing"
    ]
    assert rerank_processing_counts[0] == 0
    assert any(0 < current < 25 for current in rerank_processing_counts)
    assert rerank_processing_counts[-1] == 25
    conn.close()


def test_generate_recommendations_replaces_existing_rows_for_new_run_only(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, config, client=RecommendationFakeLastFmClient())

    first = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )
    second = generate_recommendations(
        conn,
        config,
        limit=1,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert first.run_id != second.run_id
    assert conn.execute(
        "SELECT count(*) FROM recommendation WHERE run_id = ?",
        [first.run_id],
    ).fetchone() == (2,)
    assert conn.execute(
        "SELECT count(*) FROM recommendation WHERE run_id = ?",
        [second.run_id],
    ).fetchone() == (1,)
    conn.close()


def test_generate_recommendations_orders_explanation_tags_before_slicing(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, config, client=RecommendationFakeLastFmClient())

    cure_id = _artist_id(conn, "The Cure")
    siouxsie_id = _artist_id(conn, "Siouxsie and the Banshees")
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
        VALUES (?, 'coldwave', 'lastfm', 500.0, 5.0, current_timestamp)
        """,
        [cure_id],
    )
    for index, tag in enumerate(["filler-a", "filler-b", "filler-c", "filler-d"], start=1):
        conn.execute(
            """
            INSERT INTO tag_top_artist (
                tag,
                artist_id,
                source,
                tag_rank,
                tag_count,
                fetched_at
            )
            VALUES (?, ?, 'lastfm', ?, 1.0, current_timestamp)
            """,
            [tag, siouxsie_id, index],
        )
    conn.execute(
        """
        INSERT INTO tag_top_artist (
            tag,
            artist_id,
            source,
            tag_rank,
            tag_count,
            fetched_at
        )
        VALUES ('coldwave', ?, 'lastfm', 100, 1.0, current_timestamp)
        """,
        [siouxsie_id],
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=3,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    (explanation_json,) = conn.execute(
        """
        SELECT r.explanation
        FROM recommendation r
        JOIN artist a ON a.artist_id = r.artist_id
        WHERE r.run_id = ?
          AND a.canonical_name = 'Siouxsie and the Banshees'
        """,
        [stats.run_id],
    ).fetchone()
    explanation = json.loads(explanation_json)

    assert explanation["tags"][0]["tag"] == "coldwave"
    assert len(explanation["tags"]) == 5
    conn.close()


def test_generate_recommendations_explanation_uses_current_seed_edges_only(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, config, client=RecommendationFakeLastFmClient())

    stale_source_id = _artist_id(conn, "Cocteau Twins")
    siouxsie_id = _artist_id(conn, "Siouxsie and the Banshees")
    conn.execute(
        """
        INSERT INTO artist_similarity_edge (
            source_artist_id,
            target_artist_id,
            source,
            source_score,
            normalized_score,
            evidence,
            fetched_at
        )
        VALUES (?, ?, 'lastfm', 99.0, 99.0, '{}', current_timestamp)
        """,
        [stale_source_id, siouxsie_id],
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=3,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    (explanation_json,) = conn.execute(
        """
        SELECT r.explanation
        FROM recommendation r
        JOIN artist a ON a.artist_id = r.artist_id
        WHERE r.run_id = ?
          AND a.canonical_name = 'Siouxsie and the Banshees'
        """,
        [stats.run_id],
    ).fetchone()
    explanation = json.loads(explanation_json)

    assert [seed["artist"] for seed in explanation["seed_artists"]] == ["The Cure"]
    conn.close()


def _artist_id(conn, artist_name: str) -> int:
    (artist_id,) = conn.execute(
        "SELECT artist_id FROM artist WHERE canonical_name = ?",
        [artist_name],
    ).fetchone()
    return int(artist_id)


def test_generate_recommendations_hard_hides_obvious_high_affinity_artist(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=100,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=100, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=100,
        candidate_id=101,
        candidate_name="Obvious Rank Two",
        tag="post-punk",
        tag_rank=2,
        similarity=0.95,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=100,
        candidate_id=102,
        candidate_name="Less Obvious Rank Twenty",
        tag="post-punk",
        tag_rank=20,
        similarity=0.75,
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    names = _recommendation_names(conn, stats.run_id)
    assert names == ["Less Obvious Rank Twenty"]
    conn.close()


def test_generate_recommendations_keeps_obvious_candidate_when_obviousness_disabled(
    tmp_path: Path,
) -> None:
    config = load_config(write_config_with_obviousness_disabled(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=104,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=104, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=104,
        candidate_id=105,
        candidate_name="Obvious Rank Two",
        tag="post-punk",
        tag_rank=2,
        similarity=0.95,
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == ["Obvious Rank Two"]
    (obviousness, explanation_json) = conn.execute(
        "SELECT obviousness, explanation FROM recommendation WHERE run_id = ? AND rank = 1",
        [stats.run_id],
    ).fetchone()
    explanation = json.loads(explanation_json)
    assert obviousness == 0.0
    assert explanation["penalties"]["obviousness"] == 0.0
    assert explanation["classification"]["is_obvious"] is False
    conn.close()


def test_generate_recommendations_keeps_obvious_rediscovery_candidate(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=110,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=110, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=110,
        candidate_id=111,
        candidate_name="Dormant Rank Two",
        tag="post-punk",
        tag_rank=2,
        similarity=0.95,
    )
    conn.execute(
        """
        INSERT INTO artist_listening_fact (
            artist_id,
            play_count,
            track_count,
            active_months,
            first_played_at,
            last_played_at
        )
        VALUES (111, 1, 1, 1, '2018-01-01', '2019-01-01')
        """
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == ["Dormant Rank Two"]
    (explanation_json,) = conn.execute(
        "SELECT explanation FROM recommendation WHERE run_id = ? AND rank = 1",
        [stats.run_id],
    ).fetchone()
    explanation = json.loads(explanation_json)
    assert explanation["classification"]["is_obvious"] is True
    assert explanation["classification"]["is_rediscovery"] is True
    conn.close()


def test_generate_recommendations_skips_rediscovery_candidate_selected_as_seed(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=112,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=112, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=112,
        candidate_id=113,
        candidate_name="Seeded Dormant Rank Two",
        tag="post-punk",
        tag_rank=2,
        similarity=0.95,
    )
    conn.execute(
        """
        INSERT INTO artist_listening_fact (
            artist_id,
            play_count,
            track_count,
            active_months,
            first_played_at,
            last_played_at
        )
        VALUES (113, 5, 3, 2, '2018-01-01', '2019-01-01')
        """
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == []
    conn.close()


def test_anti_obvious_mode_uses_stricter_top_tag_hide(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=120,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=120, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=120,
        candidate_id=121,
        candidate_name="Rank Eight Mainstream",
        tag="post-punk",
        tag_rank=8,
        similarity=0.95,
    )

    balanced = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )
    anti_obvious = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="anti-obvious",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, balanced.run_id) == ["Rank Eight Mainstream"]
    assert _recommendation_names(conn, anti_obvious.run_id) == []
    conn.close()


def test_tag_focus_mode_boosts_matching_candidate_tags(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=130,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=130, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=130,
        candidate_id=131,
        candidate_name="Lower Similarity Gothic Fit",
        tag="gothic rock",
        tag_rank=25,
        similarity=0.55,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=130,
        candidate_id=132,
        candidate_name="Higher Similarity Punk Fit",
        tag="punk",
        tag_rank=25,
        similarity=0.70,
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="tag:gothic rock",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == [
        "Lower Similarity Gothic Fit",
        "Higher Similarity Punk Fit",
    ]
    (explanation_json,) = conn.execute(
        """
        SELECT r.explanation
        FROM recommendation r
        JOIN artist a ON a.artist_id = r.artist_id
        WHERE r.run_id = ?
          AND a.canonical_name = 'Lower Similarity Gothic Fit'
        """,
        [stats.run_id],
    ).fetchone()
    explanation = json.loads(explanation_json)
    assert explanation["tags"][0]["tag"] == "gothic rock"
    assert explanation["tags"][0]["user_weight"] == pytest.approx(1.0)
    conn.close()


def test_tag_focus_mode_persists_effective_mode_scoring_metadata(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=140,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=140, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=140,
        candidate_id=141,
        candidate_name="Gothic Metadata Fit",
        tag="gothic rock",
        tag_rank=25,
        similarity=0.55,
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=1,
        mode="tag:gothic rock",
        as_of=datetime(2026, 6, 4),
    )

    (scoring_weights_json,) = conn.execute(
        "SELECT scoring_weights FROM recommendation_run WHERE run_id = ?",
        [stats.run_id],
    ).fetchone()
    scoring_weights = json.loads(scoring_weights_json)
    mode = scoring_weights["mode"]
    assert mode["lastfm_similarity_multiplier"] == pytest.approx(1.0)
    assert mode["tag_similarity_multiplier"] == pytest.approx(1.25)
    assert mode["rediscovery_multiplier"] == pytest.approx(1.0)
    assert mode["tag_focus_boost"] == pytest.approx(1.60)
    assert mode["high_affinity_tag_weight"] == pytest.approx(0.75)
    assert mode["obviousness_enabled"] is True
    conn.close()


def test_generate_recommendations_caps_same_primary_tag_in_top_twenty(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    config.recommendation.diversity.max_same_primary_tag_in_top_20 = 2
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=200,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=200, tag="post-punk", weight=100.0)
    for offset, similarity in enumerate([0.95, 0.90, 0.85, 0.80], start=1):
        _insert_candidate_from_seed(
            conn,
            seed_id=200,
            candidate_id=200 + offset,
            candidate_name=f"Post Punk Candidate {offset}",
            tag="post-punk",
            tag_rank=25 + offset,
            similarity=similarity,
        )
    _insert_candidate_from_seed(
        conn,
        seed_id=200,
        candidate_id=210,
        candidate_name="Dream Pop Candidate",
        tag="dream pop",
        tag_rank=30,
        similarity=0.70,
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    top_names = _recommendation_names(conn, stats.run_id)
    assert top_names[:3] == [
        "Post Punk Candidate 1",
        "Dream Pop Candidate",
        "Post Punk Candidate 2",
    ]
    assert sum(1 for name in top_names[:5] if name.startswith("Post Punk")) == 2
    conn.close()


def test_generate_recommendations_persists_diversity_penalty_for_similar_followups(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=220,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=220, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=220,
        candidate_id=221,
        candidate_name="First Similar Candidate",
        tag="post-punk",
        tag_rank=30,
        similarity=0.95,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=220,
        candidate_id=222,
        candidate_name="Second Similar Candidate",
        tag="post-punk",
        tag_rank=31,
        similarity=0.90,
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    rows = conn.execute(
        """
        SELECT a.canonical_name, r.diversity_penalty, r.explanation
        FROM recommendation r
        JOIN artist a ON a.artist_id = r.artist_id
        WHERE r.run_id = ?
        ORDER BY r.rank
        """,
        [stats.run_id],
    ).fetchall()
    assert rows[0][0] == "First Similar Candidate"
    assert rows[0][1] == pytest.approx(0.0)
    assert rows[1][0] == "Second Similar Candidate"
    assert rows[1][1] > 0.0
    explanation = json.loads(rows[1][2])
    assert explanation["penalties"]["diversity"] == pytest.approx(rows[1][1])
    conn.close()


def test_generate_recommendations_applies_direct_feedback_scores(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=300,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=300, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=300,
        candidate_id=301,
        candidate_name="Lower Similarity Saved",
        tag="post-punk",
        tag_rank=30,
        similarity=0.62,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=300,
        candidate_id=302,
        candidate_name="Higher Similarity Neutral",
        tag="post-punk",
        tag_rank=31,
        similarity=0.70,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000301")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000301",
        artist_id=301,
        action="save",
        context={"tags": ["post-punk"]},
        feedback_id="00000000-0000-0000-0000-000000000401",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == [
        "Lower Similarity Saved",
        "Higher Similarity Neutral",
    ]
    row = conn.execute(
        """
        SELECT r.feedback_score, r.explanation
        FROM recommendation r
        WHERE r.run_id = ?
          AND r.artist_id = 301
        """,
        [stats.run_id],
    ).fetchone()
    assert row[0] > 0.0
    explanation = json.loads(row[1])
    assert explanation["feedback"]["score"] == pytest.approx(row[0])
    assert explanation["feedback"]["effects"] == ["artist"]
    assert explanation["feedback"]["contributions"]["artist"] > 0.0
    assert explanation["feedback"]["contributions"]["tag"] == 0.0
    assert explanation["feedback"]["contributions"]["neighborhood"] == 0.0
    conn.close()


def test_generate_recommendations_keeps_saved_current_candidate_with_outgoing_edges(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=330,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=330, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=330,
        candidate_id=331,
        candidate_name="Saved Candidate With Edges",
        tag="post-punk",
        tag_rank=30,
        similarity=0.62,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=330,
        candidate_id=332,
        candidate_name="Higher Similarity Neutral",
        tag="post-punk",
        tag_rank=31,
        similarity=0.70,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=331,
        candidate_id=333,
        candidate_name="Saved Candidate Neighbor",
        tag="coldwave",
        tag_rank=40,
        similarity=0.95,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000304")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000304",
        artist_id=331,
        action="save",
        context={"tags": ["post-punk"]},
        feedback_id="00000000-0000-0000-0000-000000000404",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=3,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id)[:2] == [
        "Saved Candidate With Edges",
        "Higher Similarity Neutral",
    ]
    row = conn.execute(
        """
        SELECT r.feedback_score, r.explanation
        FROM recommendation r
        WHERE r.run_id = ?
          AND r.artist_id = 331
        """,
        [stats.run_id],
    ).fetchone()
    assert row[0] > 0.0
    explanation = json.loads(row[1])
    assert explanation["feedback"]["effects"] == ["artist"]
    assert explanation["feedback"]["contributions"]["artist"] > 0.0

    (seed_summary_json,) = conn.execute(
        "SELECT seed_summary FROM recommendation_run WHERE run_id = ?",
        [stats.run_id],
    ).fetchone()
    seed_summary = json.loads(seed_summary_json)
    assert "feedback" not in seed_summary["regions"]
    conn.close()


def test_generate_recommendations_excludes_hidden_feedback_artist(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=310,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=310, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=310,
        candidate_id=311,
        candidate_name="Hidden Artist",
        tag="post-punk",
        tag_rank=30,
        similarity=0.90,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=310,
        candidate_id=312,
        candidate_name="Visible Artist",
        tag="post-punk",
        tag_rank=31,
        similarity=0.70,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000302")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000302",
        artist_id=311,
        action="hide_artist",
        feedback_id="00000000-0000-0000-0000-000000000402",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == ["Visible Artist"]
    conn.close()


def test_generate_recommendations_uses_more_like_this_as_future_seed(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=320,
        name="Original Seed",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=320, tag="post-punk", weight=100.0)
    _insert_scoring_artist(
        conn,
        artist_id=321,
        name="Feedback Seed",
        play_count=0,
        lifetime_weight=0.0,
        recent_weight=0.0,
        knownness=0.0,
        rediscovery_score=0.0,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=321,
        candidate_id=322,
        candidate_name="Feedback Neighborhood Artist",
        tag="coldwave",
        tag_rank=40,
        similarity=0.95,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000303")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000303",
        artist_id=321,
        action="more_like_this",
        context={"tags": ["coldwave"]},
        feedback_id="00000000-0000-0000-0000-000000000403",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == ["Feedback Neighborhood Artist"]
    row = conn.execute(
        "SELECT feedback_score FROM recommendation WHERE run_id = ? AND artist_id = 322",
        [stats.run_id],
    ).fetchone()
    assert row[0] > 0.0
    (explanation_json,) = conn.execute(
        "SELECT explanation FROM recommendation WHERE run_id = ? AND artist_id = 322",
        [stats.run_id],
    ).fetchone()
    explanation = json.loads(explanation_json)
    assert "neighborhood" in explanation["feedback"]["effects"]
    assert "artist" not in explanation["feedback"]["effects"]
    assert explanation["feedback"]["contributions"]["neighborhood"] > 0.0
    assert explanation["feedback"]["contributions"]["artist"] == 0.0
    (seed_summary_json,) = conn.execute(
        "SELECT seed_summary FROM recommendation_run WHERE run_id = ?",
        [stats.run_id],
    ).fetchone()
    seed_summary = json.loads(seed_summary_json)
    assert seed_summary["regions"]["feedback"] == 1
    conn.close()


def test_generate_recommendations_does_not_mode_multiply_feedback_seed_weight(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=380,
        name="Original Seed",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=380, tag="post-punk", weight=100.0)
    _insert_scoring_artist(
        conn,
        artist_id=381,
        name="Feedback Seed",
        play_count=0,
        lifetime_weight=0.0,
        recent_weight=0.0,
        knownness=0.0,
        rediscovery_score=0.0,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=381,
        candidate_id=382,
        candidate_name="Feedback Neighbor",
        tag="coldwave",
        tag_rank=40,
        similarity=0.95,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000309")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000309",
        artist_id=381,
        action="more_like_this",
        feedback_id="00000000-0000-0000-0000-000000000409",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=1,
        mode="safe",
        as_of=datetime(2026, 6, 4),
    )

    (seed_summary_json,) = conn.execute(
        "SELECT seed_summary FROM recommendation_run WHERE run_id = ?",
        [stats.run_id],
    ).fetchone()
    seed_summary = json.loads(seed_summary_json)
    feedback_seed = next(
        seed for seed in seed_summary["top_seeds"] if seed["artist"] == "Feedback Seed"
    )
    assert feedback_seed["seed_weight"] == pytest.approx(0.25)
    assert seed_summary["regions"]["feedback"] == 1
    assert set(seed_summary["regions"]) == {"feedback", "lifetime", "recent"}
    conn.close()


def test_generate_recommendations_applies_tag_feedback_scores(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=340,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=340, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=340,
        candidate_id=341,
        candidate_name="Lower Similarity Boosted Tag",
        tag="coldwave",
        tag_rank=30,
        similarity=0.62,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=340,
        candidate_id=342,
        candidate_name="Higher Similarity Neutral Tag",
        tag="noise",
        tag_rank=31,
        similarity=0.65,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000305")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000305",
        artist_id=341,
        action="boost_tag",
        context={"tags": ["coldwave"]},
        feedback_id="00000000-0000-0000-0000-000000000405",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == [
        "Lower Similarity Boosted Tag",
        "Higher Similarity Neutral Tag",
    ]
    row = conn.execute(
        """
        SELECT r.feedback_score, r.explanation
        FROM recommendation r
        WHERE r.run_id = ?
          AND r.artist_id = 341
        """,
        [stats.run_id],
    ).fetchone()
    assert row[0] > 0.0
    explanation = json.loads(row[1])
    assert explanation["feedback"]["effects"] == ["tag"]
    assert explanation["feedback"]["contributions"]["artist"] == 0.0
    assert explanation["feedback"]["contributions"]["tag"] > 0.0
    assert explanation["feedback"]["contributions"]["neighborhood"] == 0.0
    conn.close()


def test_generate_recommendations_matches_feedback_to_mixed_case_candidate_tags(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=390,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=390, tag="Post-Punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=390,
        candidate_id=391,
        candidate_name="Lower Similarity Mixed Case Tag",
        tag="ColdWave",
        tag_rank=30,
        similarity=0.62,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=390,
        candidate_id=392,
        candidate_name="Higher Similarity Neutral Tag",
        tag="Noise",
        tag_rank=31,
        similarity=0.65,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000310")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000310",
        artist_id=391,
        action="boost_tag",
        context={"tags": ["coldwave"]},
        feedback_id="00000000-0000-0000-0000-000000000410",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == [
        "Lower Similarity Mixed Case Tag",
        "Higher Similarity Neutral Tag",
    ]
    row = conn.execute(
        """
        SELECT r.feedback_score, r.explanation
        FROM recommendation r
        WHERE r.run_id = ?
          AND r.artist_id = 391
        """,
        [stats.run_id],
    ).fetchone()
    assert row[0] > 0.0
    explanation = json.loads(row[1])
    assert explanation["feedback"]["effects"] == ["tag"]
    assert explanation["feedback"]["contributions"]["tag"] > 0.0
    conn.close()


def test_generate_recommendations_marks_already_known_candidates_with_knownness_penalty(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=350,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=350, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=350,
        candidate_id=351,
        candidate_name="Already Known Candidate",
        tag="coldwave",
        tag_rank=30,
        similarity=0.95,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000306")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000306",
        artist_id=351,
        action="already_know",
        feedback_id="00000000-0000-0000-0000-000000000406",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=1,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    row = conn.execute(
        """
        SELECT r.knownness, r.feedback_score, r.explanation
        FROM recommendation r
        WHERE r.run_id = ?
          AND r.artist_id = 351
        """,
        [stats.run_id],
    ).fetchone()
    assert row[0] == pytest.approx(1.0)
    assert row[1] < 0.0
    explanation = json.loads(row[2])
    assert explanation["penalties"]["knownness"] > 0.0
    assert explanation["feedback"]["effects"] == ["artist"]
    conn.close()


def test_generate_recommendations_applies_too_obvious_tag_feedback_penalty(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=360,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=360, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=360,
        candidate_id=361,
        candidate_name="Too Obvious Tag Candidate",
        tag="synthpop",
        tag_rank=30,
        similarity=0.95,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000307")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000307",
        artist_id=999,
        action="too_obvious",
        context={"tags": ["synthpop"]},
        feedback_id="00000000-0000-0000-0000-000000000407",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=1,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    row = conn.execute(
        """
        SELECT r.obviousness, r.feedback_score, r.explanation
        FROM recommendation r
        WHERE r.run_id = ?
          AND r.artist_id = 361
        """,
        [stats.run_id],
    ).fetchone()
    assert row[0] > 0.0
    assert row[1] < 0.0
    explanation = json.loads(row[2])
    assert explanation["penalties"]["obviousness"] > 0.0
    assert explanation["feedback"]["effects"] == ["tag"]
    assert explanation["feedback"]["contributions"]["artist"] == 0.0
    assert explanation["feedback"]["contributions"]["tag"] < 0.0
    assert explanation["feedback"]["contributions"]["neighborhood"] == 0.0
    conn.close()


def test_generate_recommendations_demotes_suppressed_feedback_candidate(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=370,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=370, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=370,
        candidate_id=371,
        candidate_name="Higher Similarity Suppressed",
        tag="post-punk",
        tag_rank=30,
        similarity=0.76,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=370,
        candidate_id=372,
        candidate_name="Lower Similarity Neutral",
        tag="post-punk",
        tag_rank=31,
        similarity=0.70,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000308")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000308",
        artist_id=371,
        action="less_like_this",
        feedback_id="00000000-0000-0000-0000-000000000408",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == [
        "Lower Similarity Neutral",
        "Higher Similarity Suppressed",
    ]
    row = conn.execute(
        """
        SELECT r.feedback_score, r.explanation
        FROM recommendation r
        WHERE r.run_id = ?
          AND r.artist_id = 371
        """,
        [stats.run_id],
    ).fetchone()
    assert row[0] < 0.0
    explanation = json.loads(row[1])
    assert explanation["feedback"]["effects"] == ["artist"]
    assert explanation["feedback"]["contributions"]["artist"] < 0.0
    conn.close()


def test_generate_recommendations_applies_suppressed_feedback_neighborhood(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=400,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=400, tag="post-punk", weight=100.0)
    _insert_scoring_artist(
        conn,
        artist_id=401,
        name="Suppressed Feedback Source",
        play_count=0,
        lifetime_weight=0.0,
        recent_weight=0.0,
        knownness=0.0,
        rediscovery_score=0.0,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=400,
        candidate_id=402,
        candidate_name="Suppressed Neighborhood Candidate",
        tag="post-punk",
        tag_rank=30,
        similarity=0.80,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=401,
        candidate_id=402,
        candidate_name="Suppressed Neighborhood Candidate",
        tag="darkwave",
        tag_rank=30,
        similarity=0.95,
        insert_artist=False,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000311")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000311",
        artist_id=401,
        action="less_like_this",
        feedback_id="00000000-0000-0000-0000-000000000411",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=1,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    row = conn.execute(
        """
        SELECT r.feedback_score, r.explanation
        FROM recommendation r
        WHERE r.run_id = ?
          AND r.artist_id = 402
        """,
        [stats.run_id],
    ).fetchone()
    assert row[0] < 0.0
    explanation = json.loads(row[1])
    assert explanation["feedback"]["effects"] == ["neighborhood"]
    assert explanation["feedback"]["contributions"]["artist"] == 0.0
    assert explanation["feedback"]["contributions"]["tag"] == 0.0
    assert explanation["feedback"]["contributions"]["neighborhood"] < 0.0
    conn.close()


def _insert_scoring_artist(
    conn,
    *,
    artist_id: int,
    name: str,
    play_count: int,
    lifetime_weight: float,
    recent_weight: float,
    knownness: float,
    rediscovery_score: float,
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
        VALUES (?, ?, 10, 10, '2024-01-01', '2026-05-01', ?, ?, ?, ?)
        """,
        [
            artist_id,
            play_count,
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
        [artist_id, tag, weight, weight / 100.0],
    )


def _insert_candidate_from_seed(
    conn,
    *,
    seed_id: int,
    candidate_id: int,
    candidate_name: str,
    tag: str,
    tag_rank: int,
    similarity: float,
    insert_artist: bool = True,
) -> None:
    if insert_artist:
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
            [
                candidate_id,
                candidate_name,
                candidate_name.casefold().replace(" ", "-"),
                candidate_name,
            ],
        )
    conn.execute(
        """
        INSERT INTO artist_similarity_edge (
            source_artist_id,
            target_artist_id,
            source,
            source_score,
            normalized_score,
            evidence,
            fetched_at
        )
        VALUES (?, ?, 'lastfm', ?, ?, '{}', current_timestamp)
        """,
        [seed_id, candidate_id, similarity, similarity],
    )
    conn.execute(
        """
        INSERT INTO tag_top_artist (
            tag,
            artist_id,
            source,
            tag_rank,
            tag_count,
            fetched_at
        )
        VALUES (?, ?, 'lastfm', ?, 1.0, current_timestamp)
        """,
        [tag, candidate_id, tag_rank],
    )


def _recommendation_names(conn, run_id: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """
            SELECT a.canonical_name
            FROM recommendation r
            JOIN artist a ON a.artist_id = r.artist_id
            WHERE r.run_id = ?
            ORDER BY r.rank
            """,
            [run_id],
        ).fetchall()
    ]


def _insert_run_for_feedback(conn, *, run_id: str) -> None:
    conn.execute(
        """
        INSERT INTO recommendation_run (
            run_id,
            config_hash,
            mode,
            seed_summary,
            scoring_weights
        )
        VALUES (?, 'hash', 'balanced', '{}', '{}')
        """,
        [run_id],
    )


def test_generate_recommendations_skips_rediscovery_without_current_taste_match(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=500,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=500, tag="post-punk", weight=100.0)
    _insert_scoring_artist(
        conn,
        artist_id=501,
        name="Old Random Local",
        play_count=5,
        lifetime_weight=0.0,
        recent_weight=0.0,
        knownness=0.02,
        rediscovery_score=0.95,
    )
    conn.execute(
        """
        INSERT INTO tag_top_artist (
            tag,
            artist_id,
            source,
            tag_rank,
            tag_count,
            fetched_at
        )
        VALUES ('spoken word', 501, 'lastfm', 91, 1.0, current_timestamp)
        """
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert stats.recommendations == 0
    assert _recommendation_names(conn, stats.run_id) == []
    conn.close()


def test_generate_recommendations_allows_rediscovery_with_tag_match(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=510,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=510, tag="coldwave", weight=100.0)

    # Insert candidate manually with dates that produce a positive rediscovery_score
    conn.execute(
        """
        INSERT INTO artist (artist_id, canonical_name, artist_key, lastfm_name)
        VALUES (511, 'Old Matching Local', 'old-matching-local', 'Old Matching Local')
        """
    )
    conn.execute(
        """
        INSERT INTO artist_listening_fact (
            artist_id, play_count, track_count, active_months,
            first_played_at, last_played_at
        )
        VALUES (511, 1, 3, 2, '2018-01-01', '2023-01-01')
        """
    )
    conn.execute(
        """
        INSERT INTO tag_top_artist (
            tag, artist_id, source, tag_rank, tag_count, fetched_at
        )
        VALUES ('coldwave', 511, 'lastfm', 40, 1.0, current_timestamp)
        """
    )
    conn.execute(
        """
        INSERT INTO artist_similarity_edge (
            source_artist_id, target_artist_id, source,
            source_score, normalized_score, evidence, fetched_at
        )
        VALUES (510, 511, 'lastfm', 0.10, 0.10, '{}', current_timestamp)
        """
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == ["Old Matching Local"]
    (explanation_json,) = conn.execute(
        "SELECT explanation FROM recommendation WHERE run_id = ? AND artist_id = 511",
        [stats.run_id],
    ).fetchone()
    explanation = json.loads(explanation_json)
    assert explanation["quality_gate"]["passed"] is True
    assert "tag_similarity" in explanation["quality_gate"]["reasons"]
    conn.close()


def test_generate_recommendations_rediscovery_mode_relaxes_gate_thresholds(
    tmp_path: Path,
) -> None:
    config_path = write_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["recommendation"]["quality_gate"] = {
        "enabled": True,
        "rediscovery_requires_taste_match": True,
        "min_lastfm_similarity_score": 0.50,
        "min_tag_similarity_score": 0.50,
        "min_positive_feedback_score": 0.05,
        "rediscovery_mode_multiplier": 0.50,
    }
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(config_path)
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=520,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=520, tag="coldwave", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=520,
        candidate_id=521,
        candidate_name="Relaxed Threshold Candidate",
        tag="noise",
        tag_rank=80,
        similarity=0.30,
    )

    balanced = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )
    rediscovery = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="rediscovery",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, balanced.run_id) == []
    assert _recommendation_names(conn, rediscovery.run_id) == ["Relaxed Threshold Candidate"]
    conn.close()


def test_generate_recommendations_demotes_recently_recommended_artist(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=530,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=530, tag="coldwave", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=530,
        candidate_id=531,
        candidate_name="Repeated Candidate",
        tag="coldwave",
        tag_rank=40,
        similarity=0.80,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=530,
        candidate_id=532,
        candidate_name="Fresh Candidate",
        tag="coldwave",
        tag_rank=41,
        similarity=0.78,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000530")
    conn.execute(
        """
        INSERT INTO recommendation (
            run_id, rank, artist_id, final_score, raw_score, explanation
        )
        VALUES (
            '00000000-0000-0000-0000-000000000530',
            1, 531, 0.9, 0.9, '{}'
        )
        """
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == [
        "Fresh Candidate",
        "Repeated Candidate",
    ]
    (explanation_json,) = conn.execute(
        "SELECT explanation FROM recommendation WHERE run_id = ? AND artist_id = 531",
        [stats.run_id],
    ).fetchone()
    explanation = json.loads(explanation_json)
    assert explanation["fatigue"]["prior_run_appearances"] == 1
    assert explanation["fatigue"]["latest_run_rank"] == 1
    assert explanation["fatigue"]["penalty"] > 0.0
    conn.close()


def test_generate_recommendations_positive_feedback_bypasses_fatigue(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=540,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=540, tag="coldwave", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=540,
        candidate_id=541,
        candidate_name="Saved Repeated Candidate",
        tag="coldwave",
        tag_rank=40,
        similarity=0.80,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=540,
        candidate_id=542,
        candidate_name="Fresh Candidate",
        tag="coldwave",
        tag_rank=41,
        similarity=0.78,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000540")
    conn.execute(
        """
        INSERT INTO recommendation (
            run_id, rank, artist_id, final_score, raw_score, explanation
        )
        VALUES (
            '00000000-0000-0000-0000-000000000540',
            1, 541, 0.9, 0.9, '{}'
        )
        """
    )
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000540",
        artist_id=541,
        action="save",
        feedback_id="00000000-0000-0000-0000-000000000541",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id)[0] == "Saved Repeated Candidate"
    (explanation_json,) = conn.execute(
        "SELECT explanation FROM recommendation WHERE run_id = ? AND artist_id = 541",
        [stats.run_id],
    ).fetchone()
    explanation = json.loads(explanation_json)
    assert explanation["fatigue"]["penalty"] == 0.0
    conn.close()


def test_generate_recommendations_respects_hide_artist_across_runs(
    tmp_path: Path,
) -> None:
    """Hidden artists from any run must be excluded from new recommendation runs."""
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=600,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=600, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=600,
        candidate_id=601,
        candidate_name="Hidden Artist",
        tag="post-punk",
        tag_rank=30,
        similarity=0.85,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=600,
        candidate_id=602,
        candidate_name="Visible Artist",
        tag="post-punk",
        tag_rank=31,
        similarity=0.83,
    )
    # Record hide_artist from a previous run (different run_id)
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000600")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000600",
        artist_id=601,
        action="hide_artist",
        feedback_id="00000000-0000-0000-0000-000000000601",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    names = _recommendation_names(conn, stats.run_id)
    assert "Hidden Artist" not in names
    assert "Visible Artist" in names
    conn.close()


def test_generate_recommendations_less_like_this_suppresses_across_runs(
    tmp_path: Path,
) -> None:
    """less_like_this from any run must meaningfully suppress the artist in new runs."""
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=610,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=610, tag="post-punk", weight=100.0)
    # Two similar candidates — one suppressed
    _insert_candidate_from_seed(
        conn,
        seed_id=610,
        candidate_id=611,
        candidate_name="Suppressed Artist",
        tag="post-punk",
        tag_rank=30,
        similarity=0.85,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=610,
        candidate_id=612,
        candidate_name="Unsullied Artist",
        tag="post-punk",
        tag_rank=31,
        similarity=0.84,
    )
    # Record less_like_this with relevant tags so tag penalty also applies
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000610")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000610",
        artist_id=611,
        action="less_like_this",
        context={"tags": ["post-punk"]},
        feedback_id="00000000-0000-0000-0000-000000000611",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    names = _recommendation_names(conn, stats.run_id)
    # The unsullied artist should rank ABOVE the suppressed one
    # (both still appear since limit=2 and only 2 candidates)
    assert names[0] == "Unsullied Artist"
    assert names[1] == "Suppressed Artist"

    # Verify the suppressed artist has a negative feedback score component
    (feedback_score,) = conn.execute(
        """
        SELECT feedback_score
        FROM recommendation
        WHERE run_id = ? AND artist_id = 611
        """,
        [stats.run_id],
    ).fetchone()
    assert feedback_score < -0.3  # Should be substantially negative now
    conn.close()


def test_recommendation_candidate_migration_creates_table():
    from pathlib import Path

    import duckdb

    conn = duckdb.connect(":memory:")
    migration_path = Path("src/scrobble_recs/sql/008_recommendation_candidate.sql")
    conn.execute(migration_path.read_text(encoding="utf-8"))

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "recommendation_candidate" in tables
    conn.close()


def test_generate_recommendations_persists_candidates(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
        run_lastfm_enrichment(conn, config, client=RecommendationFakeLastFmClient())

        stats = generate_recommendations(
            conn,
            config,
            limit=3,
            mode="balanced",
            as_of=datetime(2026, 6, 4),
        )

        rows = conn.execute(
            "SELECT count(*) FROM recommendation_candidate WHERE run_id = CAST(? AS UUID)",
            [stats.run_id],
        ).fetchone()
        assert int(rows[0]) > 0
    finally:
        conn.close()
