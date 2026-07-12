from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from math import log, sqrt
from typing import Literal, TypeAlias
from urllib.parse import quote
from uuid import uuid4

from duckdb import DuckDBPyConnection

from scrobble_recs.config import RecommendationWeightsConfig, ScrobbleRecsConfig
from scrobble_recs.export import _display_score
from scrobble_recs.models.feedback import (
    FeedbackProfile,
    apply_feedback_to_tag_profile,
    feedback_obviousness_for_candidate,
    load_cooldown_artist_ids,
    load_feedback_profile,
)
from scrobble_recs.models.knownness import ArtistListeningInput, compute_artist_listening_scores
from scrobble_recs.models.listening import refresh_artist_listening_scores
from scrobble_recs.models.modes import RecommendationModeSettings, resolve_recommendation_mode
from scrobble_recs.models.profile import SeedArtist, select_seed_artists
from scrobble_recs.models.profile_window import ListeningWindow, resolve_listening_window

RecommendationProgressPhase: TypeAlias = Literal[
    "refresh_listening",
    "select_seeds",
    "build_profiles",
    "load_candidates",
    "score_candidates",
    "rerank_diversity",
    "build_explanations",
    "persist_run",
]
RecommendationProgressStatus: TypeAlias = Literal["processing", "completed"]
RecommendationProgressCallback: TypeAlias = Callable[["RecommendationProgress"], None]


@dataclass(frozen=True)
class RecommendationStats:
    run_id: str
    mode: str
    recommendations: int


@dataclass(frozen=True)
class RecommendationProgress:
    phase: RecommendationProgressPhase
    current: int
    total: int
    subject: str
    status: RecommendationProgressStatus


@dataclass(frozen=True)
class _CandidateScore:
    artist_id: int
    artist_name: str
    final_score: float
    raw_score: float
    lastfm_similarity_score: float
    tag_similarity_score: float
    feedback_score: float
    feedback_contributions: dict[str, float]
    feedback_effects: list[str]
    rediscovery_score: float
    knownness: float
    obviousness: float
    knownness_penalty: float
    obviousness_penalty: float
    diversity_penalty: float
    primary_tag: str | None
    quality_gate: _QualityGateDecision
    fatigue: _FatiguePenalty
    explanation: dict[str, object]
    score_band: str = "candidate_only"
    normalized_score: float = 0.0
    evidence_source_count: int = 0
    trial_penalty: float = 0.0
    local_scrobble_count: int = 0
    listened_day_count: int = 0
    first_listened_at: datetime | None = None
    last_listened_at: datetime | None = None
    years_since_last_listen: float = 0.0
    rediscovery_eligible: bool = False
    identity_confidence: float = 0.50
    identity_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ScoringData:
    artist_names: dict[int, str]
    candidates: set[int]
    similarity_by_target: dict[int, list[tuple[int, float]]]
    feedback_similarity_by_target: dict[int, list[tuple[int, float]]]
    direct_tags_by_artist: dict[int, dict[str, float]]
    tag_ranks_by_artist: dict[int, list[tuple[str, int]]]
    listening_by_artist: dict[int, tuple[float, float, int, int, datetime | None, datetime | None]]
    identity_confidence_by_artist: dict[int, float]
    identity_warnings_by_artist: dict[int, list[str]]


@dataclass(frozen=True)
class _FeedbackScore:
    score: float
    contributions: dict[str, float]
    effects: list[str]


@dataclass(frozen=True)
class _QualityGateDecision:
    passed: bool
    reasons: tuple[str, ...]
    thresholds: dict[str, float]


@dataclass(frozen=True)
class _CandidateDiagnostic:
    artist_id: int
    state: Literal["eligible", "excluded", "rejected"]
    reasons: tuple[str, ...]
    quality_gate: dict[str, object]


@dataclass(frozen=True)
class _FatiguePenalty:
    prior_run_appearances: int
    latest_run_rank: int | None
    penalty: float


def generate_recommendations(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    limit: int,
    mode: str | None = None,
    as_of: datetime | None = None,
    history_window: ListeningWindow | None = None,
    local_history_end: datetime | None = None,
    progress_callback: RecommendationProgressCallback | None = None,
) -> RecommendationStats:
    if limit < 1:
        raise ValueError("limit must be at least 1")

    mode_settings = resolve_recommendation_mode(mode, config)
    resolved_mode = mode_settings.name
    resolved_as_of = as_of or datetime.now(tz=UTC).replace(tzinfo=None)
    if history_window is None:
        resolved_history_window = resolve_listening_window(
            as_of=resolved_as_of,
            default_months=config.recommendation.profile_window_months,
            since=None,
            until=None,
        )
        history_window_source = "default"
    else:
        resolved_history_window = history_window
        history_window_source = "explicit"
    _report_progress(
        progress_callback,
        phase="refresh_listening",
        current=0,
        total=1,
        subject="artist listening scores",
        status="processing",
    )
    refresh_artist_listening_scores(conn, as_of=resolved_as_of)
    _report_progress(
        progress_callback,
        phase="refresh_listening",
        current=1,
        total=1,
        subject="artist listening scores",
        status="completed",
    )
    _report_progress(
        progress_callback,
        phase="select_seeds",
        current=0,
        total=1,
        subject=resolved_mode,
        status="processing",
    )
    feedback_profile = load_feedback_profile(conn)
    cooldown_artist_ids = load_cooldown_artist_ids(
        conn,
        as_of=resolved_as_of,
        days=config.recommendation.feedback_cooldown_days,
    )
    seeds = select_seed_artists(
        conn,
        limit=config.recommendation.seed_artist_limit,
        min_scrobbles=config.recommendation.min_seed_scrobbles,
        regions=mode_settings.seed_regions,
        window=resolved_history_window,
    )
    seeds = _add_feedback_seed_artists(conn, seeds, feedback_profile)
    seed_ids = {seed.artist_id for seed in seeds}
    _report_progress(
        progress_callback,
        phase="select_seeds",
        current=1,
        total=1,
        subject=f"{len(seeds)} seed artists",
        status="completed",
    )
    _report_progress(
        progress_callback,
        phase="build_profiles",
        current=0,
        total=1,
        subject="tags and feedback",
        status="processing",
    )
    tag_profile = _load_user_tag_profile(
        conn,
        seeds,
        weak_tags=config.recommendation.tag_profile.weak_tags,
        weak_tag_penalty=config.recommendation.tag_profile.weak_tag_penalty,
    )
    effective_tag_profile = apply_feedback_to_tag_profile(
        _apply_tag_focus(tag_profile, mode_settings),
        feedback_profile,
    )
    _report_progress(
        progress_callback,
        phase="build_profiles",
        current=1,
        total=1,
        subject=f"{len(effective_tag_profile)} tag weights",
        status="completed",
    )
    _report_progress(
        progress_callback,
        phase="load_candidates",
        current=0,
        total=1,
        subject="local graph",
        status="processing",
    )
    scoring_data = _load_scoring_data(
        conn,
        seed_ids,
        feedback_profile,
        local_history_end=local_history_end,
    )
    fatigue_by_artist = _load_fatigue_by_artist(conn, config, feedback_profile)
    _report_progress(
        progress_callback,
        phase="load_candidates",
        current=1,
        total=1,
        subject=f"{len(scoring_data.candidates)} candidates",
        status="completed",
    )
    run_id = str(uuid4())
    scores, candidate_diagnostics = _score_candidates(
        config,
        mode_settings,
        seeds,
        seed_ids,
        effective_tag_profile,
        scoring_data,
        feedback_profile,
        fatigue_by_artist,
        cooldown_artist_ids,
        as_of=resolved_as_of,
        progress_callback=progress_callback,
    )
    _persist_candidates(conn, run_id, scoring_data, candidate_diagnostics)
    _report_progress(
        progress_callback,
        phase="rerank_diversity",
        current=0,
        total=limit,
        subject="ranked candidates",
        status="processing",
    )
    ranked_without_explanations = _rerank_for_diversity(
        scores,
        mode_settings,
        scoring_data,
        limit=limit,
        enabled=config.recommendation.diversity.enabled,
        progress_callback=progress_callback,
    )
    max_final = max((s.final_score for s in ranked_without_explanations), default=0.0)
    banded = [
        replace(
            score,
            normalized_score=_display_score(score.final_score, max_final),
            score_band=_classify_score_band(
                normalized=_display_score(score.final_score, max_final),
                raw=score.raw_score,
                evidence_count=score.evidence_source_count,
            ),
        )
        for score in ranked_without_explanations
    ]
    _report_progress(
        progress_callback,
        phase="rerank_diversity",
        current=len(banded),
        total=limit,
        subject=f"{len(banded)} selected",
        status="completed",
    )
    _report_progress(
        progress_callback,
        phase="build_explanations",
        current=0,
        total=len(banded),
        subject="recommendations",
        status="processing",
    )
    ranked = [
        replace(
            score,
            explanation=_explanation(
                score,
                scoring_data,
                seed_weight_by_id={seed.artist_id: seed.seed_weight for seed in seeds},
                tag_profile=effective_tag_profile,
            ),
        )
        for score in banded
    ]
    _report_progress(
        progress_callback,
        phase="build_explanations",
        current=len(ranked),
        total=len(banded),
        subject="recommendations",
        status="completed",
    )

    _report_progress(
        progress_callback,
        phase="persist_run",
        current=0,
        total=len(ranked),
        subject="recommendation rows",
        status="processing",
    )
    _persist_run(
        conn,
        run_id,
        resolved_mode,
        config,
        mode_settings,
        seeds,
        ranked,
        history_window=resolved_history_window,
        history_window_source=history_window_source,
    )
    _report_progress(
        progress_callback,
        phase="persist_run",
        current=len(ranked),
        total=len(ranked),
        subject="recommendation rows",
        status="completed",
    )
    return RecommendationStats(run_id=run_id, mode=resolved_mode, recommendations=len(ranked))


