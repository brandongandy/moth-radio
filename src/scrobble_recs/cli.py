from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, cast

import typer
from duckdb import DuckDBPyConnection
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn

from scrobble_recs.admin.init import initialize_project
from scrobble_recs.config import ScrobbleRecsConfig, load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.enrichment.lastfm import (
    LastFmEnrichmentProgress,
    LastFmEnrichmentStats,
    run_lastfm_enrichment,
)
from scrobble_recs.export import ExportFormat, export_recommendations
from scrobble_recs.importers.lastfm_csv import ImportStats, get_import_stats, import_lastfm_csv
from scrobble_recs.models.feedback import record_feedback_event, rerank_recommendation_run
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
            stats = import_lastfm_csv(conn, csv_path)
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


@app.command("doctor")
def doctor_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
) -> None:
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        stats = get_import_stats(conn)
    finally:
        conn.close()

    console.print("Config: ok")
    console.print("Database: ok")
    if resolved_config.input.lastfm_csv.exists():
        console.print("CSV: ok")
    else:
        console.print(f"CSV: missing at {resolved_config.input.lastfm_csv}")
    console.print("API keys: not required for import/doctor")
    _print_stats(stats)


@app.command("enrich")
def enrich_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    source: Annotated[str, typer.Option("--source", help="Enrichment source to run.")] = "lastfm",
) -> None:
    if source != "lastfm":
        raise typer.BadParameter("only --source lastfm is supported in this milestone")

    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        try:
            console.print(
                "Large Last.fm enrichment runs can take some time; "
                "Last.fm API calls are rate limited, so please be patient."
            )
            stats = _run_lastfm_enrichment_with_progress(conn, resolved_config)
        except RuntimeError as exc:
            console.print(f"Error: {exc}")
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    console.print("Last.fm enrichment complete")
    _print_enrichment_stats(stats)


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


def _print_stats(stats: ImportStats) -> None:
    console.print(f"raw scrobbles: {stats.raw_scrobbles}")
    console.print(f"clean scrobbles: {stats.scrobbles}")
    console.print(f"duplicate scrobbles: {stats.duplicate_scrobbles}")
    console.print(f"distinct artists: {stats.distinct_artists}")
    console.print(f"distinct tracks: {stats.distinct_tracks}")
    console.print(f"null artist MBID scrobbles: {stats.null_artist_mbid_scrobbles}")


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
