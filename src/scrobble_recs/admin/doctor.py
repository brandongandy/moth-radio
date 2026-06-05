from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from duckdb import DuckDBPyConnection

from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.config import EXPECTED_CSV_COLUMNS, ScrobbleRecsConfig
from scrobble_recs.importers.lastfm_csv import get_import_stats


class DiagnosticStatus(Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    status: DiagnosticStatus
    message: str


@dataclass(frozen=True)
class DoctorResult:
    checks: tuple[DiagnosticCheck, ...]

    @property
    def has_errors(self) -> bool:
        return any(check.status is DiagnosticStatus.ERROR for check in self.checks)


def run_doctor(
    config: ScrobbleRecsConfig,
    conn: DuckDBPyConnection,
    *,
    now: datetime | None = None,
) -> DoctorResult:
    resolved_now = now or datetime.now(UTC).replace(tzinfo=None)
    checks = [
        _config_check(config),
        _database_check(config),
        _csv_check(config.input.lastfm_csv),
        _cache_path_check(config.app.cache_dir),
        _lastfm_api_key_check(config),
        _rate_limit_check(config),
        _api_cache_check(conn, resolved_now),
        _import_stats_check(conn),
    ]
    return DoctorResult(checks=tuple(checks))


def _config_check(config: ScrobbleRecsConfig) -> DiagnosticCheck:
    return DiagnosticCheck("config", DiagnosticStatus.OK, f"name={config.app.name}")


def _database_check(config: ScrobbleRecsConfig) -> DiagnosticCheck:
    db_path = config.app.db_path
    if db_path.exists():
        return DiagnosticCheck("database", DiagnosticStatus.OK, str(db_path))
    return DiagnosticCheck("database", DiagnosticStatus.WARN, f"will be created at {db_path}")


def _csv_check(csv_path: Path) -> DiagnosticCheck:
    if not csv_path.exists():
        return DiagnosticCheck("csv", DiagnosticStatus.ERROR, f"missing at {csv_path}")
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as file:
            reader = csv.reader(file)
            header = next(reader, [])
    except OSError as exc:
        return DiagnosticCheck("csv", DiagnosticStatus.ERROR, f"unreadable: {exc}")
    if header != list(EXPECTED_CSV_COLUMNS):
        return DiagnosticCheck(
            "csv",
            DiagnosticStatus.ERROR,
            f"columns must match {list(EXPECTED_CSV_COLUMNS)}",
        )
    return DiagnosticCheck("csv", DiagnosticStatus.OK, str(csv_path))


def _cache_path_check(cache_dir: Path) -> DiagnosticCheck:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return DiagnosticCheck("cache", DiagnosticStatus.ERROR, f"unwritable: {exc}")
    return DiagnosticCheck("cache", DiagnosticStatus.OK, str(cache_dir))


def _lastfm_api_key_check(config: ScrobbleRecsConfig) -> DiagnosticCheck:
    lastfm = config.apis.lastfm
    if not lastfm.enabled:
        return DiagnosticCheck("lastfm_api_key", DiagnosticStatus.OK, "Last.fm disabled")
    if lastfm.api_key is None:
        return DiagnosticCheck(
            "lastfm_api_key",
            DiagnosticStatus.WARN,
            "missing; import, doctor, TUI, and local recommendation browsing still work",
        )
    return DiagnosticCheck("lastfm_api_key", DiagnosticStatus.OK, "configured")


def _rate_limit_check(config: ScrobbleRecsConfig) -> DiagnosticCheck:
    return DiagnosticCheck(
        "rate_limits",
        DiagnosticStatus.OK,
        (
            f"lastfm={config.apis.lastfm.rate_limit_per_second}/s; "
            f"musicbrainz={config.apis.musicbrainz.rate_limit_per_second}/s"
        ),
    )


def _api_cache_check(conn: DuckDBPyConnection, now: datetime) -> DiagnosticCheck:
    summary = ApiCache(conn).summary(now)
    status = DiagnosticStatus.WARN if summary.error_entries else DiagnosticStatus.OK
    return DiagnosticCheck(
        "api_cache",
        status,
        (
            f"entries={summary.total_entries}; fresh={summary.fresh_successes}; "
            f"stale={summary.stale_successes}; errors={summary.error_entries}"
        ),
    )


def _import_stats_check(conn: DuckDBPyConnection) -> DiagnosticCheck:
    stats = get_import_stats(conn)
    if stats.scrobbles == 0:
        return DiagnosticCheck("import", DiagnosticStatus.WARN, "no imported scrobbles")
    return DiagnosticCheck(
        "import",
        DiagnosticStatus.OK,
        (
            f"scrobbles={stats.scrobbles}; artists={stats.distinct_artists}; "
            f"tracks={stats.distinct_tracks}"
        ),
    )
