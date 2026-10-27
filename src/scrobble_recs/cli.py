from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from duckdb import DuckDBPyConnection
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn

from scrobble_recs.admin.cache_admin import (
    expire_cache,
    inspect_cache,
    stats_cache,
)
from scrobble_recs.admin.doctor import DiagnosticCheck, run_doctor
from scrobble_recs.admin.init import initialize_project
from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.apis.musicbrainz import MusicBrainzClient
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import ScrobbleRecsConfig, load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.enrichment.lastfm import (
    LastFmEnrichmentProgress,
    LastFmEnrichmentStats,
    run_lastfm_enrichment,
)
from scrobble_recs.enrichment.listenbrainz import (
    plan_listenbrainz_enrichment,
    run_listenbrainz_enrichment,
)
from scrobble_recs.enrichment.musicbrainz import (
    plan_musicbrainz_catalog_enrichment,
    run_musicbrainz_catalog_enrichment,
)
from scrobble_recs.enrichment.queue import (
    enrichment_queue_status,
    plan_enrichment,
    retry_failed_enrichment,
)
from scrobble_recs.export import ExportFormat, export_recommendations
from scrobble_recs.importers.lastfm_csv import ImportStats, get_import_stats, import_lastfm_csv
from scrobble_recs.models.compare import ScorerComparison, compare_scorers
from scrobble_recs.models.evaluate import (
    ArtistEvaluationResult,
    ArtistRollingEvaluationReport,
    EvaluationResult,
    EvaluationWindow,
    evaluate_recommendations,
    evaluate_rolling_recommendations,
)
from scrobble_recs.models.feedback import record_feedback_event, rerank_recommendation_run
from scrobble_recs.models.profile_builder import build_profile
from scrobble_recs.models.profile_window import (
    ListeningWindow,
    normalize_timestamp,
    resolve_listening_window,
)
from scrobble_recs.models.recommend import (
    RecommendationProgress,
    RecommendationStats,
    generate_recommendations,
)
from scrobble_recs.models.recommendation_query import RecommendationRequest
from scrobble_recs.models.relationship import resolve_target_artist
from scrobble_recs.tui.app import ScrobbleRecsTui

app = typer.Typer(help="Local-first Last.fm recommendation tooling.")
console = Console()
_ENRICHMENT_PHASE_LABELS = {
    "seed_similarity": "Seed similar artists",
    "seed_tags": "Seed artist tags",
    "tag_top_artists": "Tag top artists",
}
_RECOMMENDATION_PHASE_LABELS = {
    "refresh_listening": "Refresh listening scores",
    "select_seeds": "Select seed artists",
    "build_profiles": "Build profiles",
    "load_candidates": "Load candidates",
    "score_candidates": "Score candidates",
    "rerank_diversity": "Rerank diversity",
    "build_explanations": "Build explanations",
    "persist_candidates": "Persist candidates",
    "persist_run": "Persist run",
}


@app.command("init")
def init_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing config file."),
    ] = False,
) -> None:
    try:
        result = initialize_project(config, force=force)
    except FileExistsError as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"Config: {result.config_path}", soft_wrap=True)
    console.print(f"Database: {result.db_path}", soft_wrap=True)
    console.print(f"Cache: {result.cache_dir}", soft_wrap=True)
    console.print(f"Log: {result.log_path}", soft_wrap=True)


@app.command("import")
def import_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
) -> None:
    resolved_config = load_config(config)
    logger = _configure_logging(resolved_config.app.log_path)
    csv_path = resolved_config.input.lastfm_csv
    console.print(f"Log: {resolved_config.app.log_path}", soft_wrap=True)
    console.print(f"Importing Last.fm CSV from {csv_path}", soft_wrap=True)
    logger.info("Starting import from %s", csv_path)

    conn = initialize_database(resolved_config)
    try:
        with console.status(f"Importing Last.fm CSV from {csv_path}...", spinner="dots"):
            stats = import_lastfm_csv(
                conn,
                csv_path,
                near_duplicate_window_seconds=resolved_config.input.near_duplicate_window_seconds,
            )
        logger.info(
            "Import complete: raw_scrobbles=%s clean_scrobbles=%s duplicate_scrobbles=%s",
            stats.raw_scrobbles,
            stats.scrobbles,
            stats.duplicate_scrobbles,
        )
    except Exception:
        logger.exception("Import failed")
        raise
    finally:
        conn.close()

    console.print(f"Imported {stats.scrobbles} scrobbles")
    _print_stats(stats)


@app.command("build-profile")
def build_profile_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
) -> None:
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        console.print("Refreshing local listening profile from local DuckDB data...")
        with console.status("Refreshing local listening profile...", spinner="dots"):
            stats = build_profile(
                conn,
                resolved_config,
                as_of=datetime.now(tz=UTC).replace(tzinfo=None),
            )
    finally:
        conn.close()

    console.print("Profile refreshed")
    console.print(f"artists updated: {stats.artists_updated}")
    console.print(f"tag weights: {stats.tag_weights}")
    console.print(f"genre clusters: {stats.genre_clusters}")


@app.command("profile-tags")
def profile_tags_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
) -> None:
    """Print the user tag profile."""
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        rows = conn.execute(
            """
            SELECT tag, user_weight, artist_count, top_seed_artists
            FROM tag_profile
            WHERE source = 'lastfm'
            ORDER BY user_weight DESC
            """
        ).fetchall()

        if not rows:
            console.print("No tag profile found. Run 'scrobble-recs build-profile' first.")
            return

        import json as _json

        console.print("Tag Profile")
        console.print("")
        for tag, weight, count, top_json in rows:
            top_artists = _json.loads(top_json) if isinstance(top_json, str) else (top_json or [])
            top_names = [entry.get("artist", "?") for entry in top_artists[:3]]
            console.print(
                f"  {tag}: weight={float(weight):.4f} artists={int(count)} "
                f"top={', '.join(top_names)}"
            )
    finally:
        conn.close()


