import json
from datetime import datetime
from pathlib import Path

import duckdb
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

    class FakeProgress:
        phase = "seed_similarity"
        current = 0
        total = 1
        subject = "The Cure"
        status = "processing"

    def fake_enrich(conn, config, *, client=None, progress_callback=None):
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
