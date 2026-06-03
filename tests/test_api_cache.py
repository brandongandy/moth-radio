from pathlib import Path

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database


def test_enrichment_migration_creates_cache_and_normalized_tables(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        f"""
        {{
          "app": {{
            "name": "scrobble-recs",
            "db_path": "{(tmp_path / "db.duckdb").as_posix()}",
            "cache_dir": "{(tmp_path / "cache").as_posix()}",
            "log_path": "{(tmp_path / "app.log").as_posix()}"
          }},
          "input": {{
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
              "track_mbid"
            ]
          }}
        }}
        """,
        encoding="utf-8",
    )
    conn = initialize_database(load_config(config_path))

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

    assert "api_cache" in tables
    assert "artist_similarity_edge" in tables
    assert "artist_tag" in tables
    assert "tag_top_artist" in tables
    conn.close()