@app.command("doctor")
def doctor_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
) -> None:
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        result = run_doctor(resolved_config, conn)
        stats = get_import_stats(conn)
    finally:
        conn.close()

    for check in result.checks:
        _print_diagnostic_check(check)
    _print_stats(stats)
    if result.has_errors:
        raise typer.Exit(code=1)


@app.command("enrich")
def enrich_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    source: Annotated[str, typer.Option("--source", help="Enrichment source to run.")] = "lastfm",
    depth: Annotated[
        int,
        typer.Option(
            "--depth",
            help="Graph depth to enrich. 1 (default) enriches seeds only; "
            "2 also enriches the hop-1 frontier.",
        ),
    ] = 1,
    max_frontier_artists: Annotated[
        int | None,
        typer.Option(
            "--max-frontier-artists",
            help="Cap on frontier artists enriched during hop expansion.",
        ),
    ] = None,
) -> None:
    if source not in ("lastfm", "musicbrainz", "listenbrainz"):
        raise typer.BadParameter("--source must be 'lastfm', 'musicbrainz', or 'listenbrainz'")
    if depth < 1:
        raise typer.BadParameter("--depth must be at least 1")
    if max_frontier_artists is not None and max_frontier_artists < 0:
        raise typer.BadParameter("--max-frontier-artists must be non-negative")

    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        if source == "lastfm":
            try:
                console.print(
                    "Large Last.fm enrichment runs can take some time; "
                    "Last.fm API calls are rate limited, so please be patient."
                )
                stats = _run_lastfm_enrichment_with_progress(
                    conn,
                    resolved_config,
                    depth=depth,
                    max_frontier_artists=max_frontier_artists,
                )
            except RuntimeError as exc:
                console.print(f"Error: {exc}")
                raise typer.Exit(code=1) from exc
            console.print("Last.fm enrichment complete")
            _print_enrichment_stats(stats)
        elif source == "listenbrainz":
            try:
                radio_stats = run_listenbrainz_enrichment(conn, resolved_config)
            except RuntimeError as exc:
                console.print(f"Error: {exc}")
                raise typer.Exit(code=1) from exc
            console.print("ListenBrainz enrichment complete")
            for label, value in vars(radio_stats).items():
                console.print(f"{label}: {value}")
        else:
            _run_musicbrainz_enrichment(conn, resolved_config)
    finally:
        conn.close()


@app.command("enrich-plan")
def enrich_plan_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    source: Annotated[str, typer.Option("--source", help="Enrichment source.")] = "lastfm",
) -> None:
    """Plan enrichment work without calling the network."""
    if source not in ("lastfm", "musicbrainz", "listenbrainz"):
        raise typer.BadParameter("--source must be 'lastfm', 'musicbrainz', or 'listenbrainz'")

    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        if source == "lastfm":
            plan = plan_enrichment(
                conn,
                source=source,
                seed_artist_limit=resolved_config.enrichment.seed_artist_limit,
                similar_artists_per_seed=resolved_config.enrichment.similar_artists_per_seed,
                top_tags_per_artist=resolved_config.enrichment.top_tags_per_artist,
                tag_top_artists_limit=resolved_config.enrichment.tag_top_artists_limit,
            )
            console.print(f"{source.capitalize()} enrichment plan")
            console.print("")
            console.print("artist.getSimilar:")
            console.print(f"  queued: {plan.similar_artist_queued}")
            console.print(f"  cached fresh: {plan.similar_artist_cached_fresh}")
            console.print(f"  missing: {plan.similar_artist_queued}")
            console.print("")
            console.print("artist.getTopTags:")
            console.print(f"  queued: {plan.artist_tag_queued}")
            console.print(f"  cached fresh: {plan.artist_tag_cached_fresh}")
            console.print(f"  missing: {plan.artist_tag_queued}")
            console.print("")
            console.print("tag.getTopArtists:")
            console.print(f"  queued: {plan.tag_top_artist_queued}")
            console.print(f"  cached fresh: {plan.tag_top_artist_cached_fresh}")
            console.print(f"  missing: {plan.tag_top_artist_queued}")
            console.print("")
            console.print(f"total queued: {plan.total_queued}")
        elif source == "listenbrainz":
            radio_plan = plan_listenbrainz_enrichment(conn, resolved_config)
            console.print("ListenBrainz artist-radio enrichment plan")
            for label, value in vars(radio_plan).items():
                console.print(f"{label}: {value}")
        else:
            _print_musicbrainz_enrichment_plan(conn, resolved_config)
    finally:
        conn.close()


@app.command("enrich-status")
def enrich_status_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    source: Annotated[str, typer.Option("--source", help="Enrichment source.")] = "lastfm",
) -> None:
    """Show enrichment queue status."""
    if source not in ("lastfm", "musicbrainz", "listenbrainz"):
        raise typer.BadParameter("--source must be 'lastfm', 'musicbrainz', or 'listenbrainz'")

    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        status = enrichment_queue_status(conn, source=source)
    finally:
        conn.close()

    console.print(f"{source.capitalize()} enrichment queue status")
    console.print(f"  total: {status.total}")
    console.print(f"  queued: {status.queued}")
    console.print(f"  running: {status.running}")
    console.print(f"  complete: {status.complete}")
    console.print(f"  failed: {status.failed}")
    console.print(f"  skipped: {status.skipped}")


