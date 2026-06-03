# Import And Inspect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first local-only vertical slice: project scaffold, config loading, DuckDB schema, Last.fm CSV import, normalization, duplicate marking, artist listening facts, and CLI inspection.

**Architecture:** The app is a Python package under `src/scrobble_recs` with Typer commands at the boundary, DuckDB as the local state store, and small import/normalization modules that can be tested without network access. The importer validates the exact Last.fm CSV schema, preserves raw rows, writes normalized scrobble rows, marks duplicates instead of deleting them, creates provisional local artist entities, and computes import statistics from persisted tables.

**Tech Stack:** Python 3.11+, DuckDB, Typer, Pydantic v2, Rich, pytest, ruff, mypy.

---

## Scope

This plan covers Milestone 1 from `docs/spec.md`: import and inspect. Last.fm enrichment, recommendation scoring, TUI feedback, MusicBrainz resolution, and ListenBrainz modeling are separate subsystems and should each get their own implementation plan.

## File Structure

- Create: `pyproject.toml` for packaging, dependencies, console script, pytest, ruff, and mypy configuration.
- Create: `README.md` with Milestone 1 commands and local-first constraints.
- Create: `config.example.json` with a runnable local config that does not require API keys.
- Create: `src/scrobble_recs/__init__.py` for package metadata.
- Create: `src/scrobble_recs/cli.py` for `import` and `doctor` Typer commands.
- Create: `src/scrobble_recs/config.py` for JSON config loading, path expansion, and environment-variable interpolation.
- Create: `src/scrobble_recs/db.py` for DuckDB connection and SQL migration application.
- Create: `src/scrobble_recs/importers/__init__.py`.
- Create: `src/scrobble_recs/importers/lastfm_csv.py` for CSV validation, raw import, normalized scrobbles, duplicate marking, artist creation, and stats.
- Create: `src/scrobble_recs/normalize/__init__.py`.
- Create: `src/scrobble_recs/normalize/strings.py` for stable artist/album/track keys.
- Create: `src/scrobble_recs/normalize/timestamps.py` for `uts` parsing and impossible date rejection.
- Create: `src/scrobble_recs/sql/001_init.sql` for Milestone 1 tables.
- Create: `tests/fixtures/tiny_lastfm_export.csv` for deterministic import tests.
- Create: `tests/test_config.py`.
- Create: `tests/test_normalize.py`.
- Create: `tests/test_import.py`.
- Create: `tests/test_cli.py`.
- Create: `docs/implementation-log.md` for verification notes.

---

### Task 1: Project Scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `config.example.json`
- Create: `src/scrobble_recs/__init__.py`

- [ ] **Step 1: Create packaging and tool configuration**

Create `pyproject.toml` with:

```toml
[build-system]
requires = ["setuptools>=69", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "scrobble-recs"
version = "0.1.0"
description = "Local-first artist recommendations from a Last.fm CSV export."
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
  "duckdb>=1.0",
  "httpx>=0.27",
  "platformdirs>=4.0",
  "pydantic>=2.0",
  "python-dotenv>=1.0",
  "rapidfuzz>=3.0",
  "rich>=13.0",
  "textual>=0.80",
  "typer>=0.12",
]

[project.optional-dependencies]
dev = [
  "mypy>=1.10",
  "pytest>=8.0",
  "ruff>=0.5",
]

[project.scripts]
scrobble-recs = "scrobble_recs.cli:app"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
scrobble_recs = ["sql/*.sql"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.mypy]
python_version = "3.11"
strict = true
mypy_path = "src"
packages = ["scrobble_recs"]
```

- [ ] **Step 2: Add README for the first vertical slice**

Create `README.md` with:

```markdown
# scrobble-recs

`scrobble-recs` is a local-first Python application for importing a Last.fm CSV export and building inspectable artist recommendation data in DuckDB.

## Milestone 1

This slice can:

- Validate the Last.fm CSV column contract.
- Import raw scrobbles into DuckDB.
- Normalize artist, album, and track keys.
- Mark exact duplicate scrobbles without deleting them.
- Create provisional local artist entities.
- Build `artist_listening_fact`.
- Show import statistics through the CLI.

No API keys or network access are required.

## Commands

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

```bash
scrobble-recs import --config config.example.json
scrobble-recs doctor --config config.example.json
```
```

