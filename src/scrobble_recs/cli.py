from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.enrichment.lastfm import LastFmEnrichmentStats, run_lastfm_enrichment
from scrobble_recs.importers.lastfm_csv import ImportStats, get_import_stats, import_lastfm_csv

app = typer.Typer(help="Local-first Last.fm recommendation tooling.")
console = Console()


@app.command("import")
def import_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
) -> None:
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        stats = import_lastfm_csv(conn, resolved_config.input.lastfm_csv)
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
            stats = run_lastfm_enrichment(conn, resolved_config)
        except RuntimeError as exc:
            console.print(f"Error: {exc}")
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    console.print("Last.fm enrichment complete")
    _print_enrichment_stats(stats)


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
