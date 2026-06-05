# CLI/Admin Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the command-line/admin surface so `init`, `doctor`, config validation, API/cache diagnostics, cache fallback behavior, and missing CLI contract commands behave predictably from local state.

**Architecture:** Keep operational checks separate from Typer rendering: admin modules return typed results and the CLI only formats them and chooses exit codes. Keep the app local-first by making `doctor`, `build-profile`, and contract validation read local files/DuckDB/cache tables without live API calls.

**Tech Stack:** Python 3.11+, Typer, Rich, Pydantic v2, DuckDB, httpx mocked with `MockTransport`, pytest, ruff, mypy.

---

## Scope

This plan covers the CLI/admin hardening slice from `docs/spec.md` sections 6, 7, 18, and 23.

Acceptance criteria covered:

- `scrobble-recs init --config ./config.json` creates a config skeleton and DuckDB file.
- `doctor` validates config, DB path, CSV schema, cache path, API key presence, cache state, and rate-limit settings.
- Config validation rejects invalid operational values early.
- API cache diagnostics are visible without live network calls.
- Last.fm API client keeps stale cache available for transient failures.
- Missing §7 CLI commands are present: `build-profile` has real local behavior; `resolve` has an explicit local/admin boundary for the later MusicBrainz milestone.
- README and implementation log are updated.

Deferred to separate plans:

- Full MusicBrainz identity resolution and entity merge UI.
- ListenBrainz API/dump diagnostics beyond disabled/local path checks.
- A Textual Doctor screen; this plan only improves CLI/admin backend data that a TUI screen can later consume.

## File Structure

- Modify: `src/scrobble_recs/config.py` for stronger Pydantic field/model validation and default config serialization helpers.
- Create: `src/scrobble_recs/admin/__init__.py` for the admin package marker.
- Create: `src/scrobble_recs/admin/init.py` for config skeleton writing and database bootstrap.
- Create: `src/scrobble_recs/admin/doctor.py` for typed diagnostics and cache summary queries.
- Create: `src/scrobble_recs/models/listening.py` for public local listening-score refresh used by both `recommend` and `build-profile`.
- Modify: `src/scrobble_recs/models/recommend.py` to call the public listening refresh helper instead of a private duplicate.
- Modify: `src/scrobble_recs/apis/cache.py` to add stale-cache reads and cache statistics.
- Modify: `src/scrobble_recs/apis/lastfm.py` to retry transient failures and return stale successful cache entries when available.
- Modify: `src/scrobble_recs/cli.py` to add `init`, stronger `doctor`, `build-profile`, and `resolve`.
- Test: `tests/test_config.py` for validation and skeleton defaults.
- Test: `tests/test_cli.py` for new command behavior and doctor exit codes.
- Test: `tests/test_admin_doctor.py` for diagnostics without Typer formatting.
- Test: `tests/test_api_cache.py` for stale reads and summary counts.
- Test: `tests/test_lastfm_client.py` for transient retry/stale fallback behavior with mocked HTTP.
- Test: `tests/test_profile.py` or new `tests/test_listening.py` for local profile refresh behavior.
- Modify: `README.md` with init, doctor, build-profile, and API/cache behavior.
- Modify: `docs/implementation-log.md` with verification notes.

---

### Task 1: Stronger Config Validation And Skeleton Defaults

**Files:**
- Modify: `src/scrobble_recs/config.py`
- Test: `tests/test_config.py`

- [x] **Step 1: Add failing config validation tests**

Append these tests to `tests/test_config.py`:

```python
from pydantic import ValidationError

from scrobble_recs.config import default_config_document


def test_load_config_rejects_invalid_rate_limit_and_ttl(tmp_path: Path) -> None:
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
                    "csv_columns": list(EXPECTED_CSV_COLUMNS),
                },
                "apis": {
                    "lastfm": {
                        "rate_limit_per_second": 0,
                        "cache_ttl_days": -1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)

    message = str(exc_info.value)
    assert "rate_limit_per_second" in message
    assert "cache_ttl_days" in message


def test_load_config_rejects_wrong_csv_contract(tmp_path: Path) -> None:
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
                    "timestamp_column": "played_at",
                    "csv_columns": ["artist", "track"],
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)

    message = str(exc_info.value)
    assert "timestamp_column" in message
    assert "csv_columns" in message


def test_default_config_document_contains_spec_defaults() -> None:
    document = default_config_document()

    assert document["app"]["name"] == "scrobble-recs"
    assert document["apis"]["lastfm"]["api_key"] == "${LASTFM_API_KEY}"
    assert document["apis"]["lastfm"]["rate_limit_per_second"] == 2.0
    assert document["apis"]["musicbrainz"]["rate_limit_per_second"] == 1.0
    assert document["recommendation"]["seed_artist_limit"] == 250
    assert document["recommendation"]["similar_artists_per_seed"] == 100
    assert document["recommendation"]["obviousness"]["top_n_per_tag_hide"] == 3
```

Also add `EXPECTED_CSV_COLUMNS` to the existing import line:

```python
from scrobble_recs.config import DiversityConfig, EXPECTED_CSV_COLUMNS, load_config
```

- [x] **Step 2: Run the config tests to verify they fail**

Run:

```bash
python -m pytest tests/test_config.py::test_load_config_rejects_invalid_rate_limit_and_ttl tests/test_config.py::test_load_config_rejects_wrong_csv_contract tests/test_config.py::test_default_config_document_contains_spec_defaults -v
```

Expected:

```text
ImportError: cannot import name 'default_config_document'
```

- [x] **Step 3: Add config validators and skeleton document**

Modify `src/scrobble_recs/config.py`.

Add these imports:

```python
from math import isfinite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
```

Add this helper near `_expand_path`:

```python
def _positive_finite(value: float, *, field_name: str) -> float:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return value
```

Add these validators to `InputConfig`:

```python
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
```

Add these validators to `LastFmApiConfig`:

```python
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
```

Add equivalent rate/TTL validation to `MusicBrainzApiConfig`:

```python
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
```

Add this method inside the existing `ObviousnessConfig` class:

```python
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
```

```python
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
```

Add this public helper at the bottom of the file:

```python
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
```

- [x] **Step 4: Run the config tests to verify they pass**

Run:

```bash
python -m pytest tests/test_config.py -v
```

Expected:

```text
passed
```

- [x] **Step 5: Commit config validation**

Run:

