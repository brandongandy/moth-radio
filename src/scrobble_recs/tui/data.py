from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from duckdb import DuckDBPyConnection


@dataclass(frozen=True)
class RecommendationFilters:
    search: str = ""
    tag: str = ""
    max_knownness: float | None = None
    max_obviousness: float | None = None


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    created_at: str
    mode: str
    recommendations: int


@dataclass(frozen=True)
class RecommendationListRow:
    run_id: str
    artist_id: int
    rank: int
    artist: str
    score: float
    primary_tags: tuple[str, ...]
    knownness: float
    obviousness: float
    why_short: str
    feedback_state: str
    classification: str
    score_band: str = ""
    identity_confidence: float = 0.50
    identity_warnings: tuple[str, ...] = ()
    trial_penalty: float = 0.0


@dataclass(frozen=True)
class ScoreBreakdown:
    display_score: float
    final_score: float
    raw_score: float
    lastfm_similarity_score: float
    tag_similarity_score: float
    cluster_fit_score: float
    rediscovery_score: float
    metadata_graph_score: float
    feedback_score: float
    knownness: float
    obviousness: float
    diversity_penalty: float
    score_band: str = ""
    normalized_score: float = 0.0
    evidence_source_count: int = 0
    trial_penalty: float = 0.0
    identity_confidence: float = 0.50
    identity_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecommendationDetail:
    run_id: str
    artist_id: int
    artist: str
    rank: int
    summary: str
    seed_artists: tuple[str, ...]
    tags: tuple[str, ...]
    penalties: dict[str, float]
    classification: dict[str, bool]
    score_breakdown: ScoreBreakdown
    status_labels: tuple[str, ...]
    feedback_history: tuple[str, ...]
    score_band: str = ""
    identity_confidence: float = 0.50
    identity_warnings: tuple[str, ...] = ()
    local_scrobble_count: int = 0
    listened_day_count: int = 0
    years_since_last_listen: float = 0.0
    rediscovery_eligible: bool = False
    trial_penalty: float = 0.0
    top_seed_artists: tuple[str, ...] = ()


@dataclass(frozen=True)
class CacheSummary:
    total_entries: int
    fresh_entries: int
    stale_entries: int


