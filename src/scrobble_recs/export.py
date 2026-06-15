from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Literal

from duckdb import DuckDBPyConnection

ExportFormat = Literal["csv", "jsonl"]


def export_recommendations(
    conn: DuckDBPyConnection,
    output_path: Path,
    *,
    format_name: str,
    run_id: str | None = None,
    include_candidates: bool = False,
) -> int:
    if format_name not in ("csv", "jsonl"):
        raise ValueError("format_name must be 'csv' or 'jsonl'")
    resolved_run_id = run_id or _latest_run_id(conn)
    rows = _load_rows(conn, resolved_run_id, include_candidates=include_candidates)
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


def _load_rows(
    conn: DuckDBPyConnection, run_id: str, *, include_candidates: bool = False
) -> list[dict[str, object]]:
    filter_clause = "" if include_candidates else "AND r.score_band != 'candidate_only'"
    rows = conn.execute(
        f"""
        SELECT
            r.rank, a.canonical_name, r.final_score,
            max(r.final_score) OVER () AS max_final_score,
            r.raw_score, r.score_band, r.evidence_source_count,
            r.knownness, r.obviousness, r.rediscovery_score,
            r.local_scrobble_count, r.listened_day_count,
            r.first_listened_at, r.last_listened_at,
            r.years_since_last_listen, r.rediscovery_eligible,
            r.trial_penalty, r.candidate_artist_name,
            r.musicbrainz_artist_mbid, r.lastfm_url,
            r.musicbrainz_url, r.identity_confidence,
            r.identity_warnings, r.lastfm_similarity_score,
            r.tag_similarity_score, r.feedback_score,
            r.canonical_artist_name, r.explanation
        FROM recommendation r
        JOIN artist a ON a.artist_id = r.artist_id
        WHERE r.run_id = ? {filter_clause}
        ORDER BY r.rank
        """,
        [run_id],
    ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        (
            rank,
            artist,
            final_score,
            max_score,
            raw_score,
            score_band,
            evidence_count,
            knownness,
            obviousness,
            rediscovery_score,
            local_scrobbles,
            listened_days,
            first_listened_at,
            last_listened_at,
            years_since,
            rediscovery_eligible,
            trial_pen,
            candidate_name,
            mbid,
            lastfm_url,
            musicbrainz_url,
            identity_conf,
            identity_warns,
            lastfm_sim,
            tag_sim,
            feedback_sc,
            canonical_name,
            explanation_json,
        ) = row
        explanation = json.loads(str(explanation_json)) if explanation_json else {}
        result.append(
            {
                "rank": int(rank),
                "artist": str(artist or ""),
                "score_normalized": (
                    float(final_score or 0.0) / float(max_score or 1.0)
                    if float(max_score or 0.0) > 0
                    else 0.0
                ),
                "score_raw": float(raw_score or 0.0),
                "score_band": str(score_band or ""),
                "evidence_source_count": int(evidence_count or 0),
                "primary_tags": ", ".join(
                    str(tag.get("tag", "")) for tag in explanation.get("tags", [])[:3]
                ),
                "knownness": float(knownness or 0.0),
                "obviousness": float(obviousness or 0.0),
                "is_rediscovery": bool(float(rediscovery_score or 0.0) > 0.0),
                "rediscovery_eligible": (
                    bool(rediscovery_eligible) if rediscovery_eligible is not None else False
                ),
                "why": str(explanation.get("summary", "")),
                "lastfm_url": str(lastfm_url or ""),
                "musicbrainz_url": str(musicbrainz_url or ""),
                "local_scrobble_count": int(local_scrobbles or 0),
                "listened_day_count": int(listened_days or 0),
                "first_listened_at": str(first_listened_at) if first_listened_at else "",
                "last_listened_at": str(last_listened_at) if last_listened_at else "",
                "years_since_last_listen": float(years_since or 0.0),
                "trial_penalty": float(trial_pen or 0.0),
                "candidate_artist_name": str(candidate_name or artist or ""),
                "canonical_artist_name": str(canonical_name or artist or ""),
                "musicbrainz_artist_mbid": str(mbid or ""),
                "identity_confidence": float(identity_conf) if identity_conf is not None else 0.50,
                "identity_warnings": str(identity_warns or "[]"),
                "top_seed_artists": ", ".join(
                    str(s) for s in explanation.get("top_seed_artists", [])
                ),
                "top_seed_edge_scores": ", ".join(
                    str(s) for s in explanation.get("top_seed_edge_scores", [])
                ),
                "matching_tags": ", ".join(str(t) for t in explanation.get("matching_tags", [])),
                "tag_fit_score": float(explanation.get("tag_fit_score", 0.0)),
                "lastfm_similarity_score": float(lastfm_sim or 0.0),
                "rediscovery_score": float(rediscovery_score or 0.0),
                "feedback_score": float(feedback_sc or 0.0),
                "knownness_penalty": float(explanation.get("knownness_penalty", 0.0)),
                "obviousness_penalty": float(explanation.get("obviousness_penalty", 0.0)),
                "fatigue_penalty": float(explanation.get("fatigue_penalty", 0.0)),
                "candidate_sources": ", ".join(
                    str(s) for s in explanation.get("candidate_sources", [])
                ),
                "explanation": explanation,
            }
        )
    return result


def _write_csv(output_path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "rank",
        "artist",
        "score_normalized",
        "score_raw",
        "score_band",
        "evidence_source_count",
        "primary_tags",
        "knownness",
        "obviousness",
        "is_rediscovery",
        "rediscovery_eligible",
        "why",
        "lastfm_url",
        "musicbrainz_url",
        "local_scrobble_count",
        "listened_day_count",
        "first_listened_at",
        "last_listened_at",
        "years_since_last_listen",
        "trial_penalty",
        "candidate_artist_name",
        "canonical_artist_name",
        "musicbrainz_artist_mbid",
        "identity_confidence",
        "identity_warnings",
        "top_seed_artists",
        "top_seed_edge_scores",
        "matching_tags",
        "tag_fit_score",
        "lastfm_similarity_score",
        "rediscovery_score",
        "feedback_score",
        "knownness_penalty",
        "obviousness_penalty",
        "fatigue_penalty",
        "candidate_sources",
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