```bash
git add src/scrobble_recs/config.py tests/test_config.py
git commit -m "feat: harden config validation"
```

Checkpoint:

- Implementer status: DONE.
- Verification: focused red test failed before implementation due missing `default_config_document`; `python -m pytest tests/test_config.py -v` passed with 8 tests.
- Spec review: APPROVED.
- Code quality review: APPROVED.
- Architecture gate: severity `none`, continue `yes`.
- Deviations: copied the approved plan into this isolated worktree as the checkpoint artifact because the source plan was untracked in the original workspace.

---

### Task 2: Init Command And Bootstrap Service

**Files:**
- Create: `src/scrobble_recs/admin/__init__.py`
- Create: `src/scrobble_recs/admin/init.py`
- Modify: `src/scrobble_recs/cli.py`
- Modify: `tests/test_cli.py`

- [x] **Step 1: Add failing init CLI tests**

Append these tests to `tests/test_cli.py`:

```python
def test_init_command_writes_config_and_initializes_database(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "nested" / "config.json"

    result = runner.invoke(app, ["init", "--config", str(config_path)])

    assert result.exit_code == 0
    assert config_path.exists()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["apis"]["lastfm"]["api_key"] == "${LASTFM_API_KEY}"
    assert payload["recommendation"]["seed_artist_limit"] == 250
    db_path = tmp_path / "nested" / "data" / "scrobbles.duckdb"
    assert db_path.exists()
    assert f"Config: {config_path}" in result.output
    assert f"Database: {db_path}" in result.output


def test_init_command_refuses_to_overwrite_existing_config(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    result = runner.invoke(app, ["init", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "already exists" in result.output
    assert config_path.read_text(encoding="utf-8") == "{}"


def test_init_command_force_overwrites_existing_config(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    result = runner.invoke(app, ["init", "--config", str(config_path), "--force"])

    assert result.exit_code == 0
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["app"]["name"] == "scrobble-recs"
```

- [x] **Step 2: Run the init tests to verify they fail**

Run:

```bash
python -m pytest tests/test_cli.py::test_init_command_writes_config_and_initializes_database tests/test_cli.py::test_init_command_refuses_to_overwrite_existing_config tests/test_cli.py::test_init_command_force_overwrites_existing_config -v
```

Expected:

```text
No such command 'init'
```

- [x] **Step 3: Add the init service**

Create `src/scrobble_recs/admin/__init__.py`:

```python
"""Local administrative helpers for CLI commands."""
```

Create `src/scrobble_recs/admin/init.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scrobble_recs.config import default_config_document, load_config
from scrobble_recs.db import initialize_database


@dataclass(frozen=True)
class InitResult:
    config_path: Path
    db_path: Path
    cache_dir: Path
    log_path: Path


def initialize_project(config_path: Path, *, force: bool = False) -> InitResult:
    resolved_config_path = config_path.expanduser()
    if resolved_config_path.exists() and not force:
        raise FileExistsError(f"config already exists at {resolved_config_path}")

    document = _config_document_for_location(resolved_config_path)
    resolved_config_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_config_path.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )

    config = load_config(resolved_config_path)
    config.app.cache_dir.mkdir(parents=True, exist_ok=True)
    config.app.log_path.parent.mkdir(parents=True, exist_ok=True)
    conn = initialize_database(config)
    conn.close()
    return InitResult(
        config_path=resolved_config_path,
        db_path=config.app.db_path,
        cache_dir=config.app.cache_dir,
        log_path=config.app.log_path,
    )


def _config_document_for_location(config_path: Path) -> dict[str, Any]:
    document = default_config_document()
    document["app"]["db_path"] = str(config_path.parent / "data" / "scrobbles.duckdb")
    document["app"]["cache_dir"] = str(config_path.parent / "data" / "cache")
    document["app"]["log_path"] = str(config_path.parent / "data" / "app.log")
    return document
```

- [x] **Step 4: Add the init Typer command**

Modify `src/scrobble_recs/cli.py`.

Add this import:

```python
from scrobble_recs.admin.init import initialize_project
```

Add this command before `import_command`:

```python
@app.command("init")
def init_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing config file."),
    ] = False,
) -> None:
    try:
        result = initialize_project(config, force=force)
    except FileExistsError as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"Config: {result.config_path}")
    console.print(f"Database: {result.db_path}")
    console.print(f"Cache: {result.cache_dir}")
    console.print(f"Log: {result.log_path}")
```

- [x] **Step 5: Run the init tests to verify they pass**

Run:

```bash
python -m pytest tests/test_cli.py::test_init_command_writes_config_and_initializes_database tests/test_cli.py::test_init_command_refuses_to_overwrite_existing_config tests/test_cli.py::test_init_command_force_overwrites_existing_config -v
```

Expected:

```text
3 passed
```

- [x] **Step 6: Commit init work**

Run:

```bash
git add src/scrobble_recs/admin src/scrobble_recs/cli.py tests/test_cli.py
git commit -m "feat: add init command"
```

Checkpoint:

- Implementer status: DONE.
- Verification: focused red tests failed before implementation with missing-command exit code 2; focused init tests passed with 3 tests; `python -m pytest tests/test_cli.py -v` passed with 21 tests.
- Spec review: APPROVED.
- Code quality review: APPROVED.
- Architecture gate: severity `mechanical`, continue `yes`.
- Deviations: `init` success output uses `soft_wrap=True`; gate classified this as harmless CLI presentation detail.

---

### Task 3: API Cache Stale Reads And Statistics

**Files:**
- Modify: `src/scrobble_recs/apis/cache.py`
- Modify: `tests/test_api_cache.py`

- [x] **Step 1: Add failing cache stale/stat tests**

Append these tests to `tests/test_api_cache.py`:

```python
def test_api_cache_returns_latest_stale_success_payload(tmp_path: Path) -> None:
    config = load_config(_write_minimal_config(tmp_path))
    conn = initialize_database(config)
    cache = ApiCache(conn)
    now = datetime(2026, 6, 3, 12, 0, 0)
    params = {"method": "artist.getSimilar", "artist": "The Cure", "limit": 2}

    cache.write_response(
        source="lastfm",
        endpoint="artist.getSimilar",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params=params,
        response_status=200,
        response_json={"similarartists": {"artist": [{"name": "Old"}]}},
        fetched_at=now - timedelta(days=200),
        expires_at=now - timedelta(days=20),
        error_message=None,
    )

    assert cache.get_fresh("lastfm", "artist.getSimilar", params, now) is None
    stale = cache.get_stale_success("lastfm", "artist.getSimilar", params)

    assert stale == {"similarartists": {"artist": [{"name": "Old"}]}}
    conn.close()


def test_api_cache_summary_counts_fresh_stale_and_error_rows(tmp_path: Path) -> None:
    config = load_config(_write_minimal_config(tmp_path))
    conn = initialize_database(config)
    cache = ApiCache(conn)
    now = datetime(2026, 6, 3, 12, 0, 0)

    cache.write_response(
        source="lastfm",
        endpoint="artist.getSimilar",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params={"method": "artist.getSimilar", "artist": "A"},
        response_status=200,
        response_json={"ok": True},
        fetched_at=now,
        expires_at=now + timedelta(days=1),
        error_message=None,
    )
    cache.write_response(
        source="lastfm",
        endpoint="artist.getTopTags",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params={"method": "artist.getTopTags", "artist": "B"},
        response_status=200,
        response_json={"ok": True},
        fetched_at=now - timedelta(days=3),
        expires_at=now - timedelta(days=1),
        error_message=None,
    )
    cache.write_response(
        source="lastfm",
        endpoint="tag.getTopArtists",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params={"method": "tag.getTopArtists", "tag": "goth"},
        response_status=500,
        response_json={"message": "failed"},
        fetched_at=now,
        expires_at=now + timedelta(days=1),
        error_message="failed",
    )

    summary = cache.summary(now)

    assert summary.total_entries == 3
    assert summary.fresh_successes == 1
    assert summary.stale_successes == 1
    assert summary.error_entries == 1
    assert summary.sources == {"lastfm": 3}
    conn.close()
```

- [x] **Step 2: Run cache tests to verify they fail**

Run:

```bash
python -m pytest tests/test_api_cache.py::test_api_cache_returns_latest_stale_success_payload tests/test_api_cache.py::test_api_cache_summary_counts_fresh_stale_and_error_rows -v
```

Expected:

```text
AttributeError: 'ApiCache' object has no attribute 'get_stale_success'
```

- [x] **Step 3: Implement stale reads and summary stats**

Modify `src/scrobble_recs/apis/cache.py`.

Add this dataclass after `ApiCacheEntry`:

```python
@dataclass(frozen=True)
class ApiCacheSummary:
    total_entries: int
    fresh_successes: int
    stale_successes: int
    error_entries: int
    sources: dict[str, int]
```

Add these methods to `ApiCache`:

```python
    def get_stale_success(
        self,
        source: str,
        endpoint: str,
        request_params: JsonObject,
    ) -> JsonObject | None:
        cache_key = build_cache_key(source, endpoint, request_params)
        row = self._conn.execute(
            """
            SELECT response_json
            FROM api_cache
            WHERE cache_key = ?
              AND response_status BETWEEN 200 AND 299
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            [cache_key],
        ).fetchone()
        if row is None:
            return None
        return cast(JsonObject, json.loads(str(row[0])))

    def summary(self, now: datetime) -> ApiCacheSummary:
        total_entries = _query_int(self._conn, "SELECT count(*) FROM api_cache", [])
        fresh_successes = _query_int(
            self._conn,
            """
            SELECT count(*)
            FROM api_cache
            WHERE response_status BETWEEN 200 AND 299
              AND expires_at > ?
            """,
            [now],
        )
        stale_successes = _query_int(
            self._conn,
            """
            SELECT count(*)
            FROM api_cache
            WHERE response_status BETWEEN 200 AND 299
              AND expires_at <= ?
            """,
            [now],
        )
        error_entries = _query_int(
            self._conn,
            """
            SELECT count(*)
            FROM api_cache
            WHERE response_status < 200 OR response_status > 299
            """,
            [],
        )
        sources = {
            str(source): int(count)
            for source, count in self._conn.execute(
                """
                SELECT source, count(*)
                FROM api_cache
                GROUP BY source
                ORDER BY source
                """
            ).fetchall()
        }
        return ApiCacheSummary(
            total_entries=total_entries,
            fresh_successes=fresh_successes,
            stale_successes=stale_successes,
            error_entries=error_entries,
            sources=sources,
        )
```

Add this helper near the bottom:

```python
def _query_int(conn: DuckDBPyConnection, sql: str, params: list[object]) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)
```

- [x] **Step 4: Run cache tests to verify they pass**

Run:

```bash
python -m pytest tests/test_api_cache.py -v
```

Expected:

```text
passed
```

- [x] **Step 5: Commit cache diagnostics primitives**

Run:

```bash
git add src/scrobble_recs/apis/cache.py tests/test_api_cache.py
git commit -m "feat: expose api cache diagnostics"
```

Checkpoint:

- Implementer status: DONE.
- Verification: focused red tests failed before implementation with missing `ApiCache` methods; `python -m pytest tests/test_api_cache.py -v` passed with 9 tests; changed-file ruff check passed.
- Spec review: APPROVED.
- Code quality review: APPROVED.
- Architecture gate: severity `none`, continue `yes`.
- Deviations: none.

---

### Task 4: Last.fm Retry And Stale-Cache Fallback

**Files:**
- Modify: `src/scrobble_recs/apis/lastfm.py`
- Modify: `tests/test_lastfm_client.py`

- [x] **Step 1: Add failing Last.fm retry/stale tests**

Append these tests to `tests/test_lastfm_client.py`:

```python
def test_lastfm_client_returns_stale_cache_after_transient_5xx(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"message": "temporary failure"})

    conn = initialize_database(load_config(_config_path(tmp_path)))
    cache = ApiCache(conn)
    now = datetime(2026, 6, 3, 12, 0, 0)
    cache.write_response(
        source="lastfm",
        endpoint="artist.getSimilar",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params={"method": "artist.getSimilar", "artist": "The Cure", "limit": 1},
        response_status=200,
        response_json={
            "similarartists": {
                "artist": [{"name": "Bauhaus", "mbid": "", "match": "0.72"}]
            }
        },
        fetched_at=now.replace(year=2025),
        expires_at=now.replace(year=2025, month=12),
        error_message=None,
    )
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key", max_retries=1),
        cache=cache,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        now=lambda: now,
    )

    artists = client.get_similar_artists("The Cure", limit=1)

    assert [artist.name for artist in artists] == ["Bauhaus"]
    assert len(calls) == 2
    conn.close()


def test_lastfm_client_does_not_retry_4xx_errors(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403, json={"message": "bad key"})

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key", max_retries=2),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    with pytest.raises(RuntimeError, match="bad key"):
        client.get_similar_artists("The Cure", limit=1)

    assert len(calls) == 1
    conn.close()
```

