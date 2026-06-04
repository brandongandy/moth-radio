import json
from pathlib import Path

import pytest

from scrobble_recs.config import DiversityConfig, load_config
from scrobble_recs.db import initialize_database


def test_load_config_expands_paths_and_environment_variables(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCROBBLE_RECS_TEST_ROOT", str(tmp_path))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": "${SCROBBLE_RECS_TEST_ROOT}/db/scrobbles.duckdb",
                    "cache_dir": "${SCROBBLE_RECS_TEST_ROOT}/cache",
                    "log_path": "${SCROBBLE_RECS_TEST_ROOT}/state/app.log",
                },
                "input": {
                    "lastfm_csv": "${SCROBBLE_RECS_TEST_ROOT}/exports/scrobbles.csv",
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

    config = load_config(config_path)

    assert config.app.db_path == tmp_path / "db" / "scrobbles.duckdb"
    assert config.app.cache_dir == tmp_path / "cache"
    assert config.input.lastfm_csv == tmp_path / "exports" / "scrobbles.csv"


def test_initialize_database_applies_initial_migration(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "state" / "scrobbles.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "state" / "app.log"),
                },
                "input": {
                    "lastfm_csv": str(tmp_path / "exports" / "scrobbles.csv"),
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
    config = load_config(config_path)

    conn = initialize_database(config)

    try:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        scrobble_columns = {row[0] for row in conn.execute("DESCRIBE scrobble").fetchall()}
    finally:
        conn.close()

    assert tables == {
        "api_cache",
        "artist",
        "artist_listening_fact",
        "artist_similarity_edge",
        "artist_tag",
        "enrichment_work",
        "raw_scrobble",
        "recommendation",
        "recommendation_run",
        "scrobble",
        "tag_top_artist",
    }
    assert {"artist_key", "album_key", "track_key", "is_duplicate"} <= scrobble_columns


def test_load_config_includes_lastfm_and_recommendation_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": str(tmp_path / "scrobbles.csv"),
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

    config = load_config(config_path)

    assert config.apis.lastfm.enabled is True
    assert config.apis.lastfm.api_key is None
    assert config.apis.lastfm.base_url == "https://ws.audioscrobbler.com/2.0/"
    assert config.apis.lastfm.rate_limit_per_second == 2.0
    assert config.apis.lastfm.cache_ttl_days == 180
    assert config.apis.musicbrainz.enabled is False
    assert config.apis.listenbrainz.dump_dir == Path("~/datasets/listenbrainz").expanduser()
    assert config.recommendation.seed_artist_limit == 250
    assert config.recommendation.similar_artists_per_seed == 100
    assert config.recommendation.min_seed_scrobbles == 3


def test_lastfm_api_key_interpolates_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LASTFM_API_KEY", "test-key")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": str(tmp_path / "scrobbles.csv"),
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
                "apis": {
                    "lastfm": {
                        "enabled": True,
                        "api_key": "${LASTFM_API_KEY}",
                        "base_url": "https://ws.audioscrobbler.com/2.0/",
                        "rate_limit_per_second": 2.0,
                        "cache_ttl_days": 180,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.apis.lastfm.api_key == "test-key"


def test_diversity_lambda_accepts_json_alias_and_python_field_name(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": str(tmp_path / "scrobbles.csv"),
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
                    "diversity": {
                        "enabled": True,
                        "lambda": 0.42,
                        "max_same_primary_tag_in_top_20": 5,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)
    round_tripped = DiversityConfig.model_validate(DiversityConfig(lambda_=0.61).model_dump())

    assert config.recommendation.diversity.lambda_ == 0.42
    assert round_tripped.lambda_ == 0.61
