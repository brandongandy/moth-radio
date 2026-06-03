# Last.fm Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cache-first Last.fm enrichment so imported local artists can be used as seeds, fetch similar artists and tag top artists, persist all raw API responses, and resume without duplicate HTTP requests.

**Architecture:** Keep the TUI/recommender offline-first by making `api_cache` the API boundary: Last.fm client methods read fresh cache entries before rate limiting or HTTP. A small enrichment service selects seed artists from local listening facts, calls the Last.fm client, upserts returned artists, and writes normalized similarity/tag tables idempotently.

**Tech Stack:** Python 3.11+, DuckDB, httpx, Pydantic v2, Typer, pytest, ruff, mypy.

---

## Scope

This plan covers Milestone 2 from `docs/spec.md`: Last.fm enrichment.

Acceptance criteria covered:

- Can select seed artists.
- Can call Last.fm `artist.getSimilar`.
- Can call Last.fm `artist.getTopTags`.
- Can call Last.fm `tag.getTopArtists`.
- Can cache all responses.
- Can resume after interruption.
- Can avoid duplicate API requests.

Deferred to separate plans:

- MusicBrainz resolution.
- Recommendation scoring, obviousness scoring, and CSV export.
- Textual TUI feedback flows.
- ListenBrainz collaborative modeling.

## File Structure

- Modify: `config.example.json` to include Last.fm and recommendation defaults with `${LASTFM_API_KEY}` interpolation.
- Modify: `src/scrobble_recs/config.py` to add `apis.lastfm`, `apis.musicbrainz`, `apis.listenbrainz`, and `recommendation` config models.
- Create: `src/scrobble_recs/sql/002_enrichment.sql` for `api_cache`, `artist_similarity_edge`, `artist_tag`, and `tag_top_artist`.
- Create: `src/scrobble_recs/apis/__init__.py` for API package marker.
- Create: `src/scrobble_recs/apis/cache.py` for canonical cache keys and DuckDB-backed cache reads/writes.
- Create: `src/scrobble_recs/apis/rate_limit.py` for deterministic client-side rate limiting.
- Create: `src/scrobble_recs/apis/lastfm.py` for cache-first Last.fm client methods.
- Create: `src/scrobble_recs/models/__init__.py` for model package marker.
- Create: `src/scrobble_recs/models/profile.py` for seed artist selection.
- Create: `src/scrobble_recs/enrichment/__init__.py` for enrichment package marker.
- Create: `src/scrobble_recs/enrichment/lastfm.py` for Last.fm enrichment orchestration and idempotent normalized inserts.
- Modify: `src/scrobble_recs/cli.py` to add `enrich --source lastfm`.
- Test: `tests/test_config.py` for new config defaults and environment interpolation.
- Test: `tests/test_api_cache.py` for cache key stability, fresh cache hits, stale cache misses, and writes.
- Test: `tests/test_rate_limit.py` for deterministic wait behavior.
- Test: `tests/test_lastfm_client.py` for mocked HTTP, cache-first behavior, and no secret persistence.
- Test: `tests/test_profile.py` for seed selection.
- Test: `tests/test_lastfm_enrichment.py` for normalized enrichment inserts and resume behavior.
- Test: `tests/test_cli.py` for `enrich --source lastfm` dispatch without live HTTP.
- Modify: `README.md` with Milestone 2 commands and cache-first behavior.
- Modify: `docs/implementation-log.md` with verification notes.

---

### Task 1: Config Models For APIs And Recommendation Defaults

**Files:**
- Modify: `src/scrobble_recs/config.py`
- Modify: `config.example.json`
- Test: `tests/test_config.py`

- [ ] **Step 1: Add failing config tests**

Append these tests to `tests/test_config.py`:

```python
import pytest


def test_load_config_includes_lastfm_and_recommendation_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": str(tmp_path / "scrobbles.csv"),
                    "timezone": "UTC",
                    "timestamp_column": "uts",
                    "csv_columns": [
                        "uts",
                        "utc_time",
                        "artist",
                        "artist_mbid",
                        "album",
                        "album_mbid",
                        "track",
                        "track_mbid",
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.apis.lastfm.enabled is True
    assert config.apis.lastfm.api_key is None
    assert config.apis.lastfm.base_url == "https://ws.audioscrobbler.com/2.0/"
    assert config.apis.lastfm.rate_limit_per_second == 2.0
    assert config.apis.lastfm.cache_ttl_days == 180
    assert config.recommendation.seed_artist_limit == 250
    assert config.recommendation.similar_artists_per_seed == 100
    assert config.recommendation.min_seed_scrobbles == 3


def test_lastfm_api_key_interpolates_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LASTFM_API_KEY", "test-key")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": str(tmp_path / "scrobbles.csv"),
                    "timezone": "UTC",
                    "timestamp_column": "uts",
                    "csv_columns": [
                        "uts",
                        "utc_time",
                        "artist",
                        "artist_mbid",
                        "album",
                        "album_mbid",
                        "track",
                        "track_mbid",
                    ],
                },
                "apis": {
                    "lastfm": {
                        "enabled": True,
                        "api_key": "${LASTFM_API_KEY}",
                        "base_url": "https://ws.audioscrobbler.com/2.0/",
                        "rate_limit_per_second": 2.0,
                        "cache_ttl_days": 180,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.apis.lastfm.api_key == "test-key"
```

- [ ] **Step 2: Run config tests to verify they fail**

Run:

```bash
python -m pytest tests/test_config.py -v
```

Expected:

```text
AttributeError: 'ScrobbleRecsConfig' object has no attribute 'apis'
```

- [ ] **Step 3: Add config models**

Modify `src/scrobble_recs/config.py` to include these models before `ScrobbleRecsConfig`, then add fields to `ScrobbleRecsConfig`:

```python
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

    enabled: bool = True
    base_url: str = "https://musicbrainz.org/ws/2/"
    user_agent: str = "scrobble-recs/0.1.0"
    rate_limit_per_second: float = 1.0
    cache_ttl_days: int = 3650


class ListenBrainzApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    dump_dir: Path = Path("~/datasets/listenbrainz")
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
    model_config = ConfigDict(extra="forbid")

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
```

- [ ] **Step 4: Update example config**

Replace `config.example.json` with:

```json
{
  "app": {
    "name": "scrobble-recs",
    "db_path": "./data/scrobbles.duckdb",
    "cache_dir": "./data/cache",
    "log_path": "./data/app.log"
  },
  "input": {
    "lastfm_csv": "./tests/fixtures/tiny_lastfm_export.csv",
    "timezone": "UTC",
    "timestamp_column": "uts",
    "csv_columns": [
      "uts",
      "utc_time",
      "artist",
      "artist_mbid",
      "album",
      "album_mbid",
      "track",
      "track_mbid"
    ]
  },
  "apis": {
    "lastfm": {
      "enabled": true,
      "api_key": "${LASTFM_API_KEY}",
      "base_url": "https://ws.audioscrobbler.com/2.0/",
      "rate_limit_per_second": 2.0,
      "cache_ttl_days": 180
    },
    "musicbrainz": {
      "enabled": false,
      "base_url": "https://musicbrainz.org/ws/2/",
      "user_agent": "scrobble-recs/0.1.0",
      "rate_limit_per_second": 1.0,
      "cache_ttl_days": 3650
    },
    "listenbrainz": {
      "enabled": false,
      "dump_dir": "~/datasets/listenbrainz",
      "use_recommendation_api": false
    }
  },
  "recommendation": {
    "default_mode": "balanced",
    "seed_artist_limit": 250,
    "similar_artists_per_seed": 100,
    "min_seed_scrobbles": 3,
    "max_knownness_default": 0.65,
    "rediscovery_enabled": true,
    "obviousness": {
      "enabled": true,
      "top_n_per_tag_penalty": 10,
      "top_n_per_tag_hide": 3,
      "tag_top_artists_limit": 100,
      "penalty_weight": 0.25
    },
    "weights": {
      "lastfm_similarity": 0.35,
      "tag_similarity": 0.25,
      "personal_cluster_fit": 0.15,
      "rediscovery": 0.10,
      "metadata_graph": 0.05,
      "feedback_model": 0.10
    },
    "diversity": {
      "enabled": true,
      "lambda": 0.75,
      "max_same_primary_tag_in_top_20": 4
    }
  }
}
```

- [ ] **Step 5: Run config tests to verify they pass**

Run:

```bash
python -m pytest tests/test_config.py -v
```

Expected:

```text
4 passed
```

- [ ] **Step 6: Commit config work**

Run:

```bash
git add config.example.json src/scrobble_recs/config.py tests/test_config.py
git commit -m "feat: add api and recommendation config"
```

---

### Task 2: Enrichment Schema Migration

**Files:**
- Create: `src/scrobble_recs/sql/002_enrichment.sql`
- Test: `tests/test_api_cache.py`

- [x] **Step 1: Add failing schema test**

Create `tests/test_api_cache.py` with:

```python
from pathlib import Path

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database


def test_enrichment_migration_creates_cache_and_normalized_tables(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        f"""
        {{
          "app": {{
            "name": "scrobble-recs",
            "db_path": "{(tmp_path / "db.duckdb").as_posix()}",
            "cache_dir": "{(tmp_path / "cache").as_posix()}",
            "log_path": "{(tmp_path / "app.log").as_posix()}"
          }},
          "input": {{
            "lastfm_csv": "tests/fixtures/tiny_lastfm_export.csv",
            "timezone": "UTC",
            "timestamp_column": "uts",
            "csv_columns": [
              "uts",
              "utc_time",
              "artist",
              "artist_mbid",
              "album",
              "album_mbid",
              "track",
              "track_mbid"
            ]
          }}
        }}
        """,
        encoding="utf-8",
    )
    conn = initialize_database(load_config(config_path))

    tables = {
        row[0]
        for row in conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
            """
        ).fetchall()
    }

    assert "api_cache" in tables
    assert "artist_similarity_edge" in tables
    assert "artist_tag" in tables
    assert "tag_top_artist" in tables
    conn.close()
```

- [x] **Step 2: Run the schema test to verify it fails** _(Deviation note: the schema implementation already existed at `87a0b0d` when this checkpoint started, so the red failure was not re-run in the current worktree.)_

Run:

```bash
python -m pytest tests/test_api_cache.py::test_enrichment_migration_creates_cache_and_normalized_tables -v
```

Expected:

```text
AssertionError showing that api_cache is missing from the discovered table names
```

- [x] **Step 3: Add the enrichment migration**

Create `src/scrobble_recs/sql/002_enrichment.sql` with:

```sql
CREATE TABLE IF NOT EXISTS api_cache (
    cache_key VARCHAR PRIMARY KEY,
    source VARCHAR,
    endpoint VARCHAR,
    request_url VARCHAR,
    request_params JSON,
    response_status INTEGER,
    response_json JSON,
    fetched_at TIMESTAMP,
    expires_at TIMESTAMP,
    error_message VARCHAR
);

CREATE TABLE IF NOT EXISTS artist_similarity_edge (
    source_artist_id UBIGINT,
    target_artist_id UBIGINT,
    source VARCHAR,
    source_score DOUBLE,
    normalized_score DOUBLE,
    evidence JSON,
    fetched_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artist_tag (
    artist_id UBIGINT,
    tag VARCHAR,
    source VARCHAR,
    tag_count DOUBLE,
    normalized_weight DOUBLE,
    fetched_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tag_top_artist (
    tag VARCHAR,
    artist_id UBIGINT,
    source VARCHAR,
    tag_rank INTEGER,
    tag_count DOUBLE,
    fetched_at TIMESTAMP
);
```

In `tests/test_config.py`, update `test_initialize_database_applies_initial_migration` so the exact table assertion includes the enrichment tables:

```python
    assert tables == {
        "api_cache",
        "artist",
        "artist_listening_fact",
        "artist_similarity_edge",
        "artist_tag",
        "raw_scrobble",
        "scrobble",
        "tag_top_artist",
    }
```

- [x] **Step 4: Run the schema test to verify it passes**

Run:

```bash
python -m pytest tests/test_api_cache.py::test_enrichment_migration_creates_cache_and_normalized_tables -v
```

Expected:

```text
1 passed
```

- [x] **Step 5: Commit schema** _(Deviation note: no duplicate commit was created in this checkpoint because `87a0b0d` already contains the completed schema task.)_

Run:

```bash
git add src/scrobble_recs/sql/002_enrichment.sql tests/test_api_cache.py
git commit -m "feat: add enrichment schema"
```

---

### Task 3: DuckDB API Cache

**Files:**
- Create: `src/scrobble_recs/apis/__init__.py`
- Create: `src/scrobble_recs/apis/cache.py`
- Modify: `tests/test_api_cache.py`

- [x] **Step 1: Add failing cache behavior tests** _(Deviation note: review-driven regression tests were also added for failed replacement preservation, post-delete insert rollback, and non-2xx cache misses.)_

Append these tests to `tests/test_api_cache.py`:

