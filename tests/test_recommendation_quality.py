import pytest
from pydantic import ValidationError

from scrobble_recs.config import DiscoveryEvidenceConfig
from scrobble_recs.models.recommendation_quality import (
    calibrate_positive_components,
    evaluate_discovery_evidence,
)


def _gate() -> DiscoveryEvidenceConfig:
    return DiscoveryEvidenceConfig()


def test_tag_only_candidate_is_rejected() -> None:
    decision = evaluate_discovery_evidence(edges=(), tag_score=0.80, config=_gate())

    assert decision.passed is False
    assert decision.reasons == ("missing_direct_graph_evidence",)


def test_two_independent_seed_edges_are_admitted() -> None:
    decision = evaluate_discovery_evidence(
        edges=((1, 0.10), (2, 0.08)),
        tag_score=0.0,
        config=_gate(),
    )

    assert decision.passed is True
    assert decision.reasons == ("independent_seed_support",)


def test_strong_edge_requires_reinforcing_tag() -> None:
    assert not evaluate_discovery_evidence(
        edges=((1, 0.40),),
        tag_score=0.0,
        config=_gate(),
    ).passed
    assert evaluate_discovery_evidence(
        edges=((1, 0.40),),
        tag_score=0.10,
        config=_gate(),
    ).passed


def test_calibration_keeps_zero_at_zero_and_orders_positive_values() -> None:
    calibrated = calibrate_positive_components(
        [
            {"graph": 0.0, "tag": 0.50},
            {"graph": 0.001, "tag": 0.10},
            {"graph": 0.010, "tag": 0.20},
        ]
    )

    assert [row["graph"] for row in calibrated] == [0.0, 0.0, 1.0]
    assert calibrated[0]["tag"] > calibrated[1]["tag"]


def test_discovery_evidence_config_rejects_strong_threshold_below_minimum_edge() -> None:
    with pytest.raises(ValidationError, match="strong_seed_edge must be >= minimum_seed_edge"):
        DiscoveryEvidenceConfig(minimum_seed_edge=0.30, strong_seed_edge=0.05)
