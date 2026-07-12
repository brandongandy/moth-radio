from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from scrobble_recs.config import DiscoveryEvidenceConfig


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