@app.command("enrich-retry")
def enrich_retry_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    source: Annotated[str, typer.Option("--source", help="Enrichment source.")] = "lastfm",
) -> None:
    """Retry failed enrichment queue entries."""
    if source not in ("lastfm", "musicbrainz", "listenbrainz"):
        raise typer.BadParameter("--source must be 'lastfm', 'musicbrainz', or 'listenbrainz'")

    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        count = retry_failed_enrichment(
            conn,
            source=source,
            max_attempts=resolved_config.enrichment.max_queue_attempts,
        )
    finally:
        conn.close()

    console.print(f"Reset {count} failed queue entries to queued")


@app.command("cache")
def cache_stats_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
) -> None:
    """Print API cache statistics."""
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        stats = stats_cache(conn)
    finally:
        conn.close()

    console.print("API Cache Statistics")
    console.print(f"  total entries: {stats.total_entries}")
    console.print(f"  fresh (2xx, not expired): {stats.fresh_entries}")
    console.print(f"  stale (expired): {stats.stale_entries}")
    console.print(f"  errors (non-2xx): {stats.error_entries}")
    if stats.oldest_fetched_at:
        console.print(f"  oldest fetched: {stats.oldest_fetched_at}")
    if stats.newest_fetched_at:
        console.print(f"  newest fetched: {stats.newest_fetched_at}")
    if stats.by_source:
        console.print("  by source:")
        for source, count in stats.by_source.items():
            console.print(f"    {source}: {count}")
    if stats.by_endpoint:
        console.print("  by endpoint:")
        for endpoint, count in stats.by_endpoint.items():
            console.print(f"    {endpoint}: {count}")
    if stats.by_status:
        console.print("  by status:")
        for status, count in stats.by_status.items():
            console.print(f"    {status}: {count}")


@app.command("cache-inspect")
def cache_inspect_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    source: Annotated[
        str | None, typer.Option("--source", help="Filter by source (e.g. lastfm).")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max entries to show.")] = 100,
) -> None:
    """Inspect individual cache entries."""
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        entries = inspect_cache(conn, source=source, limit=limit)
    finally:
        conn.close()

    console.print(f"Cache entries ({len(entries)}):")
    for entry in entries:
        freshness = "fresh" if entry.is_fresh else "stale"
        status = entry.response_status or "err"
        console.print(
            f"  [{freshness}] {entry.source}:{entry.endpoint} "
            f"status={status} fetched={entry.fetched_at} "
            f"expires={entry.expires_at}"
        )


@app.command("cache-expire")
def cache_expire_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    source: Annotated[str | None, typer.Option("--source", help="Filter by source.")] = None,
    older_than_days: Annotated[
        int,
        typer.Option("--older-than-days", help="Remove entries older than this many days."),
    ] = 180,
) -> None:
    """Expire stale cache entries."""
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        removed = expire_cache(conn, source=source, older_than_days=older_than_days)
    finally:
        conn.close()

    console.print(f"Removed {removed} stale cache entries")


@app.command("resolve")
def resolve_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    source: Annotated[
        str,
        typer.Option("--source", help="Resolution source to run."),
    ] = "musicbrainz",
) -> None:
    if source != "musicbrainz":
        raise typer.BadParameter("only --source musicbrainz is supported")

    resolved_config = load_config(config)
    if not resolved_config.apis.musicbrainz.enabled:
        console.print("MusicBrainz resolution is disabled")
        return

    conn = initialize_database(resolved_config)
    try:
        unresolved = conn.execute(
            """
            SELECT artist_id, canonical_name, artist_key, identity_confidence
            FROM artist
            WHERE coalesce(identity_confidence, 0.0) < 0.80
              AND (resolution_status IS NULL
                   OR resolution_status = 'unresolved'
                   OR resolution_status LIKE 'provisional%')
            ORDER BY artist_id
            """
        ).fetchall()

        console.print(f"unresolved artists: {len(unresolved)}")
        if not unresolved:
            return

        resolve_stats = _run_musicbrainz_resolution(conn, resolved_config, unresolved)
    finally:
        conn.close()

    console.print(f"resolved: {resolve_stats['resolved']}")
    console.print(f"ambiguous: {resolve_stats['ambiguous']}")
    console.print(f"not_found: {resolve_stats['not_found']}")
    console.print(f"skipped: {resolve_stats['skipped']}")
    console.print(f"errors: {resolve_stats['errors']}")