- [x] **Step 2: Run Last.fm tests to verify they fail**

Run:

```bash
python -m pytest tests/test_lastfm_client.py::test_lastfm_client_returns_stale_cache_after_transient_5xx tests/test_lastfm_client.py::test_lastfm_client_does_not_retry_4xx_errors -v
```

Expected:

```text
TypeError: LastFmApiConfig.__init__() got an unexpected keyword argument 'max_retries'
```

- [x] **Step 3: Implement retry/stale behavior**

Modify `src/scrobble_recs/apis/lastfm.py`.

Replace the HTTP part of `_request` after the fresh-cache check with retry
handling that:

- Attempts each uncached Last.fm request `max_retries + 1` times.
- Caches successful 2xx responses.
- Writes non-transient 4xx error responses and raises without retry.
- Retries transient 429 and 5xx responses.
- Checks for a stale successful cache entry after exhausted transient responses
  before writing any final transient failure row, because `api_cache.cache_key`
  replacement would otherwise erase the stale success needed for fallback.
- Treats `httpx.TransportError` as a transient failure with retry and stale
  fallback. Transport errors have no HTTP response to cache, so they raise after
  retries only when no stale success exists.

The implementation shape is:

```python
        request_params = {
            "method": method,
            "api_key": self._config.api_key,
            "format": "json",
            **params,
        }
        request_url = self._config.base_url
        attempts = self._config.max_retries + 1
        last_status: int | None = None
        last_payload: JsonObject | None = None
        last_transport_error: str | None = None

        for attempt in range(attempts):
            self._rate_limiter.wait()
            try:
                response = self._http_client.get(request_url, params=request_params)
            except httpx.TransportError as exc:
                last_transport_error = str(exc).strip()
                if attempt < attempts - 1:
                    retry_sleep(2.0**attempt)
                    continue
                break

            if 200 <= response.status_code <= 299:
                response_json = _response_json(response)
                write_success_response(response_json)
                return response_json

            response_json = _error_response_json(response)
            last_status = response.status_code
            last_payload = response_json
            if response.status_code < 500 and response.status_code != 429:
                write_error_response(response.status_code, response_json)
                raise RuntimeError(error_message(response.status_code, response_json))
            if attempt < attempts - 1 and response.status_code == 429:
                retry_sleep(2.0**attempt)

        stale = self._cache.get_stale_success(_SOURCE, method, cache_params)
        if stale is not None:
            return stale
        if last_status is None or last_payload is None:
            raise RuntimeError(last_transport_error or "Last.fm request failed before receiving a response")
        write_error_response(last_status, last_payload)
        raise RuntimeError(error_message(last_status, last_payload))
```

- [x] **Step 4: Run Last.fm tests to verify they pass**

Run:

```bash
python -m pytest tests/test_lastfm_client.py -v
```

Expected:

```text
passed
```

- [x] **Step 5: Commit API retry work**

Run:

```bash
git add src/scrobble_recs/apis/lastfm.py tests/test_lastfm_client.py
git commit -m "feat: use stale cache for transient lastfm failures"
```

Checkpoint:

- Implementer status: DONE_WITH_APPROVED_DEVIATIONS.
- Verification: focused stale fallback and 4xx tests were exercised red/green;
  additional regression tests covered non-JSON 503 responses, transport-error
  fallback, and 429 retry backoff. `python -m pytest tests/test_lastfm_client.py
  -v` passed with 14 tests. `python -m ruff check
  src\scrobble_recs\apis\lastfm.py tests\test_lastfm_client.py` passed.
- Spec review: APPROVED after Option A; no gaps or unapproved extra behavior.
- Code quality review: APPROVED after Option A; no blocking/material findings.
- Architecture gate: severity `none`, continue `yes`.
- Deviations: approved by human decision Option A on 2026-06-05. Task 4
  preserves stale successful cache rows before writing final transient HTTP
  failures, because replacing the cache row first would erase fallback data.
  Task 4 also treats `httpx.TransportError` as transient with retry and stale
  fallback, with no cache write when no HTTP response exists.

---

### Task 5: Public Local Profile Refresh And Build-Profile Command

**Files:**
- Create: `src/scrobble_recs/models/listening.py`
- Create: `src/scrobble_recs/models/profile_builder.py`
- Create: `src/scrobble_recs/sql/005_profile.sql`
- Modify: `src/scrobble_recs/models/recommend.py`
- Modify: `src/scrobble_recs/cli.py`
- Test: `tests/test_profile.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_config.py`

- [x] **Step 1: Add failing local profile refresh tests**

Append this test to `tests/test_profile.py`:

```python
def test_refresh_artist_listening_scores_updates_weights_for_build_profile(tmp_path: Path) -> None:
    from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
    from scrobble_recs.models.listening import refresh_artist_listening_scores

    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    try:
        import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
        stats = refresh_artist_listening_scores(conn, as_of=datetime(2026, 6, 5, 12, 0, 0))

        assert stats.artists_updated == 4
        missing_scores = conn.execute(
            """
            SELECT count(*)
            FROM artist_listening_fact
            WHERE lifetime_weight IS NULL
               OR recent_weight IS NULL
               OR knownness IS NULL
               OR rediscovery_score IS NULL
            """
        ).fetchone()
        assert missing_scores == (0,)
    finally:
        conn.close()
```

If `tests/test_profile.py` does not already import these names, add:

```python
from datetime import datetime
from pathlib import Path
```

- [x] **Step 2: Add failing build-profile CLI test**

Append this test to `tests/test_cli.py`:

```python
def test_build_profile_command_refreshes_local_scores(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    runner.invoke(app, ["import", "--config", str(config_path)])

    result = runner.invoke(app, ["build-profile", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Profile refreshed" in result.output
    assert "artists updated: 4" in result.output
```

- [x] **Step 3: Run profile tests to verify they fail**

Run:

```bash
python -m pytest tests/test_profile.py::test_refresh_artist_listening_scores_updates_weights_for_build_profile tests/test_cli.py::test_build_profile_command_refreshes_local_scores -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.models.listening'
```

