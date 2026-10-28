from __future__ import annotations

import pytest
from pydantic import ValidationError

from scrobble_recs import config as config_model
from scrobble_recs.models import evaluate


@pytest.mark.parametrize(
    ("counts", "labels", "expected"),
    [
        ((0, 0, 0), [], 0.0),
        ((1, 0, 0), [], 1.0),
        ((0, 1, 0), [], 1.0),
        ((0, 0, 1), [], 1.0),
        ((2, 1, 1), [], 2.0),
        ((0, 2, 0), [], 3.0),
        ((0, 0, 2), [], 3.0),
        ((0, 0, 0), ["want_to_try"], 1.0),
        ((0, 0, 0), ["strong_fit"], 2.0),
        ((0, 0, 0), ["surprising_good"], 3.0),
        ((0, 0, 0), ["already_known"], 0.0),
        ((2, 1, 1), ["already_known"], 2.0),
        ((2, 2, 2), ["reject", "surprising_good"], 0.0),
        ((2, 2, 2), ["wrong_direction"], 0.0),
        ((0, 0, 0), ["strong_fit", "strong_fit", "want_to_try"], 2.0),
    ],
)
def test_relevance_boundaries_and_explicit_override(counts, labels, expected):
    # Dropping any count/label branch or the negative override breaks this oracle.
    assert (
        evaluate.graded_artist_relevance(
            future_play_count=counts[0],
            future_active_days=counts[1],
            future_active_months=counts[2],
            explicit_labels=labels,
        )
        == expected
    )


@pytest.mark.parametrize("value", [-1, True, "1", 1.5, float("nan"), float("inf")])
def test_relevance_rejects_non_integral_counts(value):
    with pytest.raises(ValueError):
        evaluate.graded_artist_relevance(
            future_play_count=value,
            future_active_days=0,
            future_active_months=0,
            explicit_labels=[],
        )


@pytest.mark.parametrize("labels", ["reject", ["unknown"], None, 4, {"strong_fit"}])
def test_relevance_rejects_invalid_label_sequences(labels):
    with pytest.raises(ValueError):
        evaluate.graded_artist_relevance(
            future_play_count=0,
            future_active_days=0,
            future_active_months=0,
            explicit_labels=labels,
        )


@pytest.mark.parametrize("k", [10, 25, 50])
def test_exponential_ndcg_uses_entire_relevance_universe(k):
    assert evaluate._adoption_ndcg_at_k({1: 3, 2: 1}, [1, 2], k) == 1
    assert evaluate._adoption_ndcg_at_k({1: 3, 2: 1}, [2, 1], k) == pytest.approx(
        0.7098097413968655
    )
    assert evaluate._adoption_ndcg_at_k({1: 3, 2: 1}, [2], k) == pytest.approx(0.1310456303875653)
    assert evaluate._adoption_ndcg_at_k({}, [2], k) == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("minimum_cutoffs", 0),
        ("minimum_cutoffs", 3.0),
        ("max_runtime_multiplier", 0),
        ("max_storage_growth_mb", -1),
        ("max_single_provider_share", 1.01),
        ("minimum_identity_resolved_rate", -0.01),
        ("minimum_cutoffs", True),
        ("max_runtime_multiplier", "2"),
        ("max_storage_growth_mb", None),
        ("max_single_provider_share", float("nan")),
        ("minimum_identity_resolved_rate", float("inf")),
        ("unexpected", 1),
    ],
)
def test_promotion_config_rejects_unsafe_or_coerced_bounds(field, value):
    with pytest.raises(ValidationError):
        config_model.PromotionConfig.model_validate({field: value})


def test_promotion_defaults_are_generated_and_strict():
    config = config_model.PromotionConfig()
    assert config.model_dump() == {
        "minimum_cutoffs": 3,
        "max_runtime_multiplier": 2.0,
        "max_storage_growth_mb": 512,
        "max_single_provider_share": 0.8,
        "minimum_identity_resolved_rate": 0.9,
    }
    assert (
        config_model.default_config_document()["recommendation"]["promotion"] == config.model_dump()
    )
    assert not config_model.default_config_document()["recommendation"]["fusion"]["enabled"]


def test_quantiles_interpolate_supported_values_and_never_zero_fill():
    from scrobble_recs.models.evaluation_metrics import quantile

    assert quantile([1, 3, 5, 7], 0.25) == 2.5
    assert quantile([1, 3, 5, 7], 0.5) == 4
    assert quantile([1, 3, 5, 7], 0.75) == 5.5
    assert quantile([1, 3, 5, 7], 0.9) == 6.4
    assert quantile([3], 0.9) == 3
    assert quantile([], 0.5) is None


def test_behavior_fingerprint_ignores_destinations_and_evaluation_but_captures_provider_flags(
    tmp_path,
):
    from scrobble_recs.config import ScrobbleRecsConfig, default_config_document
    from scrobble_recs.models.evaluation_identity import behavior_fingerprint

    first = ScrobbleRecsConfig.model_validate(default_config_document())
    second = first.model_copy(deep=True)
    second.app.db_path = tmp_path / "elsewhere.duckdb"
    second.recommendation.promotion.minimum_cutoffs = 9
    assert behavior_fingerprint(first) == behavior_fingerprint(second)
    second.apis.listenbrainz.candidate_generation_enabled = True
    assert behavior_fingerprint(first) != behavior_fingerprint(second)
