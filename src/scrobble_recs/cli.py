from __future__ import annotations

import logging
from datetime import UTC, datetime
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
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import ScrobbleRecsConfig, load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.enrichment.lastfm import (
    LastFmEnrichmentProgress,
    LastFmEnrichmentStats,
    run_lastfm_enrichment,
)
from scrobble_recs.enrichment.queue import (
    enrichment_queue_status,
    plan_enrichment,
    retry_failed_enrichment,
)
from scrobble_recs.export import ExportFormat, export_recommendations
from scrobble_recs.importers.lastfm_csv import ImportStats, get_import_stats, import_lastfm_csv
from scrobble_recs.models.evaluate import EvaluationResult, evaluate_recommendations
from scrobble_recs.models.feedback import record_feedback_event, rerank_recommendation_run
from scrobble_recs.models.profile_builder import build_profile
from scrobble_recs.models.recommend import (
    RecommendationProgress,
    RecommendationStats,
    generate_recommendations,
)
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
) -> None:
    if source not in ("lastfm", "musicbrainz"):
        raise typer.BadParameter("--source must be 'lastfm' or 'musicbrainz'")

    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        if source == "lastfm":
            try:
                console.print(
                    "Large Last.fm enrichment runs can take some time; "
                    "Last.fm API calls are rate limited, so please be patient."
                )
                stats = _run_lastfm_enrichment_with_progress(conn, resolved_config)
            except RuntimeError as exc:
                console.print(f"Error: {exc}")
                raise typer.Exit(code=1) from exc
            console.print("Last.fm enrichment complete")
            _print_enrichment_stats(stats)
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
    if source not in ("lastfm", "musicbrainz"):
        raise typer.BadParameter("--source must be 'lastfm' or 'musicbrainz'")

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
    if source not in ("lastfm", "musicbrainz"):
        raise typer.BadParameter("--source must be 'lastfm' or 'musicbrainz'")

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
    if source not in ("lastfm", "musicbrainz"):
        raise typer.BadParameter("--source must be 'lastfm' or 'musicbrainz'")

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
) -> dict[str, int]:
    from scrobble_recs.apis.musicbrainz import MusicBrainzClient

    stats = {"resolved": 0, "ambiguous": 0, "not_found": 0, "skipped": 0, "errors": 0}
    cache = ApiCache(conn)
    client = MusicBrainzClient(
        config=config.apis.musicbrainz,
        cache=cache,
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
    from datetime import datetime as _dt

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
            _dt.utcnow(),
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

    console.print(f"unresolved artists: {len(unresolved)}")
    if not unresolved:
        return

    resolve_stats = _run_musicbrainz_resolution(conn, resolved_config, unresolved)

    console.print(f"resolved: {resolve_stats['resolved']}")
    console.print(f"ambiguous: {resolve_stats['ambiguous']}")
    console.print(f"not_found: {resolve_stats['not_found']}")
    console.print(f"skipped: {resolve_stats['skipped']}")
    console.print(f"errors: {resolve_stats['errors']}")


@app.command("recommend")
def recommend_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    limit: Annotated[
        int,
        typer.Option("--limit", help="Number of recommendations to persist."),
    ] = 500,
    mode: Annotated[str | None, typer.Option("--mode", help="Recommendation mode.")] = None,
) -> None:
    if limit < 1:
        console.print("Error: limit must be at least 1")
        raise typer.Exit(code=1)

    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        try:
            console.print("Generating recommendations from local DuckDB data...")
            stats = _generate_recommendations_with_progress(
                conn,
                resolved_config,
                limit=limit,
                mode=mode,
            )
        except ValueError as exc:
            console.print(f"Error: {exc}")
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    console.print("Recommendation run complete")
    _print_recommendation_stats(stats)


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
) -> None:
    """Evaluate recommendation quality using a train/test split."""
    from datetime import timedelta

    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        # Resolve split dates.
        if train_until and test_from:
            train_dt = _parse_timestamp(train_until)
            test_dt = _parse_timestamp(test_from)
        elif split_date:
            train_dt = _parse_timestamp(split_date).replace(hour=23, minute=59, second=59)
            test_dt = _parse_timestamp(split_date).replace(hour=0, minute=0, second=0) + timedelta(
                days=1
            )
        else:
            console.print("Error: provide --split-date or both --train-until and --test-from")
            raise typer.Exit(code=1)

        console.print("Generating evaluation recommendations from local DuckDB data...")
        result = evaluate_recommendations_from_config(
            conn,
            resolved_config,
            train_until=train_dt,
            test_from=test_dt,
        )
    finally:
        conn.close()

    console.print("")
    console.print("Evaluation")
    console.print("")
    console.print(f"train_until: {result.train_until}")
    console.print(f"test_from: {result.test_from}")
    console.print("")
    console.print(f"recall@50: {result.recall_at_50:.3f}")
    console.print(f"recall@100: {result.recall_at_100:.3f}")
    console.print(f"ndcg@50: {result.ndcg_at_50:.3f}")
    console.print(f"tag_diversity@20: {result.tag_diversity_at_20:.3f}")
    console.print(f"knownness median: {result.knownness_median:.3f}")
    console.print(f"obviousness median: {result.obviousness_median:.3f}")
    console.print(f"rediscovery_hit_rate: {result.rediscovery_hit_rate:.3f}")
    console.print(f"external_candidate_share: {result.external_candidate_share:.3f}")


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
        return datetime.fromisoformat(value)
    except ValueError as err:
        raise typer.BadParameter(f"invalid timestamp: {value}") from err


def evaluate_recommendations_from_config(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    train_until: datetime,
    test_from: datetime,
) -> EvaluationResult:
    """Thin wrapper for CLI monkeypatching in tests."""
    return evaluate_recommendations(conn, config, train_until=train_until, test_from=test_from)


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

        return run_lastfm_enrichment(conn, resolved_config, progress_callback=on_progress)


def _format_enrichment_progress_description(event: LastFmEnrichmentProgress) -> str:
    phase = _ENRICHMENT_PHASE_LABELS.get(event.phase, event.phase)
    return f"{phase}: {escape(event.subject)} ({event.status})"


def _generate_recommendations_with_progress(
    conn: DuckDBPyConnection,
    resolved_config: ScrobbleRecsConfig,
    *,
    limit: int,
    mode: str | None,
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

        return generate_recommendations(
            conn,
            resolved_config,
            limit=limit,
            mode=mode,
            progress_callback=on_progress,
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
