from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite

from scrobble_recs.config import DiscoveryEvidenceConfig
from scrobble_recs.models.evidence import (
    AdmissionDecision,
    CandidateEvidence,
    EvidenceObservation,
    is_legacy_compatible,
    is_translated_legacy,
)
from scrobble_recs.models.recommendation_query import QueryContext


def decide_candidate_admission(
    evidence: CandidateEvidence,
    *,
    stage: int,
    hidden: bool,
    strongly_rejected: bool,
    identity_confidence: float,
    config: DiscoveryEvidenceConfig,
    context: QueryContext | None = None,
    tag_score: float = 0.0,
    seed_ids: set[int] | None = None,
    walk_supported: bool = False,
    legacy_policy_passed: bool = False,
    target_neighborhood_member: bool | None = None,
) -> AdmissionDecision:
    """Evaluate one explicit retrieval stage, before relevance calibration.

    Only translated legacy facts may use the migration identity exception. Raw
    provider values never qualify another provider's observations.
    """
    if stage not in range(5):
        raise ValueError("stage must be between 0 and 4")

    def result(admitted: bool, *reasons: str) -> AdmissionDecision:
        return AdmissionDecision(admitted, stage, reasons)

    if hidden:
        return result(False, "hidden_artist")
    if strongly_rejected:
        return result(False, "strongly_rejected")
    if (
        not isfinite(identity_confidence)
        or identity_confidence < config.low_confidence_identity_threshold
    ):
        return result(False, "identity_below_minimum")
    if (
        context is not None
        and context.query_kind == "artist"
        and target_neighborhood_member is not None
    ):
        if identity_confidence < config.identity_safety_threshold:
            return result(False, "identity_below_safety_threshold")
        if target_neighborhood_member:
            return result(True, "target_neighborhood_evidence")
        return result(False, "outside_target_neighborhood")
    legacy = tuple(
        row for row in evidence.observations if row.source == "lastfm" and is_translated_legacy(row)
    )
    edges = tuple(
        (row.source_artist_id, _lastfm_edge(row))
        for row in legacy
        if row.relationship_kind == "similar_artist"
        and row.source_artist_id is not None
        and (seed_ids is None or row.source_artist_id in seed_ids)
    )
    old = evaluate_discovery_evidence(edges=edges, tag_score=tag_score, config=config)
    only_legacy = all(is_legacy_compatible(row) for row in evidence.observations)
    legacy_passed = only_legacy and (old.passed or legacy_policy_passed)
    low_identity = identity_confidence < config.identity_safety_threshold
    if low_identity and stage < 4 and not legacy_passed:
        return result(False, "identity_below_safety_threshold")
    if legacy_passed:
        reasons = old.reasons if old.passed else ("legacy_policy_admission",)
        return result(True, *reasons, *(("legacy_identity_compatibility",) if low_identity else ()))
    positive_personal = any(_positive_personal(row) for row in evidence.observations)
    if only_legacy and legacy and not positive_personal and not (stage >= 3 and walk_supported):
        return result(False, *old.reasons)

    rows = tuple(
        row for row in evidence.observations if row.family != "personal" or _positive_personal(row)
    )
    families = {row.family for row in rows}
    strengths = []
    for row in rows:
        strength = row.evidence.get("strength")
        if (
            row.source == "lastfm"
            and row.relationship_kind == "similar_artist"
            and (seed_ids is None or row.source_artist_id in seed_ids)
        ):
            edge = _lastfm_edge(row)
            strength = (
                "strong"
                if edge >= config.strong_seed_edge
                else "moderate"
                if edge >= config.minimum_seed_edge
                else None
            )
        strengths.append((row.family, strength))
    reason: str | None = None
    radio = any(
        row.source == "listenbrainz"
        and row.relationship_kind == "artist_radio_membership"
        and row.source_artist_id in (seed_ids or set())
        and _safe_identity_observation(row, config)
        for row in rows
    )
    semantic = (
        tag_score > 0
        and tag_score >= config.minimum_tag_reinforcement
        and any(row.family == "semantic" and row.source != "listenbrainz" for row in rows)
    )
    catalog = config.catalog_relationships_enabled and any(
        row.source == "musicbrainz"
        and row.relationship_kind == "catalog_relationship"
        and row.source_artist_id in (seed_ids or set())
        and _safe_identity_observation(row, config)
        and row.evidence.get("supported_relationship") is True
        for row in rows
    )
    if (
        radio
        and not low_identity
        and (semantic or catalog)
        and 1 + int(semantic) + int(catalog) >= config.minimum_independent_families
    ):
        reason = "unranked_radio_independent_reinforcement_v1"
    elif (
        any(family == "collaborative" and strength == "strong" for family, strength in strengths)
        and len(families) >= 2
    ):
        reason = "strong_collaborative_with_independent_family"
    elif (
        len({family for family, strength in strengths if strength in ("strong", "moderate")})
        >= config.minimum_independent_families
    ):
        reason = "independent_family_agreement"
    elif positive_personal:
        reason = "positive_personal_evidence"
    elif (
        context is not None
        and context.query_kind == "artist"
        and config.catalog_relationships_enabled
        and any(
            row.relationship_kind == "catalog_relationship"
            and row.evidence.get("supported_relationship") is True
            for row in rows
        )
    ):
        reason = "enabled_catalog_relationship"
    elif (
        stage >= 2
        and len(families) >= 2
        and any(
            family == "collaborative" and strength in ("moderate", "weak")
            for family, strength in strengths
        )
    ):
        reason = "relaxed_collaborative_with_independent_family"
    elif stage >= 3 and walk_supported:
        reason = "walk_lift_and_reachability"
    if reason is None:
        return result(False, "missing_source_aware_evidence")
    return result(True, reason, *(("low_confidence",) if low_identity else ()))


