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

    assert stats.recommendations == 3
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
        "Joy Division",
    ]
    assert rows[0][2] > rows[1][2] > rows[2][2]
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
