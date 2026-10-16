"""Legacy admission, penalty and deterministic slate policy, with explicit reasons."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from math import isfinite, sqrt
from typing import TYPE_CHECKING, Literal

from duckdb import DuckDBPyConnection

from scrobble_recs.config import ScrobbleRecsConfig, WalkConfig
from scrobble_recs.export import _display_score
from scrobble_recs.models.feedback import (
    FeedbackProfile,
    load_feedback_profile,
)
from scrobble_recs.models.modes import RecommendationModeSettings, resolve_recommendation_mode
from scrobble_recs.models.recommendation_quality import DirectEvidence, evaluate_discovery_evidence
from scrobble_recs.models.recommendation_query import QueryContext
from scrobble_recs.models.retrieval import (
    RecommendationProgressCallback,
    _candidate_tag_vector,
    _report_progress,
    _ScoringData,
)
from scrobble_recs.models.serendipity import SerendipityEvidence
from scrobble_recs.models.walk import WalkNode, WalkResult

if TYPE_CHECKING:
    from scrobble_recs.models.ranking import RankedCandidate


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
    raw_components: dict[str, float]
    calibrated_components: dict[str, float]
    fatigue: _FatiguePenalty
    serendipity: SerendipityEvidence
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
    hop_distance: int | None = None
    walk_node: WalkNode | None = None


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
    evidence: DirectEvidence


@dataclass(frozen=True)
class _EligibleCandidate:
    artist_id: int
    artist_name: str
    lastfm_similarity_score: float
    tag_similarity_score: float
    feedback: _FeedbackScore
    rediscovery_score: float
    knownness: float
    obviousness: float
    trial_penalty: float
    primary_tag: str | None
    quality_gate: _QualityGateDecision
    fatigue: _FatiguePenalty
    local_scrobble_count: int
    listened_day_count: int
    first_listened_at: datetime | None
    last_listened_at: datetime | None
    years_since_last_listen: float
    rediscovery_eligible: bool
    identity_confidence: float
    identity_warnings: tuple[str, ...]
    hop_distance: int | None = None
    walk_node: WalkNode | None = None


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


@dataclass(frozen=True)
class LegacyPolicyAdjustments:
    knownness_penalty: float
    obviousness_penalty: float
    final_score: float


@dataclass(frozen=True)
class PolicyDecision:
    artist_id: int
    included: bool
    final_rank: int | None
    final_score: float
    adjustment: float
    reasons: tuple[str, ...]
    legacy_score: _CandidateScore | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "legacy_score", deepcopy(self.legacy_score))


@dataclass(frozen=True)
class CandidateEligibility:
    """The shared pre-calibration mode/safety view used by retrieval and ranking."""

    artist_id: int
    exclusion_reason: str | None
    rediscovery_score: float
    rediscovery_eligible: bool
    walk_node: WalkNode | None

    @property
    def eligible(self) -> bool:
        return self.exclusion_reason is None


def candidate_eligibility(
    artist_id: int,
    *,
    seed_ids: set[int],
    feedback_profile: FeedbackProfile,
    cooldown_artist_ids: set[int],
    scoring_data: _ScoringData,
    mode: RecommendationModeSettings,
    config: ScrobbleRecsConfig,
    tag_profile: dict[str, float],
    walk_result: WalkResult | None,
) -> CandidateEligibility:
    listening = scoring_data.listening_by_artist.get(artist_id, (0.0, 0.0, 0, 0, None, None))
    rediscovery = listening[1] if mode.name == "rediscovery" else 0.0
    rediscovery_eligible = _is_rediscovery_eligible(
        play_count=listening[2],
        active_days=listening[3],
        has_positive_feedback=artist_id in feedback_profile.boosted_seed_artist_ids,
    )
    node = walk_result.nodes.get(artist_id) if walk_result is not None else None
    reason = _admission_exclusion_reason(
        artist_id,
        seed_ids=seed_ids,
        feedback_profile=feedback_profile,
        cooldown_artist_ids=cooldown_artist_ids,
        scoring_data=scoring_data,
        mode=mode,
    )
    if reason is None:
        reason = _evidence_exclusion_reason(
            artist_id,
            mode=mode,
            rediscovery=rediscovery,
            rediscovery_eligible=rediscovery_eligible,
            walk_engine=config.recommendation.scoring.engine == "walk",
            walk_node=node,
            obviousness_enabled=config.recommendation.obviousness.enabled,
            tag_profile=tag_profile,
            scoring_data=scoring_data,
        )
    return CandidateEligibility(artist_id, reason, rediscovery, rediscovery_eligible, node)


def _admission_exclusion_reason(
    artist_id: int,
    *,
    seed_ids: set[int],
    feedback_profile: FeedbackProfile,
    cooldown_artist_ids: set[int],
    scoring_data: _ScoringData,
    mode: RecommendationModeSettings,
) -> str | None:
    """Legacy admission precedes calibration; its population must stay fixed."""
    if artist_id in seed_ids:
        return "seed_artist"
    if artist_id in feedback_profile.hidden_artist_ids:
        return "hidden_artist"
    if artist_id in cooldown_artist_ids:
        return "feedback_cooldown"
    if artist_id not in scoring_data.artist_names:
        return "missing_artist_name"
    listening = scoring_data.listening_by_artist.get(artist_id)
    if mode.name != "rediscovery" and listening is not None and listening[2] > 0:
        return "locally_listened"
    return None


def _evidence_exclusion_reason(
    artist_id: int,
    *,
    mode: RecommendationModeSettings,
    rediscovery: float,
    rediscovery_eligible: bool,
    walk_engine: bool,
    walk_node: WalkNode | None,
    obviousness_enabled: bool,
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
) -> str | None:
    if mode.name == "rediscovery" and rediscovery > 0.0 and not rediscovery_eligible:
        return "rediscovery_not_eligible"
    if walk_engine and (
        walk_node is None or walk_node.hop_distance < 0 or not isfinite(walk_node.lift)
    ):
        return "walk_unreachable"
    if obviousness_enabled and _hard_hides_obvious_candidate(
        artist_id, mode, tag_profile, scoring_data, rediscovery_score=rediscovery
    ):
        return "obvious_candidate"
    return None


def _legacy_policy_adjustments(
    raw_score: float,
    candidate: _EligibleCandidate,
    mode: RecommendationModeSettings,
    *,
    obviousness_enabled: bool,
    serendipity_bonus: float,
) -> LegacyPolicyAdjustments:
    knownness = 0.40 * max(0.0, candidate.knownness - mode.max_knownness)
    obviousness = (
        mode.obviousness_penalty_weight * candidate.obviousness if obviousness_enabled else 0.0
    )
    final = raw_score - knownness - obviousness - candidate.fatigue.penalty
    if mode.name == "balanced":
        final -= candidate.trial_penalty
    final *= 0.5 + 0.5 * candidate.identity_confidence
    final += serendipity_bonus
    return LegacyPolicyAdjustments(knownness, obviousness, final)


def _load_fatigue_by_artist(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    feedback_profile: FeedbackProfile,
    *,
    as_of: datetime | None = None,
) -> dict[int, _FatiguePenalty]:
    fatigue = config.recommendation.fatigue
    if not fatigue.enabled:
        return {}
    cutoff_clause = "" if as_of is None else "WHERE created_at < ?"
    params: list[object] = (
        [fatigue.lookback_runs] if as_of is None else [as_of, fatigue.lookback_runs]
    )
    recent_runs = conn.execute(
        f"""
        SELECT run_id
        FROM recommendation_run
        {cutoff_clause}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
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