def _safe_identity_observation(row: EvidenceObservation, config: DiscoveryEvidenceConfig) -> bool:
    warnings = row.evidence.get("identity_warnings", [])
    return (
        row.identity_confidence >= config.identity_safety_threshold
        and isinstance(warnings, list)
        and all(isinstance(warning, str) for warning in warnings)
        and not any(
            token in warning.lower()
            for warning in warnings
            for token in ("ambiguous", "conflict", "multiple", "invalid")
        )
    )


def _lastfm_edge(row: EvidenceObservation) -> float:
    value = row.evidence.get("normalized_score", row.raw_score)
    return float(value) if isinstance(value, (int, float)) and isfinite(value) else 0.0


def _positive_personal(row: EvidenceObservation) -> bool:
    return (
        row.family == "personal"
        and row.relationship_kind == "personal_feedback"
        and row.raw_score is not None
        and row.raw_score > 0
        and row.evidence.get("action") in ("save", "more_like_this", "rediscovery_good")
    )


@dataclass(frozen=True)
class DirectEvidence:
    distinct_seed_count: int
    strongest_edge: float
    qualifying_edges: tuple[tuple[int, float], ...]


@dataclass(frozen=True)
class QualityDecision:
    passed: bool
    reasons: tuple[str, ...]
    evidence: DirectEvidence


def evaluate_discovery_evidence(
    *,
    edges: Sequence[tuple[int, float]],
    tag_score: float,
    config: DiscoveryEvidenceConfig,
) -> QualityDecision:
    qualifying = tuple(
        sorted(
            ((seed_id, edge) for seed_id, edge in edges if edge >= config.minimum_seed_edge),
            key=lambda item: (-item[1], item[0]),
        )
    )
    evidence = DirectEvidence(
        distinct_seed_count=len({seed_id for seed_id, _edge in qualifying}),
        strongest_edge=max((edge for _seed_id, edge in qualifying), default=0.0),
        qualifying_edges=qualifying,
    )
    if evidence.distinct_seed_count >= config.minimum_seed_count:
        return QualityDecision(True, ("independent_seed_support",), evidence)
    if (
        qualifying
        and evidence.strongest_edge >= config.strong_seed_edge
        and tag_score >= config.minimum_tag_reinforcement
    ):
        return QualityDecision(True, ("strong_edge_with_tag_reinforcement",), evidence)
    return QualityDecision(False, ("missing_direct_graph_evidence",), evidence)


def calibrate_positive_components(
    rows: Sequence[Mapping[str, float]],
) -> list[dict[str, float]]:
    calibrated = [{key: 0.0 for key in row} for row in rows]
    component_keys = {key for row in rows for key in row}
    for key in component_keys:
        positive_values = sorted(
            {float(row.get(key, 0.0)) for row in rows if row.get(key, 0.0) > 0.0}
        )
        if not positive_values:
            continue
        denominator = max(1, len(positive_values) - 1)
        percentile_by_value = {
            value: index / denominator for index, value in enumerate(positive_values)
        }
        if len(positive_values) == 1:
            percentile_by_value[positive_values[0]] = 1.0
        for index, row in enumerate(rows):
            value = float(row.get(key, 0.0))
            if value > 0.0:
                calibrated[index][key] = percentile_by_value[value]
    return calibrated
