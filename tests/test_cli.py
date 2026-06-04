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
