from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from duckdb import DuckDBPyConnection

ExportFormat = Literal["csv", "jsonl"]


def export_recommendations(
    conn: DuckDBPyConnection,
    output_path: Path,
    *,
    format_name: str,
    run_id: str | None = None,
) -> int:
    if format_name not in ("csv", "jsonl"):
        raise ValueError("format_name must be 'csv' or 'jsonl'")
    resolved_run_id = run_id or _latest_run_id(conn)
    rows = _load_rows(conn, resolved_run_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if format_name == "csv":
        _write_csv(output_path, rows)
    else:
        _write_jsonl(output_path, rows)
    return len(rows)


def _latest_run_id(conn: DuckDBPyConnection) -> str:
    row = conn.execute(
        """
        SELECT CAST(run_id AS VARCHAR)
        FROM recommendation_run
        ORDER BY created_at DESC, run_id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise ValueError("no recommendation runs exist")
    return str(row[0])


def _load_rows(conn: DuckDBPyConnection, run_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            r.rank,
            a.canonical_name,
            r.final_score,
            max(r.final_score) OVER () AS max_final_score,
            r.knownness,
            r.obviousness,
            r.rediscovery_score,
            r.explanation
        FROM recommendation r
        JOIN artist a ON a.artist_id = r.artist_id
        WHERE r.run_id = ?
        ORDER BY r.rank
        """,
        [run_id],
    ).fetchall()
    result: list[dict[str, object]] = []
    for (
        rank,
        artist,
        score,
        max_score,
        knownness,
        obviousness,
        rediscovery_score,
        explanation_json,
    ) in rows:
        explanation = json.loads(str(explanation_json))
        tags = explanation.get("tags", [])
        primary_tags = ", ".join(str(tag["tag"]) for tag in tags[:3])
        artist_name = str(artist)
        result.append(
            {
                "rank": int(rank),
                "artist": artist_name,
                "score": _display_score(float(score or 0.0), float(max_score or 0.0)),
                "primary_tags": primary_tags,
                "knownness": float(knownness),
                "obviousness": float(obviousness),
                "is_rediscovery": bool(float(rediscovery_score) > 0.0),
                "why": str(explanation.get("summary", "")),
                "lastfm_url": f"https://www.last.fm/music/{quote(artist_name, safe='')}",
                "musicbrainz_url": "",
                "explanation": explanation,
            }
        )
    return result


def _write_csv(output_path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "rank",
        "artist",
        "score",
        "primary_tags",
        "knownness",
        "obviousness",
        "is_rediscovery",
        "why",
        "lastfm_url",
        "musicbrainz_url",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in fieldnames})


def _write_jsonl(output_path: Path, rows: list[dict[str, object]]) -> None:
    with output_path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, sort_keys=True) + "\n")


def _display_score(final_score: float, max_final_score: float) -> float:
    if max_final_score <= 0.0:
        return 0.0
    return max(0.0, min(final_score / max_final_score, 1.0))