- [x] **Step 4: Create the public listening refresh module**

Create `src/scrobble_recs/models/listening.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from duckdb import DuckDBPyConnection

from scrobble_recs.models.knownness import ArtistListeningInput, compute_artist_listening_scores


@dataclass(frozen=True)
class ListeningRefreshStats:
    artists_updated: int


def refresh_artist_listening_scores(
    conn: DuckDBPyConnection,
    *,
    as_of: datetime,
) -> ListeningRefreshStats:
    rows = conn.execute(
        """
        SELECT
            artist_id,
            play_count,
            track_count,
            active_months,
            first_played_at,
            last_played_at
        FROM artist_listening_fact
        WHERE play_count IS NOT NULL
          AND track_count IS NOT NULL
          AND active_months IS NOT NULL
          AND first_played_at IS NOT NULL
          AND last_played_at IS NOT NULL
        """
    ).fetchall()

    updated = 0
    for row in rows:
        scores = compute_artist_listening_scores(
            ArtistListeningInput(
                play_count=int(row[1]),
                track_count=int(row[2]),
                active_months=int(row[3]),
                first_played_at=row[4],
                last_played_at=row[5],
            ),
            as_of=as_of,
        )
        conn.execute(
            """
            UPDATE artist_listening_fact
            SET lifetime_weight = ?,
                recent_weight = ?,
                knownness = ?,
                rediscovery_score = ?
            WHERE artist_id = ?
            """,
            [
                scores.lifetime_weight,
                scores.recent_weight,
                scores.knownness,
                scores.rediscovery_score,
                int(row[0]),
            ],
        )
        updated += 1
    return ListeningRefreshStats(artists_updated=updated)
```

- [x] **Step 5: Reuse the public refresh helper in recommendations**

Modify `src/scrobble_recs/models/recommend.py`.

Add this import:

```python
from scrobble_recs.models.listening import refresh_artist_listening_scores
```

Replace:

```python
    _refresh_artist_listening_scores(conn, resolved_as_of)
```

with:

```python
    refresh_artist_listening_scores(conn, as_of=resolved_as_of)
```

Delete the private `_refresh_artist_listening_scores` function and remove now-unused imports:

```python
from scrobble_recs.models.knownness import ArtistListeningInput, compute_artist_listening_scores
```

- [x] **Step 6: Add the build-profile CLI command**

Modify `src/scrobble_recs/cli.py`.

Add this import:

```python
from datetime import datetime
from scrobble_recs.models.listening import refresh_artist_listening_scores
```

Add this command after `import_command`:

```python
@app.command("build-profile")
def build_profile_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
) -> None:
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        stats = refresh_artist_listening_scores(conn, as_of=datetime.utcnow())
    finally:
        conn.close()

    console.print("Profile refreshed")
    console.print(f"artists updated: {stats.artists_updated}")
```

- [x] **Step 7: Run profile and recommendation tests**

Run:

```bash
python -m pytest tests/test_profile.py tests/test_recommend.py tests/test_cli.py::test_build_profile_command_refreshes_local_scores -v
```

Expected:

```text
passed
```

- [x] **Step 8: Expand build-profile to full local profile behavior**

Human decision: Option B, require full build-profile behavior now rather than
committing the narrow listening-score-only implementation.

Implemented:

- Added `tag_profile` and `profile_cluster` persistence in
  `src/scrobble_recs/sql/005_profile.sql`.
- Added `src/scrobble_recs/models/profile_builder.py` as the public local
  profile builder.
- `build_profile()` refreshes artist listening scores, selects local seed
  artists using configured seed limits and min scrobbles, derives normalized
  tag weights from local `artist_tag` rows for selected seeds, and persists
  deterministic tag-based genre clusters with inspectable JSON payloads.
- Review fix: `profile_cluster.cluster_key` now uses
  `source:readable-slug:stable-tag-hash` so distinct tags that slugify to the
  same value still persist as separate clusters.
- `scrobble-recs build-profile --config ...` now prints artist, tag weight,
  and genre cluster counts.

- [x] **Step 9: Parent gate review / commit decision**

Task 5 checkpoint notes:

- Human decision on resume: Option A approved on 2026-06-05.
  `profile_cluster` is approved as the durable storage contract for §7 genre
  clusters, including the columns and indexes in `005_profile.sql`,
  inspectable JSON evidence payloads, deterministic one-tag-per-cluster
  baseline behavior, and collision-safe `source:readable-slug:stable-tag-hash`
  keys.
- Deviation: `build-profile` uses `datetime.now(tz=UTC).replace(tzinfo=None)`
  instead of `datetime.utcnow()` to keep naive UTC behavior without Python 3.12
  deprecation warnings.
- Deviation: `refresh_artist_listening_scores()` also updates
  `artist_listening_fact.updated_at = current_timestamp`, preserving the
  previous recommendation refresh behavior that the plan's extracted helper
  omitted.
- Verified red:
  `python -m pytest tests/test_profile.py::test_refresh_artist_listening_scores_updates_weights_for_build_profile tests/test_cli.py::test_build_profile_command_refreshes_local_scores -v`
  failed before implementation with missing `scrobble_recs.models.listening`.
- Verified green:
  `python -m pytest tests/test_profile.py tests/test_recommend.py tests/test_cli.py::test_build_profile_command_refreshes_local_scores -v`
  passed with 38 tests.
- Option B red verification:
  `python -m pytest tests/test_profile.py::test_build_profile_persists_tag_profile_and_genre_clusters tests/test_cli.py::test_build_profile_command_prints_profile_weight_counts -v`
  failed before implementation with missing `scrobble_recs.models.profile_builder`
  and missing CLI tag/cluster counts.
- Option B green verification:
  `python -m pytest tests/test_profile.py tests/test_recommend.py tests/test_cli.py::test_build_profile_command_refreshes_local_scores tests/test_cli.py::test_build_profile_command_prints_profile_weight_counts -v`
  passed with 40 tests.
- Review fix red verification:
  `python -m pytest tests/test_profile.py::test_build_profile_persists_clusters_for_slug_colliding_tags -v`
  failed before the fix with a duplicate `profile_cluster` key
  `lastfm:post-punk`.
- Review fix green verification:
  `python -m pytest tests/test_profile.py tests/test_recommend.py tests/test_cli.py::test_build_profile_command_refreshes_local_scores tests/test_cli.py::test_build_profile_command_prints_profile_weight_counts tests/test_config.py::test_initialize_database_applies_initial_migration -v`
  passed with 42 tests.
