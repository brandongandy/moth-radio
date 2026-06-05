import json
from pathlib import Path

from typer.testing import CliRunner

from scrobble_recs.cli import app


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


def test_import_command_prints_stats(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["import", "--config", str(write_config(tmp_path))])

    assert result.exit_code == 0
    assert "Imported 6 scrobbles" in result.output
    assert "duplicate scrobbles: 1" in result.output
    assert "distinct artists: 4" in result.output


def test_doctor_command_runs_without_api_keys(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    runner.invoke(app, ["import", "--config", str(config_path)])

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Config: ok" in result.output
    assert "Database: ok" in result.output
    assert "API keys: not required for import/doctor" in result.output


def test_enrich_command_dispatches_lastfm_source(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    runner.invoke(app, ["import", "--config", str(config_path)])
    calls: list[str] = []

    class FakeStats:
        seed_artists = 1
        similar_artist_requests = 1
        artist_tag_requests = 1
        tag_top_artist_requests = 2
        artists_created = 3
        similarity_edges = 2
        artist_tags = 2
        tag_top_artists = 4

    def fake_enrich(conn, config, *, client=None):
        calls.append(config.app.name)
        return FakeStats()

    monkeypatch.setattr("scrobble_recs.cli.run_lastfm_enrichment", fake_enrich)

    result = runner.invoke(app, ["enrich", "--config", str(config_path), "--source", "lastfm"])

    assert result.exit_code == 0
    assert calls == ["scrobble-recs"]
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


def test_recommend_command_generates_persisted_run(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    calls: list[tuple[str, int, str | None]] = []

    class FakeRecommendationStats:
        run_id = "00000000-0000-0000-0000-000000000001"
        mode = "balanced"
        recommendations = 5

    def fake_generate(conn, config, *, limit, mode=None):
        calls.append((config.app.name, limit, mode))
        return FakeRecommendationStats()

    monkeypatch.setattr("scrobble_recs.cli.generate_recommendations", fake_generate)

    result = runner.invoke(app, ["recommend", "--config", str(config_path), "--limit", "5"])

    assert result.exit_code == 0
    assert calls == [("scrobble-recs", 5, None)]
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

    def fake_generate(conn, config, *, limit, mode=None):
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


def test_recommend_command_rejects_nonpositive_limit(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    calls: list[int] = []

    class FakeRecommendationStats:
        run_id = "00000000-0000-0000-0000-000000000001"
        mode = "balanced"
        recommendations = 0

    def fake_generate(conn, config, *, limit, mode=None):
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

    def fake_generate(conn, config, *, limit, mode=None):
        raise ValueError("no recommendation candidates found")

    monkeypatch.setattr("scrobble_recs.cli.generate_recommendations", fake_generate)

    result = runner.invoke(app, ["recommend", "--config", str(config_path), "--limit", "5"])

    assert result.exit_code == 1
    assert "Error: no recommendation candidates found" in result.output


def test_recommend_command_prints_invalid_mode_error(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)

    def fake_generate(conn, config, *, limit, mode=None):
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

    def fake_export(conn, output_path, *, format_name, run_id=None):
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

    def fake_export(conn, output_path, *, format_name, run_id=None):
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
    calls: list[tuple[str, int, str, dict[str, object]]] = []
    reranks: list[str] = []

    class FakeEvent:
        feedback_id = "00000000-0000-0000-0000-000000000701"
        action = "more_like_this"

    class FakeRerankStats:
        run_id = "00000000-0000-0000-0000-000000000001"
        recommendations = 4
        hidden = 1

    def fake_record(conn, *, run_id, artist_id, action, value=1.0, context=None, note=None):
        calls.append((run_id, artist_id, action, context or {}))
        return FakeEvent()

    def fake_rerank(conn, run_id):
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
            {"tags": ["post-punk", "coldwave"]},
        )
    ]
    assert reranks == ["00000000-0000-0000-0000-000000000001"]
    assert "Feedback recorded" in result.output
    assert "reranked recommendations: 4" in result.output
    assert "hidden recommendations: 1" in result.output


def test_feedback_command_can_skip_rerank(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    reranks: list[str] = []

    class FakeEvent:
        feedback_id = "00000000-0000-0000-0000-000000000702"
        action = "note"

    def fake_record(conn, *, run_id, artist_id, action, value=1.0, context=None, note=None):
        return FakeEvent()

    def fake_rerank(conn, run_id):
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
