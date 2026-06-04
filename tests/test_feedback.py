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
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_feedback_migration_creates_event_table(tmp_path: Path) -> None:
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

        assert "feedback_event" in tables
        assert _columns(conn, "feedback_event") == [
            ("feedback_id", "UUID"),
            ("created_at", "TIMESTAMP"),
            ("run_id", "UUID"),
            ("artist_id", "UBIGINT"),
            ("action", "VARCHAR"),
            ("value", "DOUBLE"),
            ("context", "JSON"),
            ("note", "VARCHAR"),
        ]

        conn.execute(
            """
            INSERT INTO feedback_event (
                feedback_id,
                run_id,
                artist_id,
                action,
                value,
                context,
                note
            )
            VALUES (
                '00000000-0000-0000-0000-000000000101',
                '00000000-0000-0000-0000-000000000001',
                10,
                'more_like_this',
                1.0,
                '{"tags":["post-punk"]}',
                'good fit'
            )
            """
        )

        with pytest.raises(ConstraintException):
            conn.execute(
                """
                INSERT INTO feedback_event (
                    feedback_id,
                    run_id,
                    artist_id,
                    action,
                    value,
                    context
                )
                VALUES (
                    '00000000-0000-0000-0000-000000000101',
                    '00000000-0000-0000-0000-000000000001',
                    11,
                    'reject',
                    1.0,
                    '{}'
                )
                """
            )
    finally:
        conn.close()


def _columns(conn, table_name: str) -> list[tuple[str, str]]:
    return [
        (str(column_name), str(data_type))
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
    ]