class TuiRecommendationStore:
    def __init__(self, conn: DuckDBPyConnection) -> None:
        self._conn = conn

    def list_runs(self) -> list[RunSummary]:
        rows = self._conn.execute(
            """
            SELECT
                CAST(rr.run_id AS VARCHAR),
                CAST(rr.created_at AS VARCHAR),
                rr.mode,
                count(r.artist_id)
            FROM recommendation_run rr
            LEFT JOIN recommendation r ON r.run_id = rr.run_id
            GROUP BY rr.run_id, rr.created_at, rr.mode
            ORDER BY rr.created_at DESC, rr.run_id DESC
            """
        ).fetchall()
        return [
            RunSummary(
                run_id=str(run_id),
                created_at=str(created_at),
                mode=str(mode),
                recommendations=int(count),
            )
            for run_id, created_at, mode, count in rows
        ]

    def latest_run_id(self) -> str:
        runs = self.list_runs()
        if not runs:
            raise ValueError("no recommendation runs exist")
        return runs[0].run_id

    def list_recommendations(
        self,
        filters: RecommendationFilters | None = None,
        *,
        run_id: str | None = None,
    ) -> list[RecommendationListRow]:
        resolved_run_id = run_id or self.latest_run_id()
        resolved_filters = filters or RecommendationFilters()
        rows = self._conn.execute(
            """
            SELECT
                CAST(r.run_id AS VARCHAR),
                r.artist_id,
                r.rank,
                a.canonical_name,
                r.final_score,
                max(r.final_score) OVER () AS max_final_score,
                r.knownness,
                r.obviousness,
                r.feedback_score,
                r.explanation,
                r.score_band,
                r.identity_confidence,
                r.identity_warnings,
                r.trial_penalty
            FROM recommendation r
            JOIN artist a ON a.artist_id = r.artist_id
            WHERE r.run_id = ?
            ORDER BY r.rank
            """,
            [resolved_run_id],
        ).fetchall()
        recommendations = [
            _list_row(
                run_id=str(row[0]),
                artist_id=int(row[1]),
                rank=int(row[2]),
                artist=str(row[3]),
                final_score=_display_score(float(row[4] or 0.0), float(row[5] or 0.0)),
                knownness=float(row[6] or 0.0),
                obviousness=float(row[7] or 0.0),
                feedback_score=float(row[8] or 0.0),
                explanation=_parse_json(row[9]),
                score_band=str(row[10] or ""),
                identity_confidence=float(row[11] or 0.50),
                identity_warnings=str(row[12] or "[]"),
                trial_penalty=float(row[13] or 0.0),
            )
            for row in rows
        ]
        return [row for row in recommendations if _matches_filters(row, resolved_filters)]

    def cache_summary(self) -> CacheSummary:
        total_row = self._conn.execute("SELECT count(*) FROM api_cache").fetchone()
        total = int(total_row[0]) if total_row else 0

        from datetime import datetime

        now = datetime.now(UTC).replace(tzinfo=None)
        fresh_row = self._conn.execute(
            """SELECT count(*) FROM api_cache
               WHERE expires_at > ? AND response_status BETWEEN 200 AND 299""",
            [now],
        ).fetchone()
        fresh = int(fresh_row[0]) if fresh_row else 0

        stale = total - fresh
        return CacheSummary(total_entries=total, fresh_entries=fresh, stale_entries=stale)

    def get_detail(self, run_id: str, *, artist_id: int) -> RecommendationDetail:
        row = self._conn.execute(
            """
            SELECT
                r.rank,
                a.canonical_name,
                r.final_score,
                (
                    SELECT max(final_score)
                    FROM recommendation
                    WHERE run_id = ?
                ) AS max_final_score,
                r.raw_score,
                r.lastfm_similarity_score,
                r.tag_similarity_score,
                r.cluster_fit_score,
                r.rediscovery_score,
                r.metadata_graph_score,
                r.feedback_score,
                r.knownness,
                r.obviousness,
                r.diversity_penalty,
                r.explanation,
                r.score_band,
                r.evidence_source_count,
                r.local_scrobble_count,
                r.listened_day_count,
                r.years_since_last_listen,
                r.rediscovery_eligible,
                r.trial_penalty,
                r.identity_confidence,
                r.identity_warnings
            FROM recommendation r
            JOIN artist a ON a.artist_id = r.artist_id
            WHERE r.run_id = ?
              AND r.artist_id = ?
            """,
            [run_id, run_id, artist_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"recommendation {artist_id} does not exist in run {run_id}")

        explanation = _parse_json(row[14])
        max_final = float(row[3] or 0.0)
        identity_warnings_raw = _parse_json(row[23])
        if isinstance(identity_warnings_raw, list):
            identity_warning_tuple = tuple(str(w) for w in identity_warnings_raw)
        else:
            identity_warning_tuple = ()
        score_breakdown = ScoreBreakdown(
            display_score=_display_score(float(row[2] or 0.0), max_final),
            final_score=float(row[2] or 0.0),
            raw_score=float(row[4] or 0.0),
            lastfm_similarity_score=float(row[5] or 0.0),
            tag_similarity_score=float(row[6] or 0.0),
            cluster_fit_score=float(row[7] or 0.0),
            rediscovery_score=float(row[8] or 0.0),
            metadata_graph_score=float(row[9] or 0.0),
            feedback_score=float(row[10] or 0.0),
            knownness=float(row[11] or 0.0),
            obviousness=float(row[12] or 0.0),
            diversity_penalty=float(row[13] or 0.0),
            score_band=str(row[15] or ""),
            normalized_score=_display_score(float(row[2] or 0.0), max_final),
            evidence_source_count=int(row[16] or 0),
            trial_penalty=float(row[21] or 0.0),
            identity_confidence=float(row[22] or 0.50),
            identity_warnings=identity_warning_tuple,
        )
        top_seed = explanation.get("top_seed_artists", [])
        if not isinstance(top_seed, list):
            top_seed = []
        return RecommendationDetail(
            run_id=run_id,
            artist_id=artist_id,
            artist=str(row[1]),
            rank=int(row[0]),
            summary=str(explanation.get("summary", "")),
            seed_artists=_seed_names(explanation),
            tags=_tag_names(explanation),
            penalties=_float_map(explanation.get("penalties", {})),
            classification=_bool_map(explanation.get("classification", {})),
            score_breakdown=score_breakdown,
            status_labels=_status_labels(explanation, score_breakdown),
            feedback_history=self.feedback_history(run_id, artist_id),
            score_band=str(row[15] or ""),
            identity_confidence=float(row[22] or 0.50),
            identity_warnings=identity_warning_tuple,
            local_scrobble_count=int(row[17] or 0),
            listened_day_count=int(row[18] or 0),
            years_since_last_listen=float(row[19] or 0.0),
            rediscovery_eligible=bool(row[20]),
            trial_penalty=float(row[21] or 0.0),
            top_seed_artists=tuple(str(s) for s in top_seed),
        )

    def feedback_history(self, run_id: str, artist_id: int) -> tuple[str, ...]:
        table_exists = self._conn.execute(
            """
            SELECT count(*)
            FROM information_schema.tables
            WHERE table_schema = 'main'
              AND table_name = 'feedback_event'
            """
        ).fetchone()
        if table_exists == (0,):
            return ()

        rows = self._conn.execute(
            """
            SELECT action
            FROM feedback_event
            WHERE run_id = ?
              AND artist_id = ?
            ORDER BY created_at, feedback_id
            """,
            [run_id, artist_id],
        ).fetchall()
        return tuple(str(row[0]) for row in rows)


def _list_row(
    *,
    run_id: str,
    artist_id: int,
    rank: int,
    artist: str,
    final_score: float,
    knownness: float,
    obviousness: float,
    feedback_score: float,
    explanation: dict[str, Any],
    score_band: str = "",
    identity_confidence: float = 0.50,
    identity_warnings: str = "[]",
    trial_penalty: float = 0.0,
) -> RecommendationListRow:
    parsed_warnings = _parse_json(identity_warnings)
    if isinstance(parsed_warnings, list):
        warning_tuple = tuple(str(w) for w in parsed_warnings)
    else:
        warning_tuple = ()
    return RecommendationListRow(
        run_id=run_id,
        artist_id=artist_id,
        rank=rank,
        artist=artist,
        score=final_score,
        primary_tags=_tag_names(explanation)[:3],
        knownness=knownness,
        obviousness=obviousness,
        why_short=str(explanation.get("summary", "")),
        feedback_state=_feedback_state(feedback_score),
        classification=_classification_label(explanation, score_band=score_band),
        score_band=score_band,
        identity_confidence=identity_confidence,
        identity_warnings=warning_tuple,
        trial_penalty=trial_penalty,
    )


def _matches_filters(row: RecommendationListRow, filters: RecommendationFilters) -> bool:
    search = filters.search.strip().casefold()
    tag = filters.tag.strip().casefold()
    if search and search not in row.artist.casefold() and search not in row.why_short.casefold():
        return False
    if tag and tag not in {item.casefold() for item in row.primary_tags}:
        return False
    if filters.max_knownness is not None and row.knownness > filters.max_knownness:
        return False
    if filters.max_obviousness is not None and row.obviousness > filters.max_obviousness:
        return False
    return True


def _parse_json(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}


def _tag_names(explanation: dict[str, Any]) -> tuple[str, ...]:
    tags = explanation.get("tags", [])
    if not isinstance(tags, list):
        return ()
    names = []
    for item in tags:
        if isinstance(item, dict):
            tag = str(item.get("tag", "")).strip()
            if tag:
                names.append(tag)
    return tuple(names)


def _seed_names(explanation: dict[str, Any]) -> tuple[str, ...]:
    seeds = explanation.get("seed_artists", [])
    if not isinstance(seeds, list):
        return ()
    names = []
    for item in seeds:
        if isinstance(item, dict):
            artist = str(item.get("artist", "")).strip()
            if artist:
                names.append(artist)
    return tuple(names)


def _float_map(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {str(key): float(raw or 0.0) for key, raw in value.items()}


def _bool_map(value: object) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {str(key): bool(raw) for key, raw in value.items()}


def _status_labels(
    explanation: dict[str, Any],
    score_breakdown: ScoreBreakdown,
) -> tuple[str, ...]:
    labels = []
    if not _seed_names(explanation) or score_breakdown.lastfm_similarity_score == 0.0:
        labels.append("partial_similarity")
    if not _tag_names(explanation) or score_breakdown.tag_similarity_score == 0.0:
        labels.append("partial_tags")
    for w in score_breakdown.identity_warnings:
        labels.append(f"id:{w}")
    if score_breakdown.score_band == "weak":
        labels.append("band:weak")
    elif score_breakdown.score_band == "candidate_only":
        labels.append("band:candidate_only")
    return tuple(labels) or ("complete",)


def _feedback_state(feedback_score: float) -> str:
    if feedback_score > 0.0:
        return "boosted"
    if feedback_score < 0.0:
        return "suppressed"
    return ""


def _display_score(final_score: float, max_final_score: float) -> float:
    if max_final_score <= 0.0:
        return 0.0
    return max(0.0, min(final_score / max_final_score, 1.0))


def _classification_label(explanation: dict[str, Any], *, score_band: str = "") -> str:
    if score_band == "candidate_only":
        return "cand"
    if score_band == "weak":
        return "weak"
    classification = _bool_map(explanation.get("classification", {}))
    if classification.get("is_rediscovery"):
        return "redis"
    if classification.get("is_obvious"):
        return "obvious"
    if classification.get("is_new_artist"):
        return "new"
    return "sim"
