from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from scrobble_recs.config import ScrobbleRecsConfig

SeedRegionName = Literal["lifetime", "recent", "old_obsession", "underexplored"]
SeedRegionSetting = tuple[SeedRegionName, int, float]


@dataclass(frozen=True)
class RecommendationModeSettings:
    name: str
    seed_regions: tuple[SeedRegionSetting, ...]
    diversity_lambda: float
    max_knownness: float
    obviousness_penalty_weight: float
    hard_hide_top_tag_rank: int
    high_affinity_tag_weight: float
    same_primary_tag_cap: int
    rediscovery_multiplier: float = 1.0
    lastfm_similarity_multiplier: float = 1.0
    tag_similarity_multiplier: float = 1.0
    tag_focus_boost: float = 1.0
    focus_tags: tuple[str, ...] = ()


def resolve_recommendation_mode(
    requested_mode: str | None,
    config: ScrobbleRecsConfig,
) -> RecommendationModeSettings:
    mode_name = (requested_mode or config.recommendation.default_mode).strip().casefold()
    if mode_name.startswith("tag:"):
        focus_tags = tuple(
            tag.strip().casefold()
            for tag in mode_name.removeprefix("tag:").split(",")
            if tag.strip()
        )
        if not focus_tags:
            raise ValueError("tag focus mode requires at least one tag")
        return RecommendationModeSettings(
            name="tag",
            seed_regions=(
                ("lifetime", 125, 0.85),
                ("recent", 75, 1.00),
                ("underexplored", 50, 0.75),
            ),
            diversity_lambda=0.70,
            max_knownness=config.recommendation.max_knownness_default,
            obviousness_penalty_weight=config.recommendation.obviousness.penalty_weight,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.75,
            same_primary_tag_cap=config.recommendation.diversity.max_same_primary_tag_in_top_20,
            tag_similarity_multiplier=1.25,
            tag_focus_boost=1.60,
            focus_tags=focus_tags,
        )

    base = {
        "balanced": RecommendationModeSettings(
            name="balanced",
            seed_regions=(
                ("lifetime", 150, 1.00),
                ("recent", 75, 1.00),
                ("old_obsession", 50, 0.85),
                ("underexplored", 50, 0.75),
            ),
            diversity_lambda=config.recommendation.diversity.lambda_,
            max_knownness=config.recommendation.max_knownness_default,
            obviousness_penalty_weight=config.recommendation.obviousness.penalty_weight,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.75,
            same_primary_tag_cap=config.recommendation.diversity.max_same_primary_tag_in_top_20,
        ),
        "explore": RecommendationModeSettings(
            name="explore",
            seed_regions=(
                ("underexplored", 100, 1.20),
                ("recent", 75, 1.00),
                ("lifetime", 75, 0.65),
                ("old_obsession", 50, 0.80),
            ),
            diversity_lambda=0.55,
            max_knownness=0.45,
            obviousness_penalty_weight=0.35,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.70,
            same_primary_tag_cap=3,
            tag_similarity_multiplier=1.10,
        ),
        "safe": RecommendationModeSettings(
            name="safe",
            seed_regions=(
                ("lifetime", 175, 1.15),
                ("recent", 75, 1.00),
            ),
            diversity_lambda=0.90,
            max_knownness=0.75,
            obviousness_penalty_weight=0.15,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.80,
            same_primary_tag_cap=5,
            lastfm_similarity_multiplier=1.20,
        ),
        "rediscovery": RecommendationModeSettings(
            name="rediscovery",
            seed_regions=(
                ("old_obsession", 125, 1.30),
                ("underexplored", 75, 1.00),
                ("recent", 50, 0.80),
            ),
            diversity_lambda=0.80,
            max_knownness=0.80,
            obviousness_penalty_weight=0.20,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.75,
            same_primary_tag_cap=4,
            rediscovery_multiplier=1.75,
        ),
        "recent": RecommendationModeSettings(
            name="recent",
            seed_regions=(
                ("recent", 175, 1.30),
                ("lifetime", 75, 0.70),
            ),
            diversity_lambda=0.75,
            max_knownness=0.60,
            obviousness_penalty_weight=0.25,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.75,
            same_primary_tag_cap=4,
        ),
        "old": RecommendationModeSettings(
            name="old",
            seed_regions=(
                ("old_obsession", 150, 1.30),
                ("lifetime", 100, 0.80),
            ),
            diversity_lambda=0.80,
            max_knownness=0.75,
            obviousness_penalty_weight=0.20,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.75,
            same_primary_tag_cap=4,
            rediscovery_multiplier=1.35,
        ),
        "anti-obvious": RecommendationModeSettings(
            name="anti-obvious",
            seed_regions=(
                ("underexplored", 100, 1.15),
                ("recent", 75, 1.00),
                ("old_obsession", 50, 0.90),
                ("lifetime", 50, 0.60),
            ),
            diversity_lambda=0.65,
            max_knownness=0.55,
            obviousness_penalty_weight=0.45,
            hard_hide_top_tag_rank=10,
            high_affinity_tag_weight=0.55,
            same_primary_tag_cap=3,
            tag_similarity_multiplier=1.10,
        ),
    }
    try:
        return base[mode_name]
    except KeyError as exc:
        supported = ", ".join(sorted(base)) + ", tag:<tag>[,<tag>]"
        raise ValueError(
            f"unsupported recommendation mode {requested_mode!r}; supported modes: {supported}"
        ) from exc
