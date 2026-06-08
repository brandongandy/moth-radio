from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from math import log, sqrt
from typing import Literal, TypeAlias
from uuid import uuid4

from duckdb import DuckDBPyConnection

from scrobble_recs.config import RecommendationWeightsConfig, ScrobbleRecsConfig
from scrobble_recs.models.feedback import (
    FeedbackProfile,
    apply_feedback_to_tag_profile,
    feedback_obviousness_for_candidate,
    load_feedback_profile,
)
from scrobble_recs.models.listening import refresh_artist_listening_scores
from scrobble_recs.models.modes import RecommendationModeSettings, resolve_recommendation_mode
from scrobble_recs.models.profile import SeedArtist, select_seed_artists

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
    explanation: dict[str, object]


@dataclass(frozen=True)
class _ScoringData:
    artist_names: dict[int, str]
    candidates: set[int]
    similarity_by_target: dict[int, list[tuple[int, float]]]
    feedback_similarity_by_target: dict[int, list[tuple[int, float]]]
    direct_tags_by_artist: dict[int, dict[str, float]]
    tag_ranks_by_artist: dict[int, list[tuple[str, int]]]
    listening_by_artist: dict[int, tuple[float, float]]


@dataclass(frozen=True)
class _FeedbackScore:
    score: float
    contributions: dict[str, float]
    effects: list[str]


def generate_recommendations(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    limit: int,
    mode: str | None = None,
    as_of: datetime | None = None,
    progress_callback: RecommendationProgressCallback | None = None,
) -> RecommendationStats:
    if limit < 1:
        raise ValueError("limit must be at least 1")

    mode_settings = resolve_recommendation_mode(mode, config)
    resolved_mode = mode_settings.name
    resolved_as_of = as_of or datetime.now(tz=UTC).replace(tzinfo=None)
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
    seeds = select_seed_artists(
        conn,
        limit=config.recommendation.seed_artist_limit,
        min_scrobbles=config.recommendation.min_seed_scrobbles,
        regions=mode_settings.seed_regions,
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
    scoring_data = _load_scoring_data(conn, seed_ids, feedback_profile)
    _report_progress(
        progress_callback,
        phase="load_candidates",
        current=1,
        total=1,
        subject=f"{len(scoring_data.candidates)} candidates",
        status="completed",
    )
    _report_progress(
        progress_callback,
        phase="score_candidates",
        current=0,
        total=len(scoring_data.candidates),
        subject="candidate artists",
        status="processing",
    )
    scores = _score_candidates(
        config,
        mode_settings,
        seeds,
        seed_ids,
        effective_tag_profile,
        scoring_data,
        feedback_profile,
    )
    _report_progress(
        progress_callback,
        phase="score_candidates",
        current=len(scoring_data.candidates),
        total=len(scoring_data.candidates),
        subject=f"{len(scores)} positive scores",
        status="completed",
    )
    _report_progress(
        progress_callback,
        phase="rerank_diversity",
        current=0,
        total=len(scores),
        subject="ranked candidates",
        status="processing",
    )
    ranked_without_explanations = _rerank_for_diversity(
        scores,
        mode_settings,
        scoring_data,
        limit=limit,
        enabled=config.recommendation.diversity.enabled,
    )
    _report_progress(
        progress_callback,
        phase="rerank_diversity",
        current=len(ranked_without_explanations),
        total=len(scores),
        subject=f"{len(ranked_without_explanations)} selected",
        status="completed",
    )
    _report_progress(
        progress_callback,
        phase="build_explanations",
        current=0,
        total=len(ranked_without_explanations),
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
        for score in ranked_without_explanations
    ]
    _report_progress(
        progress_callback,
        phase="build_explanations",
        current=len(ranked),
        total=len(ranked_without_explanations),
        subject="recommendations",
        status="completed",
    )

    run_id = str(uuid4())
    _report_progress(
        progress_callback,
        phase="persist_run",
        current=0,
        total=len(ranked),
        subject="recommendation rows",
        status="processing",
    )
    _persist_run(conn, run_id, resolved_mode, config, mode_settings, seeds, ranked)
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
        _normalize_tag_key(tag)
        for tag in (weak_tags or [])
        if _normalize_tag_key(tag) != ""
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
        weak_multiplier = (
            weak_tag_penalty if normalized_tag in normalized_weak_tags else 1.0
        )
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

    listening_by_artist = {
        int(artist_id): (float(knownness), float(rediscovery_score))
        for artist_id, knownness, rediscovery_score in conn.execute(
            """
            SELECT
                artist_id,
                coalesce(knownness, 0.0),
                coalesce(rediscovery_score, 0.0)
            FROM artist_listening_fact
            """
        ).fetchall()
    }

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
    )