def _report_progress(
    progress_callback: RecommendationProgressCallback | None,
    *,
    phase: RecommendationProgressPhase,
    current: int,
    total: int,
    subject: str,
    status: RecommendationProgressStatus,
) -> None:
    if progress_callback is None:
        return
    progress_callback(
        RecommendationProgress(
            phase=phase,
            current=current,
            total=total,
            subject=subject,
            status=status,
        )
    )


def _add_feedback_seed_artists(
    conn: DuckDBPyConnection,
    seeds: list[SeedArtist],
    feedback_profile: FeedbackProfile,
) -> list[SeedArtist]:
    existing_seed_ids = {seed.artist_id for seed in seeds}
    current_candidate_ids = _candidate_targets_from_seeds(conn, existing_seed_ids)
    feedback_seed_ids = sorted(
        feedback_profile.boosted_seed_artist_ids
        - feedback_profile.hidden_artist_ids
        - feedback_profile.suppressed_seed_artist_ids
        - existing_seed_ids
        - current_candidate_ids
    )
    if not feedback_seed_ids:
        return seeds
    rows = conn.execute(
        """
        SELECT
            a.artist_id,
            a.canonical_name,
            a.artist_key,
            coalesce(nullif(a.lastfm_name, ''), a.canonical_name) AS lastfm_name
        FROM artist a
        WHERE a.artist_id IN (
            SELECT source_artist_id
            FROM artist_similarity_edge
            WHERE source = 'lastfm'
              AND source_artist_id IN (SELECT unnest(?))
        )
        ORDER BY a.canonical_name
        """,
        [feedback_seed_ids],
    ).fetchall()
    feedback_seeds = [
        SeedArtist(
            artist_id=int(row[0]),
            name=str(row[1]),
            artist_key=str(row[2]),
            lastfm_name=str(row[3]),
            play_count=0,
            seed_weight=max(0.25, feedback_profile.artist_adjustments.get(int(row[0]), 0.0)),
            regions=("feedback",),
        )
        for row in rows
    ]
    return seeds + feedback_seeds


def _candidate_targets_from_seeds(
    conn: DuckDBPyConnection,
    seed_ids: set[int],
) -> set[int]:
    if not seed_ids:
        return set()
    return {
        int(target_artist_id)
        for (target_artist_id,) in conn.execute(
            """
            SELECT DISTINCT target_artist_id
            FROM artist_similarity_edge
            WHERE source = 'lastfm'
              AND source_artist_id IN (SELECT unnest(?))
            """,
            [sorted(seed_ids)],
        ).fetchall()
    }


def _load_user_tag_profile(
    conn: DuckDBPyConnection,
    seeds: list[SeedArtist],
    *,
    weak_tags: list[str] | None = None,
    weak_tag_penalty: float = 1.0,
) -> dict[str, float]:
    profile: dict[str, float] = {}
    seed_weight_by_id = {seed.artist_id: seed.seed_weight for seed in seeds}
    if not seed_weight_by_id:
        return profile

    rows = conn.execute(
        """
        SELECT artist_id, tag, normalized_weight
        FROM artist_tag
        WHERE source = 'lastfm'
        """
    ).fetchall()

    normalized_weak_tags = {
        _normalize_tag_key(tag) for tag in (weak_tags or []) if _normalize_tag_key(tag) != ""
    }
    artists_by_tag: dict[str, set[int]] = {}
    for artist_id, tag, _normalized_weight in rows:
        seed_weight = seed_weight_by_id.get(int(artist_id))
        if seed_weight is None:
            continue
        normalized_tag = _normalize_tag_key(tag)
        if normalized_tag:
            artists_by_tag.setdefault(normalized_tag, set()).add(int(artist_id))

    for artist_id, tag, normalized_weight in rows:
        seed_weight = seed_weight_by_id.get(int(artist_id))
        if seed_weight is None:
            continue
        normalized_tag = _normalize_tag_key(tag)
        specificity = _tag_specificity(len(artists_by_tag.get(normalized_tag, set())))
        weak_multiplier = weak_tag_penalty if normalized_tag in normalized_weak_tags else 1.0
        profile[normalized_tag] = profile.get(normalized_tag, 0.0) + (
            seed_weight * float(normalized_weight) * specificity * weak_multiplier
        )

    max_weight = max(profile.values(), default=0.0)
    if max_weight <= 0:
        return {}
    return {tag: weight / max_weight for tag, weight in profile.items()}