def _rerank_for_diversity(
    scores: list[_CandidateScore],
    mode: RecommendationModeSettings,
    scoring_data: _ScoringData,
    *,
    limit: int,
    enabled: bool,
    progress_callback: RecommendationProgressCallback | None = None,
) -> list[_CandidateScore]:
    remaining = sorted(scores, key=lambda item: (-item.final_score, item.artist_id))
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
                and candidate.artist_id < remaining[best_index].artist_id
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


def _quality_gate_decision(
    config: ScrobbleRecsConfig,
    mode: RecommendationModeSettings,
    *,
    edges: tuple[tuple[int, float], ...],
    tag_score: float,
    rediscovery_score: float,
) -> _QualityGateDecision:
    gate = config.recommendation.quality_gate
    evidence_decision = evaluate_discovery_evidence(
        edges=edges,
        tag_score=tag_score,
        config=config.recommendation.discovery_evidence,
    )
    thresholds = {
        "minimum_seed_edge": config.recommendation.discovery_evidence.minimum_seed_edge,
        "strong_seed_edge": config.recommendation.discovery_evidence.strong_seed_edge,
        "minimum_seed_count": float(config.recommendation.discovery_evidence.minimum_seed_count),
        "minimum_tag_reinforcement": (
            config.recommendation.discovery_evidence.minimum_tag_reinforcement
        ),
    }
    if mode.name != "rediscovery":
        return _QualityGateDecision(
            passed=evidence_decision.passed,
            reasons=evidence_decision.reasons,
            thresholds=thresholds,
            evidence=evidence_decision.evidence,
        )
    if gate.rediscovery_requires_taste_match:
        return _QualityGateDecision(
            passed=evidence_decision.passed,
            reasons=evidence_decision.reasons,
            thresholds=thresholds,
            evidence=evidence_decision.evidence,
        )
    if not gate.enabled:
        return _QualityGateDecision(
            passed=True,
            reasons=("disabled",),
            thresholds=thresholds,
            evidence=evidence_decision.evidence,
        )
    if mode.name == "rediscovery" and rediscovery_score > 0.0:
        return _QualityGateDecision(
            passed=True,
            reasons=("rediscovery",),
            thresholds=thresholds,
            evidence=evidence_decision.evidence,
        )
    return _QualityGateDecision(
        passed=evidence_decision.passed,
        reasons=evidence_decision.reasons,
        thresholds=thresholds,
        evidence=evidence_decision.evidence,
    )