- Spec review: APPROVED after the collision fix.
- Code quality review: APPROVED.
- Architecture gate verdict after resume: `severity: local-design`,
  `continue: yes`. The prior architectural stop condition was resolved by the
  recorded Option A approval.
- Previous architecture gate verdict: `severity: architectural`,
  `continue: no`. Execution paused before commit because `profile_cluster` was
  a new durable schema contract not explicitly specified in the approved schema.
- Required human decision: approve `profile_cluster` as the durable storage
  contract for §7 genre clusters, including deterministic one-tag-per-cluster
  baseline behavior and collision-safe keys, or require a different
  representation before commit. Decision applied: Option A approved.
- Commit status: gate permits Task 5 commit after Option A approval.

---

### Task 6: Doctor Diagnostic Backend

**Files:**
- Create: `src/scrobble_recs/admin/doctor.py`
- Test: `tests/test_admin_doctor.py`

- [ ] **Step 1: Add failing doctor backend tests**

Create `tests/test_admin_doctor.py`:

```python
import json
from pathlib import Path

from scrobble_recs.admin.doctor import DiagnosticStatus, run_doctor
from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database


def _write_config(tmp_path: Path, *, csv_path: str = "tests/fixtures/tiny_lastfm_export.csv") -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "scrobbles.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": csv_path,
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
    return config_path


def test_run_doctor_reports_ok_local_state(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    conn = initialize_database(config)
    try:
        result = run_doctor(config, conn)
    finally:
        conn.close()

    checks = {check.name: check for check in result.checks}
    assert result.has_errors is False
    assert checks["config"].status is DiagnosticStatus.OK
    assert checks["database"].status is DiagnosticStatus.OK
    assert checks["csv"].status is DiagnosticStatus.OK
    assert checks["cache"].status is DiagnosticStatus.OK
    assert checks["lastfm_api_key"].status is DiagnosticStatus.WARN
    assert checks["rate_limits"].status is DiagnosticStatus.OK


def test_run_doctor_reports_missing_csv_as_error(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, csv_path=str(tmp_path / "missing.csv")))
    conn = initialize_database(config)
    try:
        result = run_doctor(config, conn)
    finally:
        conn.close()

    checks = {check.name: check for check in result.checks}
    assert result.has_errors is True
    assert checks["csv"].status is DiagnosticStatus.ERROR
    assert "missing" in checks["csv"].message


def test_run_doctor_includes_api_cache_summary(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    conn = initialize_database(config)
    try:
        cache = ApiCache(conn)
        cache.write_response(
            source="lastfm",
            endpoint="artist.getSimilar",
            request_url="https://ws.audioscrobbler.com/2.0/",
            request_params={"method": "artist.getSimilar", "artist": "The Cure"},
            response_status=200,
            response_json={"ok": True},
            fetched_at=datetime(2026, 6, 3, 12, 0, 0),
            expires_at=datetime(2026, 6, 4, 12, 0, 0),
            error_message=None,
        )
        result = run_doctor(config, conn, now=datetime(2026, 6, 3, 12, 0, 0))
    finally:
        conn.close()

    checks = {check.name: check for check in result.checks}
    assert checks["api_cache"].status is DiagnosticStatus.OK
    assert "entries=1" in checks["api_cache"].message
    assert "fresh=1" in checks["api_cache"].message
```

Add this import at the top:

```python
from datetime import datetime
```

- [ ] **Step 2: Run doctor backend tests to verify they fail**

Run:

```bash
python -m pytest tests/test_admin_doctor.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.admin.doctor'
```

- [ ] **Step 3: Implement doctor diagnostics**

Create `src/scrobble_recs/admin/doctor.py`:

```python
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from duckdb import DuckDBPyConnection

from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.config import EXPECTED_CSV_COLUMNS, ScrobbleRecsConfig
from scrobble_recs.importers.lastfm_csv import get_import_stats


class DiagnosticStatus(Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    status: DiagnosticStatus
    message: str


@dataclass(frozen=True)
class DoctorResult:
    checks: tuple[DiagnosticCheck, ...]

    @property
    def has_errors(self) -> bool:
        return any(check.status is DiagnosticStatus.ERROR for check in self.checks)


def run_doctor(
    config: ScrobbleRecsConfig,
    conn: DuckDBPyConnection,
    *,
    now: datetime | None = None,
) -> DoctorResult:
    resolved_now = now or datetime.utcnow()
    checks = [
        _config_check(config),
        _database_check(config),
        _csv_check(config.input.lastfm_csv),
        _cache_path_check(config.app.cache_dir),
        _lastfm_api_key_check(config),
        _rate_limit_check(config),
        _api_cache_check(conn, resolved_now),
        _import_stats_check(conn),
    ]
    return DoctorResult(checks=tuple(checks))


def _config_check(config: ScrobbleRecsConfig) -> DiagnosticCheck:
    return DiagnosticCheck("config", DiagnosticStatus.OK, f"name={config.app.name}")


def _database_check(config: ScrobbleRecsConfig) -> DiagnosticCheck:
    db_path = config.app.db_path
    if db_path.exists():
        return DiagnosticCheck("database", DiagnosticStatus.OK, str(db_path))
    return DiagnosticCheck("database", DiagnosticStatus.WARN, f"will be created at {db_path}")


def _csv_check(csv_path: Path) -> DiagnosticCheck:
    if not csv_path.exists():
        return DiagnosticCheck("csv", DiagnosticStatus.ERROR, f"missing at {csv_path}")
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as file:
            reader = csv.reader(file)
            header = next(reader, [])
    except OSError as exc:
        return DiagnosticCheck("csv", DiagnosticStatus.ERROR, f"unreadable: {exc}")
    if header != list(EXPECTED_CSV_COLUMNS):
        return DiagnosticCheck(
            "csv",
            DiagnosticStatus.ERROR,
            f"columns must match {list(EXPECTED_CSV_COLUMNS)}",
        )
    return DiagnosticCheck("csv", DiagnosticStatus.OK, str(csv_path))


def _cache_path_check(cache_dir: Path) -> DiagnosticCheck:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return DiagnosticCheck("cache", DiagnosticStatus.ERROR, f"unwritable: {exc}")
    return DiagnosticCheck("cache", DiagnosticStatus.OK, str(cache_dir))


def _lastfm_api_key_check(config: ScrobbleRecsConfig) -> DiagnosticCheck:
    lastfm = config.apis.lastfm
    if not lastfm.enabled:
        return DiagnosticCheck("lastfm_api_key", DiagnosticStatus.OK, "Last.fm disabled")
    if lastfm.api_key is None:
        return DiagnosticCheck(
            "lastfm_api_key",
            DiagnosticStatus.WARN,
            "missing; import, doctor, TUI, and local recommendation browsing still work",
        )
    return DiagnosticCheck("lastfm_api_key", DiagnosticStatus.OK, "configured")


def _rate_limit_check(config: ScrobbleRecsConfig) -> DiagnosticCheck:
    return DiagnosticCheck(
        "rate_limits",
        DiagnosticStatus.OK,
        (
            f"lastfm={config.apis.lastfm.rate_limit_per_second}/s; "
            f"musicbrainz={config.apis.musicbrainz.rate_limit_per_second}/s"
        ),
    )


def _api_cache_check(conn: DuckDBPyConnection, now: datetime) -> DiagnosticCheck:
    summary = ApiCache(conn).summary(now)
    status = DiagnosticStatus.WARN if summary.error_entries else DiagnosticStatus.OK
    return DiagnosticCheck(
        "api_cache",
        status,
        (
            f"entries={summary.total_entries}; fresh={summary.fresh_successes}; "
            f"stale={summary.stale_successes}; errors={summary.error_entries}"
        ),
    )


def _import_stats_check(conn: DuckDBPyConnection) -> DiagnosticCheck:
    stats = get_import_stats(conn)
    if stats.scrobbles == 0:
        return DiagnosticCheck("import", DiagnosticStatus.WARN, "no imported scrobbles")
    return DiagnosticCheck(
        "import",
        DiagnosticStatus.OK,
        f"scrobbles={stats.scrobbles}; artists={stats.distinct_artists}; tracks={stats.distinct_tracks}",
    )
```