```python
from datetime import datetime, timedelta

from scrobble_recs.apis.cache import ApiCache, build_cache_key


def test_build_cache_key_is_stable_for_parameter_order() -> None:
    first = build_cache_key(
        "lastfm",
        "artist.getSimilar",
        {"artist": "The Cure", "limit": 100},
    )
    second = build_cache_key(
        "lastfm",
        "artist.getSimilar",
        {"limit": 100, "artist": "The Cure"},
    )

    assert first == second
    assert first.startswith("lastfm:artist.getSimilar:")


def test_api_cache_returns_fresh_payload_and_ignores_stale_payload(tmp_path: Path) -> None:
    config = load_config(_write_minimal_config(tmp_path))
    conn = initialize_database(config)
    cache = ApiCache(conn)
    now = datetime(2026, 6, 3, 12, 0, 0)
    params = {"artist": "The Cure", "limit": 2}

    cache.write_response(
        source="lastfm",
        endpoint="artist.getSimilar",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params=params,
        response_status=200,
        response_json={"similarartists": {"artist": [{"name": "Siouxsie"}]}},
        fetched_at=now,
        expires_at=now + timedelta(days=180),
        error_message=None,
    )

    assert cache.get_fresh("lastfm", "artist.getSimilar", params, now) == {
        "similarartists": {"artist": [{"name": "Siouxsie"}]}
    }
    assert cache.get_fresh(
        "lastfm",
        "artist.getSimilar",
        params,
        now + timedelta(days=181),
    ) is None
    conn.close()
```

Also add this helper near the top of `tests/test_api_cache.py`:

```python
def _write_minimal_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        f"""
        {{
          "app": {{
            "name": "scrobble-recs",
            "db_path": "{(tmp_path / "db.duckdb").as_posix()}",
            "cache_dir": "{(tmp_path / "cache").as_posix()}",
            "log_path": "{(tmp_path / "app.log").as_posix()}"
          }},
          "input": {{
            "lastfm_csv": "tests/fixtures/tiny_lastfm_export.csv",
            "timezone": "UTC",
            "timestamp_column": "uts",
            "csv_columns": [
              "uts",
              "utc_time",
              "artist",
              "artist_mbid",
              "album",
              "album_mbid",
              "track",
              "track_mbid"
            ]
          }}
        }}
        """,
        encoding="utf-8",
    )
    return config_path
```

- [x] **Step 2: Run cache tests to verify they fail**

Run:

```bash
python -m pytest tests/test_api_cache.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.apis'
```

- [x] **Step 3: Add API package marker**

Create `src/scrobble_recs/apis/__init__.py` with:

```python
"""Cache-first API clients."""
```

