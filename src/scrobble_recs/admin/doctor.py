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
        _graph_depth_check(conn),
        _import_stats_check(conn),
        _import_warnings_check(conn),
        _musicbrainz_resolution_check(conn),
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


_DANGLING_WARN_FRACTION = 0.5


def _graph_depth_check(conn: DuckDBPyConnection) -> DiagnosticCheck:
    """Report similarity-graph depth so the walk scorer's reliability is visible.

    A graph enriched only one hop out from seeds leaves every frontier node
    dangling (zero outgoing edges), which collapses a random walk back toward a
    plain weighted average. When that fraction is high, recommend a deeper enrich.
    """
    row = conn.execute(
        """
        WITH nodes AS (
            SELECT source_artist_id AS artist_id
            FROM artist_similarity_edge
            WHERE source = 'lastfm'
            UNION
            SELECT target_artist_id
            FROM artist_similarity_edge
            WHERE source = 'lastfm'
        ),
        sources AS (
            SELECT DISTINCT source_artist_id AS artist_id
            FROM artist_similarity_edge
            WHERE source = 'lastfm'
        )
        SELECT
            (SELECT count(*) FROM nodes) AS node_count,
            (SELECT count(*) FROM artist_similarity_edge WHERE source = 'lastfm') AS edge_count,
            (SELECT count(*) FROM sources) AS nodes_with_outgoing
        """
    ).fetchone()
    node_count = int(row[0]) if row else 0
    edge_count = int(row[1]) if row else 0
    nodes_with_outgoing = int(row[2]) if row else 0

    if node_count == 0:
        return DiagnosticCheck(
            "graph_depth",
            DiagnosticStatus.WARN,
            "no similarity graph yet; run 'enrich' to build one",
        )

    dangling = node_count - nodes_with_outgoing
    dangling_fraction = dangling / node_count
    mean_out_degree = edge_count / node_count
    base_message = (
        f"nodes={node_count}; edges={edge_count}; "
        f"dangling={dangling} ({dangling_fraction:.0%}); "
        f"mean_out_degree={mean_out_degree:.2f}"
    )
    if dangling_fraction >= _DANGLING_WARN_FRACTION:
        return DiagnosticCheck(
            "graph_depth",
            DiagnosticStatus.WARN,
            (
                f"{base_message}; high dangling fraction makes the walk scorer "
                "unreliable -- run a deeper enrich (e.g. --depth 2)"
            ),
        )
    return DiagnosticCheck("graph_depth", DiagnosticStatus.OK, base_message)


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


def _import_warnings_check(conn: DuckDBPyConnection) -> DiagnosticCheck:
    warning_count = get_import_stats(conn).import_warnings
    if warning_count == 0:
        return DiagnosticCheck("import_warnings", DiagnosticStatus.OK, "warnings=0")
    return DiagnosticCheck(
        "import_warnings",
        DiagnosticStatus.WARN,
        f"warnings={warning_count}",
    )


def _musicbrainz_resolution_check(conn: DuckDBPyConnection) -> DiagnosticCheck:
    try:
        rows = conn.execute(
            """
            SELECT status, count(*)
            FROM artist_identity_resolution
            WHERE source = 'musicbrainz'
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
    except Exception:
        return DiagnosticCheck(
            "musicbrainz_resolution",
            DiagnosticStatus.OK,
            "no resolution data",
        )

    if not rows:
        return DiagnosticCheck(
            "musicbrainz_resolution",
            DiagnosticStatus.OK,
            "no resolution data",
        )

    counts = {str(status): int(count) for status, count in rows}
    resolved = counts.get("resolved", 0)
    ambiguous = counts.get("ambiguous", 0)
    not_found = counts.get("not_found", 0)
    total = sum(counts.values())

    if not_found > 0:
        return DiagnosticCheck(
            "musicbrainz_resolution",
            DiagnosticStatus.WARN,
            f"total={total}; resolved={resolved}; ambiguous={ambiguous}; not_found={not_found}",
        )

    return DiagnosticCheck(
        "musicbrainz_resolution",
        DiagnosticStatus.OK,
        f"total={total}; resolved={resolved}; ambiguous={ambiguous}; not_found={not_found}",
    )
