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


class ScrobbleRecsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app: AppConfig
    input: InputConfig


def load_config(path: str | Path) -> ScrobbleRecsConfig:
    config_path = Path(path).expanduser()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    resolved = _interpolate_env(raw)
    return ScrobbleRecsConfig.model_validate(resolved)
