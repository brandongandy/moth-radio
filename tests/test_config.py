import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from scrobble_recs.config import (
    EXPECTED_CSV_COLUMNS,
    DiversityConfig,
    default_config_document,
    load_config,
)
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
        track_columns = {row[0] for row in conn.execute("DESCRIBE track").fetchall()}
        warning_columns = {row[0] for row in conn.execute("DESCRIBE import_warning").fetchall()}
        index_rows = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                """
                SELECT index_name, table_name, is_unique, expressions
                FROM duckdb_indexes()
                WHERE index_name IN (
                    'track_artist_key_natural_key',
                    'import_warning_id_key',
                    'import_warning_code_key',
                    'import_warning_row_key'
                )
                """
            ).fetchall()
        }
    finally:
        conn.close()

    assert tables == {
        "api_cache",
        "artist",
        "artist_listening_fact",
        "artist_similarity_edge",
        "artist_tag",
        "enrichment_queue",
        "enrichment_work",
        "feedback_event",
        "import_warning",
        "profile_cluster",
        "raw_scrobble",
        "recommendation",
        "recommendation_run",
        "scrobble",
        "tag_profile",
        "tag_top_artist",
        "track",
    }
    assert {"artist_key", "album_key", "track_key", "is_duplicate"} <= scrobble_columns
    assert {
        "track_id",
        "artist_id",
        "canonical_title",
        "track_key",
        "musicbrainz_recording_mbid",
        "lastfm_track_name",
        "resolution_status",
        "resolution_confidence",
        "created_at",
        "updated_at",
    } <= track_columns
    assert {
        "warning_id",
        "import_row_number",
        "warning_code",
        "severity",
        "message",
        "evidence",
        "created_at",
    } <= warning_columns
    assert index_rows == {
        "track_artist_key_natural_key": ("track", True, "[artist_id, track_key]"),
        "import_warning_id_key": ("import_warning", True, "[warning_id]"),
        "import_warning_code_key": ("import_warning", False, "[warning_code]"),
        "import_warning_row_key": ("import_warning", False, "[import_row_number]"),
    }


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


def test_load_config_includes_near_duplicate_default(tmp_path: Path) -> None:
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
                    "csv_columns": list(EXPECTED_CSV_COLUMNS),
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.input.near_duplicate_window_seconds == 0


def test_load_config_accepts_and_validates_near_duplicate_window(tmp_path: Path) -> None:
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
                    "csv_columns": list(EXPECTED_CSV_COLUMNS),
                    "near_duplicate_window_seconds": 30,
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.input.near_duplicate_window_seconds == 30

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
                    "csv_columns": list(EXPECTED_CSV_COLUMNS),
                    "near_duplicate_window_seconds": -1,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)

    assert "near_duplicate_window_seconds must be non-negative" in str(exc_info.value)


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


def test_load_config_rejects_invalid_rate_limit_and_ttl(tmp_path: Path) -> None:
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
                    "csv_columns": list(EXPECTED_CSV_COLUMNS),
                },
                "apis": {
                    "lastfm": {
                        "rate_limit_per_second": 0,
                        "cache_ttl_days": -1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)

    message = str(exc_info.value)
    assert "rate_limit_per_second" in message
    assert "cache_ttl_days" in message


def test_load_config_rejects_wrong_csv_contract(tmp_path: Path) -> None:
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
                    "timestamp_column": "played_at",
                    "csv_columns": ["artist", "track"],
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)

    message = str(exc_info.value)
    assert "timestamp_column" in message
    assert "csv_columns" in message