def _run_musicbrainz_resolution(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    unresolved: list[Any],
    *,
    client: MusicBrainzClient | None = None,
) -> dict[str, int]:
    stats = {"resolved": 0, "ambiguous": 0, "not_found": 0, "skipped": 0, "errors": 0}
    owns_client = client is None
    if client is None:
        client = MusicBrainzClient(
            config=config.apis.musicbrainz,
            cache=ApiCache(conn),
            rate_limiter=RateLimiter(config.apis.musicbrainz.rate_limit_per_second),
        )

    try:
        for idx, row in enumerate(unresolved, start=1):
            artist_id = int(row[0])
            canonical_name = str(row[1] or "")
            artist_key = str(row[2] or "")
            identity_confidence = float(row[3] or 0.0)

            search_name = canonical_name or artist_key
            if not search_name:
                stats["skipped"] += 1
                continue

            try:
                matches = client.search_artist(search_name, limit=5)
            except Exception:
                stats["errors"] += 1
                if idx % 10 == 0:
                    console.print(f"  progress: {idx}/{len(unresolved)}")
                continue

            if not matches:
                _store_resolution(
                    conn,
                    artist_id=artist_id,
                    source="musicbrainz",
                    status="not_found",
                    search_name=search_name,
                    confidence=0.0,
                )
                _update_artist_resolution(
                    conn,
                    artist_id=artist_id,
                    status="unresolved",
                    confidence=identity_confidence,
                )
                stats["not_found"] += 1
            elif len(matches) == 1 or (len(matches) > 1 and matches[0].score >= 90.0):
                best = matches[0]
                confidence = min(best.score / 100.0, 1.0)
                _store_resolution(
                    conn,
                    artist_id=artist_id,
                    source="musicbrainz",
                    status="resolved",
                    search_name=search_name,
                    external_id=best.mbid,
                    canonical_name=best.name,
                    disambiguation=best.disambiguation,
                    country=best.country,
                    type=best.type,
                    confidence=confidence,
                )
                _update_artist_resolution(
                    conn,
                    artist_id=artist_id,
                    status="resolved_musicbrainz",
                    confidence=confidence,
                    mbid=best.mbid,
                    name=best.name,
                    disambiguation=best.disambiguation,
                    country=best.country,
                )
                stats["resolved"] += 1
            else:
                best = matches[0]
                confidence = min(best.score / 100.0, 1.0)
                _store_resolution(
                    conn,
                    artist_id=artist_id,
                    source="musicbrainz",
                    status="ambiguous",
                    search_name=search_name,
                    external_id=best.mbid,
                    canonical_name=best.name,
                    disambiguation=best.disambiguation,
                    country=best.country,
                    type=best.type,
                    confidence=confidence,
                )
                _update_artist_resolution(
                    conn,
                    artist_id=artist_id,
                    status="ambiguous_musicbrainz",
                    confidence=confidence,
                )
                stats["ambiguous"] += 1

            if idx % 10 == 0:
                console.print(f"  progress: {idx}/{len(unresolved)}")
    finally:
        if owns_client:
            client.close()

    return stats


def _store_resolution(
    conn: DuckDBPyConnection,
    *,
    artist_id: int,
    source: str,
    status: str,
    search_name: str,
    external_id: str | None = None,
    canonical_name: str | None = None,
    disambiguation: str | None = None,
    country: str | None = None,
    type: str | None = None,
    confidence: float = 0.0,
) -> None:
    conn.execute(
        """
        INSERT INTO artist_identity_resolution (
            artist_id, source, external_id, canonical_name,
            disambiguation, country_or_area, type, confidence, status, resolved_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (artist_id, source) DO UPDATE SET
            external_id = excluded.external_id,
            canonical_name = excluded.canonical_name,
            disambiguation = excluded.disambiguation,
            country_or_area = excluded.country_or_area,
            type = excluded.type,
            confidence = excluded.confidence,
            status = excluded.status,
            resolved_at = excluded.resolved_at
        """,
        [
            artist_id,
            source,
            external_id,
            canonical_name,
            disambiguation,
            country,
            type,
            confidence,
            status,
            datetime.now(UTC).replace(tzinfo=None),
        ],
    )


def _update_artist_resolution(
    conn: DuckDBPyConnection,
    *,
    artist_id: int,
    status: str,
    confidence: float,
    mbid: str | None = None,
    name: str | None = None,
    disambiguation: str | None = None,
    country: str | None = None,
) -> None:
    updates = ["resolution_status = ?", "identity_confidence = ?", "updated_at = current_timestamp"]
    params: list[object] = [status, confidence]

    if mbid is not None:
        updates.append("musicbrainz_artist_mbid = ?")
        params.append(mbid)
    if name is not None:
        updates.append("canonical_name = ?")
        params.append(name)
    if disambiguation is not None:
        updates.append("disambiguation = ?")
        params.append(disambiguation)
    if country is not None:
        updates.append("country = ?")
        params.append(country)

    params.append(artist_id)
    conn.execute(
        f"UPDATE artist SET {', '.join(updates)} WHERE artist_id = ?",
        params,
    )


def _print_musicbrainz_enrichment_plan(
    conn: DuckDBPyConnection,
    resolved_config: ScrobbleRecsConfig,
) -> None:
    unresolved_row = conn.execute(
        """
        SELECT count(*) FROM artist
        WHERE coalesce(identity_confidence, 0.0) < 0.80
          AND (resolution_status IS NULL
               OR resolution_status = 'unresolved'
               OR resolution_status LIKE 'provisional%')
        """
    ).fetchone()
    unresolved_count = int(unresolved_row[0]) if unresolved_row else 0

    resolved_row = conn.execute(
        """
        SELECT count(*) FROM artist_identity_resolution
        WHERE source = 'musicbrainz'
        """
    ).fetchone()
    resolved_count = int(resolved_row[0]) if resolved_row else 0

    console.print("Musicbrainz enrichment plan")
    console.print(f"  unresolved artists: {unresolved_count}")
    console.print(f"  previously resolved: {resolved_count}")
    plan = plan_musicbrainz_catalog_enrichment(conn, resolved_config)
    console.print("artist-catalog:")
    for label, value in vars(plan).items():
        console.print(f"  {label}: {value}")


def _run_musicbrainz_enrichment(
    conn: DuckDBPyConnection,
    resolved_config: ScrobbleRecsConfig,
) -> None:
    if not resolved_config.apis.musicbrainz.enabled:
        console.print("MusicBrainz enrichment is disabled in config")
        return

    unresolved = conn.execute(
        """
        SELECT artist_id, canonical_name, artist_key, identity_confidence
        FROM artist
        WHERE coalesce(identity_confidence, 0.0) < 0.80
          AND (resolution_status IS NULL
               OR resolution_status = 'unresolved'
               OR resolution_status LIKE 'provisional%')
        ORDER BY identity_confidence NULLS FIRST, artist_id
        """
    ).fetchall()

    client = MusicBrainzClient(
        config=resolved_config.apis.musicbrainz,
        cache=ApiCache(conn),
        rate_limiter=RateLimiter(resolved_config.apis.musicbrainz.rate_limit_per_second),
    )
    try:
        console.print(f"unresolved artists: {len(unresolved)}")
        if unresolved:
            resolve_stats = _run_musicbrainz_resolution(
                conn, resolved_config, unresolved, client=client
            )
            for label, value in resolve_stats.items():
                console.print(f"{label}: {value}")
        console.print("MusicBrainz catalog lookup (bounded resolved artists)")
        stats = run_musicbrainz_catalog_enrichment(
            conn,
            resolved_config,
            client=client,
            progress=lambda done, total: console.print(f"  catalog progress: {done}/{total}"),
        )
        for label, value in vars(stats).items():
            console.print(f"{label}: {value}")
    finally:
        client.close()