- [x] **Step 4: Implement the API cache** _(Deviation note: `write_response` prepares serialized values before mutation and wraps replacement in a transaction with rollback protection, rather than using the plan's literal unguarded delete-then-insert sequence.)_

Create `src/scrobble_recs/apis/cache.py` with:

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from duckdb import DuckDBPyConnection


JsonObject = dict[str, Any]


def canonical_json_params(params: JsonObject) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_cache_key(source: str, endpoint: str, params: JsonObject) -> str:
    digest = hashlib.sha256(canonical_json_params(params).encode("utf-8")).hexdigest()
    return f"{source}:{endpoint}:{digest}"


@dataclass(frozen=True)
class ApiCacheEntry:
    cache_key: str
    source: str
    endpoint: str
    request_url: str
    request_params: JsonObject
    response_status: int
    response_json: JsonObject
    fetched_at: datetime
    expires_at: datetime
    error_message: str | None


class ApiCache:
    def __init__(self, conn: DuckDBPyConnection) -> None:
        self._conn = conn

    def get_fresh(
        self,
        source: str,
        endpoint: str,
        request_params: JsonObject,
        now: datetime,
    ) -> JsonObject | None:
        cache_key = build_cache_key(source, endpoint, request_params)
        row = self._conn.execute(
            """
            SELECT response_json
            FROM api_cache
            WHERE cache_key = ?
              AND response_status BETWEEN 200 AND 299
              AND expires_at > ?
            """,
            [cache_key, now],
        ).fetchone()
        if row is None:
            return None
        return json.loads(str(row[0]))

    def write_response(
        self,
        *,
        source: str,
        endpoint: str,
        request_url: str,
        request_params: JsonObject,
        response_status: int,
        response_json: JsonObject,
        fetched_at: datetime,
        expires_at: datetime,
        error_message: str | None,
    ) -> None:
        cache_key = build_cache_key(source, endpoint, request_params)
        self._conn.execute(
            "DELETE FROM api_cache WHERE cache_key = ?",
            [cache_key],
        )
        self._conn.execute(
            """
            INSERT INTO api_cache (
                cache_key,
                source,
                endpoint,
                request_url,
                request_params,
                response_status,
                response_json,
                fetched_at,
                expires_at,
                error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                cache_key,
                source,
                endpoint,
                request_url,
                json.dumps(request_params, sort_keys=True),
                response_status,
                json.dumps(response_json, sort_keys=True),
                fetched_at,
                expires_at,
                error_message,
            ],
        )
```

- [x] **Step 5: Run cache tests to verify they pass** _(Deviation note: final targeted result is 6 passed because review-driven regression coverage was added, not the original 3 passed.)_

Run:

```bash
python -m pytest tests/test_api_cache.py -v
```

Expected:

```text
3 passed
```

- [x] **Step 6: Commit cache** _(Deviation note: the Task 3 commit was amended to include `docs/implementation-log.md` to satisfy the repository done criteria; the plan file was not added to the Task 3 commit, then was added by the Task 4 commit.)_

Run:

```bash
git add src/scrobble_recs/apis tests/test_api_cache.py
git commit -m "feat: add duckdb api cache"
```

---

### Task 4: Deterministic Rate Limiter

**Files:**
- Create: `src/scrobble_recs/apis/rate_limit.py`
- Test: `tests/test_rate_limit.py`

- [x] **Step 1: Write failing rate limiter tests**

Create `tests/test_rate_limit.py` with:

```python
import pytest

from scrobble_recs.apis.rate_limit import RateLimiter


def test_rate_limiter_waits_until_next_allowed_request() -> None:
    times = iter([10.0, 10.1])
    slept: list[float] = []
    limiter = RateLimiter(
        rate_limit_per_second=2.0,
        monotonic=lambda: next(times),
        sleep=slept.append,
    )

    limiter.wait()
    limiter.wait()

    assert slept == pytest.approx([0.4])


def test_rate_limiter_does_not_wait_when_interval_has_elapsed() -> None:
    times = iter([10.0, 10.6])
    slept: list[float] = []
    limiter = RateLimiter(
        rate_limit_per_second=2.0,
        monotonic=lambda: next(times),
        sleep=slept.append,
    )

    limiter.wait()
    limiter.wait()

    assert slept == []
```

Deviation: the wait assertion uses `pytest.approx([0.4])` instead of exact list equality because Python float arithmetic produces `0.40000000000000036` for this deterministic input.

- [x] **Step 2: Run rate limiter tests to verify they fail**

Run:

```bash
python -m pytest tests/test_rate_limit.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.apis.rate_limit'
```

- [x] **Step 3: Implement rate limiter**

Create `src/scrobble_recs/apis/rate_limit.py` with:

```python
from __future__ import annotations

import time
from collections.abc import Callable
from math import isfinite


class RateLimiter:
    def __init__(
        self,
        rate_limit_per_second: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isfinite(rate_limit_per_second) or rate_limit_per_second <= 0:
            raise ValueError(
                "rate_limit_per_second must be finite and greater than zero"
            )
        self._minimum_interval = 1.0 / rate_limit_per_second
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request_at: float | None = None

    def wait(self) -> None:
        now = self._monotonic()
        if self._last_request_at is None:
            self._last_request_at = now
            return

        elapsed = now - self._last_request_at
        wait_seconds = self._minimum_interval - elapsed
        if wait_seconds > 0:
            self._sleep(wait_seconds)
            self._last_request_at = now + wait_seconds
            return

        self._last_request_at = now
```

Hardening deviation: review follow-up rejects non-finite rates such as `nan` and `inf` in addition to non-positive rates.

- [x] **Step 4: Run rate limiter tests to verify they pass**

Run:

```bash
python -m pytest tests/test_rate_limit.py -v
```

Expected:

```text
8 passed
```

- [x] **Step 5: Commit rate limiter**

Run:

```bash
git add src/scrobble_recs/apis/rate_limit.py tests/test_rate_limit.py
git commit -m "feat: add api rate limiter"
```

Deviation: the commit also stages the Task 4 plan checklist update and implementation-log entry because those files are part of the requested Task 4 write set.

Follow-up: `b76f920 test: harden api rate limiter` added review-requested coverage for invalid rates and post-sleep state handling, plus finite-positive rate validation.

---

### Task 5: Cache-First Last.fm Client

**Files:**
- Create: `src/scrobble_recs/apis/lastfm.py`
- Test: `tests/test_lastfm_client.py`

- [x] **Step 1: Write failing Last.fm client tests**

Create `tests/test_lastfm_client.py` with:

```python
from datetime import datetime
from pathlib import Path

import httpx

from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.apis.lastfm import LastFmClient
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import LastFmApiConfig, load_config
from scrobble_recs.db import initialize_database


def _config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        f"""
        {{
          "app": {{
            "name": "scrobble-recs",
            "db_path": "{(tmp_path / "db.duckdb").as_posix()}",
            "cache_dir": "{(tmp_path / "cache").as_posix()}",
            "log_path": "{(tmp_path / "app.log").as_posix()}"
          }},
          "input": {{
            "lastfm_csv": "tests/fixtures/tiny_lastfm_export.csv",
            "timezone": "UTC",
            "timestamp_column": "uts",
            "csv_columns": [
              "uts",
              "utc_time",
              "artist",
              "artist_mbid",
              "album",
              "album_mbid",
              "track",
              "track_mbid"
            ]
          }}
        }}
        """,
        encoding="utf-8",
    )
    return path


def test_get_similar_artists_uses_http_once_then_fresh_cache(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "similarartists": {
                    "artist": [
                        {"name": "Siouxsie and the Banshees", "mbid": "mbid-siouxsie", "match": "0.91"},
                        {"name": "Bauhaus", "mbid": "", "match": "0.72"},
                    ]
                }
            },
        )

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key"),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    first = client.get_similar_artists("The Cure", limit=2)
    second = client.get_similar_artists("The Cure", limit=2)

    assert [artist.name for artist in first] == ["Siouxsie and the Banshees", "Bauhaus"]
    assert [artist.name for artist in second] == ["Siouxsie and the Banshees", "Bauhaus"]
    assert len(calls) == 1
    assert calls[0].url.params["api_key"] == "secret-key"
    cached = conn.execute("SELECT request_params, request_url FROM api_cache").fetchone()
    assert cached is not None
    assert "secret-key" not in str(cached[0])
    assert "secret-key" not in str(cached[1])
    conn.close()


def test_get_artist_tags_and_tag_top_artists_parse_lastfm_payloads(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.params["method"]
        if method == "artist.getTopTags":
            return httpx.Response(
                200,
                json={
                    "toptags": {
                        "tag": [
                            {"name": "post-punk", "count": 100},
                            {"name": "gothic rock", "count": "75"},
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "topartists": {
                    "artist": [
                        {"name": "The Cure", "mbid": "mbid-cure", "count": "1000"},
                        {"name": "Joy Division", "mbid": "", "count": 800},
                    ]
                }
            },
        )

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key"),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    tags = client.get_artist_tags("The Cure")
    top_artists = client.get_tag_top_artists("post-punk", limit=2)

    assert [(tag.name, tag.count) for tag in tags] == [("post-punk", 100.0), ("gothic rock", 75.0)]
    assert [(artist.rank, artist.name, artist.count) for artist in top_artists] == [
        (1, "The Cure", 1000.0),
        (2, "Joy Division", 800.0),
    ]
    conn.close()
```

- [x] **Step 2: Run Last.fm client tests to verify they fail**

Run:

```bash
python -m pytest tests/test_lastfm_client.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.apis.lastfm'
```

- [x] **Step 3: Implement Last.fm client**

Create `src/scrobble_recs/apis/lastfm.py` with:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from scrobble_recs.apis.cache import ApiCache, JsonObject
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import LastFmApiConfig


@dataclass(frozen=True)
class LastFmSimilarArtist:
    name: str
    mbid: str | None
    match: float


@dataclass(frozen=True)
class LastFmArtistTag:
    name: str
    count: float


@dataclass(frozen=True)
class LastFmTagTopArtist:
    rank: int
    name: str
    mbid: str | None
    count: float


class LastFmClient:
    def __init__(
        self,
        *,
        config: LastFmApiConfig,
        cache: ApiCache,
        http_client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._http_client = http_client or httpx.Client(timeout=30.0)
        self._rate_limiter = rate_limiter or RateLimiter(config.rate_limit_per_second)
        self._now = now or (lambda: datetime.now(tz=UTC).replace(tzinfo=None))

    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        payload = self._request(
            "artist.getSimilar",
            {
                "artist": artist,
                "limit": limit,
            },
        )
        raw_artists = _as_list(payload.get("similarartists", {}).get("artist", []))
        return [
            LastFmSimilarArtist(
                name=str(item.get("name", "")).strip(),
                mbid=_optional_text(item.get("mbid")),
                match=_float_value(item.get("match")),
            )
            for item in raw_artists
            if str(item.get("name", "")).strip() != ""
        ]

    def get_artist_tags(self, artist: str) -> list[LastFmArtistTag]:
        payload = self._request(
            "artist.getTopTags",
            {
                "artist": artist,
            },
        )
        raw_tags = _as_list(payload.get("toptags", {}).get("tag", []))
        return [
            LastFmArtistTag(
                name=str(item.get("name", "")).strip(),
                count=_float_value(item.get("count")),
            )
            for item in raw_tags
            if str(item.get("name", "")).strip() != ""
        ]

    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        payload = self._request(
            "tag.getTopArtists",
            {
                "tag": tag,
                "limit": limit,
            },
        )
        raw_artists = _as_list(payload.get("topartists", {}).get("artist", []))
        parsed: list[LastFmTagTopArtist] = []
        for index, item in enumerate(raw_artists, start=1):
            name = str(item.get("name", "")).strip()
            if name == "":
                continue
            parsed.append(
                LastFmTagTopArtist(
                    rank=index,
                    name=name,
                    mbid=_optional_text(item.get("mbid")),
                    count=_float_value(item.get("count")),
                )
            )
        return parsed

    def _request(self, method: str, params: JsonObject) -> JsonObject:
        if not self._config.enabled:
            raise RuntimeError("Last.fm API is disabled in config")
        if self._config.api_key is None:
            raise RuntimeError("Last.fm API key is required for enrichment")

        cache_params = {"method": method, **params}
        now = self._now()
        cached = self._cache.get_fresh("lastfm", method, cache_params, now)
        if cached is not None:
            return cached

        self._rate_limiter.wait()
        request_params = {
            **cache_params,
            "api_key": self._config.api_key,
            "format": "json",
        }
        response = self._http_client.get(self._config.base_url, params=request_params)
        response_json = _response_json(response)
        fetched_at = self._now()
        self._cache.write_response(
            source="lastfm",
            endpoint=method,
            request_url=self._config.base_url,
            request_params=cache_params,
            response_status=response.status_code,
            response_json=response_json,
            fetched_at=fetched_at,
            expires_at=fetched_at + timedelta(days=self._config.cache_ttl_days),
            error_message=None if response.is_success else response.text,
        )
        response.raise_for_status()
        return response_json


def _response_json(response: httpx.Response) -> JsonObject:
    value = response.json()
    if isinstance(value, dict):
        return value
    return {"value": value}


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
```

- [x] **Step 4: Run Last.fm client tests to verify they pass**

Run:

```bash
python -m pytest tests/test_lastfm_client.py -v
```

Expected:

```text
2 passed
```

- [x] **Step 5: Commit Last.fm client** _(Deviation note: the final Task 5 commit also updates `docs/implementation-log.md` to satisfy the repository Done requirement; that file is outside the Task 5 file list.)_

Run:

```bash
git add src/scrobble_recs/apis/lastfm.py tests/test_lastfm_client.py
git commit -m "feat: add cache-first lastfm client"
```

---

### Task 6: Seed Artist Selection

**Files:**
- Create: `src/scrobble_recs/models/__init__.py`
- Create: `src/scrobble_recs/models/profile.py`
- Test: `tests/test_profile.py`

- [ ] **Step 1: Write failing seed selection tests**

Create `tests/test_profile.py` with:

```python
import json
from pathlib import Path

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
from scrobble_recs.models.profile import select_seed_artists


def _config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": "tests/fixtures/tiny_lastfm_export.csv",
                    "timezone": "UTC",
                    "timestamp_column": "uts",
                    "csv_columns": [
                        "uts",
                        "utc_time",
                        "artist",
                        "artist_mbid",
                        "album",
                        "album_mbid",
                        "track",
                        "track_mbid",
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_select_seed_artists_orders_by_weight_then_name(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))

    seeds = select_seed_artists(conn, limit=3, min_scrobbles=1)

    assert [(seed.artist_id, seed.name, seed.play_count) for seed in seeds] == [
        (4, "The Cure", 2),
        (1, "Cocteau Twins", 1),
        (2, "Pixies", 1),
    ]
    conn.close()


def test_select_seed_artists_respects_min_scrobbles(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))

    seeds = select_seed_artists(conn, limit=10, min_scrobbles=2)

    assert [seed.name for seed in seeds] == ["The Cure"]
    conn.close()
```

- [ ] **Step 2: Run seed tests to verify they fail**

Run:

```bash
python -m pytest tests/test_profile.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.models'
```

- [ ] **Step 3: Add models package marker**

Create `src/scrobble_recs/models/__init__.py` with:

```python
"""Taste profile and recommendation model helpers."""
```

- [ ] **Step 4: Implement seed selection**

Create `src/scrobble_recs/models/profile.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass

from duckdb import DuckDBPyConnection


@dataclass(frozen=True)
class SeedArtist:
    artist_id: int
    name: str
    artist_key: str
    lastfm_name: str
    play_count: int
    seed_weight: float


def select_seed_artists(
    conn: DuckDBPyConnection,
    *,
    limit: int,
    min_scrobbles: int,
) -> list[SeedArtist]:
    rows = conn.execute(
        """
        SELECT
            a.artist_id,
            a.canonical_name,
            a.artist_key,
            coalesce(nullif(a.lastfm_name, ''), a.canonical_name) AS lastfm_name,
            f.play_count,
            coalesce(f.recent_weight, f.lifetime_weight, ln(1 + f.play_count)) AS seed_weight
        FROM artist a
        JOIN artist_listening_fact f ON f.artist_id = a.artist_id
        WHERE f.play_count >= ?
        ORDER BY seed_weight DESC, a.canonical_name ASC
        LIMIT ?
        """,
        [min_scrobbles, limit],
    ).fetchall()
    return [
        SeedArtist(
            artist_id=int(row[0]),
            name=str(row[1]),
            artist_key=str(row[2]),
            lastfm_name=str(row[3]),
            play_count=int(row[4]),
            seed_weight=float(row[5]),
        )
        for row in rows
    ]
```

- [ ] **Step 5: Run seed tests to verify they pass**

Run:

```bash
python -m pytest tests/test_profile.py -v
```

Expected:

```text
2 passed
```

- [ ] **Step 6: Commit seed selection**

Run:

```bash
git add src/scrobble_recs/models tests/test_profile.py
git commit -m "feat: select lastfm enrichment seed artists"
```

---

### Task 7: Last.fm Enrichment Service

**Files:**
- Create: `src/scrobble_recs/enrichment/__init__.py`
- Create: `src/scrobble_recs/enrichment/lastfm.py`
- Test: `tests/test_lastfm_enrichment.py`

- [ ] **Step 1: Write failing enrichment service tests**

Create `tests/test_lastfm_enrichment.py` with:

```python
import json
from pathlib import Path

from scrobble_recs.apis.lastfm import LastFmArtistTag, LastFmSimilarArtist, LastFmTagTopArtist
from scrobble_recs.config import ScrobbleRecsConfig, load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.enrichment.lastfm import run_lastfm_enrichment
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv


class FakeLastFmClient:
    def __init__(self) -> None:
        self.similar_calls: list[tuple[str, int]] = []
        self.tag_calls: list[str] = []
        self.top_artist_calls: list[tuple[str, int]] = []

    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        self.similar_calls.append((artist, limit))
        return [
            LastFmSimilarArtist("Siouxsie and the Banshees", "mbid-siouxsie", 0.91),
            LastFmSimilarArtist("Bauhaus", None, 0.72),
        ]

    def get_artist_tags(self, artist: str) -> list[LastFmArtistTag]:
        self.tag_calls.append(artist)
        return [
            LastFmArtistTag("post-punk", 100.0),
            LastFmArtistTag("gothic rock", 75.0),
        ]

    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        self.top_artist_calls.append((tag, limit))
        return [
            LastFmTagTopArtist(1, "The Cure", "mbid-cure", 1000.0),
            LastFmTagTopArtist(2, "Joy Division", None, 800.0),
        ]


def _config(tmp_path: Path) -> ScrobbleRecsConfig:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": "tests/fixtures/tiny_lastfm_export.csv",
                    "timezone": "UTC",
                    "timestamp_column": "uts",
                    "csv_columns": [
                        "uts",
                        "utc_time",
                        "artist",
                        "artist_mbid",
                        "album",
                        "album_mbid",
                        "track",
                        "track_mbid",
                    ],
                },
                "recommendation": {
                    "seed_artist_limit": 1,
                    "similar_artists_per_seed": 2,
                    "min_seed_scrobbles": 2,
                    "obviousness": {"tag_top_artists_limit": 2},
                },
            }
        ),
        encoding="utf-8",
    )
    return load_config(path)


def test_run_lastfm_enrichment_persists_edges_tags_and_top_artists(tmp_path: Path) -> None:
    config = _config(tmp_path)
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    client = FakeLastFmClient()

    stats = run_lastfm_enrichment(conn, config, client=client)

    assert stats.seed_artists == 1
    assert stats.similar_artist_requests == 1
    assert stats.artist_tag_requests == 1
    assert stats.tag_top_artist_requests == 2
    assert stats.similarity_edges == 2
    assert stats.artist_tags == 2
    assert stats.tag_top_artists == 4
    assert client.similar_calls == [("The Cure", 2)]
    assert client.tag_calls == ["The Cure"]
    assert client.top_artist_calls == [("post-punk", 2), ("gothic rock", 2)]

    edge_rows = conn.execute(
        """
        SELECT source_artist.canonical_name, target_artist.canonical_name, edge.normalized_score
        FROM artist_similarity_edge edge
        JOIN artist source_artist ON source_artist.artist_id = edge.source_artist_id
        JOIN artist target_artist ON target_artist.artist_id = edge.target_artist_id
        ORDER BY target_artist.canonical_name
        """
    ).fetchall()
    assert edge_rows == [
        ("The Cure", "Bauhaus", 0.72),
        ("The Cure", "Siouxsie and the Banshees", 0.91),
    ]
    conn.close()


def test_run_lastfm_enrichment_skips_already_normalized_work_on_second_run(tmp_path: Path) -> None:
    config = _config(tmp_path)
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    first_client = FakeLastFmClient()
    second_client = FakeLastFmClient()

    run_lastfm_enrichment(conn, config, client=first_client)
    stats = run_lastfm_enrichment(conn, config, client=second_client)

    assert stats.similar_artist_requests == 0
    assert stats.artist_tag_requests == 0
    assert stats.tag_top_artist_requests == 0
    assert second_client.similar_calls == []
    assert second_client.tag_calls == []
    assert second_client.top_artist_calls == []
    assert conn.execute("SELECT count(*) FROM artist_similarity_edge").fetchone() == (2,)
    assert conn.execute("SELECT count(*) FROM artist_tag").fetchone() == (2,)
    assert conn.execute("SELECT count(*) FROM tag_top_artist").fetchone() == (4,)
    conn.close()
```

- [ ] **Step 2: Run enrichment service tests to verify they fail**

Run:

```bash
python -m pytest tests/test_lastfm_enrichment.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.enrichment'
```

- [ ] **Step 3: Add enrichment package marker**

Create `src/scrobble_recs/enrichment/__init__.py` with:

```python
"""Offline-first enrichment pipelines."""
```

- [ ] **Step 4: Implement Last.fm enrichment service**

Create `src/scrobble_recs/enrichment/lastfm.py` with:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from duckdb import DuckDBPyConnection

from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.apis.lastfm import (
    LastFmArtistTag,
    LastFmClient,
    LastFmSimilarArtist,
    LastFmTagTopArtist,
)
from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.profile import SeedArtist, select_seed_artists
from scrobble_recs.normalize.strings import normalize_key


class LastFmClientProtocol(Protocol):
    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        raise NotImplementedError

    def get_artist_tags(self, artist: str) -> list[LastFmArtistTag]:
        raise NotImplementedError

    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        raise NotImplementedError


@dataclass(frozen=True)
class LastFmEnrichmentStats:
    seed_artists: int
    similar_artist_requests: int
    artist_tag_requests: int
    tag_top_artist_requests: int
    artists_created: int
    similarity_edges: int
    artist_tags: int
    tag_top_artists: int


def run_lastfm_enrichment(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    client: LastFmClientProtocol | None = None,
) -> LastFmEnrichmentStats:
    resolved_client = client or LastFmClient(
        config=config.apis.lastfm,
        cache=ApiCache(conn),
    )
    seeds = select_seed_artists(
        conn,
        limit=config.recommendation.seed_artist_limit,
        min_scrobbles=config.recommendation.min_seed_scrobbles,
    )
    stats = _MutableStats(seed_artists=len(seeds))

    for seed in seeds:
        _enrich_seed_similarity(conn, resolved_client, seed, config, stats)
        _enrich_seed_tags(conn, resolved_client, seed, stats)

    _enrich_tag_top_artists(conn, resolved_client, config, stats)

    return stats.snapshot()


@dataclass
class _MutableStats:
    seed_artists: int = 0
    similar_artist_requests: int = 0
    artist_tag_requests: int = 0
    tag_top_artist_requests: int = 0
    artists_created: int = 0
    similarity_edges: int = 0
    artist_tags: int = 0
    tag_top_artists: int = 0

    def snapshot(self) -> LastFmEnrichmentStats:
        return LastFmEnrichmentStats(
            seed_artists=self.seed_artists,
            similar_artist_requests=self.similar_artist_requests,
            artist_tag_requests=self.artist_tag_requests,
            tag_top_artist_requests=self.tag_top_artist_requests,
            artists_created=self.artists_created,
            similarity_edges=self.similarity_edges,
            artist_tags=self.artist_tags,
            tag_top_artists=self.tag_top_artists,
        )


def _enrich_seed_similarity(
    conn: DuckDBPyConnection,
    client: LastFmClientProtocol,
    seed: SeedArtist,
    config: ScrobbleRecsConfig,
    stats: _MutableStats,
) -> None:
    limit = config.recommendation.similar_artists_per_seed
    if _count_rows(
        conn,
        "SELECT count(*) FROM artist_similarity_edge WHERE source = 'lastfm' AND source_artist_id = ?",
        [seed.artist_id],
    ) >= limit:
        return

    similar_artists = client.get_similar_artists(seed.lastfm_name, limit=limit)
    stats.similar_artist_requests += 1
    for similar in similar_artists:
        target_id, created = _ensure_artist(conn, similar.name, similar.mbid)
        stats.artists_created += int(created)
        inserted = _insert_similarity_edge(conn, seed.artist_id, target_id, similar)
        stats.similarity_edges += int(inserted)


def _enrich_seed_tags(
    conn: DuckDBPyConnection,
    client: LastFmClientProtocol,
    seed: SeedArtist,
    stats: _MutableStats,
) -> None:
    if _count_rows(
        conn,
        "SELECT count(*) FROM artist_tag WHERE source = 'lastfm' AND artist_id = ?",
        [seed.artist_id],
    ) > 0:
        return

    tags = client.get_artist_tags(seed.lastfm_name)
    stats.artist_tag_requests += 1
    max_count = max((tag.count for tag in tags), default=0.0)
    for tag in tags:
        normalized_weight = 0.0 if max_count <= 0 else tag.count / max_count
        inserted = _insert_artist_tag(conn, seed.artist_id, tag, normalized_weight)
        stats.artist_tags += int(inserted)


def _enrich_tag_top_artists(
    conn: DuckDBPyConnection,
    client: LastFmClientProtocol,
    config: ScrobbleRecsConfig,
    stats: _MutableStats,
) -> None:
    limit = config.recommendation.obviousness.tag_top_artists_limit
    tags = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT tag
            FROM artist_tag
            WHERE source = 'lastfm'
            GROUP BY tag
            ORDER BY sum(normalized_weight) DESC, tag ASC
            """
        ).fetchall()
    ]
    for tag in tags:
        if _count_rows(
            conn,
            "SELECT count(*) FROM tag_top_artist WHERE source = 'lastfm' AND tag = ?",
            [tag],
        ) >= limit:
            continue

        top_artists = client.get_tag_top_artists(tag, limit=limit)
        stats.tag_top_artist_requests += 1
        for top_artist in top_artists:
            artist_id, created = _ensure_artist(conn, top_artist.name, top_artist.mbid)
            stats.artists_created += int(created)
            inserted = _insert_tag_top_artist(conn, tag, artist_id, top_artist)
            stats.tag_top_artists += int(inserted)