- [ ] **Step 4: Run doctor backend tests to verify they pass**

Run:

```bash
python -m pytest tests/test_admin_doctor.py -v
```

Expected:

```text
3 passed
```

- [ ] **Step 5: Commit doctor backend**

Run:

```bash
git add src/scrobble_recs/admin/doctor.py tests/test_admin_doctor.py
git commit -m "feat: add doctor diagnostics backend"
```

---

### Task 7: Stronger Doctor CLI Output And Exit Codes

**Files:**
- Modify: `src/scrobble_recs/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Update doctor CLI tests for diagnostics**

Replace `test_doctor_command_runs_without_api_keys` in `tests/test_cli.py` with:

```python
def test_doctor_command_runs_without_api_keys(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    runner.invoke(app, ["import", "--config", str(config_path)])

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "config: ok" in result.output
    assert "database: ok" in result.output
    assert "csv: ok" in result.output
    assert "cache: ok" in result.output
    assert "lastfm_api_key: warn" in result.output
    assert "api_cache: ok" in result.output
    assert "clean scrobbles: 6" in result.output
```

Append this test:

```python
def test_doctor_command_exits_nonzero_when_csv_is_missing(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["input"]["lastfm_csv"] = str(tmp_path / "missing.csv")
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "csv: error" in result.output
    assert "missing" in result.output
```

- [ ] **Step 2: Run doctor CLI tests to verify they fail**

Run:

```bash
python -m pytest tests/test_cli.py::test_doctor_command_runs_without_api_keys tests/test_cli.py::test_doctor_command_exits_nonzero_when_csv_is_missing -v
```

Expected:

```text
AssertionError because the current doctor output is shallow and missing diagnostic lines
```

- [ ] **Step 3: Wire doctor CLI to diagnostic backend**

Modify `src/scrobble_recs/cli.py`.

Add imports:

```python
from scrobble_recs.admin.doctor import DiagnosticCheck, run_doctor
```

Replace `doctor_command` with:

```python
@app.command("doctor")
def doctor_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
) -> None:
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        result = run_doctor(resolved_config, conn)
        stats = get_import_stats(conn)
    finally:
        conn.close()

    for check in result.checks:
        _print_diagnostic_check(check)
    _print_stats(stats)
    if result.has_errors:
        raise typer.Exit(code=1)
```

Add this helper near `_print_stats`:

```python
def _print_diagnostic_check(check: DiagnosticCheck) -> None:
    console.print(f"{check.name}: {check.status.value} - {check.message}", soft_wrap=True)
```

- [ ] **Step 4: Run CLI tests to verify they pass**

Run:

```bash
python -m pytest tests/test_cli.py -v
```

Expected:

```text
passed
```

- [ ] **Step 5: Commit doctor CLI hardening**

Run:

```bash
git add src/scrobble_recs/cli.py tests/test_cli.py
git commit -m "feat: harden doctor command"
```

---

### Task 8: Resolve Command Contract Boundary

**Files:**
- Modify: `src/scrobble_recs/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing resolve CLI tests**

Append these tests to `tests/test_cli.py`:

```python
def test_resolve_command_reports_musicbrainz_disabled_by_default(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)

    result = runner.invoke(app, ["resolve", "--config", str(config_path), "--source", "musicbrainz"])

    assert result.exit_code == 0
    assert "MusicBrainz resolution is disabled" in result.output
    assert "unresolved artists:" in result.output


def test_resolve_command_rejects_unknown_source(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)

    result = runner.invoke(app, ["resolve", "--config", str(config_path), "--source", "spotify"])

    assert result.exit_code != 0
    assert "only --source musicbrainz is supported" in result.output
```

- [ ] **Step 2: Run resolve CLI tests to verify they fail**

Run:

```bash
python -m pytest tests/test_cli.py::test_resolve_command_reports_musicbrainz_disabled_by_default tests/test_cli.py::test_resolve_command_rejects_unknown_source -v
```

Expected:

```text
No such command 'resolve'
```

- [ ] **Step 3: Add local resolve boundary command**

Modify `src/scrobble_recs/cli.py`.

Add this command after `enrich_command`:

```python
@app.command("resolve")
def resolve_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    source: Annotated[str, typer.Option("--source", help="Resolution source to run.")] = "musicbrainz",
) -> None:
    if source != "musicbrainz":
        raise typer.BadParameter("only --source musicbrainz is supported")

    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        unresolved_artists = _count_query(
            conn,
            """
            SELECT count(*)
            FROM artist
            WHERE resolution_status IS NULL
               OR resolution_status IN ('provisional', 'unresolved')
            """,
        )
    finally:
        conn.close()

    console.print(f"unresolved artists: {unresolved_artists}")
    if not resolved_config.apis.musicbrainz.enabled:
        console.print("MusicBrainz resolution is disabled")
        return
    console.print("MusicBrainz resolution is not implemented in this version")
    raise typer.Exit(code=1)
```

Add this helper:

```python
def _count_query(conn: DuckDBPyConnection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)
```

- [ ] **Step 4: Run resolve tests to verify they pass**

Run:

```bash
python -m pytest tests/test_cli.py::test_resolve_command_reports_musicbrainz_disabled_by_default tests/test_cli.py::test_resolve_command_rejects_unknown_source -v
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Commit resolve command boundary**

Run:

```bash
git add src/scrobble_recs/cli.py tests/test_cli.py
git commit -m "feat: add resolve command boundary"
```

---

### Task 9: Documentation, Implementation Log, And Final Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/implementation-log.md`

- [ ] **Step 1: Run the full test suite**

Run:

```bash
python -m pytest
```

Expected:

```text
passed
```

- [ ] **Step 2: Run ruff check**

Run:

```bash
python -m ruff check .
```

Expected:

```text
All checks passed
```

- [ ] **Step 3: Run ruff format check**

Run:

```bash
python -m ruff format --check .
```

Expected:

```text
already formatted
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

- [ ] **Step 5: Verify admin commands through the CLI**

Run:

```bash
python -m scrobble_recs init --config ./.tmp-admin-hardening/config.json --force
python -m scrobble_recs doctor --config ./.tmp-admin-hardening/config.json
python -m scrobble_recs build-profile --config config.example.json
python -m scrobble_recs resolve --config config.example.json --source musicbrainz
```

Expected:

```text
init prints Config, Database, Cache, and Log paths
doctor prints diagnostic lines and exits nonzero only for true errors
build-profile prints Profile refreshed
resolve prints unresolved artists and disabled/not-implemented status
```

- [ ] **Step 6: Update README**

Update `README.md` so the "How To Use It" section includes:

```markdown
Create a local config and DuckDB database:

```bash
scrobble-recs init --config ./config.json
```

`init` writes a JSON config skeleton with `${LASTFM_API_KEY}` interpolation,
creates parent directories, and applies DuckDB migrations. It refuses to
overwrite an existing config unless `--force` is passed.

Run admin diagnostics:

```bash
scrobble-recs doctor --config config.json
```

`doctor` validates the config, database path, CSV header, cache path, API key
state, rate-limit settings, import counts, and API cache state. Missing Last.fm
keys are warnings because import, local browsing, and persisted recommendation
runs do not require live APIs.

Refresh local listening scores without generating recommendations:

```bash
scrobble-recs build-profile --config config.json
```

MusicBrainz resolution is intentionally separate from local profile and
recommendation work:

```bash
scrobble-recs resolve --config config.json --source musicbrainz
```
```

Also update the Last.fm enrichment section with:

```markdown
When Last.fm returns a transient 429 or 5xx response, the client retries up to
the configured maximum and falls back to a stale successful cache entry when one
exists. Tests cover this behavior with mocked HTTP; they do not call Last.fm.
```

- [ ] **Step 7: Update implementation log**

Append this entry to `docs/implementation-log.md`:

```markdown
## 2026-06-05: CLI/Admin hardening

Changed:

- Added stricter config validation for CSV contract fields, API rate limits,
  cache TTLs, retry counts, recommendation thresholds, and sensible defaults.
- Added `scrobble-recs init` to write a config skeleton and initialize the local
  DuckDB file.
- Added typed doctor diagnostics for config, database, CSV schema, cache path,
  Last.fm API key state, rate limits, API cache state, and import counts.
- Hardened `scrobble-recs doctor` output and exit codes.
- Added API cache stale-success reads and summary counts.
- Added Last.fm retry behavior with stale-cache fallback for transient 429/5xx
  failures.
- Added `scrobble-recs build-profile` for local listening-score refresh.
- Added the `resolve --source musicbrainz` CLI boundary for the later
  MusicBrainz identity-resolution milestone.

Verified:

- `python -m pytest`
- `python -m ruff check .`
- `python -m ruff format --check .`
- `python -m mypy src`
- `python -m scrobble_recs init --config ./.tmp-admin-hardening/config.json --force`
- `python -m scrobble_recs doctor --config ./.tmp-admin-hardening/config.json`
- `python -m scrobble_recs build-profile --config config.example.json`
- `python -m scrobble_recs resolve --config config.example.json --source musicbrainz`
```

- [ ] **Step 8: Commit docs and verification**

Run:

```bash
git add README.md docs/implementation-log.md
git commit -m "docs: record cli admin hardening"
```

---

## Self Review

Spec coverage:

- §6 configuration source, environment interpolation, and sensible config defaults are covered by Tasks 1 and 2.
- §7 `init`, `doctor`, `build-profile`, and `resolve` command gaps are covered by Tasks 2, 5, 7, and 8.
- §18 cache-first behavior, stale-cache fallback, retry behavior, and cache diagnostics are covered by Tasks 3, 4, and 6.
- §23 sensible defaults are covered by `default_config_document()` and config validation tests in Task 1.
- Local-first/test constraints are preserved: all new tests use temp files, DuckDB temp DBs, or mocked HTTP.

Intentional boundary:

- `resolve --source musicbrainz` is made explicit but does not implement full MusicBrainz resolution in this hardening slice. That belongs to the separate Better Identity Resolution milestone because it requires API client behavior, confidence modeling, merge rules, and review workflows.

Placeholder scan:

- No steps contain "TBD", "TODO", "add appropriate", or open-ended implementation instructions.
- Every behavior-changing task includes a failing behavioral test, command to observe the failure, concrete implementation content, and pass verification.

Type consistency:

- `LastFmApiConfig.max_retries` is introduced before `LastFmClient` reads it.
- `ApiCache.get_stale_success()` and `ApiCache.summary()` are introduced before doctor and Last.fm client use them.
- `ListeningRefreshStats.artists_updated` matches `build-profile` CLI output and tests.
- `DiagnosticCheck`, `DiagnosticStatus`, and `DoctorResult` names match CLI imports and tests.