def _walk_gate_decision(walk_node: WalkNode, walk_config: WalkConfig) -> _QualityGateDecision:
    """Replace the two-branch discovery gate with a lift floor plus a minimum
    number of distinct seeds reaching the candidate within ``max_hops``."""
    lift_ok = walk_node.lift >= walk_config.min_lift
    seeds_ok = walk_node.distinct_seed_count >= walk_config.min_distinct_seeds
    passed = lift_ok and seeds_ok
    reasons: list[str] = []
    if passed:
        reasons.append("walk_lift_and_reachability")
    else:
        if not lift_ok:
            reasons.append("walk_below_min_lift")
        if not seeds_ok:
            reasons.append("walk_insufficient_distinct_seeds")
    evidence = DirectEvidence(
        distinct_seed_count=walk_node.distinct_seed_count,
        strongest_edge=walk_node.lift,
        qualifying_edges=(),
    )
    thresholds = {
        "min_lift": walk_config.min_lift,
        "min_distinct_seeds": float(walk_config.min_distinct_seeds),
    }
    return _QualityGateDecision(
        passed=passed,
        reasons=tuple(reasons),
        thresholds=thresholds,
        evidence=evidence,
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
    raw_components: dict[str, float],
    calibrated_components: dict[str, float],
    rediscovery_score: float,
) -> dict[str, object]:
    return {
        "passed": decision.passed,
        "reasons": list(decision.reasons),
        "thresholds": decision.thresholds,
        "direct_evidence": {
            "distinct_seed_count": decision.evidence.distinct_seed_count,
            "strongest_edge": decision.evidence.strongest_edge,
            "qualifying_edges": [list(edge) for edge in decision.evidence.qualifying_edges],
        },
        "raw_components": raw_components,
        "calibrated_components": calibrated_components,
        "scores": {
            "lastfm_similarity_score": raw_components["lastfm_similarity"],
            "tag_similarity_score": raw_components["tag_similarity"],
            "feedback_score": raw_components["feedback"],
            "rediscovery_score": rediscovery_score,
        },
    }


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