def _ensure_artist(conn: DuckDBPyConnection, name: str, mbid: str | None) -> tuple[int, bool]:
    artist_key = normalize_key(name)
    existing = conn.execute(
        "SELECT artist_id FROM artist WHERE artist_key = ?",
        [artist_key],
    ).fetchone()
    if existing is not None:
        return int(existing[0]), False

    artist_id = _next_id(conn, "artist", "artist_id")
    conn.execute(
        """
        INSERT INTO artist (
            artist_id,
            canonical_name,
            artist_key,
            musicbrainz_artist_mbid,
            lastfm_name,
            resolution_status,
            resolution_confidence
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            artist_id,
            name,
            artist_key,
            mbid,
            name,
            "lastfm_enriched",
            0.5 if mbid else 0.25,
        ],
    )
    return artist_id, True


def _insert_similarity_edge(
    conn: DuckDBPyConnection,
    source_artist_id: int,
    target_artist_id: int,
    similar: LastFmSimilarArtist,
) -> bool:
    if _count_rows(
        conn,
        """
        SELECT count(*)
        FROM artist_similarity_edge
        WHERE source = 'lastfm'
          AND source_artist_id = ?
          AND target_artist_id = ?
        """,
        [source_artist_id, target_artist_id],
    ) > 0:
        return False

    conn.execute(
        """
        INSERT INTO artist_similarity_edge (
            source_artist_id,
            target_artist_id,
            source,
            source_score,
            normalized_score,
            evidence,
            fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            source_artist_id,
            target_artist_id,
            "lastfm",
            similar.match,
            similar.match,
            json.dumps({"method": "artist.getSimilar", "target_name": similar.name}),
            _now(),
        ],
    )
    return True


