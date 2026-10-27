"""Legacy relevance features and explanations over the supplied retrieval pool."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from math import isfinite

from duckdb import DuckDBPyConnection

from scrobble_recs.config import RecommendationWeightsConfig, ScrobbleRecsConfig
from scrobble_recs.models.evidence import AdmissionDecision, CandidateEvidence
from scrobble_recs.models.feedback import (
    FeedbackProfile,
    feedback_obviousness_for_candidate,
    load_cooldown_artist_ids,
)
from scrobble_recs.models.fusion import FusedCandidate, SourceCandidate
from scrobble_recs.models.modes import RecommendationModeSettings
from scrobble_recs.models.policy import (
    PolicyDecision,
    _CandidateDiagnostic,
    _CandidateScore,
    _compute_trial_penalty,
    _EligibleCandidate,
    _excluded_candidate_diagnostic,
    _FatiguePenalty,
    _FeedbackScore,
    _legacy_policy_adjustments,
    _load_fatigue_by_artist,
    _quality_gate_decision,
    _quality_gate_payload,
    _walk_gate_decision,
    candidate_eligibility,
)
from scrobble_recs.models.profile import SeedArtist
from scrobble_recs.models.recommendation_quality import calibrate_positive_components
from scrobble_recs.models.recommendation_query import QueryContext
from scrobble_recs.models.retrieval import (
    LegacyRetrievalDetails,
    RecommendationProgressCallback,
    RetrievalResult,
    _candidate_tag_vector,
    _primary_tag,
    _rank_affinity,
    _report_progress,
    _ScoringData,
    _tag_similarity_score,
)
from scrobble_recs.models.seed_weighting import SeedIdfWeights
from scrobble_recs.models.serendipity import score_serendipity
from scrobble_recs.models.walk import WalkResult, top_paths


@dataclass(frozen=True)
class RelevanceFeatureRecord:
    schema_version: str
    values: dict[str, float | int | bool | str | None]
    missing_signals: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", dict(self.values))
        object.__setattr__(self, "missing_signals", tuple(self.missing_signals))


@dataclass(frozen=True)
class LegacyRankingDetails:
    """Preserve all legacy diagnostics, including rejected and truncated candidates."""

    score: _CandidateScore | None
    diagnostic: _CandidateDiagnostic
    diagnostic_order: int
    retrieval: LegacyRetrievalDetails


@dataclass(frozen=True)
class RankedCandidate:
    """Raw weighted candidate score and identity/serendipity relevance before policy penalties."""

    artist_id: int
    candidate_score: float
    relevance_score: float
    features: RelevanceFeatureRecord
    explanation: dict[str, object]
    legacy: LegacyRankingDetails | None = field(default=None, repr=False)
    evidence: CandidateEvidence | None = None
    admission: AdmissionDecision | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", deepcopy(self.features))
        object.__setattr__(self, "explanation", deepcopy(self.explanation))
        object.__setattr__(self, "evidence", deepcopy(self.evidence))


def _score_candidates(
    config: ScrobbleRecsConfig,
    mode: RecommendationModeSettings,
    seeds: list[SeedArtist],
    seed_ids: set[int],
    idf_weights: SeedIdfWeights,
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
    feedback_profile: FeedbackProfile,
    fatigue_by_artist: dict[int, _FatiguePenalty],
    cooldown_artist_ids: set[int],
    walk_result: WalkResult | None,
    *,
    as_of: datetime | None = None,
    progress_callback: RecommendationProgressCallback | None = None,
    admissions: dict[int, AdmissionDecision] | None = None,
    context: QueryContext | None = None,
    fused: dict[int, FusedCandidate] | None = None,
) -> tuple[list[_CandidateScore], list[_CandidateDiagnostic]]:
    # Inverse-popularity weighting is applied at aggregation only. At
    # ``seed_idf_alpha == 0`` these equal the raw seed weights exactly, so the
    # legacy path is preserved byte-for-byte.
    seed_weight_by_id = idf_weights.effective_weight_by_seed
    total_seed_weight = sum(seed_weight_by_id.values()) or 1.0
    artist_query = context is not None and context.query_kind == "artist"
    walk_engine = config.recommendation.scoring.engine == "walk"
    scores: list[_CandidateScore] = []
    candidate_only_scores: list[_CandidateScore] = []
    eligible_candidates: list[_EligibleCandidate] = []
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
        eligibility = candidate_eligibility(
            artist_id,
            seed_ids=seed_ids,
            feedback_profile=feedback_profile,
            cooldown_artist_ids=cooldown_artist_ids,
            scoring_data=scoring_data,
            mode=mode,
            config=config,
            tag_profile=tag_profile,
            walk_result=walk_result,
            context=context,
        )
        if eligibility.exclusion_reason is not None:
            diagnostics.append(
                _excluded_candidate_diagnostic(artist_id, eligibility.exclusion_reason)
            )
            continue
        artist_name = scoring_data.artist_names[artist_id]
        listening = scoring_data.listening_by_artist.get(artist_id, (0.0, 0.0, 0, 0, None, None))
        knownness, rediscovery, local_play_count, local_active_days, first_played, last_played = (
            listening
        )
        scored_rediscovery = eligibility.rediscovery_score
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
        trial_penalty = _compute_trial_penalty(
            play_count=local_play_count,
            active_days=local_active_days,
            years_since_last_listen=years_since_last_listen,
        )
        rediscovery_eligible = eligibility.rediscovery_eligible
        walk_node = eligibility.walk_node
        has_walk_signal = (
            walk_node is not None and isfinite(walk_node.lift) and walk_node.hop_distance >= 0
        )
        if walk_engine and has_walk_signal:
            assert walk_node is not None
            lastfm_score = walk_node.lift
        elif walk_engine and artist_query:
            lastfm_score = 0.0
        else:
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
        raw_components = {
            "lastfm_similarity": lastfm_score,
            "tag_similarity": tag_score,
            "feedback": feedback.score,
        }
        if walk_engine and has_walk_signal:
            assert walk_node is not None
            quality_gate = _walk_gate_decision(walk_node, config.recommendation.walk)
        else:
            quality_gate = _quality_gate_decision(
                config,
                mode,
                edges=tuple(
                    (source_artist_id, similarity)
                    for source_artist_id, similarity in scoring_data.similarity_by_target.get(
                        artist_id, []
                    )
                    if source_artist_id in seed_weight_by_id
                ),
                tag_score=tag_score,
                rediscovery_score=scored_rediscovery,
            )
        admission = (admissions or {}).get(artist_id)
        if admission is not None and admission.admitted != quality_gate.passed:
            quality_gate = replace(
                quality_gate, passed=admission.admitted, reasons=admission.reasons
            )
        if not quality_gate.passed:
            diagnostics.append(
                _CandidateDiagnostic(
                    artist_id=artist_id,
                    state="rejected",
                    reasons=("quality_gate",),
                    quality_gate=_quality_gate_payload(
                        quality_gate,
                        raw_components=raw_components,
                        calibrated_components={key: 0.0 for key in raw_components},
                        rediscovery_score=scored_rediscovery,
                    ),
                )
            )
            continue

        fatigue = fatigue_by_artist.get(
            artist_id,
            _FatiguePenalty(prior_run_appearances=0, latest_run_rank=None, penalty=0.0),
        )
        identity_conf = scoring_data.identity_confidence_by_artist.get(artist_id, 0.50)
        identity_warnings = tuple(scoring_data.identity_warnings_by_artist.get(artist_id, []))
        eligible_candidates.append(
            _EligibleCandidate(
                artist_id=artist_id,
                artist_name=artist_name,
                lastfm_similarity_score=lastfm_score,
                tag_similarity_score=tag_score,
                feedback=feedback,
                rediscovery_score=scored_rediscovery,
                knownness=knownness,
                obviousness=obviousness,
                trial_penalty=trial_penalty,
                primary_tag=_primary_tag(artist_id, tag_profile, scoring_data),
                quality_gate=quality_gate,
                fatigue=fatigue,
                local_scrobble_count=local_play_count,
                listened_day_count=local_active_days,
                first_listened_at=first_played,
                last_listened_at=last_played,
                years_since_last_listen=years_since_last_listen,
                rediscovery_eligible=rediscovery_eligible,
                identity_confidence=identity_conf,
                identity_warnings=identity_warnings,
                hop_distance=None if walk_node is None else walk_node.hop_distance,
                walk_node=walk_node,
            )
        )
        if processed % report_interval == 0:
            _report_progress(
                progress_callback,
                phase="score_candidates",
                current=processed,
                total=total_candidates,
                subject=f"{len(eligible_candidates)} positive so far",
                status="processing",
            )

    raw_component_rows = [
        {
            "lastfm_similarity": candidate.lastfm_similarity_score,
            "tag_similarity": candidate.tag_similarity_score,
            "feedback": candidate.feedback.score,
        }
        for candidate in eligible_candidates
    ]
    calibrated_component_rows = (
        calibrate_positive_components(raw_component_rows)
        if config.recommendation.calibration.enabled
        else [dict(row) for row in raw_component_rows]
    )
    for candidate, raw_components, calibrated_components in zip(
        eligible_candidates, raw_component_rows, calibrated_component_rows, strict=True
    ):
        diagnostics.append(
            _CandidateDiagnostic(
                artist_id=candidate.artist_id,
                state="eligible",
                reasons=("eligible",),
                quality_gate=_quality_gate_payload(
                    candidate.quality_gate,
                    raw_components=raw_components,
                    calibrated_components=calibrated_components,
                    rediscovery_score=candidate.rediscovery_score,
                ),
            )
        )
        calibrated_feedback = (
            calibrated_components["feedback"]
            if candidate.feedback.score > 0.0
            else candidate.feedback.score
        )
        raw_score = _raw_score(
            config.recommendation.weights,
            calibrated_components["lastfm_similarity"] * mode.lastfm_similarity_multiplier,
            calibrated_components["tag_similarity"] * mode.tag_similarity_multiplier,
            candidate.rediscovery_score * mode.rediscovery_multiplier,
            calibrated_feedback,
        )
        relevance_components = {}
        if fused is not None:
            fusion_score = fused.get(candidate.artist_id)
            relevance_components = {
                "fusion_retrieval_contribution": (
                    fusion_score.fusion_score if fusion_score else 0.0
                )
                * mode.lastfm_similarity_multiplier,
                "fusion_feedback_contribution": config.recommendation.weights.feedback_model
                * calibrated_feedback,
                "fusion_rediscovery_contribution": config.recommendation.weights.rediscovery
                * candidate.rediscovery_score
                * mode.rediscovery_multiplier,
            }
            raw_score = sum(relevance_components.values())
        if artist_query and fused is None:
            assert context is not None and context.artist_settings is not None
            settings = context.artist_settings
            target_features = scoring_data.target_features[candidate.artist_id]
            relevance_components = {
                "artist_existing_raw_score": raw_score,
                "artist_target_contribution": settings.target_weight
                * float(target_features["target_relevance"] or 0.0),
                "artist_personal_contribution": settings.personal_weight
                * float(target_features["target_personal_agreement"] or 0.0),
                "artist_existing_contribution": settings.existing_weight * raw_score,
            }
            raw_score = sum(
                relevance_components[key]
                for key in (
                    "artist_target_contribution",
                    "artist_personal_contribution",
                    "artist_existing_contribution",
                )
            )
        evidence_count = sum(
            1
            for value in (
                candidate.lastfm_similarity_score,
                candidate.tag_similarity_score,
                candidate.feedback.score,
                candidate.rediscovery_score,
            )
            if value > 0.0
        )
        serendipity = score_serendipity(
            candidate_edges=tuple(
                (source_artist_id, similarity)
                for source_artist_id, similarity in scoring_data.similarity_by_target.get(
                    candidate.artist_id, []
                )
                if source_artist_id in seed_ids
            ),
            seed_graph=scoring_data.seed_graph,
            config=config.recommendation.serendipity,
        )
        adjustments = _legacy_policy_adjustments(
            raw_score,
            candidate,
            mode,
            obviousness_enabled=obviousness_enabled,
            serendipity_bonus=serendipity.calibrated_bonus,
        )
        final_score = adjustments.final_score
        score = _CandidateScore(
            artist_id=candidate.artist_id,
            artist_name=candidate.artist_name,
            final_score=max(0.0, final_score),
            raw_score=raw_score,
            lastfm_similarity_score=candidate.lastfm_similarity_score,
            tag_similarity_score=candidate.tag_similarity_score,
            feedback_score=candidate.feedback.score,
            feedback_contributions=candidate.feedback.contributions,
            feedback_effects=candidate.feedback.effects,
            rediscovery_score=candidate.rediscovery_score,
            knownness=candidate.knownness,
            obviousness=candidate.obviousness,
            knownness_penalty=adjustments.knownness_penalty,
            obviousness_penalty=adjustments.obviousness_penalty,
            diversity_penalty=0.0,
            primary_tag=candidate.primary_tag,
            quality_gate=candidate.quality_gate,
            raw_components=raw_components,
            calibrated_components=calibrated_components,
            fatigue=candidate.fatigue,
            serendipity=serendipity,
            explanation={},
            score_band="candidate_only",
            normalized_score=0.0,
            evidence_source_count=evidence_count,
            trial_penalty=candidate.trial_penalty,
            local_scrobble_count=candidate.local_scrobble_count,
            listened_day_count=candidate.listened_day_count,
            first_listened_at=candidate.first_listened_at,
            last_listened_at=candidate.last_listened_at,
            years_since_last_listen=candidate.years_since_last_listen,
            rediscovery_eligible=candidate.rediscovery_eligible,
            identity_confidence=candidate.identity_confidence,
            identity_warnings=candidate.identity_warnings,
            hop_distance=candidate.hop_distance,
            walk_node=candidate.walk_node,
            relevance_components=relevance_components,
        )
        if final_score <= 0.0:
            candidate_only_scores.append(score)
        else:
            scores.append(score)
    _report_progress(
        progress_callback,
        phase="score_candidates",
        current=total_candidates,
        total=total_candidates,
        subject=f"{len(scores)} positive scores, {len(candidate_only_scores)} candidate_only",
        status="completed",
    )
    candidate_only_scores.sort(key=lambda s: (-s.raw_score, s.artist_id))
    return scores + (
        candidate_only_scores
        if artist_query
        else candidate_only_scores[: min(len(scores) * 2, 500)]
    ), diagnostics


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
    idf_weights: SeedIdfWeights,
    tag_profile: dict[str, float],
    walk_result: WalkResult | None,
    seed_ids: set[int],
) -> dict[str, object]:
    effective_weight_by_id = idf_weights.effective_weight_by_seed
    seed_rows = [
        (
            source_artist_id,
            scoring_data.artist_names.get(source_artist_id, ""),
            similarity,
            effective_weight_by_id.get(source_artist_id, 0.0) * similarity,
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

    walk_block = _walk_explanation(score, scoring_data, walk_result, seed_ids, idf_weights)
    explanation: dict[str, object] = {
        "summary": summary,
        "top_seed_artists": top_seed_names,
        "top_seed_edge_scores": top_seed_scores,
        "matching_tags": matching_tags[:5],
        "tag_fit_score": score.tag_similarity_score,
        "lastfm_similarity_score": score.lastfm_similarity_score,
        "rediscovery_score": score.rediscovery_score,
        "feedback_score": score.feedback_score,
        "raw_components": score.raw_components,
        "calibrated_components": score.calibrated_components,
        "serendipity": {
            "neighbors": list(score.serendipity.neighbors),
            "cohesion": score.serendipity.cohesion,
            "component_ids": list(score.serendipity.component_ids),
            "is_bridge": score.serendipity.is_bridge,
            "raw_bonus": score.serendipity.raw_bonus,
            "calibrated_bonus": score.serendipity.calibrated_bonus,
        },
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
            "direct_evidence": {
                "distinct_seed_count": score.quality_gate.evidence.distinct_seed_count,
                "strongest_edge": score.quality_gate.evidence.strongest_edge,
                "qualifying_edges": [
                    list(edge) for edge in score.quality_gate.evidence.qualifying_edges
                ],
            },
            "raw_components": score.raw_components,
            "calibrated_components": score.calibrated_components,
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
                "idf_weight": idf_weights.idf_by_seed.get(source_artist_id, 1.0),
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
    if walk_block is not None:
        explanation["walk"] = walk_block
    return explanation


def _walk_explanation(
    score: _CandidateScore,
    scoring_data: _ScoringData,
    walk_result: WalkResult | None,
    seed_ids: set[int],
    idf_weights: SeedIdfWeights,
) -> dict[str, object] | None:
    """Human-readable walk evidence: top weighted paths, contributing seeds with
    their IDF weights, hop distance, lift, and raw/baseline mass.

    PPR mass is not itself an explanation, so paths from seeds are the primary
    artifact here.
    """
    if walk_result is None or score.walk_node is None:
        return None
    node = score.walk_node
    paths = top_paths(walk_result, target=score.artist_id, seed_ids=seed_ids, max_paths=3)
    contributing_seeds = sorted(
        {seed_id for path in paths for seed_id in path.node_ids if seed_id in seed_ids}
    )
    return {
        "lift": node.lift,
        "personalized_mass": node.ppr_personalized,
        "baseline_mass": node.ppr_uniform,
        "hop_distance": node.hop_distance,
        "distinct_seed_count": node.distinct_seed_count,
        "node_cap_bound": walk_result.diagnostics.node_cap_bound,
        "iteration_cap_bound": walk_result.diagnostics.iteration_cap_bound,
        "top_paths": [
            {
                "artists": [
                    scoring_data.artist_names.get(node_id, str(node_id))
                    for node_id in path.node_ids
                ],
                "artist_ids": list(path.node_ids),
                "weight": path.weight,
            }
            for path in paths
        ],
        "contributing_seeds": [
            {
                "artist": scoring_data.artist_names.get(seed_id, str(seed_id)),
                "artist_id": seed_id,
                "idf_weight": idf_weights.idf_by_seed.get(seed_id, 1.0),
            }
            for seed_id in contributing_seeds
        ],
    }


def _rank_penalty(rank: int) -> float:
    return _rank_affinity(rank)


def rank_candidates(
    conn: DuckDBPyConnection,
    *,
    context: QueryContext,
    retrieval: RetrievalResult,
    config: ScrobbleRecsConfig,
    progress_callback: RecommendationProgressCallback | None = None,
) -> tuple[RankedCandidate, ...]:
    """Calibrate only admitted candidates; retain excluded diagnostics outside that population.

    Legacy admission and adjustment helpers belong to policy. Their compatibility
    prepass preserves the historical calibration population and arithmetic order.
    Explanations are finalized with selected ranks/penalties after slate policy.
    """
    if retrieval.legacy is None:
        raise ValueError("legacy ranking requires legacy retrieval details")
    # One owned graph snapshot is shared by every returned candidate; copying it
    # inside RankedCandidate would multiply graph work by the candidate count.
    details = deepcopy(retrieval.legacy)
    details = replace(
        details,
        scoring_data=replace(
            details.scoring_data,
            candidates={candidate.artist_id for candidate in retrieval.candidates},
        ),
    )
    inputs = details.inputs
    seed_ids = {seed.artist_id for seed in inputs.seeds}
    fatigue = _load_fatigue_by_artist(
        conn, config, inputs.feedback_profile, as_of=context.feedback_cutoff
    )
    cooldown = load_cooldown_artist_ids(
        conn,
        as_of=inputs.as_of,
        days=config.recommendation.feedback_cooldown_days,
        until=context.feedback_cutoff,
    )
    fused = (
        {row.artist_id: row for row in retrieval.fused_candidates}
        if config.recommendation.scoring.engine == "fusion"
        else None
    )
    scores, diagnostics = _score_candidates(
        config,
        inputs.mode,
        list(inputs.seeds),
        seed_ids,
        inputs.idf_weights,
        inputs.tag_profile,
        details.scoring_data,
        inputs.feedback_profile,
        fatigue,
        cooldown,
        details.walk_result,
        as_of=inputs.as_of,
        progress_callback=progress_callback,
        admissions=retrieval.admissions,
        context=context,
        fused=fused,
    )
    score_by_id = {score.artist_id: score for score in scores}
    source_rows_by_artist: dict[int, list[SourceCandidate]] = {}
    for row in retrieval.source_candidates:
        source_rows_by_artist.setdefault(row.artist_id, []).append(row)
    result = []
    for order, diagnostic in enumerate(diagnostics):
        source_rows = source_rows_by_artist.get(diagnostic.artist_id, [])
        score = score_by_id.get(diagnostic.artist_id)
        values: dict[str, float | int | bool | str | None] = {}
        if score is not None:
            values = {
                "lastfm_similarity_score": score.lastfm_similarity_score,
                "tag_similarity_score": score.tag_similarity_score,
                "feedback_score": score.feedback_score,
                "rediscovery_score": score.rediscovery_score,
                "knownness": score.knownness,
                "obviousness": score.obviousness,
                "raw_score": score.raw_score,
                "identity_confidence": score.identity_confidence,
            }
            values.update(
                {f"calibrated_{key}": value for key, value in score.calibrated_components.items()}
            )
        if context.query_kind == "artist":
            values.update(details.scoring_data.target_features.get(diagnostic.artist_id, {}))
            values["target_obviousness"] = None if score is None else score.obviousness
            if score is not None:
                values.update(score.relevance_components)
        if fused is not None:
            item = fused.get(diagnostic.artist_id)
            values.update(
                {
                    "fusion_score": item.fusion_score if item else 0.0,
                    "fusion_independent_family_count": item.independent_family_count if item else 0,
                    "fusion_unranked_membership": bool(item and item.unranked_sources),
                    "fusion_disagreement": _fusion_disagreement(source_rows),
                }
            )
            if item:
                values.update(
                    {
                        f"fusion_source_{key}": value
                        for key, value in item.source_contributions.items()
                    }
                )
                values.update(
                    {
                        f"fusion_family_{key}": value
                        for key, value in item.family_contributions.items()
                    }
                )
                values.update({f"fusion_member_{source}": True for source in item.unranked_sources})
            statuses = retrieval.diagnostics.get("fusion_sources", {})
            assert isinstance(statuses, dict)
            for source, status in statuses.items():
                values[f"fusion_status_{source}"] = status["status"]
                values[f"fusion_missing_{source}"] = status["status"] in (
                    "missing",
                    "disabled",
                    "unavailable",
                )
                values[f"fusion_stale_{source}"] = bool(status.get("stale", False))
            if score is not None:
                values.update(score.relevance_components)
        if fused is not None:
            for row in source_rows:
                values[f"fusion_rank_{row.source}"] = row.source_rank
                values[f"fusion_confidence_{row.source}"] = max(
                    float(values.get(f"fusion_confidence_{row.source}", 0.0) or 0.0), row.confidence
                )
        missing = _missing_signals(diagnostic.artist_id, details)
        explanation: dict[str, object] = {
            "eligibility_state": diagnostic.state,
            "eligibility_reasons": list(diagnostic.reasons),
        }
        if fused is not None:
            item = fused.get(diagnostic.artist_id)
            explanation["retrieval_fusion"] = {
                **(asdict(item) if item is not None else {}),
                "source_ranks": {row.source: row.source_rank for row in source_rows},
                "disagreement": values["fusion_disagreement"],
            }
        if score is not None:
            explanation.update(
                raw_components=score.raw_components,
                calibrated_components=score.calibrated_components,
                identity_confidence=score.identity_confidence,
                serendipity_bonus=score.serendipity.calibrated_bonus,
            )
        result.append(
            RankedCandidate(
                diagnostic.artist_id,
                0.0 if score is None else score.raw_score,
                0.0
                if score is None
                else (
                    score.raw_score * (0.5 + 0.5 * score.identity_confidence)
                    + score.serendipity.calibrated_bonus
                ),
                RelevanceFeatureRecord(context.versions.feature_schema, values, missing),
                explanation,
                LegacyRankingDetails(score, diagnostic, order, details),
                retrieval.evidence.get(
                    diagnostic.artist_id, CandidateEvidence(diagnostic.artist_id, ())
                ),
                (
                    replace(
                        retrieval.admissions[diagnostic.artist_id],
                        admitted=False,
                        reasons=diagnostic.reasons,
                    )
                    if diagnostic.state == "excluded"
                    and diagnostic.artist_id in retrieval.admissions
                    else retrieval.admissions.get(
                        diagnostic.artist_id,
                        AdmissionDecision(score is not None, 0, diagnostic.reasons),
                    )
                ),
            )
        )
    return tuple(sorted(result, key=lambda item: (-item.relevance_score, item.artist_id)))


def _missing_signals(artist_id: int, details: LegacyRetrievalDetails) -> tuple[str, ...]:
    data = details.scoring_data
    feedback = details.inputs.feedback_profile
    missing = []
    if not data.similarity_by_target.get(artist_id):
        missing.append("lastfm_similarity")
    if not details.inputs.tag_profile or not _candidate_tag_vector(artist_id, data):
        missing.append("tag_similarity")
    if (
        artist_id not in feedback.artist_adjustments
        and not feedback.tag_adjustments
        and not data.feedback_similarity_by_target.get(artist_id)
    ):
        missing.append("feedback")
    if artist_id not in data.listening_by_artist:
        missing.append("listening_history")
    walk_node = None if details.walk_result is None else details.walk_result.nodes.get(artist_id)
    if walk_node is None or walk_node.hop_distance < 0 or not isfinite(walk_node.lift):
        missing.append("walk")
    return tuple(missing)


def build_policy_explanations(
    decisions: tuple[PolicyDecision, ...], retrieval: RetrievalResult
) -> tuple[PolicyDecision, ...]:
    assert retrieval.legacy is not None
    details = retrieval.legacy
    inputs = details.inputs
    result = tuple(
        replace(
            decision,
            legacy_score=replace(
                decision.legacy_score,
                explanation=_explanation(
                    decision.legacy_score,
                    details.scoring_data,
                    seed_weight_by_id={seed.artist_id: seed.seed_weight for seed in inputs.seeds},
                    idf_weights=inputs.idf_weights,
                    tag_profile=inputs.tag_profile,
                    walk_result=details.walk_result,
                    seed_ids={seed.artist_id for seed in inputs.seeds},
                ),
            ),
        )
        if decision.legacy_score is not None
        else decision
        for decision in decisions
    )
    if details.scoring_data.target_features:
        result = tuple(
            replace(
                decision,
                legacy_score=replace(
                    decision.legacy_score,
                    explanation={
                        **decision.legacy_score.explanation,
                        "artist_query": {
                            "target_artist_id": retrieval.diagnostics["target_artist_id"],
                            **details.scoring_data.target_features[decision.artist_id],
                        },
                    },
                ),
            )
            if decision.legacy_score is not None
            else decision
            for decision in result
        )
    if retrieval.source_candidates:
        fusion = {row.artist_id: row for row in retrieval.fused_candidates}
        result = tuple(
            replace(
                decision,
                legacy_score=replace(
                    decision.legacy_score,
                    explanation={
                        **decision.legacy_score.explanation,
                        "retrieval_fusion": asdict(fusion[decision.artist_id])
                        if decision.artist_id in fusion
                        else {},
                        "personal_relevance": _personal_relevance_explanation(
                            decision.legacy_score
                        ),
                    },
                ),
            )
            if decision.legacy_score is not None
            else decision
            for decision in result
        )
    return result


def _fusion_disagreement(rows: list[SourceCandidate]) -> float:
    ranks = [row.source_rank for row in rows if row.source_rank is not None]
    return (max(ranks) - min(ranks)) / max(ranks) if ranks else 0.0


def _personal_relevance_explanation(score: _CandidateScore) -> dict[str, object]:
    identity_multiplier = 0.5 + 0.5 * score.identity_confidence
    components = {
        "identity_scaled_candidate": score.raw_score * identity_multiplier,
        "serendipity_bonus": score.serendipity.calibrated_bonus,
    }
    return {
        "score": sum(components.values()),
        "raw_candidate_score": score.raw_score,
        "raw_candidate_components": score.relevance_components,
        "identity_multiplier": identity_multiplier,
        "components": components,
    }