def test_default_config_document_contains_spec_defaults() -> None:
    document = default_config_document()

    assert document["app"]["name"] == "scrobble-recs"
    assert document["input"]["near_duplicate_window_seconds"] == 0
    assert document["apis"]["lastfm"]["api_key"] == "${LASTFM_API_KEY}"
    assert document["apis"]["lastfm"]["rate_limit_per_second"] == 2.0
    assert document["apis"]["musicbrainz"]["rate_limit_per_second"] == 1.0
    assert document["recommendation"]["seed_artist_limit"] == 250
    assert document["recommendation"]["similar_artists_per_seed"] == 100
    assert document["recommendation"]["obviousness"]["top_n_per_tag_hide"] == 3
    assert document["recommendation"]["quality_gate"]["enabled"] is True
    assert document["recommendation"]["quality_gate"]["rediscovery_requires_taste_match"] is True
    assert document["recommendation"]["fatigue"]["enabled"] is True
    assert document["recommendation"]["fatigue"]["lookback_runs"] == 5
    assert "soundtrack" in document["recommendation"]["tag_profile"]["weak_tags"]
    assert "spoken word" in document["recommendation"]["tag_profile"]["weak_tags"]


def test_recommendation_quality_gate_defaults_are_loaded(tmp_path: Path) -> None:
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
                    "csv_columns": list(EXPECTED_CSV_COLUMNS),
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.recommendation.quality_gate.enabled is True
    assert config.recommendation.quality_gate.rediscovery_requires_taste_match is True
    assert config.recommendation.quality_gate.min_lastfm_similarity_score == pytest.approx(0.01)
    assert config.recommendation.quality_gate.min_tag_similarity_score == pytest.approx(0.05)
    assert config.recommendation.quality_gate.min_positive_feedback_score == pytest.approx(0.05)
    assert config.recommendation.quality_gate.rediscovery_mode_multiplier == pytest.approx(0.75)
    assert config.recommendation.fatigue.enabled is True
    assert config.recommendation.fatigue.lookback_runs == 5
    assert config.recommendation.fatigue.latest_run_penalty == pytest.approx(0.12)
    assert config.recommendation.fatigue.repeat_appearance_penalty == pytest.approx(0.04)
    assert "soundtrack" in config.recommendation.tag_profile.weak_tags
    assert "spoken word" in config.recommendation.tag_profile.weak_tags
    assert config.recommendation.tag_profile.weak_tag_penalty == pytest.approx(0.25)


def test_knownness_config_defaults():
    from scrobble_recs.config import KnownnessConfig

    cfg = KnownnessConfig()
    assert cfg.enabled is True
    assert cfg.threshold == 0.65
    assert cfg.penalty_weight == 0.40


def test_knownness_config_rejects_threshold_out_of_range():
    from scrobble_recs.config import KnownnessConfig

    with pytest.raises(ValidationError):
        KnownnessConfig(threshold=1.5)
    with pytest.raises(ValidationError):
        KnownnessConfig(threshold=-0.1)


def test_enrichment_config_defaults():
    from scrobble_recs.config import EnrichmentConfig

    cfg = EnrichmentConfig()
    assert cfg.seed_artist_limit == 250
    assert cfg.candidate_artist_limit == 5000
    assert cfg.max_queue_attempts == 3


def test_lastfm_config_includes_timeout_and_stale_cache():
    from scrobble_recs.config import LastFmApiConfig

    cfg = LastFmApiConfig(api_key="key")
    assert cfg.timeout_seconds == 20.0
    assert cfg.stale_cache_on_error is True


def test_recommendation_config_includes_run_limit_and_knownness():
    from scrobble_recs.config import RecommendationConfig

    cfg = RecommendationConfig()
    assert cfg.run_limit == 500
    assert cfg.knownness.enabled is True
    assert cfg.knownness.threshold == 0.65


def test_full_config_loads_enrichment_and_knownness_sections(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "test",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": "tests/fixtures/tiny_lastfm_export.csv",
                },
                "enrichment": {
                    "seed_artist_limit": 100,
                },
                "recommendation": {
                    "knownness": {
                        "threshold": 0.50,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.enrichment.seed_artist_limit == 100
    assert cfg.recommendation.knownness.threshold == 0.50
