import json
from datetime import datetime
from pathlib import Path

from scrobble_recs.admin.doctor import DiagnosticStatus, run_doctor
from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database


def _write_config(
    tmp_path: Path,
    *,
    csv_path: str = "tests/fixtures/tiny_lastfm_export.csv",
) -> Path:
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
                    "lastfm_csv": csv_path,
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


def test_run_doctor_reports_ok_local_state(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    conn = initialize_database(config)
    try:
        result = run_doctor(config, conn)
    finally:
        conn.close()

    checks = {check.name: check for check in result.checks}
    assert result.has_errors is False
    assert checks["config"].status is DiagnosticStatus.OK
    assert checks["database"].status is DiagnosticStatus.OK
    assert checks["csv"].status is DiagnosticStatus.OK
    assert checks["cache"].status is DiagnosticStatus.OK
    assert checks["lastfm_api_key"].status is DiagnosticStatus.WARN
    assert checks["rate_limits"].status is DiagnosticStatus.OK
    assert checks["import_warnings"].status is DiagnosticStatus.OK
    assert checks["import_warnings"].message == "warnings=0"


def test_run_doctor_reports_graph_depth_with_high_dangling_fraction(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    conn = initialize_database(config)
    try:
        conn.execute(
            """
            INSERT INTO artist (artist_id, canonical_name, artist_key)
            VALUES (1, 'A', 'a'), (2, 'B', 'b'), (3, 'C', 'c')
            """
        )
        conn.execute(
            """
            INSERT INTO artist_similarity_edge
                (source_artist_id, target_artist_id, source, source_score, normalized_score)
            VALUES (1, 2, 'lastfm', 0.9, 0.9), (1, 3, 'lastfm', 0.7, 0.7)
            """
        )
        result = run_doctor(config, conn)
    finally:
        conn.close()

    checks = {check.name: check for check in result.checks}
    assert "graph_depth" in checks
    # 3 graph nodes, 2 edges, 2 dangling (B, C) -> 0.67 dangling -> WARN.
    assert checks["graph_depth"].status is DiagnosticStatus.WARN
    assert "nodes=3" in checks["graph_depth"].message
    assert "edges=2" in checks["graph_depth"].message
    assert "dangling" in checks["graph_depth"].message.lower()
    assert "enrich" in checks["graph_depth"].message.lower()


def test_run_doctor_graph_depth_ok_when_frontier_enriched(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    conn = initialize_database(config)
    try:
        conn.execute(
            """
            INSERT INTO artist (artist_id, canonical_name, artist_key)
            VALUES (1, 'A', 'a'), (2, 'B', 'b')
            """
        )
        # Both nodes have outgoing edges -> zero dangling.
        conn.execute(
            """
            INSERT INTO artist_similarity_edge
                (source_artist_id, target_artist_id, source, source_score, normalized_score)
            VALUES (1, 2, 'lastfm', 0.9, 0.9), (2, 1, 'lastfm', 0.8, 0.8)
            """
        )
        result = run_doctor(config, conn)
    finally:
        conn.close()

    checks = {check.name: check for check in result.checks}
    assert checks["graph_depth"].status is DiagnosticStatus.OK


def test_run_doctor_reports_missing_csv_as_error(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, csv_path=str(tmp_path / "missing.csv")))
    conn = initialize_database(config)
    try:
        result = run_doctor(config, conn)
    finally:
        conn.close()

    checks = {check.name: check for check in result.checks}
    assert result.has_errors is True
    assert checks["csv"].status is DiagnosticStatus.ERROR
    assert "missing" in checks["csv"].message


def test_run_doctor_includes_api_cache_summary(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    conn = initialize_database(config)
    try:
        cache = ApiCache(conn)
        cache.write_response(
            source="lastfm",
            endpoint="artist.getSimilar",
            request_url="https://ws.audioscrobbler.com/2.0/",
            request_params={"method": "artist.getSimilar", "artist": "The Cure"},
            response_status=200,
            response_json={"ok": True},
            fetched_at=datetime(2026, 6, 3, 12, 0, 0),
            expires_at=datetime(2026, 6, 4, 12, 0, 0),
            error_message=None,
        )
        result = run_doctor(config, conn, now=datetime(2026, 6, 3, 12, 0, 0))
    finally:
        conn.close()

    checks = {check.name: check for check in result.checks}
    assert checks["api_cache"].status is DiagnosticStatus.OK
    assert "entries=1" in checks["api_cache"].message
    assert "fresh=1" in checks["api_cache"].message


def test_run_doctor_reports_import_warnings(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    conn = initialize_database(config)
    try:
        conn.execute(
            """
            INSERT INTO import_warning (
                warning_id,
                import_row_number,
                warning_code,
                severity,
                message,
                evidence
            )
            VALUES (
                1,
                2,
                'artist_mbid_conflict',
                'warning',
                'artist_key has multiple supplied artist MBIDs',
                '{}'
            )
            """
        )
        result = run_doctor(config, conn)
    finally:
        conn.close()

    checks = {check.name: check for check in result.checks}
    assert checks["import_warnings"].status is DiagnosticStatus.WARN
    assert checks["import_warnings"].message == "warnings=1"
