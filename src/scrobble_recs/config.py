from __future__ import annotations

import json
import os
import re
import warnings
from math import isfinite
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EXPECTED_CSV_COLUMNS = (
    "uts",
    "utc_time",
    "artist",
    "artist_mbid",
    "album",
    "album_mbid",
    "track",
    "track_mbid",
)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda match: os.environ.get(match.group(1), ""), value)
    if isinstance(value, list):
        return [_interpolate_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _interpolate_env(item) for key, item in value.items()}
    return value


def _expand_path(value: str | Path) -> Path:
    return Path(value).expanduser()


def _positive_finite(value: float, *, field_name: str) -> float:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return value


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "scrobble-recs"
    db_path: Path
    cache_dir: Path
    log_path: Path

    @field_validator("db_path", "cache_dir", "log_path", mode="before")
    @classmethod
    def expand_app_path(cls, value: str | Path) -> Path:
        return _expand_path(value)


class InputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lastfm_csv: Path
    timezone: str = "UTC"
    timestamp_column: str = "uts"
    csv_columns: list[str] = Field(default_factory=lambda: list(EXPECTED_CSV_COLUMNS))
    near_duplicate_window_seconds: int = 0

    @field_validator("lastfm_csv", mode="before")
    @classmethod
    def expand_input_path(cls, value: str | Path) -> Path:
        return _expand_path(value)

    @field_validator("timestamp_column")
    @classmethod
    def timestamp_column_must_match_contract(cls, value: str) -> str:
        if value != "uts":
            raise ValueError("timestamp_column must be 'uts'")
        return value

    @field_validator("csv_columns")
    @classmethod
    def csv_columns_must_match_contract(cls, value: list[str]) -> list[str]:
        if tuple(value) != EXPECTED_CSV_COLUMNS:
            raise ValueError(f"csv_columns must exactly match {list(EXPECTED_CSV_COLUMNS)}")
        return value

    @field_validator("near_duplicate_window_seconds")
    @classmethod
    def near_duplicate_window_must_be_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("near_duplicate_window_seconds must be non-negative")
        return value


class LastFmApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    api_key: str | None = None
    base_url: str = "https://ws.audioscrobbler.com/2.0/"
    rate_limit_per_second: float = 2.0
    cache_ttl_days: int = 180
    max_retries: int = 2
    timeout_seconds: float = 20.0
    stale_cache_on_error: bool = True

    @field_validator("api_key")
    @classmethod
    def empty_api_key_is_none(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        return value

    @field_validator("rate_limit_per_second")
    @classmethod
    def rate_limit_must_be_positive(cls, value: float) -> float:
        return _positive_finite(value, field_name="rate_limit_per_second")

    @field_validator("cache_ttl_days")
    @classmethod
    def ttl_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("cache_ttl_days must be at least 1")
        return value

    @field_validator("max_retries")
    @classmethod
    def retries_must_be_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("max_retries must be non-negative")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def timeout_must_be_positive(cls, value: float) -> float:
        return _positive_finite(value, field_name="timeout_seconds")


class MusicBrainzApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: str = "https://musicbrainz.org/ws/2/"
    user_agent: str = "scrobble-recs/0.1.0"
    rate_limit_per_second: float = 1.0
    cache_ttl_days: int = 3650
    max_retries: int = 2

    @field_validator("rate_limit_per_second")
    @classmethod
    def rate_limit_must_be_positive(cls, value: float) -> float:
        return _positive_finite(value, field_name="rate_limit_per_second")

    @field_validator("cache_ttl_days")
    @classmethod
    def ttl_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("cache_ttl_days must be at least 1")
        return value

    @field_validator("max_retries")
    @classmethod
    def retries_must_be_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("max_retries must be non-negative")
        return value


class ListenBrainzApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    candidate_generation_enabled: bool = False
    base_url: str = "https://api.listenbrainz.org/1/"
    rate_limit_per_second: float = 1.0
    cache_ttl_days: int = Field(default=30, ge=1)
    max_retries: int = Field(default=2, ge=0)
    timeout_seconds: float = 20.0
    stale_cache_on_error: bool = True
    mode: Literal["easy", "medium", "hard"] = "easy"
    max_similar_artists: int = Field(default=100, ge=1)
    max_recordings_per_artist: int = Field(default=3, ge=1)
    pop_begin: int = Field(default=0, ge=0, le=100)
    pop_end: int = Field(default=100, ge=0, le=100)

    @model_validator(mode="before")
    @classmethod
    def reject_obsolete_fields(cls, value: Any) -> Any:
        if isinstance(value, dict):
            obsolete = sorted({"dump_dir", "use_recommendation_api"}.intersection(value))
            if obsolete:
                raise ValueError("remove obsolete ListenBrainz settings: " + ", ".join(obsolete))
        return value

    @field_validator("rate_limit_per_second", "timeout_seconds")
    @classmethod
    def positive_finite(cls, value: float) -> float:
        return _positive_finite(value, field_name="ListenBrainz rate/timeout")

    @model_validator(mode="after")
    def ordered_popularity(self) -> ListenBrainzApiConfig:
        if self.pop_begin > self.pop_end:
            raise ValueError("pop_begin must be less than or equal to pop_end")
        return self


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lastfm: LastFmApiConfig = Field(default_factory=LastFmApiConfig)
    musicbrainz: MusicBrainzApiConfig = Field(default_factory=MusicBrainzApiConfig)
    listenbrainz: ListenBrainzApiConfig = Field(default_factory=ListenBrainzApiConfig)


class EnrichmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed_artist_limit: int = 250
    candidate_artist_limit: int = 5000
    similar_artists_per_seed: int = 100
    top_tags_per_artist: int = 20
    tag_top_artists_limit: int = 100
    max_queue_attempts: int = 3
    queue_batch_size: int = 1000

    @model_validator(mode="after")
    def validate_enrichment(self) -> EnrichmentConfig:
        if self.seed_artist_limit < 1:
            raise ValueError("seed_artist_limit must be at least 1")
        if self.candidate_artist_limit < 1:
            raise ValueError("candidate_artist_limit must be at least 1")
        if self.similar_artists_per_seed < 1:
            raise ValueError("similar_artists_per_seed must be at least 1")
        if self.top_tags_per_artist < 1:
            raise ValueError("top_tags_per_artist must be at least 1")
        if self.tag_top_artists_limit < 1:
            raise ValueError("tag_top_artists_limit must be at least 1")
        if self.max_queue_attempts < 0:
            raise ValueError("max_queue_attempts must be non-negative")
        if self.queue_batch_size < 1:
            raise ValueError("queue_batch_size must be at least 1")
        return self


class ObviousnessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    top_n_per_tag_penalty: int = 10
    top_n_per_tag_hide: int = 3
    tag_top_artists_limit: int = 100
    penalty_weight: float = 0.25

    @model_validator(mode="after")
    def validate_thresholds(self) -> ObviousnessConfig:
        if self.top_n_per_tag_hide < 0:
            raise ValueError("top_n_per_tag_hide must be non-negative")
        if self.top_n_per_tag_penalty < self.top_n_per_tag_hide:
            raise ValueError("top_n_per_tag_penalty must be >= top_n_per_tag_hide")
        if self.tag_top_artists_limit < 1:
            raise ValueError("tag_top_artists_limit must be at least 1")
        if self.penalty_weight < 0:
            raise ValueError("penalty_weight must be non-negative")
        return self


class KnownnessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    threshold: float = 0.65
    penalty_weight: float = 0.40

    @model_validator(mode="after")
    def validate_knownness(self) -> KnownnessConfig:
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("knownness threshold must be between 0 and 1")
        if not 0.0 <= self.penalty_weight <= 1.0:
            raise ValueError("knownness penalty_weight must be between 0 and 1")
        return self


class RecommendationWeightsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lastfm_similarity: float = 0.35
    tag_similarity: float = 0.25
    personal_cluster_fit: float = 0.15
    rediscovery: float = 0.10
    metadata_graph: float = 0.05
    feedback_model: float = 0.20


class ScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: Literal["legacy", "walk"] = "legacy"
    seed_idf_alpha: float = 0.0

    @field_validator("seed_idf_alpha")
    @classmethod
    def idf_alpha_must_be_non_negative(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError("seed_idf_alpha must be non-negative")
        return value


class WalkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_hops: int = 2
    max_nodes: int = 250_000
    symmetrize: bool = False
    restart: float = 0.15
    baseline_exponent: float = 1.0
    min_lift: float = 0.0
    min_distinct_seeds: int = 2

    @model_validator(mode="after")
    def validate_walk(self) -> WalkConfig:
        if self.max_hops < 1:
            raise ValueError("max_hops must be at least 1")
        if self.max_nodes < 1:
            raise ValueError("max_nodes must be at least 1")
        if not 0.0 < self.restart < 1.0:
            raise ValueError("restart must be between 0 and 1 (exclusive)")
        if self.baseline_exponent < 0.0:
            raise ValueError("baseline_exponent must be non-negative")
        if self.min_distinct_seeds < 1:
            raise ValueError("min_distinct_seeds must be at least 1")
        return self


class DiversityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = True
    lambda_: float = Field(default=0.75, alias="lambda")
    max_same_primary_tag_in_top_20: int = 4


class QualityGateConfig(BaseModel):
    """Rediscovery-specific eligibility settings.

    Standard discovery always requires ``discovery_evidence`` regardless of
    ``enabled``. The legacy score thresholds are accepted only to provide a
    visible migration path for existing configuration files.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    rediscovery_requires_taste_match: bool = True

    @model_validator(mode="before")
    @classmethod
    def remove_legacy_thresholds(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        legacy_fields = {
            "min_lastfm_similarity_score",
            "min_tag_similarity_score",
            "min_positive_feedback_score",
            "rediscovery_mode_multiplier",
        }
        present_legacy_fields = sorted(legacy_fields.intersection(value))
        if not present_legacy_fields:
            return value
        warnings.warn(
            "recommendation.quality_gate settings "
            f"{', '.join(present_legacy_fields)} are deprecated and no longer affect "
            "recommendation admission; configure discovery_evidence instead.",
            UserWarning,
            stacklevel=3,
        )
        return {key: item for key, item in value.items() if key not in legacy_fields}


class DiscoveryEvidenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_seed_edge: float = 0.05
    strong_seed_edge: float = 0.30
    minimum_seed_count: int = 2
    minimum_tag_reinforcement: float = 0.05
    minimum_independent_families: int = 2
    identity_safety_threshold: float = 0.80
    low_confidence_identity_threshold: float = 0.50
    catalog_relationships_enabled: bool = False

    @model_validator(mode="after")
    def validate_thresholds(self) -> DiscoveryEvidenceConfig:
        for field_name in (
            "minimum_seed_edge",
            "strong_seed_edge",
            "minimum_tag_reinforcement",
            "identity_safety_threshold",
            "low_confidence_identity_threshold",
        ):
            value = float(getattr(self, field_name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if self.strong_seed_edge < self.minimum_seed_edge:
            raise ValueError("strong_seed_edge must be >= minimum_seed_edge")
        if self.minimum_seed_count < 1:
            raise ValueError("minimum_seed_count must be at least 1")
        if self.minimum_independent_families < 1:
            raise ValueError("minimum_independent_families must be at least 1")
        if self.low_confidence_identity_threshold > self.identity_safety_threshold:
            raise ValueError("low confidence threshold must not exceed identity safety threshold")
        return self


class CatalogEvidenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    allowed_relationship_types: list[str] = Field(
        default_factory=lambda: [
            "member of band",
            "collaboration",
            "supporting musician",
            "instrumental supporting musician",
        ]
    )


class CalibrationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class SerendipityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    minimum_neighbor_edge: float = 0.05
    minimum_neighbors: int = 2
    max_bonus: float = 0.15
    bridge_bonus: float = 0.05

    @model_validator(mode="after")
    def validate_thresholds(self) -> SerendipityConfig:
        for field_name in ("minimum_neighbor_edge", "max_bonus", "bridge_bonus"):
            value = float(getattr(self, field_name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if self.minimum_neighbors < 2:
            raise ValueError("minimum_neighbors must be at least 2")
        if self.bridge_bonus > self.max_bonus:
            raise ValueError("bridge_bonus must be <= max_bonus")
        return self


class FatigueConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    lookback_runs: int = 5
    latest_run_penalty: float = 0.12
    repeat_appearance_penalty: float = 0.04

    @model_validator(mode="after")
    def validate_fatigue(self) -> FatigueConfig:
        if self.lookback_runs < 1:
            raise ValueError("lookback_runs must be at least 1")
        if self.latest_run_penalty < 0.0:
            raise ValueError("latest_run_penalty must be non-negative")
        if self.repeat_appearance_penalty < 0.0:
            raise ValueError("repeat_appearance_penalty must be non-negative")
        return self


class TagProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weak_tags: list[str] = Field(
        default_factory=lambda: [
            "seen live",
            "favorite",
            "favorites",
            "favourite",
            "lastfm",
            "spotify",
            "soundtrack",
            "spoken word",
            "rock",
            "indie",
            "alternative",
            "pop",
            "electronic",
            "classic rock",
            "female vocalists",
            "80s",
            "90s",
            "70s",
            "american",
            "canadian",
            "british",
            "japanese",
        ]
    )
    weak_tag_penalty: float = 0.25

    @field_validator("weak_tags")
    @classmethod
    def normalize_weak_tags(cls, value: list[str]) -> list[str]:
        return sorted({tag.strip().casefold() for tag in value if tag.strip()})

    @field_validator("weak_tag_penalty")
    @classmethod
    def weak_tag_penalty_must_be_ratio(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("weak_tag_penalty must be between 0 and 1")
        return value


class ArtistRelationshipConfig(BaseModel):
    """Version-one conservative relationship thresholds, not fitted optima."""

    model_config = ConfigDict(extra="forbid")
    sampled_max_plays: int = Field(default=5, ge=0)
    established_min_plays: int = Field(default=20, ge=0)
    established_min_active_months: int = Field(default=3, ge=0)
    dormant_days: int = Field(default=730, ge=0)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> ArtistRelationshipConfig:
        if self.established_min_plays <= self.sampled_max_plays:
            raise ValueError("established_min_plays must exceed sampled_max_plays")
        return self


class ArtistStatePolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    max_hops: int = Field(default=1, ge=1, le=100)
    popularity_preference: float = Field(default=0.0, ge=-1.0, le=1.0)
    obviousness_penalty_weight: float = Field(default=0.0, ge=0.0, le=1.0)


class ArtistChallengerConfig(BaseModel):
    """Unvalidated artist variations require explicit opt-in; never auto-promoted."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = False
    version: Literal["artist-challenger-v1"] = "artist-challenger-v1"
    target_weight: float = Field(default=0.75, ge=0.0, le=1.0)
    personal_weight: float = Field(default=0.15, ge=0.0, le=1.0)
    existing_weight: float = Field(default=0.10, ge=0.0, le=1.0)
    structural_membership_prior: float = Field(default=0.25, ge=0.0, le=1.0)
    hop_proximity_scale: float = Field(default=1.0, ge=0.0, le=1.0)
    known_neighbor_penalty: float = Field(default=0.12, ge=0.0, le=1.0)
    unheard: ArtistStatePolicyConfig = Field(
        default_factory=lambda: ArtistStatePolicyConfig(
            max_hops=1, popularity_preference=0.10, obviousness_penalty_weight=0.05
        )
    )
    sampled: ArtistStatePolicyConfig = Field(
        default_factory=lambda: ArtistStatePolicyConfig(
            max_hops=1, popularity_preference=0.0, obviousness_penalty_weight=0.10
        )
    )
    established_favorite: ArtistStatePolicyConfig = Field(
        default_factory=lambda: ArtistStatePolicyConfig(
            max_hops=2, popularity_preference=-0.10, obviousness_penalty_weight=0.20
        )
    )
    dormant_favorite: ArtistStatePolicyConfig = Field(
        default_factory=lambda: ArtistStatePolicyConfig(
            max_hops=2, popularity_preference=-0.05, obviousness_penalty_weight=0.15
        )
    )

    @model_validator(mode="after")
    def validate_weight_total(self) -> ArtistChallengerConfig:
        if abs(self.target_weight + self.personal_weight + self.existing_weight - 1.0) > 1e-9:
            raise ValueError("artist challenger relevance weights must sum to 1")
        return self


class RecommendationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    artist_relationship: ArtistRelationshipConfig = Field(default_factory=ArtistRelationshipConfig)
    artist_challenger: ArtistChallengerConfig = Field(default_factory=ArtistChallengerConfig)
    default_mode: str = "balanced"
    seed_artist_limit: int = 250
    similar_artists_per_seed: int = 100
    min_seed_scrobbles: int = 3
    profile_window_months: int = 12
    max_knownness_default: float = 0.65
    rediscovery_enabled: bool = True
    run_limit: int = 500
    feedback_cooldown_days: int = 30
    obviousness: ObviousnessConfig = Field(default_factory=ObviousnessConfig)
    knownness: KnownnessConfig = Field(default_factory=KnownnessConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    walk: WalkConfig = Field(default_factory=WalkConfig)
    weights: RecommendationWeightsConfig = Field(default_factory=RecommendationWeightsConfig)
    diversity: DiversityConfig = Field(default_factory=DiversityConfig)
    quality_gate: QualityGateConfig = Field(default_factory=QualityGateConfig)
    discovery_evidence: DiscoveryEvidenceConfig = Field(default_factory=DiscoveryEvidenceConfig)
    catalog_evidence: CatalogEvidenceConfig = Field(default_factory=CatalogEvidenceConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    serendipity: SerendipityConfig = Field(default_factory=SerendipityConfig)
    fatigue: FatigueConfig = Field(default_factory=FatigueConfig)
    tag_profile: TagProfileConfig = Field(default_factory=TagProfileConfig)

    @model_validator(mode="after")
    def validate_operational_defaults(self) -> RecommendationConfig:
        if self.seed_artist_limit < 1:
            raise ValueError("seed_artist_limit must be at least 1")
        if self.similar_artists_per_seed < 1:
            raise ValueError("similar_artists_per_seed must be at least 1")
        if self.min_seed_scrobbles < 1:
            raise ValueError("min_seed_scrobbles must be at least 1")
        if self.profile_window_months < 1:
            raise ValueError("profile_window_months must be at least 1")
        if not 0.0 <= self.max_knownness_default <= 1.0:
            raise ValueError("max_knownness_default must be between 0 and 1")
        if self.run_limit < 1:
            raise ValueError("run_limit must be at least 1")
        if self.feedback_cooldown_days < 1:
            raise ValueError("feedback_cooldown_days must be at least 1")
        return self


class ScrobbleRecsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app: AppConfig
    input: InputConfig
    apis: ApiConfig = Field(default_factory=ApiConfig)
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    recommendation: RecommendationConfig = Field(default_factory=RecommendationConfig)


def load_config(path: str | Path) -> ScrobbleRecsConfig:
    config_path = Path(path).expanduser()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    resolved = _interpolate_env(raw)
    return ScrobbleRecsConfig.model_validate(resolved)


def default_config_document() -> dict[str, Any]:
    return {
        "app": {
            "name": "scrobble-recs",
            "db_path": "./data/scrobbles.duckdb",
            "cache_dir": "./data/cache",
            "log_path": "./data/app.log",
        },
        "input": {
            "lastfm_csv": "./exports/lastfm_scrobbles.csv",
            "timezone": "UTC",
            "timestamp_column": "uts",
            "csv_columns": list(EXPECTED_CSV_COLUMNS),
            "near_duplicate_window_seconds": 0,
        },
        "apis": {
            "lastfm": {
                "enabled": True,
                "api_key": "${LASTFM_API_KEY}",
                "base_url": "https://ws.audioscrobbler.com/2.0/",
                "rate_limit_per_second": 2.0,
                "cache_ttl_days": 180,
                "max_retries": 2,
                "timeout_seconds": 20.0,
                "stale_cache_on_error": True,
            },
            "musicbrainz": {
                "enabled": False,
                "base_url": "https://musicbrainz.org/ws/2/",
                "user_agent": "scrobble-recs/0.1.0",
                "rate_limit_per_second": 1.0,
                "cache_ttl_days": 3650,
                "max_retries": 2,
            },
            "listenbrainz": ListenBrainzApiConfig().model_dump(),
        },
        "enrichment": {
            "seed_artist_limit": 250,
            "candidate_artist_limit": 5000,
            "similar_artists_per_seed": 100,
            "top_tags_per_artist": 20,
            "tag_top_artists_limit": 100,
            "max_queue_attempts": 3,
            "queue_batch_size": 1000,
        },
        "recommendation": {
            "default_mode": "balanced",
            "seed_artist_limit": 250,
            "similar_artists_per_seed": 100,
            "min_seed_scrobbles": 3,
            "max_knownness_default": 0.65,
            "rediscovery_enabled": True,
            "run_limit": 500,
            "feedback_cooldown_days": 30,
            "obviousness": {
                "enabled": True,
                "top_n_per_tag_penalty": 10,
                "top_n_per_tag_hide": 3,
                "tag_top_artists_limit": 100,
                "penalty_weight": 0.25,
            },
            "knownness": {
                "enabled": True,
                "threshold": 0.65,
                "penalty_weight": 0.40,
            },
            "scoring": {
                "engine": "legacy",
                "seed_idf_alpha": 0.0,
            },
            "walk": {
                "max_hops": 2,
                "max_nodes": 250000,
                "symmetrize": False,
                "restart": 0.15,
                "baseline_exponent": 1.0,
                "min_lift": 0.0,
                "min_distinct_seeds": 2,
            },
            "weights": {
                "lastfm_similarity": 0.35,
                "tag_similarity": 0.25,
                "personal_cluster_fit": 0.15,
                "rediscovery": 0.10,
                "metadata_graph": 0.05,
                "feedback_model": 0.10,
            },
            "diversity": {
                "enabled": True,
                "lambda": 0.75,
                "max_same_primary_tag_in_top_20": 4,
            },
            "quality_gate": {
                "enabled": True,
                "rediscovery_requires_taste_match": True,
            },
            "discovery_evidence": {
                "minimum_seed_edge": 0.05,
                "strong_seed_edge": 0.30,
                "minimum_seed_count": 2,
                "minimum_tag_reinforcement": 0.05,
            },
            "catalog_evidence": CatalogEvidenceConfig().model_dump(),
            "calibration": {"enabled": True},
            "serendipity": {
                "enabled": True,
                "minimum_neighbor_edge": 0.05,
                "minimum_neighbors": 2,
                "max_bonus": 0.15,
                "bridge_bonus": 0.05,
            },
            "fatigue": {
                "enabled": True,
                "lookback_runs": 5,
                "latest_run_penalty": 0.12,
                "repeat_appearance_penalty": 0.04,
            },
            "tag_profile": {
                "weak_tags": [
                    "seen live",
                    "favorite",
                    "favorites",
                    "favourite",
                    "lastfm",
                    "spotify",
                    "soundtrack",
                    "spoken word",
                    "rock",
                    "indie",
                    "alternative",
                    "pop",
                    "electronic",
                    "classic rock",
                    "female vocalists",
                    "80s",
                    "90s",
                    "70s",
                    "american",
                    "canadian",
                    "british",
                    "japanese",
                ],
                "weak_tag_penalty": 0.25,
            },
        },
        "tui": {
            "page_size": 25,
            "open_links_with": "system",
            "show_debug_scores": False,
        },
    }