def _load_scoring_data(
    conn: DuckDBPyConnection,
    seed_ids: set[int],
    feedback_profile: FeedbackProfile,
    *,
    local_history_end: datetime | None = None,
) -> _ScoringData:
    similarity_by_target: dict[int, list[tuple[int, float]]] = {}
    candidates = set[int]()
    for source_artist_id, target_artist_id, normalized_score in conn.execute(
        """
        SELECT source_artist_id, target_artist_id, normalized_score
        FROM artist_similarity_edge
        WHERE source = 'lastfm'
        """
    ).fetchall():
        target_id = int(target_artist_id)
        candidates.add(target_id)
        similarity_by_target.setdefault(target_id, []).append(
            (int(source_artist_id), float(normalized_score))
        )

    feedback_similarity_by_target: dict[int, list[tuple[int, float]]] = {}
    feedback_neighborhood_source_ids = (
        feedback_profile.boosted_seed_artist_ids | feedback_profile.suppressed_seed_artist_ids
    ) - feedback_profile.hidden_artist_ids
    if feedback_neighborhood_source_ids:
        for source_artist_id, target_artist_id, normalized_score in conn.execute(
            """
            SELECT source_artist_id, target_artist_id, normalized_score
            FROM artist_similarity_edge
            WHERE source = 'lastfm'
              AND source_artist_id IN (SELECT unnest(?))
            """,
            [sorted(feedback_neighborhood_source_ids)],
        ).fetchall():
            source_id = int(source_artist_id)
            target_id = int(target_artist_id)
            candidates.add(target_id)
            feedback_similarity_by_target.setdefault(target_id, []).append(
                (source_id, float(normalized_score))
            )

    direct_tags_by_artist: dict[int, dict[str, float]] = {}
    for artist_id, tag, normalized_weight in conn.execute(
        """
        SELECT artist_id, tag, normalized_weight
        FROM artist_tag
        WHERE source = 'lastfm'
        """
    ).fetchall():
        direct_tags_by_artist.setdefault(int(artist_id), {})[_normalize_tag_key(tag)] = float(
            normalized_weight
        )

    tag_ranks_by_artist: dict[int, list[tuple[str, int]]] = {}
    for artist_id, tag, tag_rank in conn.execute(
        """
        SELECT artist_id, tag, tag_rank
        FROM tag_top_artist
        WHERE source = 'lastfm'
        """
    ).fetchall():
        artist = int(artist_id)
        candidates.add(artist)
        tag_ranks_by_artist.setdefault(artist, []).append((_normalize_tag_key(tag), int(tag_rank)))

    listening_rows = conn.execute(
        """
        SELECT
            artist_id,
            coalesce(knownness, 0.0),
            coalesce(rediscovery_score, 0.0),
            coalesce(play_count, 0),
            coalesce(active_days, 0),
            first_played_at,
            last_played_at
        FROM artist_listening_fact
        """
    ).fetchall()
    listening_by_artist = {
        int(row[0]): (
            float(row[1]),
            float(row[2]),
            int(row[3] or 0),
            int(row[4] or 0),
            row[5],
            row[6],
        )
        for row in listening_rows
    }
    if local_history_end is not None:
        listening_by_artist = _with_local_history_before(
            conn,
            listening_by_artist,
            end=local_history_end,
        )

    identity_by_artist = {
        int(artist_id): float(conf or 0.50)
        for artist_id, conf in conn.execute(
            "SELECT artist_id, coalesce(identity_confidence, 0.50) FROM artist"
        ).fetchall()
    }

    identity_warnings_by_artist: dict[int, list[str]] = {}
    for artist_id, warnings_json in conn.execute(
        "SELECT artist_id, coalesce(identity_warnings, '[]') FROM artist"
    ).fetchall():
        try:
            parsed = json.loads(str(warnings_json))
            identity_warnings_by_artist[int(artist_id)] = (
                list(parsed) if isinstance(parsed, list) else []
            )
        except (json.JSONDecodeError, TypeError):
            identity_warnings_by_artist[int(artist_id)] = []

    artist_names = {
        int(artist_id): str(canonical_name)
        for artist_id, canonical_name in conn.execute(
            """
            SELECT artist_id, canonical_name
            FROM artist
            """
        ).fetchall()
    }

    return _ScoringData(
        artist_names=artist_names,
        candidates=candidates - seed_ids,
        similarity_by_target=similarity_by_target,
        feedback_similarity_by_target=feedback_similarity_by_target,
        direct_tags_by_artist=direct_tags_by_artist,
        tag_ranks_by_artist=tag_ranks_by_artist,
        listening_by_artist=listening_by_artist,
        identity_confidence_by_artist=identity_by_artist,
        identity_warnings_by_artist=identity_warnings_by_artist,
    )


def _with_local_history_before(
    conn: DuckDBPyConnection,
    listening_by_artist: dict[
        int,
        tuple[float, float, int, int, datetime | None, datetime | None],
    ],
    *,
    end: datetime,
) -> dict[int, tuple[float, float, int, int, datetime | None, datetime | None]]:
    rows = conn.execute(
        """
        SELECT
            a.artist_id,
            count(*) AS play_count,
            count(DISTINCT s.track_key) AS track_count,
            count(DISTINCT date_trunc('month', s.played_at)) AS active_months,
            count(DISTINCT s.played_date) AS active_days,
            min(s.played_at) AS first_played_at,
            max(s.played_at) AS last_played_at
        FROM scrobble AS s
        JOIN artist AS a ON a.artist_key = s.artist_key
        WHERE NOT s.is_duplicate
          AND s.played_at < ?
        GROUP BY a.artist_id
        """,
        [end],
    ).fetchall()
    scoped_listening = {
        int(artist_id): (
            compute_artist_listening_scores(
                ArtistListeningInput(
                    play_count=int(play_count),
                    track_count=int(track_count),
                    active_months=int(active_months),
                    first_played_at=first_played_at,
                    last_played_at=last_played_at,
                ),
                as_of=end,
            ),
            int(play_count),
            int(active_days),
            first_played_at,
            last_played_at,
        )
        for (
            artist_id,
            play_count,
            track_count,
            active_months,
            active_days,
            first_played_at,
            last_played_at,
        ) in rows
    }
    result: dict[int, tuple[float, float, int, int, datetime | None, datetime | None]] = {}
    for artist_id in listening_by_artist | scoped_listening:
        scoped = scoped_listening.get(artist_id)
        if scoped is None:
            result[artist_id] = (0.0, 0.0, 0, 0, None, None)
            continue
        scores, play_count, active_days, first_played_at, last_played_at = scoped
        result[artist_id] = (
            scores.knownness,
            scores.rediscovery_score,
            play_count,
            active_days,
            first_played_at,
            last_played_at,
        )
    return result