def _apply_scorer_override(config: ScrobbleRecsConfig, scorer: str | None) -> ScrobbleRecsConfig:
    """Return a config whose scoring engine is overridden by ``--scorer``."""
    if scorer is None:
        return config
    if scorer not in ("legacy", "walk"):
        raise typer.BadParameter("--scorer must be 'legacy' or 'walk'")
    scoring = config.recommendation.scoring.model_copy(update={"engine": scorer})
    recommendation = config.recommendation.model_copy(update={"scoring": scoring})
    return config.model_copy(update={"recommendation": recommendation})


_WEIGHT_ABLATIONS = {
    "lastfm_similarity": "lastfm_similarity",
    "tag_similarity": "tag_similarity",
    "rediscovery": "rediscovery",
    "feedback": "feedback_model",
}
_TOGGLE_ABLATIONS = ("serendipity", "obviousness")


def _apply_ablation(config: ScrobbleRecsConfig, component: str | None) -> ScrobbleRecsConfig:
    """Return a config with one scoring component zeroed, for A/B ablation."""
    if component is None:
        return config
    recommendation = config.recommendation
    if component in _WEIGHT_ABLATIONS:
        weights = recommendation.weights.model_copy(update={_WEIGHT_ABLATIONS[component]: 0.0})
        recommendation = recommendation.model_copy(update={"weights": weights})
    elif component == "serendipity":
        serendipity = recommendation.serendipity.model_copy(update={"enabled": False})
        recommendation = recommendation.model_copy(update={"serendipity": serendipity})
    elif component == "obviousness":
        obviousness = recommendation.obviousness.model_copy(update={"enabled": False})
        recommendation = recommendation.model_copy(update={"obviousness": obviousness})
    else:
        allowed = ", ".join([*_WEIGHT_ABLATIONS, *_TOGGLE_ABLATIONS])
        raise typer.BadParameter(f"--ablate must be one of: {allowed}")
    return config.model_copy(update={"recommendation": recommendation})


@app.command("recommend")
def recommend_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    limit: Annotated[
        int,
        typer.Option("--limit", help="Number of recommendations to persist."),
    ] = 500,
    mode: Annotated[str | None, typer.Option("--mode", help="Recommendation mode.")] = None,
    artist: Annotated[
        str | None, typer.Option("--artist", help="Local target MBID, artist ID, or exact name.")
    ] = None,
    scorer: Annotated[
        str | None,
        typer.Option("--scorer", help="Scoring engine: 'legacy' or 'walk'."),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Start of listening history (ISO timestamp)."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="End of listening history (ISO timestamp)."),
    ] = None,
) -> None:
    if limit < 1:
        console.print("Error: limit must be at least 1")
        raise typer.Exit(code=1)

    resolved_config = _apply_scorer_override(load_config(config), scorer)
    try:
        history_window = resolve_listening_window(
            as_of=datetime.now(tz=UTC).replace(tzinfo=None),
            default_months=resolved_config.recommendation.profile_window_months,
            since=_parse_timestamp(since) if since is not None else None,
            until=_parse_timestamp(until) if until is not None else None,
        )
    except (typer.BadParameter, ValueError) as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1) from exc
    conn = initialize_database(resolved_config)
    try:
        try:
            request = (
                RecommendationRequest("artist", resolve_target_artist(conn, artist))
                if artist is not None
                else None
            )
            console.print("Generating recommendations from local DuckDB data...")
            stats = _generate_recommendations_with_progress(
                conn,
                resolved_config,
                limit=limit,
                mode=mode,
                history_window=history_window,
                **({"request": request} if request is not None else {}),
            )
            if request is not None:
                target = conn.execute(
                    """
                        SELECT a.canonical_name, r.query_context FROM recommendation_run r JOIN
                        artist a ON a.artist_id = r.target_artist_id WHERE r.run_id = ?
                    """,
                    [stats.run_id],
                ).fetchone()
                assert target is not None
                state = json.loads(target[1])["target_relationship"]["state"]
                console.print(
                    f"target artist: {request.target_artist_id} | "
                    f"{escape(str(target[0]))} | {state}"
                )
        except ValueError as exc:
            console.print(f"Error: {exc}")
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    console.print("Recommendation run complete")
    _print_recommendation_stats(stats)
    console.print(f"history window: {history_window.start} to {history_window.end}")


@app.command("export")
def export_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Output file path.")],
    format_name: Annotated[
        str,
        typer.Option("--format", help="Export format: csv or jsonl."),
    ] = "csv",
    run_id: Annotated[str | None, typer.Option("--run-id", help="Recommendation run UUID.")] = None,
    include_candidates: Annotated[
        bool,
        typer.Option("--include-candidates", help="Include candidate_only (zero-score) rows."),
    ] = False,
) -> None:
    if format_name not in {"csv", "jsonl"}:
        raise typer.BadParameter("format must be csv or jsonl")

    resolved_format = cast(ExportFormat, format_name)
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        try:
            exported = export_recommendations(
                conn,
                output,
                format_name=resolved_format,
                run_id=run_id,
                include_candidates=include_candidates,
            )
        except ValueError as exc:
            console.print(f"Error: {exc}")
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    console.print(f"Exported {exported} recommendations to {output}")


