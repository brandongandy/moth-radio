from __future__ import annotations

from scrobble_recs.models.seed_weighting import compute_seed_idf_weights


def test_alpha_zero_leaves_seed_weights_unchanged() -> None:
    seed_weight = {1: 2.0, 2: 5.0, 3: 1.0}
    popularity = {1: 100.0, 2: 0.2, 3: 8.0}

    result = compute_seed_idf_weights(seed_weight, popularity, alpha=0.0)

    assert result.effective_weight_by_seed == seed_weight
    assert result.missing_fraction == 0.0


def test_popular_seed_contributes_less_at_full_idf() -> None:
    # Equal raw weight; seed 1 is far more popular than seed 2.
    seed_weight = {1: 1.0, 2: 1.0}
    popularity = {1: 500.0, 2: 0.2}

    at_zero = compute_seed_idf_weights(seed_weight, popularity, alpha=0.0)
    at_one = compute_seed_idf_weights(seed_weight, popularity, alpha=1.0)

    # At alpha=0 they are equal; at alpha=1 the popular seed carries strictly less.
    assert at_zero.effective_weight_by_seed[1] == at_zero.effective_weight_by_seed[2]
    assert at_one.effective_weight_by_seed[1] < at_one.effective_weight_by_seed[2]
    assert at_one.effective_weight_by_seed[1] < at_zero.effective_weight_by_seed[1]
    assert at_one.idf_by_seed[1] < at_one.idf_by_seed[2]


def test_missing_popularity_is_neutral_not_obscure() -> None:
    # Seed 3 has no popularity data. It must get the median idf, not the max
    # (which is what an obscure artist would receive).
    seed_weight = {1: 1.0, 2: 1.0, 3: 1.0}
    popularity = {1: 500.0, 2: 0.2}  # seed 3 missing

    result = compute_seed_idf_weights(seed_weight, popularity, alpha=1.0)

    known_idfs = sorted([result.idf_by_seed[1], result.idf_by_seed[2]])
    median = (known_idfs[0] + known_idfs[1]) / 2
    assert result.idf_by_seed[3] == median
    # The obscure seed (2) gets the maximal idf; missing must be strictly below it.
    assert result.idf_by_seed[3] < result.idf_by_seed[2]
    assert result.missing_seed_ids == (3,)
    assert result.missing_fraction == 1 / 3


def test_single_seed_with_data_is_neutral() -> None:
    result = compute_seed_idf_weights({1: 3.0}, {1: 42.0}, alpha=1.0)

    assert result.idf_by_seed[1] == 1.0
    assert result.effective_weight_by_seed[1] == 3.0


def test_no_popularity_data_leaves_everything_neutral() -> None:
    result = compute_seed_idf_weights({1: 2.0, 2: 4.0}, {}, alpha=1.0)

    assert result.idf_by_seed == {1: 1.0, 2: 1.0}
    assert result.effective_weight_by_seed == {1: 2.0, 2: 4.0}
    assert result.missing_fraction == 1.0
