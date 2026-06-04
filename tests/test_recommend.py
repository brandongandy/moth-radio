import json
from pathlib import Path

import pytest
from duckdb import ConstraintException

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database


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
