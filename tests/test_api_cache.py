from datetime import datetime, timedelta
from pathlib import Path

import pytest
from duckdb import ConversionException

from scrobble_recs.apis.cache import ApiCache, build_cache_key
from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database


def _write_minimal_config(tmp_path: Path) -> Path:
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
    return config_path


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


def test_build_cache_key_is_stable_for_parameter_order() -> None:
    first = build_cache_key(
        "lastfm",
        "artist.getSimilar",
        {"artist": "The Cure", "limit": 100},
    )
    second = build_cache_key(
        "lastfm",
        "artist.getSimilar",
        {"limit": 100, "artist": "The Cure"},
    )

    assert first == second
    assert first.startswith("lastfm:artist.getSimilar:")


def test_api_cache_returns_fresh_payload_and_ignores_stale_payload(tmp_path: Path) -> None:
    config = load_config(_write_minimal_config(tmp_path))
    conn = initialize_database(config)
    cache = ApiCache(conn)
    now = datetime(2026, 6, 3, 12, 0, 0)
    params = {"artist": "The Cure", "limit": 2}

    cache.write_response(
        source="lastfm",
        endpoint="artist.getSimilar",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params=params,
        response_status=200,
        response_json={"similarartists": {"artist": [{"name": "Siouxsie"}]}},
        fetched_at=now,
        expires_at=now + timedelta(days=180),
        error_message=None,
    )

    assert cache.get_fresh("lastfm", "artist.getSimilar", params, now) == {
        "similarartists": {"artist": [{"name": "Siouxsie"}]}
    }
    assert (
        cache.get_fresh(
            "lastfm",
            "artist.getSimilar",
            params,
            now + timedelta(days=181),
        )
        is None
    )
    conn.close()


def test_failed_cache_replacement_keeps_existing_fresh_payload(tmp_path: Path) -> None:
    config = load_config(_write_minimal_config(tmp_path))
    conn = initialize_database(config)
    cache = ApiCache(conn)
    now = datetime(2026, 6, 3, 12, 0, 0)
    params = {"artist": "The Cure", "limit": 2}
    original_payload = {"similarartists": {"artist": [{"name": "Siouxsie"}]}}

    cache.write_response(
        source="lastfm",
        endpoint="artist.getSimilar",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params=params,
        response_status=200,
        response_json=original_payload,
        fetched_at=now,
        expires_at=now + timedelta(days=180),
        error_message=None,
    )

    with pytest.raises(TypeError):
        cache.write_response(
            source="lastfm",
            endpoint="artist.getSimilar",
            request_url="https://ws.audioscrobbler.com/2.0/",
            request_params=params,
            response_status=200,
            response_json={"similarartists": {"artist": [object()]}},
            fetched_at=now + timedelta(days=1),
            expires_at=now + timedelta(days=181),
            error_message=None,
        )

    assert cache.get_fresh("lastfm", "artist.getSimilar", params, now) == original_payload
    conn.close()


def test_insert_failure_after_delete_rolls_back_existing_fresh_payload(
    tmp_path: Path,
) -> None:
    config = load_config(_write_minimal_config(tmp_path))
    conn = initialize_database(config)
    cache = ApiCache(conn)
    now = datetime(2026, 6, 3, 12, 0, 0)
    params = {"artist": "The Cure", "limit": 2}
    original_payload = {"similarartists": {"artist": [{"name": "Siouxsie"}]}}

    cache.write_response(
        source="lastfm",
        endpoint="artist.getSimilar",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params=params,
        response_status=200,
        response_json=original_payload,
        fetched_at=now,
        expires_at=now + timedelta(days=180),
        error_message=None,
    )

    with pytest.raises(ConversionException):
        cache.write_response(
            source="lastfm",
            endpoint="artist.getSimilar",
            request_url="https://ws.audioscrobbler.com/2.0/",
            request_params=params,
            response_status="not-an-integer",  # type: ignore[arg-type]
            response_json={"similarartists": {"artist": [{"name": "Bauhaus"}]}},
            fetched_at=now + timedelta(days=1),
            expires_at=now + timedelta(days=181),
            error_message=None,
        )

    assert cache.get_fresh("lastfm", "artist.getSimilar", params, now) == original_payload
    conn.close()


def test_api_cache_does_not_return_future_expiring_error_payload(
    tmp_path: Path,
) -> None:
    config = load_config(_write_minimal_config(tmp_path))
    conn = initialize_database(config)
    cache = ApiCache(conn)
    now = datetime(2026, 6, 3, 12, 0, 0)
    params = {"artist": "The Cure", "limit": 2}

    cache.write_response(
        source="lastfm",
        endpoint="artist.getSimilar",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params=params,
        response_status=500,
        response_json={"error": 8, "message": "Operation failed"},
        fetched_at=now,
        expires_at=now + timedelta(days=180),
        error_message="Operation failed",
    )

    assert cache.get_fresh("lastfm", "artist.getSimilar", params, now) is None
    conn.close()