def _insert_artist_tag(
    conn: DuckDBPyConnection,
    artist_id: int,
    tag: LastFmArtistTag,
    normalized_weight: float,
) -> bool:
    if _count_rows(
        conn,
        """
        SELECT count(*)
        FROM artist_tag
        WHERE source = 'lastfm'
          AND artist_id = ?
          AND tag = ?
        """,
        [artist_id, tag.name],
    ) > 0:
        return False

    conn.execute(
        """
        INSERT INTO artist_tag (
            artist_id,
            tag,
            source,
            tag_count,
            normalized_weight,
            fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [artist_id, tag.name, "lastfm", tag.count, normalized_weight, _now()],
    )
    return True


def _insert_tag_top_artist(
    conn: DuckDBPyConnection,
    tag: str,
    artist_id: int,
    top_artist: LastFmTagTopArtist,
) -> bool:
    if _count_rows(
        conn,
        """
        SELECT count(*)
        FROM tag_top_artist
        WHERE source = 'lastfm'
          AND tag = ?
          AND artist_id = ?
        """,
        [tag, artist_id],
    ) > 0:
        return False

    conn.execute(
        """
        INSERT INTO tag_top_artist (
            tag,
            artist_id,
            source,
            tag_rank,
            tag_count,
            fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [tag, artist_id, "lastfm", top_artist.rank, top_artist.count, _now()],
    )
    return True


def _next_id(conn: DuckDBPyConnection, table: str, column: str) -> int:
    return _count_rows(conn, f"SELECT coalesce(max({column}), 0) + 1 FROM {table}", [])


def _count_rows(conn: DuckDBPyConnection, sql: str, params: list[object]) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


def _now() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)
```

- [ ] **Step 5: Run enrichment service tests to verify they pass**

Run:

```bash
python -m pytest tests/test_lastfm_enrichment.py -v
```

Expected:

```text
2 passed
```

- [ ] **Step 6: Commit enrichment service**

Run:

```bash
git add src/scrobble_recs/enrichment tests/test_lastfm_enrichment.py
git commit -m "feat: persist lastfm enrichment results"
```

---

### Task 8: CLI Enrich Command

**Files:**
- Modify: `src/scrobble_recs/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing CLI enrich test**