def apply_slate_policy(
    conn: DuckDBPyConnection,
    *,
    context: QueryContext,
    ranked: Sequence[RankedCandidate],
    limit: int,
    config: ScrobbleRecsConfig,
    progress_callback: RecommendationProgressCallback | None = None,
    mode_settings: RecommendationModeSettings | None = None,
) -> tuple[PolicyDecision, ...]:
    """Select the legacy slate and report its adjustment from pre-policy relevance."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    mode = mode_settings or next(
        (item.legacy.retrieval.inputs.mode for item in ranked if item.legacy is not None), None
    )
    if mode is None:
        if not ranked:
            return ()
        mode = resolve_recommendation_mode(context.mode, config)
    feedback = load_feedback_profile(conn, as_of=context.feedback_cutoff)
    hidden = feedback.hidden_artist_ids
    scores = [
        item.legacy.score
        for item in ranked
        if item.legacy is not None
        and item.legacy.score is not None
        and item.artist_id not in hidden
    ]
    data = next(
        (item.legacy.retrieval.scoring_data for item in ranked if item.legacy is not None), None
    )
    if scores:
        assert data is not None
        selected = _rerank_for_diversity(
            scores,
            mode,
            data,
            limit=limit,
            enabled=config.recommendation.diversity.enabled,
            progress_callback=progress_callback,
        )
    else:
        selected = []
    maximum = max((score.final_score for score in selected), default=0.0)
    chosen = {
        score.artist_id: (
            rank,
            replace(
                score,
                normalized_score=_display_score(score.final_score, maximum),
                score_band=_classify_score_band(
                    normalized=_display_score(score.final_score, maximum),
                    raw=score.raw_score,
                    evidence_count=score.evidence_source_count,
                ),
            ),
        )
        for rank, score in enumerate(selected, start=1)
    }
    decisions = []
    original_ranks = {item.artist_id: rank for rank, item in enumerate(ranked, start=1)}
    for item in ranked:
        if item.artist_id in chosen:
            rank, score = chosen[item.artist_id]
            reasons = ["included"]
            if score.fatigue.penalty:
                reasons.append("fatigue")
            if score.knownness_penalty:
                reasons.append("knownness")
            if score.obviousness_penalty:
                reasons.append("obviousness")
            if score.trial_penalty and mode.name == "balanced":
                reasons.append("trial_listen")
            if score.diversity_penalty:
                reasons.append("diversity_penalty")
            if rank != original_ranks[item.artist_id]:
                reasons.append("policy_reordered")
            decisions.append(
                PolicyDecision(
                    item.artist_id,
                    True,
                    rank,
                    score.final_score,
                    score.final_score - item.relevance_score,
                    tuple(reasons),
                    score,
                )
            )
        else:
            excluded_reasons = _excluded_slate_reasons(
                item,
                hidden,
                selected,
                mode,
                diversity_enabled=config.recommendation.diversity.enabled,
            )
            decisions.append(
                PolicyDecision(
                    item.artist_id, False, None, 0.0, -item.relevance_score, excluded_reasons
                )
            )
    return tuple(
        sorted(
            decisions,
            key=lambda item: (item.final_rank is None, item.final_rank or 0, item.artist_id),
        )
    )


def _excluded_slate_reasons(
    item: RankedCandidate,
    hidden: set[int],
    selected: list[_CandidateScore],
    mode: RecommendationModeSettings,
    *,
    diversity_enabled: bool,
) -> tuple[str, ...]:
    if item.artist_id in hidden:
        return ("hidden_artist",)
    if item.legacy is None:
        return ("missing_legacy_score",)
    diagnostic = item.legacy.diagnostic
    if diagnostic.state != "eligible":
        return diagnostic.reasons
    score = item.legacy.score
    if score is None:
        return ("candidate_only_tail",)
    if diversity_enabled and score.primary_tag is not None and len(selected) < 20:
        same_tag_count = sum(chosen.primary_tag == score.primary_tag for chosen in selected)
        if same_tag_count >= mode.same_primary_tag_cap:
            return ("primary_tag_cap",)
    return ("slate_limit",)
