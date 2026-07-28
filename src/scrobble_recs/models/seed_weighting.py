from __future__ import annotations

from dataclasses import dataclass
from math import log1p
from statistics import median


@dataclass(frozen=True)
class SeedIdfWeights:
    """Inverse-popularity weighting applied to seed edge aggregation.

    ``idf_by_seed`` is monotonically decreasing in popularity, normalized to
    ``[0, 1]`` across the seed set. ``effective_weight_by_seed`` is
    ``seed_weight * idf ** alpha``; at ``alpha == 0`` it equals the raw seed
    weight exactly (``x ** 0 == 1``), preserving legacy behavior.
    """

    idf_by_seed: dict[int, float]
    effective_weight_by_seed: dict[int, float]
    missing_seed_ids: tuple[int, ...]
    missing_fraction: float
    alpha: float


def _raw_idf(popularity: float) -> float:
    return 1.0 / (1.0 + log1p(max(0.0, popularity)))


def compute_seed_idf_weights(
    seed_weight_by_id: dict[int, float],
    popularity_by_seed: dict[int, float],
    *,
    alpha: float,
) -> SeedIdfWeights:
    seed_ids = sorted(seed_weight_by_id)
    missing_seed_ids = tuple(seed_id for seed_id in seed_ids if seed_id not in popularity_by_seed)
    missing_fraction = (len(missing_seed_ids) / len(seed_ids)) if seed_ids else 0.0

    # Normalize idf across seeds with popularity data to [0, 1] via min-max.
    raw_idf_by_seed = {
        seed_id: _raw_idf(popularity_by_seed[seed_id])
        for seed_id in seed_ids
        if seed_id in popularity_by_seed
    }
    normalized_idf_by_seed = _normalize_to_unit_interval(raw_idf_by_seed)

    # Missing popularity is neutral: the median of the observed idf values,
    # never the maximal (most obscure) value.
    known_values = sorted(normalized_idf_by_seed.values())
    neutral_idf = float(median(known_values)) if known_values else 1.0

    idf_by_seed: dict[int, float] = {}
    effective_weight_by_seed: dict[int, float] = {}
    for seed_id in seed_ids:
        idf = normalized_idf_by_seed.get(seed_id, neutral_idf)
        idf_by_seed[seed_id] = idf
        effective_weight_by_seed[seed_id] = seed_weight_by_id[seed_id] * (idf**alpha)

    return SeedIdfWeights(
        idf_by_seed=idf_by_seed,
        effective_weight_by_seed=effective_weight_by_seed,
        missing_seed_ids=missing_seed_ids,
        missing_fraction=missing_fraction,
        alpha=alpha,
    )


def _normalize_to_unit_interval(values_by_seed: dict[int, float]) -> dict[int, float]:
    if not values_by_seed:
        return {}
    values = values_by_seed.values()
    lowest = min(values)
    highest = max(values)
    span = highest - lowest
    if span <= 0.0:
        # A single value (or all-equal) carries no ranking information: neutral.
        return {seed_id: 1.0 for seed_id in values_by_seed}
    return {seed_id: (value - lowest) / span for seed_id, value in values_by_seed.items()}
