from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

    @field_validator("lastfm_csv", mode="before")
    @classmethod
    def expand_input_path(cls, value: str | Path) -> Path:
        return _expand_path(value)


class LastFmApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    api_key: str | None = None
    base_url: str = "https://ws.audioscrobbler.com/2.0/"
    rate_limit_per_second: float = 2.0
    cache_ttl_days: int = 180

    @field_validator("api_key")
    @classmethod
    def empty_api_key_is_none(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        return value


class MusicBrainzApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: str = "https://musicbrainz.org/ws/2/"
    user_agent: str = "scrobble-recs/0.1.0"
    rate_limit_per_second: float = 1.0
    cache_ttl_days: int = 3650


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
