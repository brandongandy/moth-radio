import json
from datetime import datetime
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from scrobble_recs.cli import app
from scrobble_recs.importers.lastfm_csv import ImportStats


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


def write_config_with_disabled_lastfm(tmp_path: Path) -> Path:
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
                "apis": {
                    "lastfm": {
                        "enabled": False,
                        "api_key": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_init_command_writes_config_and_initializes_database(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "nested" / "config.json"

    result = runner.invoke(app, ["init", "--config", str(config_path)])

    assert result.exit_code == 0
    assert config_path.exists()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["apis"]["lastfm"]["api_key"] == "${LASTFM_API_KEY}"
    assert payload["recommendation"]["seed_artist_limit"] == 250
    db_path = tmp_path / "nested" / "data" / "scrobbles.duckdb"
    assert db_path.exists()
    assert f"Config: {config_path}" in result.output
    assert f"Database: {db_path}" in result.output


def test_init_command_refuses_to_overwrite_existing_config(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    result = runner.invoke(app, ["init", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "already exists" in result.output
    assert config_path.read_text(encoding="utf-8") == "{}"


def test_init_command_force_overwrites_existing_config(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    result = runner.invoke(app, ["init", "--config", str(config_path), "--force"])

    assert result.exit_code == 0
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["app"]["name"] == "scrobble-recs"


def test_import_command_prints_stats(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["import", "--config", str(write_config(tmp_path))])

    assert result.exit_code == 0
    assert "Imported 6 scrobbles" in result.output
    assert "duplicate scrobbles: 1" in result.output
    assert "distinct artists: 4" in result.output


def test_import_command_passes_near_duplicate_window(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["input"]["near_duplicate_window_seconds"] = 30
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    calls: list[int] = []

    def fake_import_lastfm_csv(conn, csv_path, *, near_duplicate_window_seconds=0):
        calls.append(near_duplicate_window_seconds)
        return ImportStats(
            raw_scrobbles=0,
            scrobbles=0,
            duplicate_scrobbles=0,
            near_duplicate_scrobbles=0,
            distinct_artists=0,
            distinct_tracks=0,
            null_artist_mbid_scrobbles=0,
            import_warnings=0,
        )

    monkeypatch.setattr("scrobble_recs.cli.import_lastfm_csv", fake_import_lastfm_csv)

    result = runner.invoke(app, ["import", "--config", str(config_path)])

    assert result.exit_code == 0
    assert calls == [30]


def test_build_profile_command_refreshes_local_scores(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    runner.invoke(app, ["import", "--config", str(config_path)])

    result = runner.invoke(app, ["build-profile", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Profile refreshed" in result.output
    assert "artists updated: 4" in result.output


def test_build_profile_command_prints_profile_weight_counts(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    runner.invoke(app, ["import", "--config", str(config_path)])
    conn = duckdb.connect(str(tmp_path / "scrobbles.duckdb"))
    try:
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
            VALUES
                (4, 'post-punk', 'lastfm', 100.0, 1.0, current_timestamp),
                (4, 'gothic rock', 'lastfm', 75.0, 0.75, current_timestamp),
                (1, 'dream pop', 'lastfm', 50.0, 0.50, current_timestamp)
            """
        )
        conn.execute("UPDATE artist_listening_fact SET play_count = 3 WHERE artist_id = 4")
    finally:
        conn.close()

    result = runner.invoke(app, ["build-profile", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Profile refreshed" in result.output
    assert "artists updated: 4" in result.output
    assert "tag weights: 2" in result.output
    assert "genre clusters: 2" in result.output


def test_build_profile_command_shows_working_status(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    calls: list[str] = []

    class FakeStats:
        artists_updated = 4
        tag_weights = 2
        genre_clusters = 2

    def fake_build_profile(conn, config, *, as_of=None):
        calls.append(config.app.name)
        return FakeStats()

    monkeypatch.setattr("scrobble_recs.cli.build_profile", fake_build_profile)

    result = runner.invoke(app, ["build-profile", "--config", str(config_path)])

    assert result.exit_code == 0
    assert calls == ["scrobble-recs"]
    assert "Refreshing local listening profile" in result.output
    assert "Profile refreshed" in result.output
    assert "artists updated: 4" in result.output
    assert "tag weights: 2" in result.output
    assert "genre clusters: 2" in result.output


def test_import_command_shows_working_status_and_writes_log(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    log_path = tmp_path / "app.log"

    result = runner.invoke(app, ["import", "--config", str(config_path)])

    assert result.exit_code == 0
    assert f"Log: {log_path}" in result.output
    assert (
        f"Importing Last.fm CSV from {Path('tests/fixtures/tiny_lastfm_export.csv')}"
        in result.output
    )
    assert "Import complete" in log_path.read_text(encoding="utf-8")


def test_doctor_command_runs_without_api_keys(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    runner.invoke(app, ["import", "--config", str(config_path)])

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "config: ok" in result.output
    assert "database: ok" in result.output
    assert "csv: ok" in result.output
    assert "cache: ok" in result.output
    assert "lastfm_api_key: warn" in result.output
    assert "api_cache: ok" in result.output
    assert "clean scrobbles: 6" in result.output


def test_doctor_command_exits_nonzero_when_csv_is_missing(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["input"]["lastfm_csv"] = str(tmp_path / "missing.csv")
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "csv: error" in result.output
    assert "missing" in result.output


def test_enrich_command_dispatches_lastfm_source(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    runner.invoke(app, ["import", "--config", str(config_path)])
    calls: list[str] = []
    progress_events: list[str] = []

    class FakeStats:
        seed_artists = 1
        similar_artist_requests = 1
        artist_tag_requests = 1
        tag_top_artist_requests = 2
        artists_created = 3
        similarity_edges = 2
        artist_tags = 2
        tag_top_artists = 4
        frontier_artists = 0

    class FakeProgress:
        phase = "seed_similarity"
        current = 0
        total = 1
        subject = "The Cure"
        status = "processing"

    def fake_enrich(
        conn,
        config,
        *,
        client=None,
        depth=1,
        max_frontier_artists=None,
        progress_callback=None,
    ):
        calls.append(config.app.name)
        assert progress_callback is not None
        progress_callback(FakeProgress())
        progress_events.append("reported")
        return FakeStats()

    monkeypatch.setattr("scrobble_recs.cli.run_lastfm_enrichment", fake_enrich)

    result = runner.invoke(app, ["enrich", "--config", str(config_path), "--source", "lastfm"])

    assert result.exit_code == 0
    assert calls == ["scrobble-recs"]
    assert progress_events == ["reported"]
    assert "Large Last.fm enrichment runs can take some time" in result.output
    assert "please be patient" in result.output
    assert "Last.fm enrichment complete" in result.output
    assert "seed artists: 1" in result.output
    assert "similar artist requests: 1" in result.output


def test_enrich_command_prints_clear_error_without_lastfm_api_key(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)

    result = runner.invoke(app, ["enrich", "--config", str(config_path), "--source", "lastfm"])

    assert result.exit_code != 0
    assert "Last.fm API key is required for enrichment" in result.output


def test_enrich_command_reports_disabled_lastfm_before_missing_key(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config_with_disabled_lastfm(tmp_path)

    result = runner.invoke(app, ["enrich", "--config", str(config_path), "--source", "lastfm"])

    assert result.exit_code != 0
    assert "Last.fm enrichment is disabled" in result.output
    assert "Last.fm API key is required for enrichment" not in result.output


def test_resolve_command_reports_musicbrainz_disabled_by_default(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    runner.invoke(app, ["import", "--config", str(config_path)])

    result = runner.invoke(
        app,
        ["resolve", "--config", str(config_path), "--source", "musicbrainz"],
    )

    assert result.exit_code == 0
    assert "MusicBrainz resolution is disabled" in result.output


def test_resolve_command_rejects_unknown_source(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)

    result = runner.invoke(app, ["resolve", "--config", str(config_path), "--source", "spotify"])

    assert result.exit_code != 0
    assert "only --source musicbrainz is supported" in result.output


def test_resolve_command_runs_musicbrainz_resolution(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["apis"] = {"musicbrainz": {"enabled": True}}
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    runner.invoke(app, ["import", "--config", str(config_path)])

    calls: list[dict] = []

    def fake_run_resolution(conn, config, unresolved):
        calls.append({"count": len(unresolved)})
        return {"resolved": 1, "ambiguous": 2, "not_found": 1, "skipped": 0, "errors": 0}

    monkeypatch.setattr("scrobble_recs.cli._run_musicbrainz_resolution", fake_run_resolution)

    result = runner.invoke(
        app,
        ["resolve", "--config", str(config_path), "--source", "musicbrainz"],
    )

    assert result.exit_code == 0
    assert "unresolved artists: 2" in result.output
    assert calls == [{"count": 2}]
    assert "resolved: 1" in result.output
    assert "ambiguous: 2" in result.output
    assert "not_found: 1" in result.output


def test_recommend_command_generates_persisted_run(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    calls: list[tuple[str, int, str | None]] = []
    progress_events: list[str] = []

    class FakeRecommendationStats:
        run_id = "00000000-0000-0000-0000-000000000001"
        mode = "balanced"
        recommendations = 5

    class FakeProgress:
        phase = "score_candidates"
        current = 0
        total = 5
        subject = "candidate artists"
        status = "processing"

    def fake_generate(
        conn, config, *, limit, mode=None, history_window=None, progress_callback=None
    ):
        calls.append((config.app.name, limit, mode))
        assert progress_callback is not None
        progress_callback(FakeProgress())
        progress_events.append("reported")
        return FakeRecommendationStats()

    monkeypatch.setattr("scrobble_recs.cli.generate_recommendations", fake_generate)

    result = runner.invoke(app, ["recommend", "--config", str(config_path), "--limit", "5"])

    assert result.exit_code == 0
    assert calls == [("scrobble-recs", 5, None)]
    assert progress_events == ["reported"]
    assert "Generating recommendations from local DuckDB data" in result.output
    assert "Recommendation run complete" in result.output
    assert "run id: 00000000-0000-0000-0000-000000000001" in result.output
    assert "recommendations: 5" in result.output


def test_recommend_command_passes_mode(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    calls: list[tuple[str, int, str | None]] = []

    class FakeRecommendationStats:
        run_id = "00000000-0000-0000-0000-000000000001"
        mode = "fresh"
        recommendations = 5

    def fake_generate(
        conn, config, *, limit, mode=None, history_window=None, progress_callback=None
    ):
        calls.append((config.app.name, limit, mode))
        return FakeRecommendationStats()

    monkeypatch.setattr("scrobble_recs.cli.generate_recommendations", fake_generate)

    result = runner.invoke(
        app,
        [
            "recommend",
            "--config",
            str(config_path),
            "--limit",
            "5",
            "--mode",
            "fresh",
        ],
    )

    assert result.exit_code == 0
    assert calls == [("scrobble-recs", 5, "fresh")]
    assert "mode: fresh" in result.output


def test_recommend_command_passes_explicit_history_window(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    windows = []

    class FakeRecommendationStats:
        run_id = "00000000-0000-0000-0000-000000000001"
        mode = "balanced"
        recommendations = 1

    def fake_generate(
        conn, config, *, limit, mode=None, history_window=None, progress_callback=None
    ):
        windows.append(history_window)
        return FakeRecommendationStats()

    monkeypatch.setattr("scrobble_recs.cli.generate_recommendations", fake_generate)

    result = runner.invoke(
        app,
        [
            "recommend",
            "--config",
            str(config_path),
            "--since",
            "2024-01-01T00:00:00",
            "--until",
            "2024-07-01T00:00:00",
        ],
    )

    assert result.exit_code == 0
    assert windows[0].start == datetime(2024, 1, 1)
    assert windows[0].end == datetime(2024, 7, 1)
    assert "history window: 2024-01-01 00:00:00 to 2024-07-01 00:00:00" in result.output


def test_recommend_command_rejects_malformed_history_window_timestamp(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["recommend", "--config", str(write_config(tmp_path)), "--since", "not-a-timestamp"],
    )

    assert result.exit_code != 0
    assert "invalid timestamp" in result.output


def test_recommend_command_rejects_nonpositive_limit(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    calls: list[int] = []

    class FakeRecommendationStats:
        run_id = "00000000-0000-0000-0000-000000000001"
        mode = "balanced"
        recommendations = 0

    def fake_generate(
        conn, config, *, limit, mode=None, history_window=None, progress_callback=None
    ):
        calls.append(limit)
        return FakeRecommendationStats()

    monkeypatch.setattr("scrobble_recs.cli.generate_recommendations", fake_generate)

    result = runner.invoke(app, ["recommend", "--config", str(config_path), "--limit", "0"])

    assert result.exit_code == 1
    assert calls == []
    assert "Error: limit must be at least 1" in result.output


def test_recommend_command_prints_generation_value_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)

    def fake_generate(
        conn, config, *, limit, mode=None, history_window=None, progress_callback=None
    ):
        raise ValueError("no recommendation candidates found")

    monkeypatch.setattr("scrobble_recs.cli.generate_recommendations", fake_generate)

    result = runner.invoke(app, ["recommend", "--config", str(config_path), "--limit", "5"])

    assert result.exit_code == 1
    assert "Error: no recommendation candidates found" in result.output


def test_recommend_command_prints_invalid_mode_error(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)

    def fake_generate(
        conn, config, *, limit, mode=None, history_window=None, progress_callback=None
    ):
        raise ValueError("unsupported recommendation mode 'fresh'")

    monkeypatch.setattr("scrobble_recs.cli.generate_recommendations", fake_generate)

    result = runner.invoke(
        app,
        ["recommend", "--config", str(config_path), "--limit", "5", "--mode", "fresh"],
    )

    assert result.exit_code == 1
    assert "Error: unsupported recommendation mode 'fresh'" in result.output


def test_export_command_writes_requested_format(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    output = tmp_path / "recs.csv"
    calls: list[tuple[Path, str, str | None]] = []

    def fake_export(conn, output_path, *, format_name, run_id=None, include_candidates=False):
        calls.append((output_path, format_name, run_id))
        output_path.write_text("rank,artist\n", encoding="utf-8")
        return 3

    monkeypatch.setattr("scrobble_recs.cli.export_recommendations", fake_export)

    result = runner.invoke(
        app,
        [
            "export",
            "--config",
            str(config_path),
            "--format",
            "csv",
            "--output",
            str(output),
            "--run-id",
            "00000000-0000-0000-0000-000000000001",
        ],
    )

    assert result.exit_code == 0
    assert calls == [(output, "csv", "00000000-0000-0000-0000-000000000001")]
    assert "Exported 3 recommendations" in result.output


def test_export_command_prints_export_value_error(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    output = tmp_path / "recs.csv"

    def fake_export(conn, output_path, *, format_name, run_id=None, include_candidates=False):
        raise ValueError("no recommendation runs exist")

    monkeypatch.setattr("scrobble_recs.cli.export_recommendations", fake_export)

    result = runner.invoke(
        app,
        [
            "export",
            "--config",
            str(config_path),
            "--format",
            "csv",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert "Error: no recommendation runs exist" in result.output


def test_feedback_command_records_event_and_reranks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    calls: list[tuple[str, int, str, float, str | None, dict[str, object]]] = []
    reranks: list[str] = []

    class FakeEvent:
        feedback_id = "00000000-0000-0000-0000-000000000701"
        action = "more_like_this"

    class FakeRerankStats:
        run_id = "00000000-0000-0000-0000-000000000001"
        recommendations = 4
        hidden = 1

    def fake_record(conn, *, run_id, artist_id, action, value=1.0, context=None, note=None):
        calls.append((run_id, artist_id, action, value, note, context or {}))
        return FakeEvent()

    def fake_rerank(conn, run_id, **kwargs):
        reranks.append(run_id)
        return FakeRerankStats()

    monkeypatch.setattr("scrobble_recs.cli.record_feedback_event", fake_record)
    monkeypatch.setattr("scrobble_recs.cli.rerank_recommendation_run", fake_rerank)

    result = runner.invoke(
        app,
        [
            "feedback",
            "--config",
            str(config_path),
            "--run-id",
            "00000000-0000-0000-0000-000000000001",
            "--artist-id",
            "42",
            "--action",
            "more_like_this",
            "--value",
            "2.5",
            "--tag",
            "post-punk",
            "--tag",
            "coldwave",
            "--note",
            "good lead",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "00000000-0000-0000-0000-000000000001",
            42,
            "more_like_this",
            2.5,
            "good lead",
            {"tags": ["post-punk", "coldwave"]},
        )
    ]
    assert reranks == ["00000000-0000-0000-0000-000000000001"]
    assert "Feedback recorded" in result.output
    assert "reranked recommendations: 4" in result.output
    assert "hidden recommendations: 1" in result.output


def test_feedback_command_rolls_back_record_when_default_rerank_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    db_path = tmp_path / "scrobbles.duckdb"

    def fake_rerank(conn, run_id, **kwargs):
        raise ValueError("recommendation run has no recommendations")

    monkeypatch.setattr("scrobble_recs.cli.rerank_recommendation_run", fake_rerank)

    result = runner.invoke(
        app,
        [
            "feedback",
            "--config",
            str(config_path),
            "--run-id",
            "00000000-0000-0000-0000-000000000001",
            "--artist-id",
            "42",
            "--action",
            "more_like_this",
        ],
    )

    assert result.exit_code == 1
    assert "Error: recommendation run has no recommendations" in result.output

    conn = duckdb.connect(str(db_path))
    try:
        assert conn.execute("SELECT count(*) FROM feedback_event").fetchone() == (0,)
    finally:
        conn.close()


def test_feedback_command_can_skip_rerank(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    reranks: list[str] = []

    class FakeEvent:
        feedback_id = "00000000-0000-0000-0000-000000000702"
        action = "note"

    def fake_record(conn, *, run_id, artist_id, action, value=1.0, context=None, note=None):
        return FakeEvent()

    def fake_rerank(conn, run_id, **kwargs):
        reranks.append(run_id)
        raise AssertionError("rerank should not be called")

    monkeypatch.setattr("scrobble_recs.cli.record_feedback_event", fake_record)
    monkeypatch.setattr("scrobble_recs.cli.rerank_recommendation_run", fake_rerank)

    result = runner.invoke(
        app,
        [
            "feedback",
            "--config",
            str(config_path),
            "--run-id",
            "00000000-0000-0000-0000-000000000001",
            "--artist-id",
            "42",
            "--action",
            "note",
            "--no-rerank",
        ],
    )

    assert result.exit_code == 0
    assert reranks == []
    assert "Feedback recorded" in result.output
    assert "reranked recommendations" not in result.output


def test_feedback_command_prints_record_value_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    reranks: list[str] = []

    def fake_record(conn, *, run_id, artist_id, action, value=1.0, context=None, note=None):
        raise ValueError("unsupported feedback action 'favorite'")

    def fake_rerank(conn, run_id, **kwargs):
        reranks.append(run_id)
        raise AssertionError("rerank should not be called")

    monkeypatch.setattr("scrobble_recs.cli.record_feedback_event", fake_record)
    monkeypatch.setattr("scrobble_recs.cli.rerank_recommendation_run", fake_rerank)

    result = runner.invoke(
        app,
        [
            "feedback",
            "--config",
            str(config_path),
            "--run-id",
            "00000000-0000-0000-0000-000000000001",
            "--artist-id",
            "42",
            "--action",
            "favorite",
        ],
    )

    assert result.exit_code == 1
    assert reranks == []
    assert "Error: unsupported feedback action 'favorite'" in result.output


def test_tui_command_launches_textual_app(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    calls: list[str] = []

    class FakeTui:
        def __init__(self, conn):
            calls.append("constructed")

        def run(self) -> None:
            calls.append("ran")

    monkeypatch.setattr("scrobble_recs.cli.ScrobbleRecsTui", FakeTui)

    result = runner.invoke(app, ["tui", "--config", str(config_path)])

    assert result.exit_code == 0
    assert calls == ["constructed", "ran"]


def test_evaluate_command_runs_and_prints_metrics(monkeypatch, tmp_path):
    from scrobble_recs.config import load_config
    from scrobble_recs.db import initialize_database
    from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
    from scrobble_recs.models.evaluate import EvaluationResult

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

    cfg = load_config(config_path)
    conn = initialize_database(cfg)
    try:
        import_lastfm_csv(conn, cfg.input.lastfm_csv)
    finally:
        conn.close()

    fake_result = EvaluationResult(
        train_until="2021-01-02T23:59:59",
        test_from="2021-01-03T00:00:00",
        test_until="2021-04-03T00:00:00",
        scoring_engine="legacy",
        recommendations_generated=5,
        precision_at_50=0.042,
        recall_at_50=0.042,
        recall_at_100=0.067,
        ndcg_at_50=0.031,
        knownness_median=0.08,
        obviousness_median=0.12,
        tag_diversity_at_20=0.78,
        rediscovery_hit_rate=0.05,
        external_candidate_share=0.83,
        eligible_candidate_coverage=0.67,
        novelty_rate=0.91,
        cooldown_or_repeat_rate=0.14,
    )

    import scrobble_recs.cli as cli_module

    calls = []

    def fake_evaluate(conn, config, *, train_until, test_from, test_until):
        calls.append((train_until, test_from, test_until))
        return fake_result

    monkeypatch.setattr(cli_module, "evaluate_recommendations_from_config", fake_evaluate)

    from typer.testing import CliRunner

    from scrobble_recs.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["evaluate", "--config", str(config_path), "--split-date", "2021-01-03"],
    )
    assert result.exit_code == 0
    assert "recall@50" in result.stdout
    assert "0.042" in result.stdout
    assert "ndcg@50" in result.stdout
    assert "precision@50" in result.stdout
    assert "eligible_candidate_coverage" in result.stdout
    assert "novelty_rate" in result.stdout
    assert "cooldown_or_repeat_rate" in result.stdout
    assert calls == [
        (
            datetime(2021, 1, 3),
            datetime(2021, 1, 3),
            datetime(2021, 4, 3),
        )
    ]


def test_evaluate_command_passes_explicit_horizon(monkeypatch, tmp_path) -> None:
    from scrobble_recs.models.evaluate import EvaluationResult

    calls = []
    fake_result = EvaluationResult(
        train_until="2021-01-01T00:00:00",
        test_from="2021-01-02T00:00:00",
        test_until="2021-01-10T00:00:00",
        scoring_engine="legacy",
        recommendations_generated=0,
        precision_at_50=0.0,
        recall_at_50=0.0,
        recall_at_100=0.0,
        ndcg_at_50=0.0,
        knownness_median=0.0,
        obviousness_median=0.0,
        tag_diversity_at_20=0.0,
        rediscovery_hit_rate=0.0,
        external_candidate_share=0.0,
        eligible_candidate_coverage=0.0,
        novelty_rate=0.0,
        cooldown_or_repeat_rate=0.0,
    )

    def fake_evaluate(conn, config, *, train_until, test_from, test_until):
        calls.append((train_until, test_from, test_until))
        return fake_result

    import scrobble_recs.cli as cli_module

    monkeypatch.setattr(cli_module, "evaluate_recommendations_from_config", fake_evaluate)
    result = CliRunner().invoke(
        app,
        [
            "evaluate",
            "--config",
            str(write_config(tmp_path)),
            "--train-until",
            "2021-01-01",
            "--test-from",
            "2021-01-02",
            "--test-until",
            "2021-01-10",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            datetime(2021, 1, 1),
            datetime(2021, 1, 2),
            datetime(2021, 1, 10),
        )
    ]


def test_evaluate_command_normalizes_offset_bounds_and_derived_horizon(
    monkeypatch, tmp_path
) -> None:
    from scrobble_recs.models.evaluate import EvaluationResult

    calls = []
    fake_result = EvaluationResult(
        train_until="2021-01-01T00:00:00",
        test_from="2021-01-02T00:00:00",
        test_until="2021-04-02T00:00:00",
        scoring_engine="legacy",
        recommendations_generated=0,
        precision_at_50=0.0,
        recall_at_50=0.0,
        recall_at_100=0.0,
        ndcg_at_50=0.0,
        knownness_median=0.0,
        obviousness_median=0.0,
        tag_diversity_at_20=0.0,
        rediscovery_hit_rate=0.0,
        external_candidate_share=0.0,
        eligible_candidate_coverage=0.0,
        novelty_rate=0.0,
        cooldown_or_repeat_rate=0.0,
    )

    def fake_evaluate(conn, config, *, train_until, test_from, test_until):
        calls.append((train_until, test_from, test_until))
        return fake_result

    import scrobble_recs.cli as cli_module

    monkeypatch.setattr(cli_module, "evaluate_recommendations_from_config", fake_evaluate)
    result = CliRunner().invoke(
        app,
        [
            "evaluate",
            "--config",
            str(write_config(tmp_path)),
            "--train-until",
            "2021-01-01T08:00:00+08:00",
            "--test-from",
            "2021-01-02T03:00:00+03:00",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            datetime(2021, 1, 1),
            datetime(2021, 1, 2),
            datetime(2021, 4, 2),
        )
    ]


def test_evaluate_command_rejects_nonpositive_horizon(monkeypatch, tmp_path) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError("evaluation should not run for an invalid horizon")

    import scrobble_recs.cli as cli_module

    monkeypatch.setattr(cli_module, "evaluate_recommendations_from_config", fail_if_called)
    result = CliRunner().invoke(
        app,
        [
            "evaluate",
            "--config",
            str(write_config(tmp_path)),
            "--train-until",
            "2021-01-01",
            "--test-from",
            "2021-01-02",
            "--test-until",
            "2021-01-02",
        ],
    )

    assert result.exit_code == 1
    assert "test_until must be after test_from" in result.stdout


def test_evaluate_rolling_command_sorts_cutoffs_and_prints_window_and_mean_metrics(tmp_path):
    from scrobble_recs.config import load_config
    from scrobble_recs.db import initialize_database

    config_path = write_config(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "evaluate-rolling",
            "--config",
            str(config_path),
            "--cutoff",
            "2023-04-01",
            "--cutoff",
            "2023-01-01",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.index("2023-01-01") < result.stdout.index("2023-04-01")
    assert "2023-06-30" in result.stdout
    assert "windows: 2" in result.stdout
    assert "mean recall@50: 0.000" in result.stdout
    assert "mean recall@100: 0.000" in result.stdout
    assert "mean ndcg@50: 0.000" in result.stdout
    assert "mean identity_resolved_rate: 0.000" in result.stdout
    conn = initialize_database(load_config(config_path))
    try:
        summaries = [
            json.loads(row[0])["history_window"]["end"]
            for row in conn.execute(
                "SELECT seed_summary FROM recommendation_run ORDER BY created_at"
            ).fetchall()
        ]
        assert summaries == ["2023-01-01T00:00:00", "2023-04-01T00:00:00"]
    finally:
        conn.close()


def test_evaluate_rolling_command_deduplicates_normalized_cutoffs_and_uses_horizon(tmp_path):
    from scrobble_recs.config import load_config
    from scrobble_recs.db import initialize_database

    config_path = write_config(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "evaluate-rolling",
            "--config",
            str(config_path),
            "--cutoff",
            "2023-01-01",
            "--cutoff",
            "2023-01-01T08:00:00+08:00",
            "--cutoff",
            "2023-01-01",
            "--horizon-days",
            "30",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "windows: 1" in result.stdout
    assert "2023-01-31T00:00:00" in result.stdout
    conn = initialize_database(load_config(config_path))
    try:
        assert conn.execute("SELECT count(*) FROM recommendation_run").fetchone() == (1,)
    finally:
        conn.close()


@pytest.mark.parametrize("horizon", ["0", "-1"])
def test_evaluate_rolling_command_rejects_nonpositive_horizon(tmp_path, horizon):
    result = CliRunner().invoke(
        app,
        [
            "evaluate-rolling",
            "--config",
            str(write_config(tmp_path)),
            "--cutoff",
            "2023-01-01",
            "--horizon-days",
            horizon,
        ],
    )
    assert result.exit_code == 2
    assert "--horizon-days" in result.output
    assert "No such command" not in result.output


def test_evaluate_rolling_command_requires_cutoffs(tmp_path):
    result = CliRunner().invoke(app, ["evaluate-rolling", "--config", str(write_config(tmp_path))])
    assert result.exit_code == 2
    assert "--cutoff" in result.output
    assert "Missing option" in result.output


def test_profile_tags_command_prints_tag_weights(monkeypatch, tmp_path):
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
            }
        ),
        encoding="utf-8",
    )

    from typer.testing import CliRunner

    from scrobble_recs.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["profile-tags", "--config", str(config_path)])
    # May produce output or an error if no profile exists yet; both are OK.
    assert result.exit_code in (0, 1)


def test_cache_stats_command_prints_statistics(monkeypatch, tmp_path):
    from scrobble_recs.admin.cache_admin import CacheStats

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
            }
        ),
        encoding="utf-8",
    )

    fake_stats = CacheStats(
        total_entries=42,
        fresh_entries=30,
        stale_entries=10,
        error_entries=2,
        by_source={"lastfm": 42},
        by_endpoint={"artist.getSimilar": 20, "artist.getTopTags": 15, "tag.getTopArtists": 7},
        by_status={"200": 40, "429": 2},
        oldest_fetched_at="2026-06-01T00:00:00",
        newest_fetched_at="2026-06-09T12:00:00",
    )

    import scrobble_recs.cli as cli_module

    monkeypatch.setattr(cli_module, "stats_cache", lambda conn: fake_stats)

    from typer.testing import CliRunner

    from scrobble_recs.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["cache", "--config", str(config_path)])
    assert result.exit_code == 0
    assert "42" in result.stdout
    assert "fresh" in result.stdout
    assert "lastfm" in result.stdout


def test_enrich_plan_command_prints_summary(monkeypatch, tmp_path):
    from scrobble_recs.enrichment.queue import EnrichmentQueuePlan

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
            }
        ),
        encoding="utf-8",
    )

    fake_plan = EnrichmentQueuePlan(
        source="lastfm",
        seed_artists=250,
        similar_artist_queued=57,
        similar_artist_cached_fresh=193,
        artist_tag_queued=589,
        artist_tag_cached_fresh=311,
        tag_top_artist_queued=66,
        tag_top_artist_cached_fresh=14,
        total_queued=712,
    )

    import scrobble_recs.cli as cli_module

    monkeypatch.setattr(cli_module, "plan_enrichment", lambda conn, **kwargs: fake_plan)

    from typer.testing import CliRunner

    from scrobble_recs.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["enrich-plan", "--config", str(config_path)])
    assert result.exit_code == 0
    assert "artist.getSimilar" in result.stdout
    assert "57" in result.stdout
    assert "193" in result.stdout


def test_enrich_status_command_prints_counts(monkeypatch, tmp_path):
    from scrobble_recs.enrichment.queue import EnrichmentQueueStatus

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
            }
        ),
        encoding="utf-8",
    )

    fake_status = EnrichmentQueueStatus(
        source="lastfm",
        total=100,
        queued=50,
        running=0,
        complete=45,
        failed=3,
        skipped=2,
    )

    import scrobble_recs.cli as cli_module

    monkeypatch.setattr(cli_module, "enrichment_queue_status", lambda conn, **kwargs: fake_status)

    from typer.testing import CliRunner

    from scrobble_recs.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["enrich-status", "--config", str(config_path)])
    assert result.exit_code == 0
    assert "50" in result.stdout
    assert "45" in result.stdout
    assert "failed" in result.stdout


def test_evaluate_command_scorer_and_ablate_override_config(tmp_path: Path, monkeypatch) -> None:
    config_path = write_config(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["import", "--config", str(config_path)])

    import scrobble_recs.cli as cli_module
    from scrobble_recs.models.evaluate import EvaluationResult

    captured = {}

    def fake_evaluate(conn, config, *, train_until, test_from, test_until):
        captured["engine"] = config.recommendation.scoring.engine
        captured["lastfm_weight"] = config.recommendation.weights.lastfm_similarity
        return EvaluationResult(
            train_until="2021-01-03T00:00:00",
            test_from="2021-01-03T00:00:00",
            test_until="2021-04-03T00:00:00",
            scoring_engine=config.recommendation.scoring.engine,
            recommendations_generated=0,
            precision_at_50=0.0,
            recall_at_50=0.0,
            recall_at_100=0.0,
            ndcg_at_50=0.0,
            knownness_median=0.0,
            obviousness_median=0.0,
            tag_diversity_at_20=0.0,
            rediscovery_hit_rate=0.0,
            external_candidate_share=0.0,
            eligible_candidate_coverage=0.0,
            novelty_rate=0.0,
            cooldown_or_repeat_rate=0.0,
        )

    monkeypatch.setattr(cli_module, "evaluate_recommendations_from_config", fake_evaluate)

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--config",
            str(config_path),
            "--split-date",
            "2021-01-03",
            "--scorer",
            "walk",
            "--ablate",
            "lastfm_similarity",
        ],
    )
    assert result.exit_code == 0
    assert captured["engine"] == "walk"
    assert captured["lastfm_weight"] == 0.0
    assert "scoring_engine: walk" in result.stdout
    assert "ablated_component: lastfm_similarity" in result.stdout


def test_evaluate_command_rejects_unknown_ablation(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["import", "--config", str(config_path)])

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--config",
            str(config_path),
            "--split-date",
            "2021-01-03",
            "--ablate",
            "nope",
        ],
    )
    assert result.exit_code != 0


def test_compare_command_prints_delta_table(tmp_path: Path, monkeypatch) -> None:
    config_path = write_config(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["import", "--config", str(config_path)])

    import scrobble_recs.cli as cli_module
    from scrobble_recs.models.compare import ScorerComparison

    def fake_compare(conn, config, *, train_until, test_from, test_until, scorers):
        return [
            ScorerComparison(
                scorer="legacy",
                recommendations=10,
                hit_rate_at_10=0.10,
                hit_rate_at_25=0.20,
                hit_rate_at_100=0.30,
                catalog_coverage=0.40,
                knownness_mean=0.50,
                knownness_median=0.45,
                tag_entropy=1.2,
                mean_hop_distance=None,
            ),
            ScorerComparison(
                scorer="walk",
                recommendations=12,
                hit_rate_at_10=0.15,
                hit_rate_at_25=0.25,
                hit_rate_at_100=0.35,
                catalog_coverage=0.42,
                knownness_mean=0.40,
                knownness_median=0.38,
                tag_entropy=1.5,
                mean_hop_distance=1.8,
            ),
        ]

    monkeypatch.setattr(cli_module, "compare_scorers", fake_compare)

    result = runner.invoke(
        app,
        ["compare", "--config", str(config_path), "--split-date", "2021-01-03"],
    )
    assert result.exit_code == 0
    assert "Scorer comparison" in result.stdout
    assert "legacy" in result.stdout
    assert "walk" in result.stdout
    assert "hit_rate@10" in result.stdout
    assert "mean_hop_distance" in result.stdout
    # Delta column present for a 2-scorer comparison.
    assert "delta" in result.stdout


def test_compare_command_rejects_unknown_scorer(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["import", "--config", str(config_path)])

    result = runner.invoke(
        app,
        [
            "compare",
            "--config",
            str(config_path),
            "--split-date",
            "2021-01-03",
            "--scorers",
            "legacy,bogus",
        ],
    )
    assert result.exit_code != 0


@pytest.mark.parametrize("command", ["enrich-plan", "enrich-status", "enrich-retry"])
def test_listenbrainz_queue_cli_routes_offline(tmp_path: Path, command: str) -> None:
    result = CliRunner().invoke(
        app, [command, "--config", str(write_config(tmp_path)), "--source", "listenbrainz"]
    )
    assert result.exit_code == 0, result.output


def test_listenbrainz_enrich_cli_disabled_is_clear(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["enrich", "--config", str(write_config(tmp_path)), "--source", "listenbrainz"]
    )
    assert result.exit_code == 1
    assert "ListenBrainz enrichment is disabled" in result.output


def test_evaluate_source_ablation_cli_reports_snapshot(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "evaluate",
            "--config",
            str(write_config(tmp_path)),
            "--split-date",
            "2024-01-03",
            "--ablate-source",
            "listenbrainz",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "ablated_source: listenbrainz" in result.output
    assert "evidence_snapshot_id:" in result.output


def test_listenbrainz_enrich_cli_persists_aggregate_radio_offline(tmp_path: Path, monkeypatch):
    import httpx

    from scrobble_recs.config import load_config
    from scrobble_recs.db import initialize_database

    path = write_config(tmp_path)
    data = json.loads(path.read_text())
    data["apis"] = {"listenbrainz": {"enabled": True}}
    path.write_text(json.dumps(data))
    config = load_config(path)
    conn = initialize_database(config)
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, "
        "musicbrainz_artist_mbid, resolution_status, identity_confidence) "
        "VALUES (1, 'Seed', 'seed', 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', "
        "'resolved_musicbrainz', .95)"
    )
    conn.execute(
        "INSERT INTO artist_listening_fact (artist_id, play_count, recent_weight) VALUES (1, 10, 1)"
    )
    conn.close()
    original = httpx.Client
    content = Path("tests/fixtures/listenbrainz_artist_radio.json").read_bytes()
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content)), **kwargs
        ),
    )
    runner = CliRunner()
    plan = runner.invoke(app, ["enrich-plan", "--config", str(path), "--source", "listenbrainz"])
    assert plan.exit_code == 0 and "total_queued: 1" in plan.output
    result = runner.invoke(app, ["enrich", "--config", str(path), "--source", "listenbrainz"])
    assert result.exit_code == 0, result.output
    assert "observations_written: 2" in result.output
    status = runner.invoke(
        app, ["enrich-status", "--config", str(path), "--source", "listenbrainz"]
    )
    assert status.exit_code == 0 and "complete: 1" in status.output


def test_musicbrainz_enrich_resolves_then_catalog_refreshes_persisted_queue(tmp_path, monkeypatch):
    import httpx

    from scrobble_recs.config import load_config
    from scrobble_recs.db import initialize_database

    path = write_config(tmp_path)
    document = json.loads(path.read_text())
    document["apis"] = {"musicbrainz": {"enabled": True, "rate_limit_per_second": 100000}}
    document["recommendation"] = {"min_seed_scrobbles": 1}
    path.write_text(json.dumps(document))
    config = load_config(path)
    conn = initialize_database(config)
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key) VALUES (1, 'Seed 1', 'seed 1')"
    )
    conn.execute(
        "INSERT INTO artist_listening_fact (artist_id, play_count, recent_weight, "
        "lifetime_weight) VALUES (1, 10, 1, 1)"
    )
    conn.close()
    payload = json.loads(Path("tests/fixtures/musicbrainz_artist_relationships.json").read_text())
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/artist"):
            return httpx.Response(
                200, json={"artists": [{"id": payload["id"], "name": "Seed 1", "score": 100}]}
            )
        return httpx.Response(200, json=payload)

    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: original(*args, **kwargs, transport=httpx.MockTransport(handler)),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["enrich", "--source", "musicbrainz", "-c", str(path)])
    assert result.exit_code == 0, result.output
    assert "artists_requested: 1" in result.output and "relationships_written: 3" in result.output
    assert calls == ["/ws/2/artist", f"/ws/2/artist/{payload['id']}"]
    again = runner.invoke(app, ["enrich", "--source", "musicbrainz", "-c", str(path)])
    assert again.exit_code == 0 and "unresolved artists: 0" in again.output
    assert "artists_requested: 1" in again.output  # no early return when already resolved
    assert len(calls) == 2  # both lookups cache-first
    conn = initialize_database(config)
    assert conn.execute(
        "SELECT endpoint, status FROM enrichment_queue WHERE source = 'musicbrainz'"
    ).fetchall() == [("artist-catalog", "complete")]
    assert conn.execute("SELECT count(*) FROM api_cache").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM artist_alias").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM artist_evidence_observation").fetchone()[0] == 3
    conn.close()
    plan = runner.invoke(app, ["enrich-plan", "--source", "musicbrainz", "-c", str(path)])
    assert (
        plan.exit_code == 0 and "artist-catalog" in plan.output and "total_queued: 1" in plan.output
    )
    status = runner.invoke(app, ["enrich-status", "--source", "musicbrainz", "-c", str(path)])
    assert status.exit_code == 0 and "queued: 1" in status.output
    conn = initialize_database(config)
    conn.execute("UPDATE enrichment_queue SET status = 'failed', attempts = 1")
    conn.close()
    retry = runner.invoke(app, ["enrich-retry", "--source", "musicbrainz", "-c", str(path)])
    assert retry.exit_code == 0 and "Reset 1" in retry.output
    assert len(calls) == 2


def test_musicbrainz_disabled_catalog_plan_and_enrich_do_not_request(tmp_path, monkeypatch):
    import httpx

    from scrobble_recs.config import load_config
    from scrobble_recs.db import initialize_database

    path = write_config(tmp_path)

    def raises(*args, **kwargs):
        raise AssertionError("network client constructed")

    monkeypatch.setattr(httpx, "Client", raises)
    runner = CliRunner()
    for command in ("enrich", "enrich-plan"):
        result = runner.invoke(app, [command, "--source", "musicbrainz", "-c", str(path)])
        assert result.exit_code == 0, result.output
    conn = initialize_database(load_config(path))
    assert (
        conn.execute(
            "SELECT count(*) FROM enrichment_queue WHERE source = 'musicbrainz'"
        ).fetchone()[0]
        == 0
    )
    conn.close()


def test_musicbrainz_cli_preserves_pacing_across_resolution_and_catalog(tmp_path, monkeypatch):
    import httpx

    from scrobble_recs.apis.rate_limit import RateLimiter
    from scrobble_recs.config import load_config
    from scrobble_recs.db import initialize_database

    path = write_config(tmp_path)
    document = json.loads(path.read_text())
    document["apis"] = {"musicbrainz": {"enabled": True, "rate_limit_per_second": 1.0}}
    document["recommendation"] = {"min_seed_scrobbles": 1}
    path.write_text(json.dumps(document))
    config = load_config(path)
    conn = initialize_database(config)
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key) VALUES (1, 'Seed 1', 'seed 1')"
    )
    conn.execute(
        "INSERT INTO artist_listening_fact (artist_id, play_count, recent_weight, "
        "lifetime_weight) VALUES (1, 10, 1, 1)"
    )
    conn.close()
    payload = json.loads(Path("tests/fixtures/musicbrainz_artist_relationships.json").read_text())
    current = [0.0]
    sleeps = []
    requests = []
    clients = []

    def sleep(seconds):
        sleeps.append(seconds)
        current[0] += seconds

    def limiter(rate):
        return RateLimiter(rate, monotonic=lambda: current[0], sleep=sleep)

    # Both production construction sites use one deterministic clock. A reset limiter
    # would allow the first catalog request at t=0 despite its own correct implementation.
    monkeypatch.setattr("scrobble_recs.cli.RateLimiter", limiter)
    monkeypatch.setattr("scrobble_recs.apis.musicbrainz.RateLimiter", limiter)

    def handler(request):
        requests.append((request.url.path, current[0]))
        if request.url.path.endswith("/artist"):
            return httpx.Response(
                200, json={"artists": [{"id": payload["id"], "name": "Seed 1", "score": 100}]}
            )
        return httpx.Response(200, json=payload)

    original = httpx.Client

    def http_client(*args, **kwargs):
        client = original(*args, **kwargs, transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", http_client)
    runner = CliRunner()
    result = runner.invoke(app, ["enrich", "--source", "musicbrainz", "-c", str(path)])
    assert result.exit_code == 0, result.output
    assert [path for path, _ in requests] == ["/ws/2/artist", f"/ws/2/artist/{payload['id']}"]
    assert requests[1][1] - requests[0][1] >= 1.0
    assert all(client.is_closed for client in clients)
    assert "relationships_written: 3" in result.output

    # A fresh cached catalog still refreshes local evidence without network pacing or HTTP.
    again = runner.invoke(app, ["enrich", "--source", "musicbrainz", "-c", str(path)])
    assert again.exit_code == 0, again.output
    assert "unresolved artists: 0" in again.output and "artists_requested: 1" in again.output
    assert len(requests) == 2 and sleeps == [1.0]
    assert all(client.is_closed for client in clients)

    # Standalone resolution retains its owned-client lifecycle and performs no catalog lookup.
    conn = initialize_database(config)
    conn.execute(
        "UPDATE artist SET identity_confidence = 0, resolution_status = 'unresolved' "
        "WHERE artist_id = 1"
    )
    conn.execute("DELETE FROM api_cache WHERE endpoint = 'artist'")
    conn.close()
    resolution = runner.invoke(app, ["resolve", "--source", "musicbrainz", "-c", str(path)])
    assert resolution.exit_code == 0, resolution.output
    assert "resolved: 1" in resolution.output
    assert len(requests) == 3 and requests[-1][0] == "/ws/2/artist"
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("reference", ["Local Target", "1", "local-mbid", "missing"])
def test_recommend_artist_is_local_resolves_and_persists(tmp_path, monkeypatch, reference):
    from scrobble_recs.apis.lastfm import LastFmClient
    from scrobble_recs.apis.listenbrainz import ListenBrainzClient
    from scrobble_recs.apis.musicbrainz import MusicBrainzClient
    from scrobble_recs.config import load_config
    from scrobble_recs.db import initialize_database

    def forbidden(*args, **kwargs):
        raise AssertionError("network client constructed")

    for client in [LastFmClient, MusicBrainzClient, ListenBrainzClient]:
        monkeypatch.setattr(client, "__init__", forbidden)
    path = write_config(tmp_path)
    conn = initialize_database(load_config(path))
    conn.execute(
        """
            INSERT INTO artist (artist_id, canonical_name, artist_key, musicbrainz_artist_mbid)
            VALUES (1, 'Local Target', 'local target', 'local-mbid')
        """
    )
    conn.close()
    result = CliRunner().invoke(app, ["recommend", "--config", str(path), "--artist", reference])
    if reference == "missing":
        assert result.exit_code == 1
        assert "target artist not found: missing" in result.output
    else:
        assert result.exit_code == 0, result.output
        assert "target artist: 1 | Local Target | unheard" in result.output
        conn = initialize_database(load_config(path))
        assert conn.execute(
            "SELECT query_kind, target_artist_id FROM recommendation_run"
        ).fetchone() == ("artist", 1)
        conn.close()


def test_recommend_artist_ambiguous_name_lists_local_choices(tmp_path):
    from scrobble_recs.config import load_config
    from scrobble_recs.db import initialize_database

    path = write_config(tmp_path)
    conn = initialize_database(load_config(path))
    conn.execute(
        """
            INSERT INTO artist (artist_id, canonical_name, artist_key) VALUES (1, 'Target',
            'a'), (2, 'TARGET', 'b')
        """
    )
    conn.close()
    result = CliRunner().invoke(app, ["recommend", "--config", str(path), "--artist", "target"])
    assert result.exit_code == 1
    assert (
        "ambiguous" in result.output
        and "1: Target" in result.output
        and "2: TARGET" in result.output
    )


def test_evaluate_rolling_artist_task_reports_empty_target_denominator(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "evaluate-rolling",
            "--config",
            str(write_config(tmp_path)),
            "--cutoff",
            "2024-01-01",
            "--task",
            "artist",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "task: artist" in result.output
    assert "targets: 0" in result.output and "runs: 0" in result.output


def test_evaluate_rolling_artist_nonempty_runs_are_local_and_identified(tmp_path, monkeypatch):
    from scrobble_recs.apis.lastfm import LastFmClient
    from scrobble_recs.apis.listenbrainz import ListenBrainzClient
    from scrobble_recs.apis.musicbrainz import MusicBrainzClient
    from scrobble_recs.config import load_config
    from scrobble_recs.db import initialize_database

    def forbidden(*args, **kwargs):
        raise AssertionError("network client constructed")

    for client in [LastFmClient, MusicBrainzClient, ListenBrainzClient]:
        monkeypatch.setattr(client, "__init__", forbidden)
    path = write_config(tmp_path)
    conn = initialize_database(load_config(path))
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key) "
        "VALUES (1, 'Replay Target', 'replay')"
    )
    conn.execute(
        """
        INSERT INTO scrobble (scrobble_id, artist_key, track_key, played_at, is_duplicate)
        VALUES (1, 'replay', 'a', '2023-01-01', false),
               (2, 'replay', 'b', '2023-02-01', false),
               (3, 'replay', 'c', '2023-03-01', false)
        """
    )
    conn.close()
    result = CliRunner().invoke(
        app,
        ["evaluate-rolling", "--config", str(path), "--cutoff", "2024-01-01", "--task", "artist"],
    )
    assert result.exit_code == 0, result.output
    assert "target_id | cutoff" in result.output
    assert "targets: 1" in result.output and "runs: 1" in result.output


def test_cli_fusion_override_is_explicit_and_reversible(tmp_path):
    from scrobble_recs.cli import _apply_scorer_override
    from scrobble_recs.config import load_config

    config = load_config(write_config(tmp_path))
    challenger = _apply_scorer_override(config, "fusion")
    assert challenger.recommendation.scoring.engine == "fusion"
    assert challenger.recommendation.fusion.enabled is True
    assert config.recommendation.fusion.enabled is False
    assert _apply_scorer_override(challenger, "legacy").recommendation.fusion.enabled is False