def _score_candidates(
    config: ScrobbleRecsConfig,
    mode: RecommendationModeSettings,
    seeds: list[SeedArtist],
    seed_ids: set[int],
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
    feedback_profile: FeedbackProfile,
) -> list[_CandidateScore]:
    seed_weight_by_id = {seed.artist_id: seed.seed_weight for seed in seeds}
    total_seed_weight = sum(seed_weight_by_id.values()) or 1.0
    scores: list[_CandidateScore] = []
    obviousness_enabled = config.recommendation.obviousness.enabled
    for artist_id in scoring_data.candidates:
        if artist_id in seed_ids:
            continue
        if artist_id in feedback_profile.hidden_artist_ids:
            continue
        artist_name = scoring_data.artist_names.get(artist_id)
        if artist_name is None:
            continue
        knownness, rediscovery = scoring_data.listening_by_artist.get(artist_id, (0.0, 0.0))
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
            rediscovery_score=rediscovery,
        ):
            continue
        weights = config.recommendation.weights
        raw_score = _raw_score(
            weights,
            lastfm_score * mode.lastfm_similarity_multiplier,
            tag_score * mode.tag_similarity_multiplier,
            rediscovery * mode.rediscovery_multiplier,
            feedback.score,
        )
        knownness_penalty = 0.40 * max(0.0, knownness - mode.max_knownness)
        obviousness_penalty = (
            mode.obviousness_penalty_weight * obviousness if obviousness_enabled else 0.0
        )
        final_score = raw_score - knownness_penalty - obviousness_penalty
        if final_score <= 0:
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
                rediscovery_score=rediscovery,
                knownness=knownness,
                obviousness=obviousness,
                knownness_penalty=knownness_penalty,
                obviousness_penalty=obviousness_penalty,
                diversity_penalty=0.0,
                primary_tag=_primary_tag(artist_id, tag_profile, scoring_data),
                explanation={},
            )
        )
    return scores


def _rerank_for_diversity(
    scores: list[_CandidateScore],
    mode: RecommendationModeSettings,
    scoring_data: _ScoringData,
    *,
    limit: int,
    enabled: bool,
) -> list[_CandidateScore]:
    remaining = sorted(scores, key=lambda item: (-item.final_score, item.artist_name.casefold()))
    if not enabled:
        return remaining[:limit]

    selected: list[_CandidateScore] = []
    primary_tag_counts: dict[str, int] = {}
    while remaining and len(selected) < limit:
        best_index: int | None = None
        best_score: float | None = None
        best_penalty = 0.0
        for index, candidate in enumerate(remaining):
            if _would_break_primary_tag_cap(candidate, selected, primary_tag_counts, mode):
                continue
            similarity = max(
                (
                    _candidate_similarity(candidate, already_selected, scoring_data)
                    for already_selected in selected
                ),
                default=0.0,
            )
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
    return {
        "summary": (
            f"{score.artist_name} is similar to high-weight seed artists"
            " and fits tags from the local listening profile."
        ),
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
        "feedback": {
            "score": score.feedback_score,
            "effects": score.feedback_effects,
            "contributions": score.feedback_contributions,
        },
        "classification": {
            "is_new": score.knownness == 0.0,
            "is_rediscovery": score.rediscovery_score > 0.0,
            "is_obvious": score.obviousness >= 0.75,
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


def _persist_run(
    conn: DuckDBPyConnection,
    run_id: str,
    mode: str,
    config: ScrobbleRecsConfig,
    mode_settings: RecommendationModeSettings,
    seeds: list[SeedArtist],
    ranked: list[_CandidateScore],
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
        for rank, score in enumerate(ranked, start=1):
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
                    explanation
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    rank,
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


def _config_hash(config: ScrobbleRecsConfig) -> str:
    payload = config.recommendation.model_dump_json(by_alias=True)
    return sha256(payload.encode("utf-8")).hexdigest()