- [ ] **Step 3: Add an example config without API keys**

Create `config.example.json` with:

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
  }
}
```

- [ ] **Step 4: Add package metadata**

Create `src/scrobble_recs/__init__.py` with:

```python
"""Local-first Last.fm recommendation tooling."""

__version__ = "0.1.0"
```

- [ ] **Step 5: Run scaffold checks**

Run:

```bash
python -m pytest
```

Expected:

```text
no tests ran
```

- [ ] **Step 6: Commit scaffold**

Run:

```bash
git add pyproject.toml README.md config.example.json src/scrobble_recs/__init__.py
git commit -m "chore: scaffold scrobble recs project"
```

---

### Task 2: Config Loader And Database Migration

**Files:**
- Create: `src/scrobble_recs/config.py`
- Create: `src/scrobble_recs/db.py`
- Create: `src/scrobble_recs/sql/001_init.sql`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Create `tests/test_config.py` with:

```python
import json
from pathlib import Path

from scrobble_recs.config import load_config


def test_load_config_expands_paths_and_environment_variables(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SCROBBLE_RECS_TEST_ROOT", str(tmp_path))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": "${SCROBBLE_RECS_TEST_ROOT}/db/scrobbles.duckdb",
                    "cache_dir": "${SCROBBLE_RECS_TEST_ROOT}/cache",
                    "log_path": "${SCROBBLE_RECS_TEST_ROOT}/state/app.log",
                },
                "input": {
                    "lastfm_csv": "${SCROBBLE_RECS_TEST_ROOT}/exports/scrobbles.csv",
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

    assert config.app.db_path == tmp_path / "db" / "scrobbles.duckdb"
    assert config.app.cache_dir == tmp_path / "cache"
    assert config.input.lastfm_csv == tmp_path / "exports" / "scrobbles.csv"
```

- [ ] **Step 2: Run config test to verify it fails**

Run:

```bash
python -m pytest tests/test_config.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.config'
```

- [ ] **Step 3: Implement config loading**

Create `src/scrobble_recs/config.py` with:

```python
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
```

- [ ] **Step 4: Create the initial schema migration**

Create `src/scrobble_recs/sql/001_init.sql` with:

```sql
CREATE TABLE IF NOT EXISTS raw_scrobble (
    raw_scrobble_id UBIGINT,
    uts BIGINT,
    utc_time VARCHAR,
    artist VARCHAR,
    artist_mbid VARCHAR,
    album VARCHAR,
    album_mbid VARCHAR,
    track VARCHAR,
    track_mbid VARCHAR,
    imported_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS scrobble (
    scrobble_id UBIGINT,
    played_at TIMESTAMP,
    played_date DATE,
    artist_raw VARCHAR,
    album_raw VARCHAR,
    track_raw VARCHAR,
    artist_mbid_raw VARCHAR,
    album_mbid_raw VARCHAR,
    track_mbid_raw VARCHAR,
    artist_key VARCHAR,
    album_key VARCHAR,
    track_key VARCHAR,
    is_duplicate BOOLEAN DEFAULT false
);

CREATE TABLE IF NOT EXISTS artist (
    artist_id UBIGINT,
    canonical_name VARCHAR,
    artist_key VARCHAR,
    musicbrainz_artist_mbid VARCHAR,
    lastfm_name VARCHAR,
    disambiguation VARCHAR,
    country VARCHAR,
    begin_date VARCHAR,
    end_date VARCHAR,
    resolution_status VARCHAR,
    resolution_confidence DOUBLE,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS artist_listening_fact (
    artist_id UBIGINT,
    play_count BIGINT,
    track_count BIGINT,
    album_count BIGINT,
    first_played_at TIMESTAMP,
    last_played_at TIMESTAMP,
    active_days BIGINT,
    active_months BIGINT,
    session_count BIGINT,
    recent_90d_plays BIGINT,
    recent_365d_plays BIGINT,
    lifetime_weight DOUBLE,
    recent_weight DOUBLE,
    knownness DOUBLE,
    rediscovery_score DOUBLE,
    updated_at TIMESTAMP DEFAULT current_timestamp
);
```

- [ ] **Step 5: Implement DuckDB connection and migrations**

Create `src/scrobble_recs/db.py` with:

```python
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import duckdb
from duckdb import DuckDBPyConnection

from scrobble_recs.config import ScrobbleRecsConfig


def connect_database(db_path: Path) -> DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def apply_migrations(conn: DuckDBPyConnection) -> None:
    sql_dir = files("scrobble_recs").joinpath("sql")
    migrations = sorted(path for path in sql_dir.iterdir() if path.name.endswith(".sql"))
    for migration in migrations:
        conn.execute(migration.read_text(encoding="utf-8"))


def initialize_database(config: ScrobbleRecsConfig) -> DuckDBPyConnection:
    conn = connect_database(config.app.db_path)
    apply_migrations(conn)
    return conn
```

- [ ] **Step 6: Run config test to verify it passes**

Run:

```bash
python -m pytest tests/test_config.py -v
```

Expected:

```text
1 passed
```

- [ ] **Step 7: Commit config and database setup**

Run:

```bash
git add src/scrobble_recs/config.py src/scrobble_recs/db.py src/scrobble_recs/sql/001_init.sql tests/test_config.py
git commit -m "feat: add config loader and duckdb migrations"
```

---

### Task 3: Normalization And Timestamp Parsing

**Files:**
- Create: `src/scrobble_recs/normalize/__init__.py`
- Create: `src/scrobble_recs/normalize/strings.py`
- Create: `src/scrobble_recs/normalize/timestamps.py`
- Test: `tests/test_normalize.py`

- [ ] **Step 1: Write failing normalization tests**

Create `tests/test_normalize.py` with:

```python
from datetime import datetime

import pytest

from scrobble_recs.normalize.strings import normalize_key
from scrobble_recs.normalize.timestamps import parse_uts


def test_normalize_key_preserves_meaningful_words() -> None:
    assert normalize_key("  The   Cure  ") == "the cure"


def test_normalize_key_collapses_punctuation_and_zero_width_characters() -> None:
    assert normalize_key("Boys\u200b Don't--Cry!") == "boys don t cry"


def test_parse_uts_returns_naive_utc_datetime() -> None:
    assert parse_uts("1704067200") == datetime(2024, 1, 1, 0, 0, 0)


def test_parse_uts_rejects_non_integer_values() -> None:
    with pytest.raises(ValueError, match="uts must be an integer"):
        parse_uts("not-a-timestamp")


def test_parse_uts_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="uts is before 1970-01-01"):
        parse_uts("-1")
```

- [ ] **Step 2: Run normalization tests to verify they fail**

Run:

```bash
python -m pytest tests/test_normalize.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.normalize'
```

- [ ] **Step 3: Add normalize package marker**

Create `src/scrobble_recs/normalize/__init__.py` with:

```python
"""Normalization helpers for local identity keys."""
```

- [ ] **Step 4: Implement string normalization**

Create `src/scrobble_recs/normalize/strings.py` with:

```python
from __future__ import annotations

import re
import unicodedata


_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\ufeff]")
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_key(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _ZERO_WIDTH.sub("", normalized)
    normalized = normalized.casefold().strip()
    normalized = _PUNCTUATION.sub(" ", normalized)
    return _WHITESPACE.sub(" ", normalized).strip()
```

- [ ] **Step 5: Implement timestamp parsing**

Create `src/scrobble_recs/normalize/timestamps.py` with:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta


def parse_uts(value: str | int) -> datetime:
    try:
        timestamp = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("uts must be an integer") from exc

    if timestamp < 0:
        raise ValueError("uts is before 1970-01-01")

    played_at = datetime.fromtimestamp(timestamp, tz=UTC)
    if played_at > datetime.now(tz=UTC) + timedelta(days=1):
        raise ValueError("uts is more than one day in the future")

    return played_at.replace(tzinfo=None)
```

- [ ] **Step 6: Run normalization tests to verify they pass**

Run:

```bash
python -m pytest tests/test_normalize.py -v
```

Expected:

```text
5 passed
```

- [ ] **Step 7: Commit normalization helpers**

Run:

```bash
git add src/scrobble_recs/normalize tests/test_normalize.py
git commit -m "feat: add import normalization helpers"
```

---

### Task 4: Last.fm CSV Importer

**Files:**
- Create: `src/scrobble_recs/importers/__init__.py`
- Create: `src/scrobble_recs/importers/lastfm_csv.py`
- Create: `tests/fixtures/tiny_lastfm_export.csv`
- Test: `tests/test_import.py`

- [ ] **Step 1: Add deterministic fixture CSV**

Create `tests/fixtures/tiny_lastfm_export.csv` with:

```csv
uts,utc_time,artist,artist_mbid,album,album_mbid,track,track_mbid
1704067200,2024-01-01 00:00:00,The Cure,mbid-cure,Boys Don't Cry,,Boys Don't Cry,
1704067200,2024-01-01 00:00:00,The Cure,mbid-cure,Boys Don't Cry,,Boys Don't Cry,
1704153600,2024-01-02 00:00:00,The Cure,mbid-cure,Disintegration,,Pictures of You,
1704240000,2024-01-03 00:00:00,Cocteau Twins,,Heaven or Las Vegas,,Cherry-coloured Funk,
1704326400,2024-01-04 00:00:00,Slowdive,,Souvlaki,,Alison,
1704412800,2024-01-05 00:00:00,Pixies,mbid-pixies,Doolittle,,Debaser,
```

- [ ] **Step 2: Write failing importer tests**

Create `tests/test_import.py` with:

```python
import json
from pathlib import Path

import duckdb
import pytest

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.importers.lastfm_csv import EXPECTED_COLUMNS, import_lastfm_csv


def write_config(tmp_path: Path, csv_path: Path) -> Path:
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
                    "lastfm_csv": str(csv_path),
                    "timezone": "UTC",
                    "timestamp_column": "uts",
                    "csv_columns": list(EXPECTED_COLUMNS),
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_import_lastfm_csv_persists_raw_clean_artists_and_facts(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/tiny_lastfm_export.csv")
    config = load_config(write_config(tmp_path, fixture))
    conn = initialize_database(config)

    stats = import_lastfm_csv(conn, fixture)

    assert stats.raw_scrobbles == 6
    assert stats.scrobbles == 6
    assert stats.duplicate_scrobbles == 1
    assert stats.distinct_artists == 4
    assert stats.distinct_tracks == 5
    assert stats.null_artist_mbid_scrobbles == 2

    fact_rows = conn.execute(
        """
        SELECT a.canonical_name, f.play_count, f.track_count, f.active_days
        FROM artist_listening_fact f
        JOIN artist a ON a.artist_id = f.artist_id
        ORDER BY a.canonical_name
        """
    ).fetchall()

    assert fact_rows == [
        ("Cocteau Twins", 1, 1, 1),
        ("Pixies", 1, 1, 1),
        ("Slowdive", 1, 1, 1),
        ("The Cure", 2, 2, 2),
    ]

    conn.close()


def test_import_rejects_wrong_columns(tmp_path: Path) -> None:
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("uts,artist,track\n1704067200,The Cure,Plainsong\n", encoding="utf-8")
    conn = duckdb.connect(":memory:")

    with pytest.raises(ValueError, match="CSV columns must exactly match"):
        import_lastfm_csv(conn, bad_csv)

    conn.close()
```

- [ ] **Step 3: Run importer tests to verify they fail**

Run:

```bash
python -m pytest tests/test_import.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.importers'
```

- [ ] **Step 4: Add importers package marker**

Create `src/scrobble_recs/importers/__init__.py` with:

```python
"""Import pipelines for external listening history exports."""
```

- [ ] **Step 5: Implement Last.fm CSV importer**

Create `src/scrobble_recs/importers/lastfm_csv.py` with:

```python
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from duckdb import DuckDBPyConnection

from scrobble_recs.config import EXPECTED_CSV_COLUMNS
from scrobble_recs.normalize.strings import normalize_key
from scrobble_recs.normalize.timestamps import parse_uts


EXPECTED_COLUMNS = EXPECTED_CSV_COLUMNS


@dataclass(frozen=True)
class ImportStats:
    raw_scrobbles: int
    scrobbles: int
    duplicate_scrobbles: int
    distinct_artists: int
    distinct_tracks: int
    null_artist_mbid_scrobbles: int


def import_lastfm_csv(conn: DuckDBPyConnection, csv_path: Path) -> ImportStats:
    rows = _read_validated_rows(csv_path)
    raw_rows: list[tuple[object, ...]] = []
    scrobble_rows: list[tuple[object, ...]] = []
    artist_by_key: dict[str, dict[str, str]] = {}
    seen_duplicate_keys: set[tuple[int, str, str, str]] = set()

    for index, row in enumerate(rows, start=1):
        uts = int(row["uts"])
        played_at = parse_uts(row["uts"])
        artist_key = normalize_key(row["artist"])
        album_key = normalize_key(row["album"])
        track_key = normalize_key(row["track"])
        duplicate_key = (uts, artist_key, track_key, album_key)
        is_duplicate = duplicate_key in seen_duplicate_keys
        seen_duplicate_keys.add(duplicate_key)

        raw_rows.append(
            (
                index,
                uts,
                row["utc_time"],
                row["artist"],
                row["artist_mbid"],
                row["album"],
                row["album_mbid"],
                row["track"],
                row["track_mbid"],
            )
        )
        scrobble_rows.append(
            (
                index,
                played_at,
                played_at.date(),
                row["artist"],
                row["album"],
                row["track"],
                row["artist_mbid"],
                row["album_mbid"],
                row["track_mbid"],
                artist_key,
                album_key,
                track_key,
                is_duplicate,
            )
        )

        if artist_key not in artist_by_key:
            artist_by_key[artist_key] = {
                "canonical_name": row["artist"].strip(),
                "artist_mbid": row["artist_mbid"].strip(),
            }

    _replace_import_tables(conn)
    conn.executemany(
        """
        INSERT INTO raw_scrobble (
            raw_scrobble_id,
            uts,
            utc_time,
            artist,
            artist_mbid,
            album,
            album_mbid,
            track,
            track_mbid
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        raw_rows,
    )
    conn.executemany(
        """
        INSERT INTO scrobble (
            scrobble_id,
            played_at,
            played_date,
            artist_raw,
            album_raw,
            track_raw,
            artist_mbid_raw,
            album_mbid_raw,
            track_mbid_raw,
            artist_key,
            album_key,
            track_key,
            is_duplicate
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        scrobble_rows,
    )

    artist_rows = [
        (
            artist_id,
            artist_data["canonical_name"],
            artist_key,
            artist_data["artist_mbid"] or None,
            artist_data["canonical_name"],
            "unresolved",
            0.0,
        )
        for artist_id, (artist_key, artist_data) in enumerate(artist_by_key.items(), start=1)
    ]
    conn.executemany(
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
        artist_rows,
    )
    _rebuild_artist_facts(conn)
    return get_import_stats(conn)


def get_import_stats(conn: DuckDBPyConnection) -> ImportStats:
    row = conn.execute(
        """
        SELECT
            (SELECT count(*) FROM raw_scrobble) AS raw_scrobbles,
            (SELECT count(*) FROM scrobble) AS scrobbles,
            (SELECT count(*) FROM scrobble WHERE is_duplicate) AS duplicate_scrobbles,
            (SELECT count(DISTINCT artist_key) FROM scrobble WHERE NOT is_duplicate)
                AS distinct_artists,
            (SELECT count(DISTINCT track_key) FROM scrobble WHERE NOT is_duplicate)
                AS distinct_tracks,
            (
                SELECT count(*)
                FROM scrobble
                WHERE NOT is_duplicate
                  AND coalesce(nullif(trim(artist_mbid_raw), ''), '') = ''
            ) AS null_artist_mbid_scrobbles
        """
    ).fetchone()
    assert row is not None
    return ImportStats(
        raw_scrobbles=row[0],
        scrobbles=row[1],
        duplicate_scrobbles=row[2],
        distinct_artists=row[3],
        distinct_tracks=row[4],
        null_artist_mbid_scrobbles=row[5],
    )


def _read_validated_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        if tuple(fieldnames) != EXPECTED_COLUMNS:
            raise ValueError(
                "CSV columns must exactly match: " + ", ".join(EXPECTED_COLUMNS)
            )

        rows: list[dict[str, str]] = []
        errors: list[str] = []
        for line_number, row in enumerate(reader, start=2):
            normalized_row = {column: (row.get(column) or "").strip() for column in EXPECTED_COLUMNS}
            if normalized_row["uts"] == "uts":
                errors.append(f"line {line_number}: duplicate header row inside file")
                continue
            if normalized_row["uts"] == "":
                errors.append(f"line {line_number}: missing uts")
            if normalized_row["artist"] == "":
                errors.append(f"line {line_number}: missing artist")
            if normalized_row["track"] == "":
                errors.append(f"line {line_number}: missing track")
            if normalized_row["uts"] != "":
                try:
                    parse_uts(normalized_row["uts"])
                except ValueError as exc:
                    errors.append(f"line {line_number}: {exc}")
            rows.append(normalized_row)

    if errors:
        raise ValueError("; ".join(errors))
    return rows


def _replace_import_tables(conn: DuckDBPyConnection) -> None:
    conn.execute("DELETE FROM artist_listening_fact")
    conn.execute("DELETE FROM artist")
    conn.execute("DELETE FROM scrobble")
    conn.execute("DELETE FROM raw_scrobble")


def _rebuild_artist_facts(conn: DuckDBPyConnection) -> None:
    conn.execute("DELETE FROM artist_listening_fact")
    conn.execute(
        """
        INSERT INTO artist_listening_fact (
            artist_id,
            play_count,
            track_count,
            album_count,
            first_played_at,
            last_played_at,
            active_days,
            active_months,
            session_count,
            recent_90d_plays,
            recent_365d_plays,
            lifetime_weight,
            recent_weight,
            knownness,
            rediscovery_score
        )
        SELECT
            a.artist_id,
            count(*) AS play_count,
            count(DISTINCT s.track_key) AS track_count,
            count(DISTINCT s.album_key) AS album_count,
            min(s.played_at) AS first_played_at,
            max(s.played_at) AS last_played_at,
            count(DISTINCT s.played_date) AS active_days,
            count(DISTINCT date_trunc('month', s.played_at)) AS active_months,
            NULL AS session_count,
            count(*) FILTER (
                WHERE s.played_at >= current_timestamp - INTERVAL 90 DAY
            ) AS recent_90d_plays,
            count(*) FILTER (
                WHERE s.played_at >= current_timestamp - INTERVAL 365 DAY
            ) AS recent_365d_plays,
            NULL AS lifetime_weight,
            NULL AS recent_weight,
            NULL AS knownness,
            NULL AS rediscovery_score
        FROM scrobble s
        JOIN artist a ON a.artist_key = s.artist_key
        WHERE NOT s.is_duplicate
        GROUP BY a.artist_id
        """
    )
```

- [ ] **Step 6: Run importer tests to verify they pass**

Run:

```bash
python -m pytest tests/test_import.py -v
```

Expected:

```text
2 passed
```

- [ ] **Step 7: Commit importer**

Run:

```bash
git add src/scrobble_recs/importers tests/fixtures/tiny_lastfm_export.csv tests/test_import.py
git commit -m "feat: import lastfm csv into duckdb"
```

---

### Task 5: CLI Import And Doctor Commands

**Files:**
- Create: `src/scrobble_recs/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Create `tests/test_cli.py` with:

```python
import json
from pathlib import Path

from typer.testing import CliRunner

from scrobble_recs.cli import app


def write_config(tmp_path: Path) -> Path:
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
    return config_path


def test_import_command_prints_stats(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["import", "--config", str(write_config(tmp_path))])

    assert result.exit_code == 0
    assert "Imported 6 scrobbles" in result.output
    assert "duplicate scrobbles: 1" in result.output
    assert "distinct artists: 4" in result.output


def test_doctor_command_runs_without_api_keys(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    runner.invoke(app, ["import", "--config", str(config_path)])

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Config: ok" in result.output
    assert "Database: ok" in result.output
    assert "API keys: not required for import/doctor" in result.output
```

- [ ] **Step 2: Run CLI tests to verify they fail**

Run:

```bash
python -m pytest tests/test_cli.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.cli'
```

- [ ] **Step 3: Implement CLI commands**

Create `src/scrobble_recs/cli.py` with:

```python
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.importers.lastfm_csv import ImportStats, get_import_stats, import_lastfm_csv


app = typer.Typer(help="Local-first Last.fm recommendation tooling.")
console = Console()


@app.command("import")
def import_command(
    config: Path = typer.Option(..., "--config", "-c", help="Path to JSON config file."),
) -> None:
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        stats = import_lastfm_csv(conn, resolved_config.input.lastfm_csv)
    finally:
        conn.close()

    console.print(f"Imported {stats.scrobbles} scrobbles")
    _print_stats(stats)


@app.command("doctor")
def doctor_command(
    config: Path = typer.Option(..., "--config", "-c", help="Path to JSON config file."),
) -> None:
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        stats = get_import_stats(conn)
    finally:
        conn.close()

    console.print("Config: ok")
    console.print("Database: ok")
    if resolved_config.input.lastfm_csv.exists():
        console.print("CSV: ok")
    else:
        console.print(f"CSV: missing at {resolved_config.input.lastfm_csv}")
    console.print("API keys: not required for import/doctor")
    _print_stats(stats)


def _print_stats(stats: ImportStats) -> None:
    table = Table(show_header=False)
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("raw scrobbles", str(stats.raw_scrobbles))
    table.add_row("clean scrobbles", str(stats.scrobbles))
    table.add_row("duplicate scrobbles", str(stats.duplicate_scrobbles))
    table.add_row("distinct artists", str(stats.distinct_artists))
    table.add_row("distinct tracks", str(stats.distinct_tracks))
    table.add_row("null artist MBID scrobbles", str(stats.null_artist_mbid_scrobbles))
    console.print(table)
```

- [ ] **Step 4: Run CLI tests to verify they pass**

Run:

```bash
python -m pytest tests/test_cli.py -v
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Commit CLI commands**

Run:

```bash
git add src/scrobble_recs/cli.py tests/test_cli.py
git commit -m "feat: add import and doctor cli commands"
```

---

### Task 6: Verification, Docs, And Implementation Log

**Files:**
- Modify: `README.md`
- Create: `docs/implementation-log.md`

- [ ] **Step 1: Run the full test suite**

Run:

```bash
python -m pytest
```

Expected:

```text
10 passed
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
Success: no issues found in 9 source files
```

- [ ] **Step 5: Verify CLI manually against the fixture**

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

- [ ] **Step 6: Update README verification notes**

Append this section to `README.md`:

````markdown
## Verification

Milestone 1 was verified with:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
scrobble-recs import --config config.example.json
scrobble-recs doctor --config config.example.json
```
````

- [ ] **Step 7: Add implementation log entry**

Create `docs/implementation-log.md` with:

```markdown
# Implementation Log

## 2026-06-02: Milestone 1 import and inspect

Changed:

- Added Python project scaffold and CLI entry point.
- Added JSON config loading with path expansion and environment-variable interpolation.
- Added DuckDB schema for raw scrobbles, clean scrobbles, provisional artists, and artist listening facts.
- Added Last.fm CSV import with exact column validation.
- Added normalized artist, album, and track keys.
- Added exact duplicate marking without deleting raw or clean rows.
- Added `import` and `doctor` commands.
- Added deterministic tests and fixture data.

Verified:

- `python -m pytest`
- `python -m ruff check .`
- `python -m ruff format --check .`
- `python -m mypy src`
- `scrobble-recs import --config config.example.json`
- `scrobble-recs doctor --config config.example.json`
```

- [ ] **Step 8: Commit verification docs**

Run:

```bash
git add README.md docs/implementation-log.md
git commit -m "docs: record milestone one verification"
```

---

## Self Review

Spec coverage:

- CSV import is covered by Task 4.
- Exact CSV column validation is covered by Task 4.
- Raw tables and normalized scrobble tables are covered by Task 2 and Task 4.
- Duplicate marking without deletion is covered by Task 4.
- Provisional artist creation is covered by Task 4.
- Artist listening facts are covered by Task 4.
- Total scrobbles, distinct artists, distinct tracks, null MBID counts, and duplicate counts are covered by Task 4 and Task 5.
- `import` and `doctor` commands are covered by Task 5.
- Running without API keys is covered by Task 5.
- Deterministic tests and fixture data are covered by Tasks 2 through 5.
- README and implementation log updates are covered by Task 6.

Deferred to separate plans:

- Last.fm cache-first enrichment.
- Recommendation scoring and exports.
- Textual TUI.
- MusicBrainz identity resolution.
- ListenBrainz collaborative model.

Placeholder scan:

- The plan contains no open implementation placeholders.
- Every code-writing step includes the exact file path and complete content for that step.
- Every test-running step includes a command and expected result.

Type consistency:

- `ImportStats` field names match CLI and tests.
- `EXPECTED_CSV_COLUMNS` is defined in `config.py` and re-exported as `EXPECTED_COLUMNS` in the importer.
- `load_config`, `initialize_database`, `import_lastfm_csv`, and `get_import_stats` signatures match every call site.
