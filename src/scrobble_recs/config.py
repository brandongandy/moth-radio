from __future__ import annotations

import json
import os
import re
from math import isfinite
from pathlib import Path
from typing import Any

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
    dump_dir: Path = Field(default_factory=lambda: _expand_path("~/datasets/listenbrainz"))
    use_recommendation_api: bool = False

    @field_validator("dump_dir", mode="before")
    @classmethod
    def expand_dump_dir(cls, value: str | Path) -> Path:
        return _expand_path(value)


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lastfm: LastFmApiConfig = Field(default_factory=LastFmApiConfig)
    musicbrainz: MusicBrainzApiConfig = Field(default_factory=MusicBrainzApiConfig)
    listenbrainz: ListenBrainzApiConfig = Field(default_factory=ListenBrainzApiConfig)


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


class RecommendationWeightsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lastfm_similarity: float = 0.35
    tag_similarity: float = 0.25
    personal_cluster_fit: float = 0.15
    rediscovery: float = 0.10
    metadata_graph: float = 0.05
    feedback_model: float = 0.10


class DiversityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = True
    lambda_: float = Field(default=0.75, alias="lambda")
    max_same_primary_tag_in_top_20: int = 4


class RecommendationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    default_mode: str = "balanced"
    seed_artist_limit: int = 250
    similar_artists_per_seed: int = 100
    min_seed_scrobbles: int = 3
    max_knownness_default: float = 0.65
    rediscovery_enabled: bool = True
    obviousness: ObviousnessConfig = Field(default_factory=ObviousnessConfig)
    weights: RecommendationWeightsConfig = Field(default_factory=RecommendationWeightsConfig)
    diversity: DiversityConfig = Field(default_factory=DiversityConfig)

    @model_validator(mode="after")
    def validate_operational_defaults(self) -> RecommendationConfig:
        if self.seed_artist_limit < 1:
            raise ValueError("seed_artist_limit must be at least 1")
        if self.similar_artists_per_seed < 1:
            raise ValueError("similar_artists_per_seed must be at least 1")
        if self.min_seed_scrobbles < 1:
            raise ValueError("min_seed_scrobbles must be at least 1")
        if not 0.0 <= self.max_knownness_default <= 1.0:
            raise ValueError("max_knownness_default must be between 0 and 1")
        return self


class ScrobbleRecsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app: AppConfig
    input: InputConfig
    apis: ApiConfig = Field(default_factory=ApiConfig)
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
            },
            "musicbrainz": {
                "enabled": False,
                "base_url": "https://musicbrainz.org/ws/2/",
                "user_agent": "scrobble-recs/0.1.0",
                "rate_limit_per_second": 1.0,
                "cache_ttl_days": 3650,
                "max_retries": 2,
            },
            "listenbrainz": {
                "enabled": False,
                "dump_dir": "~/datasets/listenbrainz",
                "use_recommendation_api": False,
            },
        },
        "recommendation": {
            "default_mode": "balanced",
            "seed_artist_limit": 250,
            "similar_artists_per_seed": 100,
            "min_seed_scrobbles": 3,
            "max_knownness_default": 0.65,
            "rediscovery_enabled": True,
            "obviousness": {
                "enabled": True,
                "top_n_per_tag_penalty": 10,
                "top_n_per_tag_hide": 3,
                "tag_top_artists_limit": 100,
                "penalty_weight": 0.25,
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
        },
        "tui": {
            "page_size": 25,
            "open_links_with": "system",
            "show_debug_scores": False,
        },
    }