@app.command("tui")
def tui_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
) -> None:
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        ScrobbleRecsTui(conn).run()
    finally:
        conn.close()


@app.command("evaluate")
def evaluate_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    split_date: Annotated[
        str | None,
        typer.Option("--split-date", help="Date to split train/test (YYYY-MM-DD)."),
    ] = None,
    train_until: Annotated[
        str | None,
        typer.Option("--train-until", help="End of training period (YYYY-MM-DD or ISO timestamp)."),
    ] = None,
    test_from: Annotated[
        str | None,
        typer.Option("--test-from", help="Start of test period (YYYY-MM-DD or ISO timestamp)."),
    ] = None,
    test_until: Annotated[
        str | None,
        typer.Option("--test-until", help="End of test period (YYYY-MM-DD or ISO timestamp)."),
    ] = None,
    scorer: Annotated[
        str | None,
        typer.Option("--scorer", help="Scoring engine: 'legacy' or 'walk'."),
    ] = None,
    ablate: Annotated[
        str | None,
        typer.Option("--ablate", help="Zero one scoring component and re-run."),
    ] = None,
    ablate_source: Annotated[
        str | None,
        typer.Option(
            "--ablate-source", help="Measure without ListenBrainz on the same persisted snapshot."
        ),
    ] = None,
) -> None:
    """Evaluate recommendation quality using a train/test split."""
    from datetime import timedelta

    if ablate_source not in (None, "listenbrainz"):
        raise typer.BadParameter("--ablate-source must be listenbrainz")
    resolved_config = _apply_ablation(_apply_scorer_override(load_config(config), scorer), ablate)
    conn = initialize_database(resolved_config)
    try:
        # Resolve split dates.
        if train_until and test_from:
            train_dt = _parse_timestamp(train_until)
            test_dt = _parse_timestamp(test_from)
        elif split_date:
            train_dt = _parse_timestamp(split_date)
            test_dt = train_dt
        else:
            console.print("Error: provide --split-date or both --train-until and --test-from")
            raise typer.Exit(code=1)
        test_end_dt = _parse_timestamp(test_until) if test_until else test_dt + timedelta(days=90)
        if test_dt < train_dt:
            console.print("Error: test_from must not be before train_until")
            raise typer.Exit(code=1)
        if test_end_dt <= test_dt:
            console.print("Error: test_until must be after test_from")
            raise typer.Exit(code=1)

        console.print("Generating evaluation recommendations from local DuckDB data...")
        evaluation_options = {"ablate_source": ablate_source} if ablate_source else {}
        result = evaluate_recommendations_from_config(
            conn,
            resolved_config,
            train_until=train_dt,
            test_from=test_dt,
            test_until=test_end_dt,
            **evaluation_options,
        )
    finally:
        conn.close()

    console.print("")
    console.print("Evaluation")
    console.print("")
    console.print(f"scoring_engine: {result.scoring_engine}")
    if ablate:
        console.print(f"ablated_component: {ablate}")
    if ablate_source:
        console.print(f"ablated_source: {ablate_source}")
    console.print(f"evidence_snapshot_id: {result.evidence_snapshot_id}")
    console.print(f"candidate_count_by_source: {result.candidate_count_by_source}")
    console.print(f"future_hit_count_by_source: {result.future_hit_count_by_source}")
    console.print(f"unique_candidate_count_by_source: {result.unique_candidate_count_by_source}")
    console.print(f"unique_future_hit_count_by_source: {result.unique_future_hit_count_by_source}")
    console.print(f"train_until: {result.train_until}")
    console.print(f"test_from: {result.test_from}")
    console.print(f"test_until: {result.test_until}")
    console.print("")
    console.print(f"precision@50: {result.precision_at_50:.3f}")
    console.print(f"recall@50: {result.recall_at_50:.3f}")
    console.print(f"recall@100: {result.recall_at_100:.3f}")
    console.print(f"ndcg@50: {result.ndcg_at_50:.3f}")
    console.print(f"tag_diversity@20: {result.tag_diversity_at_20:.3f}")
    console.print(f"knownness median: {result.knownness_median:.3f}")
    console.print(f"obviousness median: {result.obviousness_median:.3f}")
    console.print(f"rediscovery_hit_rate: {result.rediscovery_hit_rate:.3f}")
    console.print(f"external_candidate_share: {result.external_candidate_share:.3f}")
    console.print(f"eligible_candidate_coverage: {result.eligible_candidate_coverage:.3f}")
    console.print(f"novelty_rate: {result.novelty_rate:.3f}")
    console.print(f"cooldown_or_repeat_rate: {result.cooldown_or_repeat_rate:.3f}")