Append this test to `tests/test_cli.py`:

```python
def test_enrich_command_dispatches_lastfm_source(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    runner.invoke(app, ["import", "--config", str(config_path)])
    calls: list[str] = []

    class FakeStats:
        seed_artists = 1
        similar_artist_requests = 1
        artist_tag_requests = 1
        tag_top_artist_requests = 2
        artists_created = 3
        similarity_edges = 2
        artist_tags = 2
        tag_top_artists = 4

    def fake_enrich(conn, config, *, client=None):
        calls.append(config.app.name)
        return FakeStats()

    monkeypatch.setattr("scrobble_recs.cli.run_lastfm_enrichment", fake_enrich)

    result = runner.invoke(app, ["enrich", "--config", str(config_path), "--source", "lastfm"])

    assert result.exit_code == 0
    assert calls == ["scrobble-recs"]
    assert "Last.fm enrichment complete" in result.output
    assert "seed artists: 1" in result.output
    assert "similar artist requests: 1" in result.output
```

- [ ] **Step 2: Run CLI test to verify it fails**

Run:

```bash
python -m pytest tests/test_cli.py::test_enrich_command_dispatches_lastfm_source -v
```

Expected:

```text
No such command 'enrich'
```

- [ ] **Step 3: Add CLI command**

Modify `src/scrobble_recs/cli.py`:

Add this import:

```python
from scrobble_recs.enrichment.lastfm import LastFmEnrichmentStats, run_lastfm_enrichment
```

Add this command after `doctor_command`:

```python
@app.command("enrich")
def enrich_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    source: Annotated[str, typer.Option("--source", help="Enrichment source to run.")] = "lastfm",
) -> None:
    if source != "lastfm":
        raise typer.BadParameter("only --source lastfm is supported in this milestone")

    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        stats = run_lastfm_enrichment(conn, resolved_config)
    finally:
        conn.close()

    console.print("Last.fm enrichment complete")
    _print_enrichment_stats(stats)
```

Add this helper after `_print_stats`:

```python
def _print_enrichment_stats(stats: LastFmEnrichmentStats) -> None:
    console.print(f"seed artists: {stats.seed_artists}")
    console.print(f"similar artist requests: {stats.similar_artist_requests}")
    console.print(f"artist tag requests: {stats.artist_tag_requests}")
    console.print(f"tag top artist requests: {stats.tag_top_artist_requests}")
    console.print(f"artists created: {stats.artists_created}")
    console.print(f"similarity edges: {stats.similarity_edges}")
    console.print(f"artist tags: {stats.artist_tags}")
    console.print(f"tag top artists: {stats.tag_top_artists}")
```

- [ ] **Step 4: Run CLI enrich test to verify it passes**

Run:

```bash
python -m pytest tests/test_cli.py::test_enrich_command_dispatches_lastfm_source -v
```

Expected:

```text
1 passed
```

- [ ] **Step 5: Commit CLI enrich command**

Run:

```bash
git add src/scrobble_recs/cli.py tests/test_cli.py
git commit -m "feat: add lastfm enrich cli command"
```

---

### Task 9: Verification, Docs, And Implementation Log

**Files:**
- Modify: `README.md`
- Modify: `docs/implementation-log.md`

- [ ] **Step 1: Run full pytest suite**

Run:

```bash
python -m pytest
```

Expected:

```text
39 passed
```

- [ ] **Step 2: Run ruff check**

Run:

```bash
python -m ruff check .
```

Expected:

```text
All checks passed!
```

- [ ] **Step 3: Run ruff format check**

Run:

```bash
python -m ruff format --check .
```

Expected:

```text
All checks passed!
```

- [ ] **Step 4: Run mypy**

Run:

```bash
python -m mypy src
```

Expected:

```text
Success: no issues found
```

- [ ] **Step 5: Verify CLI still runs without API keys for import and doctor**

Run:

```bash
scrobble-recs import --config config.example.json
scrobble-recs doctor --config config.example.json
```

Expected:

```text
Imported 6 scrobbles
Config: ok
Database: ok
CSV: ok
API keys: not required for import/doctor
```

- [ ] **Step 6: Verify enrich fails clearly without a Last.fm key**

Run:

```bash
scrobble-recs enrich --config config.example.json --source lastfm
```

Expected:

```text
RuntimeError: Last.fm API key is required for enrichment
```

- [ ] **Step 7: Update README**

Append this section to `README.md`:

```markdown
## Milestone 2

This slice adds cache-first Last.fm enrichment:

- Select seed artists from persisted local listening facts.
- Fetch Last.fm similar artists and artist tags.
- Fetch Last.fm top artists for discovered tags.
- Persist every raw Last.fm response in `api_cache`.
- Persist normalized similarity, artist tag, and tag top artist rows.
- Resume without repeating normalized enrichment work.

Tests use mocked clients and mocked HTTP. CI must not call the live Last.fm API.

```bash
scrobble-recs enrich --config config.example.json --source lastfm
```

Set `LASTFM_API_KEY` in the environment before running live enrichment.
```

- [ ] **Step 8: Update implementation log**

Append this entry to `docs/implementation-log.md`:

```markdown
## 2026-06-03: Milestone 2 Last.fm enrichment

Changed:

- Added API and recommendation config defaults.
- Added DuckDB enrichment tables for API cache, artist similarity edges, artist tags, and tag top artists.
- Added canonical Last.fm cache keys that do not include API secrets.
- Added a deterministic rate limiter.
- Added a cache-first Last.fm client for `artist.getSimilar`, `artist.getTopTags`, and `tag.getTopArtists`.
- Added local seed artist selection from `artist_listening_fact`.
- Added idempotent Last.fm enrichment that can resume without repeating normalized work.
- Added `enrich --source lastfm`.

Verified:

- `python -m pytest`
- `python -m ruff check .`
- `python -m ruff format --check .`
- `python -m mypy src`
- `scrobble-recs import --config config.example.json`
- `scrobble-recs doctor --config config.example.json`
- `scrobble-recs enrich --config config.example.json --source lastfm` without `LASTFM_API_KEY` fails with a clear key-required error.
```

- [ ] **Step 9: Commit docs and verification**

Run:

```bash
git add README.md docs/implementation-log.md
git commit -m "docs: record lastfm enrichment milestone"
```

---

## Self Review

Spec coverage:

- Last.fm API config and environment interpolation are covered by Task 1.
- Enrichment schema from spec section 8.5 and 8.6 is covered by Task 2.
- Cache-first rule and cache key design from spec section 18 are covered by Task 3 and Task 5.
- Rate limiting is covered by Task 4 and used by Task 5.
- `artist.getSimilar`, `artist.getTopTags`, and `tag.getTopArtists` are covered by Task 5.
- Seed artist selection is covered by Task 6.
- Normalized similarity edges, artist tags, and tag top artists are covered by Task 7.
- Resume behavior and avoiding repeated normalized work are covered by Task 7.
- CLI contract for `scrobble-recs enrich --config config.json --source lastfm` is covered by Task 8.
- No live network calls in tests is satisfied by fake clients and `httpx.MockTransport`.
- Documentation and implementation log updates are covered by Task 9.

Deferred requirements:

- Candidate scoring and persisted recommendation runs are deferred to Milestone 3.
- Obviousness penalties use the tag top artist data from this slice but are scored in Milestone 3.
- TUI use of persisted runs is deferred to Milestone 4.
- MusicBrainz confidence-based resolution is deferred to Milestone 5.
- ListenBrainz collaborative modeling is deferred to Milestone 6.

Placeholder scan:

- The plan contains no open placeholder instructions.
- Every code-writing step names exact files and includes the concrete code or patch content to add.
- Every test-running step includes a command and expected result.

Type consistency:

- `LastFmApiConfig`, `ApiConfig`, and `RecommendationConfig` are defined before use.
- `ApiCache.get_fresh` and `ApiCache.write_response` signatures match `LastFmClient`.
- `LastFmClientProtocol` matches the fake client in tests and the real `LastFmClient`.
- `LastFmEnrichmentStats` fields match CLI output and tests.
- `SeedArtist` fields match the enrichment service.