def _load_fatigue_by_artist(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    feedback_profile: FeedbackProfile,
) -> dict[int, _FatiguePenalty]:
    fatigue = config.recommendation.fatigue
    if not fatigue.enabled:
        return {}
    recent_runs = conn.execute(
        """
        SELECT run_id
        FROM recommendation_run
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [fatigue.lookback_runs],
    ).fetchall()
    run_ids = [row[0] for row in recent_runs]
    if not run_ids:
        return {}
    rows = conn.execute(
        """
        SELECT
            artist_id,
            count(*) AS appearances,
            min(CASE WHEN run_id = ?::UUID THEN rank ELSE NULL END) AS latest_rank
        FROM recommendation
        WHERE run_id::VARCHAR IN (SELECT unnest(?))
        GROUP BY artist_id
        """,
        [str(run_ids[0]), [str(r) for r in run_ids]],
    ).fetchall()
    result: dict[int, _FatiguePenalty] = {}
    for artist_id, appearances, latest_rank in rows:
        resolved_artist_id = int(artist_id)
        if feedback_profile.artist_adjustments.get(resolved_artist_id, 0.0) > 0.0:
            continue
        latest_penalty = fatigue.latest_run_penalty if latest_rank is not None else 0.0
        repeat_penalty = fatigue.repeat_appearance_penalty * max(0, int(appearances) - 1)
        result[resolved_artist_id] = _FatiguePenalty(
            prior_run_appearances=int(appearances),
            latest_run_rank=None if latest_rank is None else int(latest_rank),
            penalty=latest_penalty + repeat_penalty,
        )
    return result


def _score_candidates(
    config: ScrobbleRecsConfig,
    mode: RecommendationModeSettings,
    seeds: list[SeedArtist],
    seed_ids: set[int],
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
    feedback_profile: FeedbackProfile,
    fatigue_by_artist: dict[int, _FatiguePenalty],
    cooldown_artist_ids: set[int],
    *,
    as_of: datetime | None = None,
    progress_callback: RecommendationProgressCallback | None = None,
) -> tuple[list[_CandidateScore], list[_CandidateDiagnostic]]:
    seed_weight_by_id = {seed.artist_id: seed.seed_weight for seed in seeds}
    total_seed_weight = sum(seed_weight_by_id.values()) or 1.0
    scores: list[_CandidateScore] = []
    candidate_only_scores: list[_CandidateScore] = []
    diagnostics: list[_CandidateDiagnostic] = []
    obviousness_enabled = config.recommendation.obviousness.enabled
    total_candidates = len(scoring_data.candidates)
    processed = 0
    report_interval = max(1, total_candidates // 20)
    _report_progress(
        progress_callback,
        phase="score_candidates",
        current=0,
        total=total_candidates,
        subject="candidate artists",
        status="processing",
    )
    for artist_id in scoring_data.candidates:
        processed += 1
        if artist_id in seed_ids:
            diagnostics.append(_excluded_candidate_diagnostic(artist_id, "seed_artist"))
            continue
        if artist_id in feedback_profile.hidden_artist_ids:
            diagnostics.append(_excluded_candidate_diagnostic(artist_id, "hidden_artist"))
            continue
        if artist_id in cooldown_artist_ids:
            diagnostics.append(_excluded_candidate_diagnostic(artist_id, "feedback_cooldown"))
            continue
        artist_name = scoring_data.artist_names.get(artist_id)
        if artist_name is None:
            diagnostics.append(_excluded_candidate_diagnostic(artist_id, "missing_artist_name"))
            continue
        listening = scoring_data.listening_by_artist.get(artist_id, (0.0, 0.0, 0, 0, None, None))
        knownness, rediscovery, local_play_count, local_active_days, first_played, last_played = (
            listening
        )
        if mode.name != "rediscovery" and local_play_count > 0:
            diagnostics.append(_excluded_candidate_diagnostic(artist_id, "locally_listened"))
            continue
        scored_rediscovery = rediscovery if mode.name == "rediscovery" else 0.0
        candidate_tags = _candidate_tag_vector(artist_id, scoring_data)
        feedback_neighborhood_score = _feedback_neighborhood_score(
            artist_id,
            scoring_data,
            feedback_profile,
        )
        feedback = _feedback_score_for_candidate(
            feedback_profile=feedback_profile,
            artist_id=artist_id,
            candidate_tags=candidate_tags,
            direct_candidate_intent_score=_feedback_direct_candidate_intent_score(
                artist_id,
                seed_ids,
                feedback_profile,
            ),
            neighborhood_score=feedback_neighborhood_score,
        )
        if artist_id in feedback_profile.known_artist_ids:
            knownness = max(knownness, 1.0)
        years_since_last_listen = 0.0
        if as_of is not None and last_played is not None:
            years_since_last_listen = max(0.0, (as_of - last_played).days / 365.25)
        has_positive_feedback = artist_id in feedback_profile.boosted_seed_artist_ids
        trial_penalty = _compute_trial_penalty(
            play_count=local_play_count,
            active_days=local_active_days,
            years_since_last_listen=years_since_last_listen,
        )
        rediscovery_eligible = _is_rediscovery_eligible(
            play_count=local_play_count,
            active_days=local_active_days,
            has_positive_feedback=has_positive_feedback,
        )
        if mode.name == "rediscovery" and rediscovery > 0.0 and not rediscovery_eligible:
            diagnostics.append(
                _excluded_candidate_diagnostic(artist_id, "rediscovery_not_eligible")
            )
            continue
        lastfm_score = _lastfm_similarity_score(
            artist_id,
            seed_weight_by_id,
            total_seed_weight,
            scoring_data.similarity_by_target,
        )
        tag_score = _tag_similarity_score(artist_id, tag_profile, scoring_data)
        obviousness = (
            min(
                1.0,
                _obviousness(artist_id, tag_profile, scoring_data)
                + feedback_obviousness_for_candidate(
                    feedback_profile,
                    candidate_tags=candidate_tags,
                ),
            )
            if obviousness_enabled
            else 0.0
        )
        if obviousness_enabled and _hard_hides_obvious_candidate(
            artist_id,
            mode,
            tag_profile,
            scoring_data,
            rediscovery_score=scored_rediscovery,
        ):
            diagnostics.append(_excluded_candidate_diagnostic(artist_id, "obvious_candidate"))
            continue

        quality_gate = _quality_gate_decision(
            config,
            mode,
            lastfm_similarity_score=lastfm_score,
            tag_similarity_score=tag_score,
            feedback_score=feedback.score,
            rediscovery_score=scored_rediscovery,
        )
        quality_gate_payload = _quality_gate_payload(
            quality_gate,
            lastfm_similarity_score=lastfm_score,
            tag_similarity_score=tag_score,
            feedback_score=feedback.score,
            rediscovery_score=scored_rediscovery,
        )
        if not quality_gate.passed:
            diagnostics.append(
                _CandidateDiagnostic(
                    artist_id=artist_id,
                    state="rejected",
                    reasons=("quality_gate",),
                    quality_gate=quality_gate_payload,
                )
            )
            continue

        diagnostics.append(
            _CandidateDiagnostic(
                artist_id=artist_id,
                state="eligible",
                reasons=("eligible",),
                quality_gate=quality_gate_payload,
            )
        )

        weights = config.recommendation.weights
        raw_score = _raw_score(
            weights,
            lastfm_score * mode.lastfm_similarity_multiplier,
            tag_score * mode.tag_similarity_multiplier,
            scored_rediscovery * mode.rediscovery_multiplier,
            feedback.score,
        )
        evidence_count = sum(
            1 for val in (lastfm_score, tag_score, feedback.score, scored_rediscovery) if val > 0.0
        )
        knownness_penalty = 0.40 * max(0.0, knownness - mode.max_knownness)
        obviousness_penalty = (
            mode.obviousness_penalty_weight * obviousness if obviousness_enabled else 0.0
        )
        fatigue = fatigue_by_artist.get(
            artist_id,
            _FatiguePenalty(prior_run_appearances=0, latest_run_rank=None, penalty=0.0),
        )
        final_score = raw_score - knownness_penalty - obviousness_penalty - fatigue.penalty
        if mode.name == "balanced":
            final_score -= trial_penalty

        identity_conf = scoring_data.identity_confidence_by_artist.get(artist_id, 0.50)
        identity_warnings = tuple(scoring_data.identity_warnings_by_artist.get(artist_id, []))
        confidence_multiplier = 0.5 + 0.5 * identity_conf
        final_score *= confidence_multiplier

        if final_score <= 0:
            candidate_only = _CandidateScore(
                artist_id=artist_id,
                artist_name=artist_name,
                final_score=0.0,
                raw_score=raw_score,
                lastfm_similarity_score=lastfm_score,
                tag_similarity_score=tag_score,
                feedback_score=feedback.score,
                feedback_contributions=feedback.contributions,
                feedback_effects=feedback.effects,
                rediscovery_score=scored_rediscovery,
                knownness=knownness,
                obviousness=obviousness,
                knownness_penalty=knownness_penalty,
                obviousness_penalty=obviousness_penalty,
                diversity_penalty=0.0,
                primary_tag=_primary_tag(artist_id, tag_profile, scoring_data),
                quality_gate=quality_gate,
                fatigue=fatigue,
                explanation={},
                score_band="candidate_only",
                normalized_score=0.0,
                evidence_source_count=evidence_count,
                trial_penalty=trial_penalty,
                local_scrobble_count=local_play_count,
                listened_day_count=local_active_days,
                first_listened_at=first_played,
                last_listened_at=last_played,
                years_since_last_listen=years_since_last_listen,
                rediscovery_eligible=rediscovery_eligible,
                identity_confidence=identity_conf,
                identity_warnings=identity_warnings,
            )
            candidate_only_scores.append(candidate_only)
            continue
        scores.append(
            _CandidateScore(
                artist_id=artist_id,
                artist_name=artist_name,
                final_score=final_score,
                raw_score=raw_score,
                lastfm_similarity_score=lastfm_score,
                tag_similarity_score=tag_score,
                feedback_score=feedback.score,
                feedback_contributions=feedback.contributions,
                feedback_effects=feedback.effects,
                rediscovery_score=scored_rediscovery,
                knownness=knownness,
                obviousness=obviousness,
                knownness_penalty=knownness_penalty,
                obviousness_penalty=obviousness_penalty,
                diversity_penalty=0.0,
                primary_tag=_primary_tag(artist_id, tag_profile, scoring_data),
                quality_gate=quality_gate,
                fatigue=fatigue,
                explanation={},
                evidence_source_count=evidence_count,
                trial_penalty=trial_penalty,
                local_scrobble_count=local_play_count,
                listened_day_count=local_active_days,
                first_listened_at=first_played,
                last_listened_at=last_played,
                years_since_last_listen=years_since_last_listen,
                rediscovery_eligible=rediscovery_eligible,
                identity_confidence=identity_conf,
                identity_warnings=identity_warnings,
            )
        )
        if processed % report_interval == 0:
            _report_progress(
                progress_callback,
                phase="score_candidates",
                current=processed,
                total=total_candidates,
                subject=f"{len(scores)} positive so far",
                status="processing",
            )
    _report_progress(
        progress_callback,
        phase="score_candidates",
        current=total_candidates,
        total=total_candidates,
        subject=f"{len(scores)} positive scores, {len(candidate_only_scores)} candidate_only",
        status="completed",
    )
    candidate_only_scores.sort(key=lambda s: s.raw_score, reverse=True)
    return scores + candidate_only_scores[: min(len(scores) * 2, 500)], diagnostics


def _rerank_for_diversity(
    scores: list[_CandidateScore],
    mode: RecommendationModeSettings,
    scoring_data: _ScoringData,
    *,
    limit: int,
    enabled: bool,
    progress_callback: RecommendationProgressCallback | None = None,
) -> list[_CandidateScore]:
    remaining = sorted(scores, key=lambda item: (-item.final_score, item.artist_name.casefold()))
    if not enabled:
        return remaining[:limit]

    selected: list[_CandidateScore] = []
    primary_tag_counts: dict[str, int] = {}
    tag_vectors = {
        score.artist_id: _candidate_tag_vector(score.artist_id, scoring_data) for score in scores
    }
    max_similarity_by_artist = {score.artist_id: 0.0 for score in scores}
    while remaining and len(selected) < limit:
        best_index: int | None = None
        best_score: float | None = None
        best_penalty = 0.0
        for index, candidate in enumerate(remaining):
            if _would_break_primary_tag_cap(candidate, selected, primary_tag_counts, mode):
                continue
            similarity = max_similarity_by_artist[candidate.artist_id]
            diversity_penalty = (1.0 - mode.diversity_lambda) * similarity
            rerank_score = mode.diversity_lambda * candidate.final_score - diversity_penalty
            wins_tie = (
                best_index is not None
                and rerank_score == best_score
                and candidate.artist_name.casefold() < remaining[best_index].artist_name.casefold()
            )
            if best_score is None or rerank_score > best_score or wins_tie:
                best_index = index
                best_score = rerank_score
                best_penalty = diversity_penalty
        if best_index is None:
            break
        chosen = remaining.pop(best_index)
        chosen = replace(
            chosen,
            final_score=chosen.final_score - best_penalty,
            diversity_penalty=best_penalty,
        )
        selected.append(chosen)
        if chosen.primary_tag is not None and len(selected) <= 20:
            primary_tag_counts[chosen.primary_tag] = (
                primary_tag_counts.get(chosen.primary_tag, 0) + 1
            )
        for candidate in remaining:
            similarity = _candidate_similarity_from_vectors(
                candidate,
                chosen,
                tag_vectors[candidate.artist_id],
                tag_vectors[chosen.artist_id],
            )
            if similarity > max_similarity_by_artist[candidate.artist_id]:
                max_similarity_by_artist[candidate.artist_id] = similarity
        _report_progress(
            progress_callback,
            phase="rerank_diversity",
            current=len(selected),
            total=limit,
            subject=f"{len(selected)} selected",
            status="processing",
        )
    return selected


def _would_break_primary_tag_cap(
    candidate: _CandidateScore,
    selected: list[_CandidateScore],
    primary_tag_counts: dict[str, int],
    mode: RecommendationModeSettings,
) -> bool:
    if candidate.primary_tag is None or len(selected) >= 20:
        return False
    return primary_tag_counts.get(candidate.primary_tag, 0) >= mode.same_primary_tag_cap


def _candidate_similarity(
    left: _CandidateScore,
    right: _CandidateScore,
    scoring_data: _ScoringData,
) -> float:
    left_tags = _candidate_tag_vector(left.artist_id, scoring_data)
    right_tags = _candidate_tag_vector(right.artist_id, scoring_data)
    return _candidate_similarity_from_vectors(left, right, left_tags, right_tags)


def _candidate_similarity_from_vectors(
    left: _CandidateScore,
    right: _CandidateScore,
    left_tags: dict[str, float],
    right_tags: dict[str, float],
) -> float:
    if not left_tags or not right_tags:
        return (
            0.75 if left.primary_tag is not None and left.primary_tag == right.primary_tag else 0.0
        )
    shared_tags = set(left_tags).intersection(right_tags)
    dot = sum(left_tags[tag] * right_tags[tag] for tag in shared_tags)
    left_norm = sqrt(sum(weight * weight for weight in left_tags.values()))
    right_norm = sqrt(sum(weight * weight for weight in right_tags.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    cosine = dot / (left_norm * right_norm)
    if left.primary_tag is not None and left.primary_tag == right.primary_tag:
        return max(cosine, 0.75)
    return cosine


def _candidate_tag_vector(
    artist_id: int,
    scoring_data: _ScoringData,
) -> dict[str, float]:
    direct_tags = scoring_data.direct_tags_by_artist.get(artist_id, {})
    ranked_tags = {
        tag: _rank_affinity(rank)
        for tag, rank in scoring_data.tag_ranks_by_artist.get(artist_id, [])
    }
    return {**ranked_tags, **direct_tags}


def _normalize_tag_key(tag: object) -> str:
    return str(tag).strip().casefold()


def _tag_specificity(seed_artist_count: int) -> float:
    if seed_artist_count < 1:
        return 0.0
    return 1.0 / log(2 + seed_artist_count)


def _apply_tag_focus(
    tag_profile: dict[str, float],
    mode: RecommendationModeSettings,
) -> dict[str, float]:
    if not mode.focus_tags:
        return tag_profile
    focused = dict(tag_profile)
    for tag in mode.focus_tags:
        normalized_tag = _normalize_tag_key(tag)
        focused[normalized_tag] = max(focused.get(normalized_tag, 0.0), 1.0) * mode.tag_focus_boost
    max_weight = max(focused.values(), default=0.0)
    if max_weight <= 0.0:
        return {}
    return {tag: weight / max_weight for tag, weight in focused.items()}


def _hard_hides_obvious_candidate(
    artist_id: int,
    mode: RecommendationModeSettings,
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
    *,
    rediscovery_score: float,
) -> bool:
    if rediscovery_score > 0.0:
        return False
    for tag, rank in scoring_data.tag_ranks_by_artist.get(artist_id, []):
        if (
            rank <= mode.hard_hide_top_tag_rank
            and tag_profile.get(tag, 0.0) >= mode.high_affinity_tag_weight
        ):
            return True
    return False


def _primary_tag(
    artist_id: int,
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
) -> str | None:
    direct_tags = scoring_data.direct_tags_by_artist.get(artist_id, {})
    if direct_tags:
        return sorted(direct_tags.items(), key=lambda row: (-row[1], row[0]))[0][0]
    ranked_tags = scoring_data.tag_ranks_by_artist.get(artist_id, [])
    if not ranked_tags:
        return None
    return sorted(
        ranked_tags,
        key=lambda row: (-tag_profile.get(row[0], 0.0), row[1], row[0]),
    )[0][0]


def _lastfm_similarity_score(
    artist_id: int,
    seed_weight_by_id: dict[int, float],
    total_seed_weight: float,
    similarity_by_target: dict[int, list[tuple[int, float]]],
) -> float:
    return (
        sum(
            seed_weight_by_id.get(source_artist_id, 0.0) * normalized_score
            for source_artist_id, normalized_score in similarity_by_target.get(artist_id, [])
        )
        / total_seed_weight
    )


def _tag_similarity_score(
    artist_id: int,
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
) -> float:
    if not tag_profile:
        return 0.0
    direct_tags = scoring_data.direct_tags_by_artist.get(artist_id, {})
    ranked_tags = {
        tag: _rank_affinity(rank)
        for tag, rank in scoring_data.tag_ranks_by_artist.get(artist_id, [])
    }
    candidate = {**ranked_tags, **direct_tags}
    if not candidate:
        return 0.0

    dot = sum(tag_profile.get(tag, 0.0) * weight for tag, weight in candidate.items())
    profile_norm = sqrt(sum(weight * weight for weight in tag_profile.values()))
    candidate_norm = sqrt(sum(weight * weight for weight in candidate.values()))
    if profile_norm == 0.0 or candidate_norm == 0.0:
        return 0.0
    return dot / (profile_norm * candidate_norm)


def _obviousness(
    artist_id: int,
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
) -> float:
    return max(
        (
            tag_profile.get(tag, 0.0) * _rank_penalty(rank)
            for tag, rank in scoring_data.tag_ranks_by_artist.get(artist_id, [])
        ),
        default=0.0,
    )


def _feedback_neighborhood_score(
    artist_id: int,
    scoring_data: _ScoringData,
    feedback_profile: FeedbackProfile,
) -> float:
    score = 0.0
    for source_artist_id, similarity in scoring_data.feedback_similarity_by_target.get(
        artist_id,
        [],
    ):
        source_adjustment = feedback_profile.artist_adjustments.get(source_artist_id, 0.0)
        if source_adjustment > 0.0:
            score += 0.15 * similarity
        elif source_adjustment < 0.0:
            score -= 0.15 * similarity
    return score


def _feedback_direct_candidate_intent_score(
    artist_id: int,
    seed_ids: set[int],
    feedback_profile: FeedbackProfile,
) -> float:
    if artist_id in seed_ids:
        return 0.0
    if artist_id in feedback_profile.boosted_seed_artist_ids:
        return 0.10
    if artist_id in feedback_profile.suppressed_seed_artist_ids:
        return -0.10
    return 0.0


def _feedback_score_for_candidate(
    *,
    feedback_profile: FeedbackProfile,
    artist_id: int,
    candidate_tags: dict[str, float],
    direct_candidate_intent_score: float,
    neighborhood_score: float,
) -> _FeedbackScore:
    contributions = {
        "artist": feedback_profile.artist_adjustments.get(artist_id, 0.0)
        + direct_candidate_intent_score,
        "tag": sum(
            feedback_profile.tag_adjustments.get(tag, 0.0) * weight
            for tag, weight in candidate_tags.items()
        ),
        "neighborhood": neighborhood_score,
    }
    score = max(-1.0, min(1.0, sum(contributions.values())))
    effects = [
        source for source in ("artist", "tag", "neighborhood") if contributions[source] != 0.0
    ]
    return _FeedbackScore(
        score=score,
        contributions=contributions,
        effects=effects,
    )


def _raw_score(
    weights: RecommendationWeightsConfig,
    lastfm_similarity_score: float,
    tag_similarity_score: float,
    rediscovery_score: float,
    feedback_score: float,
) -> float:
    return (
        weights.lastfm_similarity * lastfm_similarity_score
        + weights.tag_similarity * tag_similarity_score
        + weights.rediscovery * rediscovery_score
        + weights.feedback_model * feedback_score
    )


def _quality_gate_decision(
    config: ScrobbleRecsConfig,
    mode: RecommendationModeSettings,
    *,
    lastfm_similarity_score: float,
    tag_similarity_score: float,
    feedback_score: float,
    rediscovery_score: float,
) -> _QualityGateDecision:
    gate = config.recommendation.quality_gate
    if not gate.enabled:
        return _QualityGateDecision(
            passed=True,
            reasons=("disabled",),
            thresholds={
                "min_lastfm_similarity_score": 0.0,
                "min_tag_similarity_score": 0.0,
                "min_positive_feedback_score": 0.0,
            },
        )

    multiplier = gate.rediscovery_mode_multiplier if mode.name == "rediscovery" else 1.0
    min_lastfm = gate.min_lastfm_similarity_score * multiplier
    min_tag = gate.min_tag_similarity_score * multiplier
    min_feedback = gate.min_positive_feedback_score * multiplier
    reasons: list[str] = []
    if lastfm_similarity_score >= min_lastfm:
        reasons.append("lastfm_similarity")
    if tag_similarity_score >= min_tag:
        reasons.append("tag_similarity")
    if feedback_score >= min_feedback:
        reasons.append("feedback")
    if not gate.rediscovery_requires_taste_match and rediscovery_score > 0.0:
        reasons.append("rediscovery")
    return _QualityGateDecision(
        passed=bool(reasons),
        reasons=tuple(reasons),
        thresholds={
            "min_lastfm_similarity_score": min_lastfm,
            "min_tag_similarity_score": min_tag,
            "min_positive_feedback_score": min_feedback,
        },
    )


def _excluded_candidate_diagnostic(artist_id: int, reason: str) -> _CandidateDiagnostic:
    return _CandidateDiagnostic(
        artist_id=artist_id,
        state="excluded",
        reasons=(reason,),
        quality_gate={
            "passed": None,
            "reasons": [],
            "thresholds": {},
            "scores": {},
        },
    )


def _quality_gate_payload(
    decision: _QualityGateDecision,
    *,
    lastfm_similarity_score: float,
    tag_similarity_score: float,
    feedback_score: float,
    rediscovery_score: float,
) -> dict[str, object]:
    return {
        "passed": decision.passed,
        "reasons": list(decision.reasons),
        "thresholds": decision.thresholds,
        "scores": {
            "lastfm_similarity_score": lastfm_similarity_score,
            "tag_similarity_score": tag_similarity_score,
            "feedback_score": feedback_score,
            "rediscovery_score": rediscovery_score,
        },
    }


def _explanation(
    score: _CandidateScore,
    scoring_data: _ScoringData,
    *,
    seed_weight_by_id: dict[int, float],
    tag_profile: dict[str, float],
) -> dict[str, object]:
    seed_rows = [
        (
            source_artist_id,
            scoring_data.artist_names.get(source_artist_id, ""),
            similarity,
            seed_weight_by_id[source_artist_id] * similarity,
        )
        for source_artist_id, similarity in scoring_data.similarity_by_target.get(
            score.artist_id, []
        )
        if source_artist_id in seed_weight_by_id
    ]
    seed_rows.sort(key=lambda row: (-row[3], row[1]))
    tag_rows = scoring_data.tag_ranks_by_artist.get(score.artist_id, [])
    ordered_tag_rows = sorted(
        tag_rows,
        key=lambda row: (-tag_profile.get(row[0], 0.0), row[1], row[0]),
    )
    matching_tags = [tag for tag, _rank in ordered_tag_rows if tag_profile.get(tag, 0.0) > 0]
    top_seed_names = [seed_name for _sid, seed_name, _sim, _c in seed_rows[:5]]
    top_seed_scores = [similarity for _sid, _name, similarity, _c in seed_rows[:5]]

    evidence_sources: list[str] = []
    if score.lastfm_similarity_score > 0:
        evidence_sources.append("last.fm similarity")
    if score.tag_similarity_score > 0:
        evidence_sources.append("tag similarity")
    if score.feedback_score > 0:
        evidence_sources.append("feedback")
    if score.rediscovery_score > 0:
        evidence_sources.append("rediscovery")

    caution_parts: list[str] = []
    if score.trial_penalty > 0:
        caution_parts.append(f"trial listen penalty ({score.trial_penalty:.2f})")
    if score.knownness_penalty > 0:
        caution_parts.append(f"already known ({score.knownness_penalty:.2f})")
    if score.obviousness_penalty > 0:
        caution_parts.append(f"obvious ({score.obviousness_penalty:.2f})")
    if score.fatigue.penalty > 0:
        caution_parts.append(f"recently recommended ({score.fatigue.penalty:.2f})")
    identity_warnings_list = list(score.identity_warnings)
    if identity_warnings_list:
        caution_parts.append("identity unresolved")

    evidence_str = ", ".join(evidence_sources) if evidence_sources else "none"
    caution_str = "; ".join(caution_parts) if caution_parts else "none"

    summary = (
        f"Matched tags: {len(matching_tags)}."
        f" Nearest seeds: {len(seed_rows)}."
        f" Evidence: {evidence_str}."
        f" Caution: {caution_str}."
    )

    return {
        "summary": summary,
        "top_seed_artists": top_seed_names,
        "top_seed_edge_scores": top_seed_scores,
        "matching_tags": matching_tags[:5],
        "tag_fit_score": score.tag_similarity_score,
        "lastfm_similarity_score": score.lastfm_similarity_score,
        "rediscovery_score": score.rediscovery_score,
        "feedback_score": score.feedback_score,
        "knownness_penalty": score.knownness_penalty,
        "obviousness_penalty": score.obviousness_penalty,
        "fatigue_penalty": score.fatigue.penalty,
        "trial_penalty": score.trial_penalty,
        "identity_confidence": score.identity_confidence,
        "identity_warnings": identity_warnings_list,
        "candidate_sources": evidence_sources,
        "quality_gate": {
            "passed": score.quality_gate.passed,
            "reasons": list(score.quality_gate.reasons),
            "thresholds": score.quality_gate.thresholds,
            "scores": {
                "lastfm_similarity_score": score.lastfm_similarity_score,
                "tag_similarity_score": score.tag_similarity_score,
                "feedback_score": score.feedback_score,
                "rediscovery_score": score.rediscovery_score,
            },
        },
        "seed_artists": [
            {
                "artist": seed_name,
                "seed_weight": seed_weight_by_id[source_artist_id],
                "source": "lastfm_artist_similarity",
                "similarity": similarity,
            }
            for source_artist_id, seed_name, similarity, _contribution in seed_rows[:5]
        ],
        "tags": [
            {
                "tag": str(tag),
                "user_weight": tag_profile.get(str(tag), 0.0),
                "candidate_weight": _rank_affinity(int(rank)),
                "candidate_rank_for_tag": int(rank),
            }
            for tag, rank in ordered_tag_rows[:5]
        ],
        "penalties": {
            "knownness": score.knownness_penalty,
            "obviousness": score.obviousness_penalty,
            "diversity": score.diversity_penalty,
        },
        "fatigue": {
            "prior_run_appearances": score.fatigue.prior_run_appearances,
            "latest_run_rank": score.fatigue.latest_run_rank,
            "penalty": score.fatigue.penalty,
        },
        "feedback": {
            "score": score.feedback_score,
            "effects": score.feedback_effects,
            "contributions": score.feedback_contributions,
        },
        "classification": {
            "is_new": score.knownness == 0.0,
            "is_rediscovery": score.rediscovery_score > 0.0,
            "is_obvious": score.obviousness >= 0.75,
            "score_band": score.score_band,
            "rediscovery_eligible": score.rediscovery_eligible,
        },
    }


def _seed_region_counts(
    seeds: list[SeedArtist],
    mode_settings: RecommendationModeSettings,
) -> dict[str, int]:
    configured_regions = {region for region, _limit, _multiplier in mode_settings.seed_regions}
    counts: dict[str, int] = {
        region: sum(1 for seed in seeds if region in seed.regions)
        for region, _limit, _multiplier in mode_settings.seed_regions
    }
    for seed in seeds:
        for region in seed.regions:
            if region not in configured_regions:
                counts[region] = counts.get(region, 0) + 1
    return counts


def _persist_candidates(
    conn: DuckDBPyConnection,
    run_id: str,
    scoring_data: _ScoringData,
    diagnostics: list[_CandidateDiagnostic],
) -> None:
    """Persist candidate artists for the current recommendation run."""
    for diagnostic in diagnostics:
        artist_id = diagnostic.artist_id
        sources = scoring_data.similarity_by_target.get(artist_id, [])
        tags = scoring_data.tag_ranks_by_artist.get(artist_id, [])
        conn.execute(
            """
            INSERT INTO recommendation_candidate
                (run_id, artist_id, candidate_sources, max_source_score,
                 appeared_from_seed_count, appeared_from_tag_count, eligibility_state,
                 eligibility_reasons, quality_gate)
            VALUES (CAST(? AS UUID), ?, ?::JSON, ?, ?, ?, ?, ?::JSON, ?::JSON)
            ON CONFLICT (run_id, artist_id) DO UPDATE SET
                candidate_sources = excluded.candidate_sources,
                max_source_score = excluded.max_source_score,
                appeared_from_seed_count = excluded.appeared_from_seed_count,
                appeared_from_tag_count = excluded.appeared_from_tag_count,
                eligibility_state = excluded.eligibility_state,
                eligibility_reasons = excluded.eligibility_reasons,
                quality_gate = excluded.quality_gate
            """,
            [
                run_id,
                artist_id,
                json.dumps([int(s[0]) for s in sources]),
                float(max((s[1] for s in sources), default=0.0)),
                len(sources),
                len(tags),
                diagnostic.state,
                json.dumps(list(diagnostic.reasons)),
                json.dumps(diagnostic.quality_gate, sort_keys=True),
            ],
        )


def _persist_run(
    conn: DuckDBPyConnection,
    run_id: str,
    mode: str,
    config: ScrobbleRecsConfig,
    mode_settings: RecommendationModeSettings,
    seeds: list[SeedArtist],
    ranked: list[_CandidateScore],
    *,
    history_window: ListeningWindow,
    history_window_source: str,
) -> None:
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            """
            INSERT INTO recommendation_run (
                run_id,
                config_hash,
                mode,
                seed_summary,
                scoring_weights,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                _config_hash(config),
                mode,
                json.dumps(
                    {
                        "seed_count": len(seeds),
                        "history_window": {
                            "start": history_window.start.isoformat(),
                            "end": history_window.end.isoformat(),
                            "source": history_window_source,
                        },
                        "regions": _seed_region_counts(seeds, mode_settings),
                        "top_seeds": [
                            {
                                "artist": seed.name,
                                "seed_weight": seed.seed_weight,
                                "regions": list(seed.regions),
                            }
                            for seed in seeds[:10]
                        ],
                    }
                ),
                json.dumps(
                    {
                        "base": config.recommendation.weights.model_dump(mode="json"),
                        "mode": {
                            "name": mode_settings.name,
                            "diversity_lambda": mode_settings.diversity_lambda,
                            "max_knownness": mode_settings.max_knownness,
                            "obviousness_penalty_weight": (
                                mode_settings.obviousness_penalty_weight
                            ),
                            "hard_hide_top_tag_rank": mode_settings.hard_hide_top_tag_rank,
                            "same_primary_tag_cap": mode_settings.same_primary_tag_cap,
                            "focus_tags": list(mode_settings.focus_tags),
                            "lastfm_similarity_multiplier": (
                                mode_settings.lastfm_similarity_multiplier
                            ),
                            "tag_similarity_multiplier": (mode_settings.tag_similarity_multiplier),
                            "rediscovery_multiplier": mode_settings.rediscovery_multiplier,
                            "tag_focus_boost": mode_settings.tag_focus_boost,
                            "high_affinity_tag_weight": mode_settings.high_affinity_tag_weight,
                            "obviousness_enabled": config.recommendation.obviousness.enabled,
                        },
                    },
                    sort_keys=True,
                ),
                "offline lastfm/tag first recommender",
            ],
        )
        candidate_only_offset = 999900
        artist_identity = {
            int(aid): (
                str(canonical_name or ""),
                str(mbid or ""),
                float(conf or 0.50),
                str(warnings or "[]"),
            )
            for aid, canonical_name, mbid, conf, warnings in conn.execute(
                """
                SELECT artist_id, canonical_name,
                       coalesce(musicbrainz_artist_mbid, ''),
                       coalesce(identity_confidence, 0.50),
                       coalesce(identity_warnings, '[]')
                FROM artist
                """
            ).fetchall()
        }
        for rank, score in enumerate(ranked, start=1):
            if getattr(score, "score_band", "") == "candidate_only":
                effective_rank = candidate_only_offset
                candidate_only_offset += 1
            else:
                effective_rank = rank
            identity = artist_identity.get(score.artist_id, (score.artist_name, "", 0.50, "[]"))
            mbid_val = identity[1]
            lastfm_url_val = f"https://www.last.fm/music/{quote(score.artist_name, safe='')}"
            musicbrainz_url_val = f"https://musicbrainz.org/artist/{mbid_val}" if mbid_val else ""
            conn.execute(
                """
                INSERT INTO recommendation (
                    run_id,
                    rank,
                    artist_id,
                    final_score,
                    raw_score,
                    lastfm_similarity_score,
                    tag_similarity_score,
                    cluster_fit_score,
                    rediscovery_score,
                    metadata_graph_score,
                    feedback_score,
                    knownness,
                    obviousness,
                    diversity_penalty,
                    explanation,
                    score_band,
                    evidence_source_count,
                    local_scrobble_count,
                    listened_day_count,
                    first_listened_at,
                    last_listened_at,
                    years_since_last_listen,
                    rediscovery_eligible,
                    trial_penalty,
                    candidate_artist_name,
                    canonical_artist_name,
                    musicbrainz_artist_mbid,
                    lastfm_url,
                    musicbrainz_url,
                    identity_confidence,
                    identity_warnings
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?
                )
                """,
                [
                    run_id,
                    effective_rank,
                    score.artist_id,
                    score.final_score,
                    score.raw_score,
                    score.lastfm_similarity_score,
                    score.tag_similarity_score,
                    0.0,
                    score.rediscovery_score,
                    0.0,
                    score.feedback_score,
                    score.knownness,
                    score.obviousness,
                    score.diversity_penalty,
                    json.dumps(score.explanation),
                    score.score_band,
                    score.evidence_source_count,
                    score.local_scrobble_count,
                    score.listened_day_count,
                    score.first_listened_at,
                    score.last_listened_at,
                    score.years_since_last_listen,
                    score.rediscovery_eligible,
                    score.trial_penalty,
                    score.artist_name,
                    identity[0],
                    identity[1],
                    lastfm_url_val,
                    musicbrainz_url_val,
                    identity[2],
                    identity[3],
                ],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _rank_affinity(rank: int) -> float:
    if rank <= 0:
        return 0.0
    if rank <= 3:
        return 1.0
    if rank <= 10:
        return 0.75
    if rank <= 25:
        return 0.40
    if rank <= 100:
        return 0.15
    return 0.0


def _rank_penalty(rank: int) -> float:
    return _rank_affinity(rank)


def _classify_score_band(*, normalized: float, raw: float, evidence_count: int) -> str:
    if raw <= 0.0 or normalized <= 0.0:
        return "candidate_only"
    if normalized >= 0.50 and evidence_count >= 2:
        return "strong"
    if normalized >= 0.10 and evidence_count >= 1:
        return "plausible"
    return "weak"


def _compute_trial_penalty(
    *, play_count: int, active_days: int, years_since_last_listen: float
) -> float:
    if years_since_last_listen < 2.0:
        return 0.0
    if play_count == 1 and active_days <= 1:
        return 0.35
    if play_count == 2 and active_days <= 1:
        return 0.20
    return 0.0


def _is_rediscovery_eligible(
    *, play_count: int, active_days: int, has_positive_feedback: bool
) -> bool:
    return play_count >= 3 or active_days >= 2 or has_positive_feedback


def _config_hash(config: ScrobbleRecsConfig) -> str:
    payload = config.recommendation.model_dump_json(by_alias=True)
    return sha256(payload.encode("utf-8")).hexdigest()


def compute_identity_confidence(
    *,
    artist_name: str,
    mbid: str | None,
    resolution_status: str,
    lastfm_enrichment_mbid: str | None = None,
    lastfm_url_from_enrichment: str | None = None,
    canonical_name_match: bool = True,
) -> tuple[float, list[str]]:
    confidence = 0.50
    warnings: list[str] = []

    if mbid and resolution_status == "provisional_mbid":
        confidence += 0.30
    if lastfm_enrichment_mbid or lastfm_url_from_enrichment:
        confidence += 0.15
    if canonical_name_match:
        confidence += 0.10

    name_parts = artist_name.strip().split()
    if len(name_parts) <= 1 and not mbid:
        confidence -= 0.30
        warnings.append("generic_short_name_no_mbid")
    if resolution_status == "provisional_mbid_conflict":
        confidence -= 0.20
        warnings.append("multiple_possible_local_matches")
    if not mbid:
        warnings.append("missing_musicbrainz_mbid")

    return (max(0.0, min(1.0, confidence)), sorted(warnings))