@app.command("evaluate-rolling")
def evaluate_rolling_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    cutoff: Annotated[
        list[str],
        typer.Option("--cutoff", help="Training cutoff (YYYY-MM-DD or ISO timestamp); repeatable."),
    ],
    task: Annotated[
        str, typer.Option("--task", help="Evaluation task: general or artist.")
    ] = "general",
    horizon_days: Annotated[
        int,
        typer.Option("--horizon-days", min=1, help="Outcome horizon in days after each cutoff."),
    ] = 90,
) -> None:
    """Replay historical recommendation windows and report their mean quality."""
    if task not in ("general", "artist"):
        raise typer.BadParameter("task must be general or artist")
    cutoffs = sorted({_parse_timestamp(value) for value in cutoff})
    windows = [
        EvaluationWindow(value, value, value + timedelta(days=horizon_days)) for value in cutoffs
    ]
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        report = evaluate_rolling_recommendations(
            conn, resolved_config, windows=windows, evaluation_task=cast(Any, task)
        )
    finally:
        conn.close()

    console.print("Rolling evaluation")
    console.print(f"windows: {report.window_count}")
    if isinstance(report, ArtistRollingEvaluationReport):
        console.print("task: artist")
        console.print(f"targets: {report.target_count}")
        console.print(f"runs: {report.run_count}")
        console.print(f"mean graded adoption ndcg@50: {report.mean_graded_adoption_ndcg_at_50:.3f}")
    prefix = "target_id | " if report.evaluation_task == "artist" else ""
    console.print(
        prefix + "cutoff | test_until | recall@50 | recall@100 | ndcg@50 | identity_resolved_rate"
    )
    for result in report.windows:
        prefix = (
            f"{result.target_artist_id} | " if isinstance(result, ArtistEvaluationResult) else ""
        )
        console.print(
            prefix + f"{result.train_until} | {result.test_until} | "
            f"{result.recall_at_50:.3f} | {result.recall_at_100:.3f} | "
            f"{result.ndcg_at_50:.3f} | {result.identity_resolved_rate:.3f}"
        )
    console.print(f"mean recall@50: {report.mean_recall_at_50:.3f}")
    console.print(f"mean recall@100: {report.mean_recall_at_100:.3f}")
    console.print(f"mean ndcg@50: {report.mean_ndcg_at_50:.3f}")
    console.print(f"mean identity_resolved_rate: {report.mean_identity_resolved_rate:.3f}")


@app.command("compare")
def compare_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    split_date: Annotated[
        str | None,
        typer.Option("--split-date", help="Date to split train/test (YYYY-MM-DD)."),
    ] = None,
    train_until: Annotated[
        str | None,
        typer.Option("--train-until", help="End of training period (YYYY-MM-DD or ISO timestamp)."),
    ] = None,
    test_from: Annotated[
        str | None,
        typer.Option("--test-from", help="Start of test period (YYYY-MM-DD or ISO timestamp)."),
    ] = None,
    test_until: Annotated[
        str | None,
        typer.Option("--test-until", help="End of test period (YYYY-MM-DD or ISO timestamp)."),
    ] = None,
    scorers: Annotated[
        str,
        typer.Option("--scorers", help="Comma-separated engines to compare."),
    ] = "legacy,walk",
) -> None:
    """Run several scoring engines over one identical split and print deltas."""
    from datetime import timedelta

    resolved_config = load_config(config)
    scorer_list = [item.strip() for item in scorers.split(",") if item.strip()]
    for scorer in scorer_list:
        if scorer not in ("legacy", "walk"):
            raise typer.BadParameter("--scorers may only contain 'legacy' and 'walk'")

    conn = initialize_database(resolved_config)
    try:
        if train_until and test_from:
            train_dt = _parse_timestamp(train_until)
            test_dt = _parse_timestamp(test_from)
        elif split_date:
            train_dt = _parse_timestamp(split_date)
            test_dt = train_dt
        else:
            console.print("Error: provide --split-date or both --train-until and --test-from")
            raise typer.Exit(code=1)
        test_end_dt = _parse_timestamp(test_until) if test_until else test_dt + timedelta(days=90)

        console.print("Comparing scorers over identical split...")
        results = compare_scorers(
            conn,
            resolved_config,
            train_until=train_dt,
            test_from=test_dt,
            test_until=test_end_dt,
            scorers=scorer_list,
        )
    finally:
        conn.close()

    _print_comparison(results)


def _print_comparison(results: list[ScorerComparison]) -> None:
    def fmt(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.3f}"

    metrics: list[tuple[str, list[float | None]]] = [
        ("recommendations", [float(r.recommendations) for r in results]),
        ("hit_rate@10", [r.hit_rate_at_10 for r in results]),
        ("hit_rate@25", [r.hit_rate_at_25 for r in results]),
        ("hit_rate@100", [r.hit_rate_at_100 for r in results]),
        ("catalog_coverage", [r.catalog_coverage for r in results]),
        ("knownness_mean", [r.knownness_mean for r in results]),
        ("knownness_median", [r.knownness_median for r in results]),
        ("tag_entropy", [r.tag_entropy for r in results]),
        ("mean_hop_distance", [r.mean_hop_distance for r in results]),
    ]
    header = "metric".ljust(20) + "".join(r.scorer.rjust(12) for r in results)
    delta_header = "delta".rjust(12) if len(results) == 2 else ""
    console.print("")
    console.print("Scorer comparison")
    console.print("")
    console.print(header + delta_header)
    for name, values in metrics:
        line = name.ljust(20) + "".join(fmt(value).rjust(12) for value in values)
        if len(results) == 2 and values[0] is not None and values[1] is not None:
            line += fmt(values[1] - values[0]).rjust(12)
        console.print(line)


@app.command("feedback")
def feedback_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    run_id: Annotated[str, typer.Option("--run-id", help="Recommendation run UUID.")],
    artist_id: Annotated[int, typer.Option("--artist-id", help="Artist ID to annotate.")],
    action: Annotated[str, typer.Option("--action", help="Feedback action.")],
    value: Annotated[float, typer.Option("--value", help="Feedback strength multiplier.")] = 1.0,
    tag: Annotated[
        list[str] | None,
        typer.Option("--tag", help="Tag affected by this feedback. Can be repeated."),
    ] = None,
    note: Annotated[str | None, typer.Option("--note", help="Optional note.")] = None,
    rerank: Annotated[
        bool,
        typer.Option("--rerank/--no-rerank", help="Rerank the target run after recording."),
    ] = True,
) -> None:
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        try:
            if rerank:
                conn.execute("BEGIN TRANSACTION")
                try:
                    event = record_feedback_event(
                        conn,
                        run_id=run_id,
                        artist_id=artist_id,
                        action=action,
                        value=value,
                        context={"tags": tag or []},
                        note=note,
                    )
                    stats = rerank_recommendation_run(
                        conn,
                        run_id,
                        _manage_transaction=False,
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            else:
                event = record_feedback_event(
                    conn,
                    run_id=run_id,
                    artist_id=artist_id,
                    action=action,
                    value=value,
                    context={"tags": tag or []},
                    note=note,
                )
                stats = None
            console.print("Feedback recorded")
            console.print(f"feedback id: {event.feedback_id}")
            console.print(f"action: {event.action}")
            if stats is not None:
                console.print(f"reranked recommendations: {stats.recommendations}")
                console.print(f"hidden recommendations: {stats.hidden}")
        except ValueError as exc:
            console.print(f"Error: {exc}")
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()


def _parse_timestamp(value: str) -> datetime:
    """Parse YYYY-MM-DD or ISO-8601 timestamp string."""
    try:
        return normalize_timestamp(datetime.fromisoformat(value))
    except ValueError as err:
        raise typer.BadParameter(f"invalid timestamp: {value}") from err


def evaluate_recommendations_from_config(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    train_until: datetime,
    test_from: datetime,
    test_until: datetime,
    ablate_source: str | None = None,
) -> EvaluationResult:
    """Thin wrapper for CLI monkeypatching in tests."""
    return evaluate_recommendations(
        conn,
        config,
        train_until=train_until,
        test_from=test_from,
        test_until=test_until,
        ablate_source=ablate_source,
    )


def _count_query(conn: DuckDBPyConnection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


def _print_stats(stats: ImportStats) -> None:
    console.print(f"raw scrobbles: {stats.raw_scrobbles}")
    console.print(f"clean scrobbles: {stats.scrobbles}")
    console.print(f"duplicate scrobbles: {stats.duplicate_scrobbles}")
    console.print(f"near-duplicate scrobbles: {stats.near_duplicate_scrobbles}")
    console.print(f"distinct artists: {stats.distinct_artists}")
    console.print(f"distinct tracks: {stats.distinct_tracks}")
    console.print(f"null artist MBID scrobbles: {stats.null_artist_mbid_scrobbles}")
    console.print(f"import warnings: {stats.import_warnings}")


def _print_diagnostic_check(check: DiagnosticCheck) -> None:
    console.print(f"{check.name}: {check.status.value} - {check.message}", soft_wrap=True)


def _print_enrichment_stats(stats: LastFmEnrichmentStats) -> None:
    console.print(f"seed artists: {stats.seed_artists}")
    console.print(f"frontier artists: {stats.frontier_artists}")
    console.print(f"similar artist requests: {stats.similar_artist_requests}")
    console.print(f"artist tag requests: {stats.artist_tag_requests}")
    console.print(f"tag top artist requests: {stats.tag_top_artist_requests}")
    console.print(f"artists created: {stats.artists_created}")
    console.print(f"similarity edges: {stats.similarity_edges}")
    console.print(f"artist tags: {stats.artist_tags}")
    console.print(f"tag top artists: {stats.tag_top_artists}")


def _run_lastfm_enrichment_with_progress(
    conn: DuckDBPyConnection,
    resolved_config: ScrobbleRecsConfig,
    *,
    depth: int = 1,
    max_frontier_artists: int | None = None,
) -> LastFmEnrichmentStats:
    task_ids: dict[str, TaskID] = {}
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:

        def on_progress(event: LastFmEnrichmentProgress) -> None:
            task_id = task_ids.get(event.phase)
            if task_id is None:
                task_id = progress.add_task(
                    _format_enrichment_progress_description(event),
                    total=event.total,
                )
                task_ids[event.phase] = task_id
            progress.update(
                task_id,
                completed=event.current,
                total=event.total,
                description=_format_enrichment_progress_description(event),
            )

        return run_lastfm_enrichment(
            conn,
            resolved_config,
            depth=depth,
            max_frontier_artists=max_frontier_artists,
            progress_callback=on_progress,
        )


def _format_enrichment_progress_description(event: LastFmEnrichmentProgress) -> str:
    phase = _ENRICHMENT_PHASE_LABELS.get(event.phase, event.phase)
    return f"{phase}: {escape(event.subject)} ({event.status})"


def _generate_recommendations_with_progress(
    conn: DuckDBPyConnection,
    resolved_config: ScrobbleRecsConfig,
    *,
    limit: int,
    mode: str | None,
    history_window: ListeningWindow | None = None,
    request: RecommendationRequest | None = None,
) -> RecommendationStats:
    task_ids: dict[str, TaskID] = {}
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:

        def on_progress(event: RecommendationProgress) -> None:
            task_id = task_ids.get(event.phase)
            if task_id is None:
                task_id = progress.add_task(
                    _format_recommendation_progress_description(event),
                    total=event.total,
                )
                task_ids[event.phase] = task_id
            progress.update(
                task_id,
                completed=event.current,
                total=event.total,
                description=_format_recommendation_progress_description(event),
            )
            progress.refresh()

        request_options: dict[str, Any] = {"request": request} if request is not None else {}
        return generate_recommendations(
            conn,
            resolved_config,
            limit=limit,
            mode=mode,
            history_window=history_window,
            progress_callback=on_progress,
            **request_options,
        )


def _format_recommendation_progress_description(event: RecommendationProgress) -> str:
    phase = _RECOMMENDATION_PHASE_LABELS.get(event.phase, event.phase)
    return f"{phase}: {escape(event.subject)} ({event.status})"


def _print_recommendation_stats(stats: RecommendationStats) -> None:
    console.print(f"run id: {stats.run_id}")
    console.print(f"mode: {stats.mode}")
    console.print(f"recommendations: {stats.recommendations}")


def _configure_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("scrobble_recs")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    return logger
