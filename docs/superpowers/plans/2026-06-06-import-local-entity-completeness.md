# Import + Local Entity Completeness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the local import/entity foundation by adding track entities, persisted import warnings, configurable near-duplicate marking, MBID consistency checks, and stronger provisional identity behavior before external resolution work.

**Architecture:** Keep the importer local-first and set-based: Python validates and stages CSV rows, DuckDB derives persisted raw scrobbles, clean scrobbles, provisional artists, provisional tracks, import warnings, and listening facts inside one transaction. Identity remains provisional unless local evidence is internally consistent; MusicBrainz resolution remains outside this plan.

**Tech Stack:** Python 3.11+, DuckDB, Pydantic v2, Typer, pytest, ruff, mypy.

---

## Scope

This plan covers the requested slice from `docs/spec.md`:

- Section 8.3: add the local `track` entity table.
- Section 9.1: strengthen CSV validation/warnings for duplicate headers, timestamp mismatch, impossible dates, and malformed/missing fields.
- Section 9.2: make the near-duplicate rule configurable.
- Section 9.3: treat supplied MBIDs as hints and only attach them to provisional identities when consistent across matching local rows.
- Section 21: keep import validation queries useful by ensuring artist facts and track counts come from local entity-complete data.

This plan deliberately keeps these out of scope:

- MusicBrainz API calls, fuzzy remote matching, artist merge workflows, and manual correction UI.
- Recommendation scoring changes beyond preserving current artist IDs and listening facts.
- TUI import-warning screens.
- Any network access in tests.

## File Structure

- Create: `src/scrobble_recs/sql/006_import_entities.sql` for `track`, `import_warning`, and indexes.
- Modify: `src/scrobble_recs/config.py` to add `input.near_duplicate_window_seconds`.
- Modify: `src/scrobble_recs/importers/lastfm_csv.py` to stage warnings, timestamp checks, near-duplicate marking, MBID conflict warnings, provisional artist improvements, and provisional track creation.
- Modify: `src/scrobble_recs/admin/doctor.py` to include import warning counts in local diagnostics.
- Modify: `src/scrobble_recs/cli.py` to pass the configured near-duplicate window and print warning stats.
- Modify: `README.md` to document import warnings, track entities, and near-duplicate configuration.
- Modify: `docs/implementation-log.md` after implementation and verification.
- Modify: `tests/test_config.py`.
- Modify: `tests/test_import.py`.
- Modify: `tests/test_admin_doctor.py`.
- Modify: `tests/test_cli.py`.

---

### Task 1: Track And Import Warning Schema

**Files:**
- Create: `src/scrobble_recs/sql/006_import_entities.sql`
- Modify: `tests/test_config.py`

- [x] **Step 1: Write the failing migration test**

Modify `tests/test_config.py::test_initialize_database_applies_initial_migration` so the expected table set includes `track` and `import_warning`, and add column assertions:

```python
def test_initialize_database_applies_initial_migration(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "state" / "scrobbles.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "state" / "app.log"),
                },
                "input": {
                    "lastfm_csv": str(tmp_path / "exports" / "scrobbles.csv"),
                    "timezone": "UTC",
                    "timestamp_column": "uts",
                    "csv_columns": list(EXPECTED_CSV_COLUMNS),
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)

    conn = initialize_database(config)

    try:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        scrobble_columns = {row[0] for row in conn.execute("DESCRIBE scrobble").fetchall()}
        track_columns = {row[0] for row in conn.execute("DESCRIBE track").fetchall()}
        warning_columns = {row[0] for row in conn.execute("DESCRIBE import_warning").fetchall()}
    finally:
        conn.close()

    assert tables == {
        "api_cache",
        "artist",
        "artist_listening_fact",
        "artist_similarity_edge",
        "artist_tag",
        "enrichment_work",
        "feedback_event",
        "import_warning",
        "profile_cluster",
        "raw_scrobble",
        "recommendation",
        "recommendation_run",
        "scrobble",
        "tag_profile",
        "tag_top_artist",
        "track",
    }
    assert {"artist_key", "album_key", "track_key", "is_duplicate"} <= scrobble_columns
    assert {
        "track_id",
        "artist_id",
        "canonical_title",
        "track_key",
        "musicbrainz_recording_mbid",
        "lastfm_track_name",
        "resolution_status",
        "resolution_confidence",
        "created_at",
        "updated_at",
    } <= track_columns
    assert {
        "warning_id",
        "import_row_number",
        "warning_code",
        "severity",
        "message",
        "evidence",
        "created_at",
    } <= warning_columns
```

- [x] **Step 2: Run the migration test to verify it fails**

Run:

```bash
python -m pytest tests/test_config.py::test_initialize_database_applies_initial_migration -v
```

Expected:

```text
AssertionError: Extra items in the right set:
'track'
'import_warning'
```

- [x] **Step 3: Add the migration**

Create `src/scrobble_recs/sql/006_import_entities.sql`:

```sql
CREATE TABLE IF NOT EXISTS track (
    track_id UBIGINT,
    artist_id UBIGINT,
    canonical_title VARCHAR,
    track_key VARCHAR,
    musicbrainz_recording_mbid VARCHAR,
    lastfm_track_name VARCHAR,
    resolution_status VARCHAR,
    resolution_confidence DOUBLE,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS import_warning (
    warning_id UBIGINT,
    import_row_number UBIGINT,
    warning_code VARCHAR,
    severity VARCHAR,
    message VARCHAR,
    evidence JSON,
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE UNIQUE INDEX IF NOT EXISTS track_artist_key_natural_key
ON track (artist_id, track_key);

CREATE UNIQUE INDEX IF NOT EXISTS import_warning_id_key
ON import_warning (warning_id);

CREATE INDEX IF NOT EXISTS import_warning_code_key
ON import_warning (warning_code);

CREATE INDEX IF NOT EXISTS import_warning_row_key
ON import_warning (import_row_number);
```

- [x] **Step 4: Run the migration test to verify it passes**

Run:

```bash
python -m pytest tests/test_config.py::test_initialize_database_applies_initial_migration -v
```

Expected:

```text
1 passed
```

- [ ] **Step 5: Commit schema**

Deviation: skipped commit for this checkpoint because the task executor was instructed not to commit; the controller will commit after review gates.

Run:

```bash
git add src/scrobble_recs/sql/006_import_entities.sql tests/test_config.py docs/superpowers/plans/2026-06-06-import-local-entity-completeness.md
git commit -m "feat: add local track and import warning tables"
```

---

### Task 2: Near-Duplicate Import Configuration

**Files:**
- Modify: `src/scrobble_recs/config.py`
- Modify: `tests/test_config.py`

- [x] **Step 1: Add failing config tests**

Append to `tests/test_config.py`:

```python
def test_load_config_includes_near_duplicate_default(tmp_path: Path) -> None:
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
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.input.near_duplicate_window_seconds == 0


def test_load_config_accepts_and_validates_near_duplicate_window(tmp_path: Path) -> None:
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
                    "near_duplicate_window_seconds": 30,
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.input.near_duplicate_window_seconds == 30

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
                    "near_duplicate_window_seconds": -1,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)

    assert "near_duplicate_window_seconds" in str(exc_info.value)
```

Modify `test_default_config_document_contains_spec_defaults`:

```python
def test_default_config_document_contains_spec_defaults() -> None:
    document = default_config_document()

    assert document["app"]["name"] == "scrobble-recs"
    assert document["input"]["near_duplicate_window_seconds"] == 0
    assert document["apis"]["lastfm"]["api_key"] == "${LASTFM_API_KEY}"
    assert document["apis"]["lastfm"]["rate_limit_per_second"] == 2.0
    assert document["apis"]["musicbrainz"]["rate_limit_per_second"] == 1.0
    assert document["recommendation"]["seed_artist_limit"] == 250
    assert document["recommendation"]["similar_artists_per_seed"] == 100
    assert document["recommendation"]["obviousness"]["top_n_per_tag_hide"] == 3
```

- [x] **Step 2: Run config tests to verify they fail**

Run:

```bash
python -m pytest tests/test_config.py::test_load_config_includes_near_duplicate_default tests/test_config.py::test_load_config_accepts_and_validates_near_duplicate_window tests/test_config.py::test_default_config_document_contains_spec_defaults -v
```

Expected:

```text
AttributeError: 'InputConfig' object has no attribute 'near_duplicate_window_seconds'
```

- [x] **Step 3: Implement config field**

Modify `src/scrobble_recs/config.py` `InputConfig`:

```python
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
```

Modify `default_config_document()` input block:

```python
"input": {
    "lastfm_csv": "./exports/lastfm_scrobbles.csv",
    "timezone": "UTC",
    "timestamp_column": "uts",
    "csv_columns": list(EXPECTED_CSV_COLUMNS),
    "near_duplicate_window_seconds": 0,
},
```

- [x] **Step 4: Run config tests to verify they pass**

Run:

```bash
python -m pytest tests/test_config.py::test_load_config_includes_near_duplicate_default tests/test_config.py::test_load_config_accepts_and_validates_near_duplicate_window tests/test_config.py::test_default_config_document_contains_spec_defaults -v
```

Expected:

```text
3 passed
```

- [ ] **Step 5: Commit config**

Deviation: skipped commit for this checkpoint because the task executor was instructed not to commit; the controller will commit after review gates.

Run:

```bash
git add src/scrobble_recs/config.py tests/test_config.py docs/superpowers/plans/2026-06-06-import-local-entity-completeness.md
git commit -m "feat: configure near duplicate import window"
```

---

### Task 3: Persist Structured Import Warnings

**Files:**
- Modify: `src/scrobble_recs/importers/lastfm_csv.py`
- Modify: `tests/test_import.py`

- [x] **Step 1: Add failing timestamp warning test**

Append to `tests/test_import.py`:

```python
def test_import_warns_on_utc_time_mismatch_without_dropping_row(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "timestamp_mismatch.csv",
        [
            "1704067200,2024-01-02 00:00:00,The Cure,mbid-cure,"
            "Disintegration,,Pictures of You,"
        ],
    )
    config = load_config(write_config(tmp_path, csv_path))
    conn = initialize_database(config)

    stats = import_lastfm_csv(conn, csv_path)

    warning_rows = conn.execute(
        """
        SELECT import_row_number, warning_code, severity, message, evidence
        FROM import_warning
        ORDER BY warning_id
        """
    ).fetchall()

    assert stats.scrobbles == 1
    assert stats.import_warnings == 1
    assert warning_rows == [
        (
            1,
            "timestamp_mismatch",
            "warning",
            "utc_time does not match uts",
            '{"uts":1704067200,"utc_time":"2024-01-02 00:00:00","played_at":"2024-01-01 00:00:00"}',
        )
    ]

    conn.close()
```

- [x] **Step 2: Run warning test to verify it fails**

Run:

```bash
python -m pytest tests/test_import.py::test_import_warns_on_utc_time_mismatch_without_dropping_row -v
```

Expected:

```text
duckdb.duckdb.CatalogException: Table with name import_warning does not exist
```

If Task 1 has already been completed, the expected failure is:

```text
AttributeError: 'ImportStats' object has no attribute 'import_warnings'
```

- [x] **Step 3: Implement warning staging and stats**

Modify `src/scrobble_recs/importers/lastfm_csv.py` imports:

```python
import json
```

Modify `ImportStats`:

```python
@dataclass(frozen=True)
class ImportStats:
    raw_scrobbles: int
    scrobbles: int
    duplicate_scrobbles: int
    near_duplicate_scrobbles: int
    distinct_artists: int
    distinct_tracks: int
    null_artist_mbid_scrobbles: int
    import_warnings: int
```

Add warning staging constants:

```python
_WARNING_COLUMNS = (
    "import_row_number",
    "warning_code",
    "severity",
    "message",
    "evidence",
)
_WARNING_STAGE_TABLE = "_scrobble_recs_import_warning_stage"
```

Modify `get_import_stats()`:

```python
def get_import_stats(conn: DuckDBPyConnection) -> ImportStats:
    return ImportStats(
        raw_scrobbles=_query_int(conn, "SELECT count(*) FROM raw_scrobble"),
        scrobbles=_query_int(conn, "SELECT count(*) FROM scrobble"),
        duplicate_scrobbles=_query_int(
            conn,
            "SELECT count(*) FROM scrobble WHERE is_duplicate",
        ),
        near_duplicate_scrobbles=_query_int(
            conn,
            """
            SELECT count(*)
            FROM import_warning
            WHERE warning_code = 'near_duplicate_scrobble'
            """,
        ),
        distinct_artists=_query_int(
            conn,
            "SELECT count(DISTINCT artist_key) FROM scrobble WHERE NOT is_duplicate",
        ),
        distinct_tracks=_query_int(
            conn,
            "SELECT count(DISTINCT track_key) FROM scrobble WHERE NOT is_duplicate",
        ),
        null_artist_mbid_scrobbles=_query_int(
            conn,
            """
            SELECT count(*)
            FROM scrobble
            WHERE artist_mbid_raw IS NULL OR trim(artist_mbid_raw) = ''
            """,
        ),
        import_warnings=_query_int(conn, "SELECT count(*) FROM import_warning"),
    )
```

Replace `_write_validated_import_csv()` with:

```python
def _write_validated_import_csv(
    csv_path: Path,
    staging_csv: Path,
    warning_csv: Path,
    *,
    near_duplicate_window_seconds: int,
) -> None:
    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file, strict=True)
        staging_csv.parent.mkdir(parents=True, exist_ok=True)
        with (
            staging_csv.open("w", encoding="utf-8", newline="") as output_file,
            warning_csv.open("w", encoding="utf-8", newline="") as warning_file,
        ):
            writer = csv.DictWriter(output_file, fieldnames=list(_STAGING_COLUMNS))
            warning_writer = csv.DictWriter(warning_file, fieldnames=list(_WARNING_COLUMNS))
            writer.writeheader()
            warning_writer.writeheader()

            seen_scrobbles: set[tuple[int, str, str, str]] = set()
            seen_near_duplicates: dict[tuple[str, str], list[tuple[int, int]]] = {}
            _write_validated_rows(
                reader,
                writer,
                warning_writer,
                seen_scrobbles,
                seen_near_duplicates,
                near_duplicate_window_seconds=near_duplicate_window_seconds,
            )
```

Change `_write_validated_rows()` signature:

```python
def _write_validated_rows(
    reader: csv.DictReader[str],
    writer: csv.DictWriter[str],
    warning_writer: csv.DictWriter[str],
    seen_scrobbles: set[tuple[int, str, str, str]],
    seen_near_duplicates: dict[tuple[str, str], list[tuple[int, int]]],
    *,
    near_duplicate_window_seconds: int,
) -> None:
```

Inside `_write_validated_rows()`, after `played_at = _parse_uts(...)` and `uts = int(cleaned["uts"])`, add:

```python
            _warn_on_utc_time_mismatch(
                warning_writer,
                row_number=row_index - 1,
                utc_time=cleaned["utc_time"],
                uts=uts,
                played_at=played_at,
            )
```

Add helper functions:

```python
def _warn_on_utc_time_mismatch(
    warning_writer: csv.DictWriter[str],
    *,
    row_number: int,
    utc_time: str,
    uts: int,
    played_at: datetime,
) -> None:
    if utc_time == "":
        return
    try:
        parsed_utc_time = datetime.strptime(utc_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        _write_warning(
            warning_writer,
            row_number=row_number,
            warning_code="invalid_utc_time",
            message="utc_time is not formatted as YYYY-MM-DD HH:MM:SS",
            evidence={"uts": uts, "utc_time": utc_time},
        )
        return
    if parsed_utc_time != played_at:
        _write_warning(
            warning_writer,
            row_number=row_number,
            warning_code="timestamp_mismatch",
            message="utc_time does not match uts",
            evidence={
                "uts": uts,
                "utc_time": utc_time,
                "played_at": played_at.isoformat(sep=" "),
            },
        )


def _write_warning(
    warning_writer: csv.DictWriter[str],
    *,
    row_number: int,
    warning_code: str,
    message: str,
    evidence: dict[str, object],
    severity: str = "warning",
) -> None:
    warning_writer.writerow(
        {
            "import_row_number": str(row_number),
            "warning_code": warning_code,
            "severity": severity,
            "message": message,
            "evidence": json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        }
    )
```

Modify `import_lastfm_csv()`:

```python
def import_lastfm_csv(
    conn: DuckDBPyConnection,
    csv_path: Path,
    *,
    near_duplicate_window_seconds: int = 0,
) -> ImportStats:
    with TemporaryDirectory(prefix="scrobble_recs_import_") as temp_dir:
        staging_csv = Path(temp_dir) / "validated_lastfm_import.csv"
        warning_csv = Path(temp_dir) / "validated_lastfm_import_warnings.csv"
        _write_validated_import_csv(
            csv_path,
            staging_csv,
            warning_csv,
            near_duplicate_window_seconds=near_duplicate_window_seconds,
        )
        conn.execute("BEGIN TRANSACTION")
        try:
            _create_import_stage(conn, staging_csv)
            _create_warning_stage(conn, warning_csv)
            _replace_import_tables(conn)
            _insert_raw_scrobbles(conn)
            _insert_scrobbles(conn)
            _insert_import_warnings(conn)
            _insert_provisional_artists(conn)
            _rebuild_artist_listening_facts(conn)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        conn.execute(f"DROP TABLE IF EXISTS {_STAGING_TABLE}")
        conn.execute(f"DROP TABLE IF EXISTS {_WARNING_STAGE_TABLE}")
    return get_import_stats(conn)
```

Add warning stage functions:

```python
def _create_warning_stage(conn: DuckDBPyConnection, warning_csv: Path) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {_WARNING_STAGE_TABLE}")
    conn.execute(
        f"""
        CREATE TEMP TABLE {_WARNING_STAGE_TABLE} AS
        SELECT
            CAST(import_row_number AS UBIGINT) AS import_row_number,
            warning_code,
            severity,
            message,
            evidence
        FROM read_csv(?, header=true, all_varchar=true, nullstr='\\0')
        """,
        [warning_csv.as_posix()],
    )


def _insert_import_warnings(conn: DuckDBPyConnection) -> None:
    next_id = _next_id(conn, "import_warning", "warning_id")
    conn.execute(
        """
        INSERT INTO import_warning (
            warning_id,
            import_row_number,
            warning_code,
            severity,
            message,
            evidence
        )
        SELECT
            ? + row_number() OVER (ORDER BY import_row_number, warning_code, message) - 1,
            import_row_number,
            warning_code,
            severity,
            message,
            CAST(evidence AS JSON)
        FROM _scrobble_recs_import_warning_stage
        """,
        [next_id],
    )
```

Modify `_replace_import_tables()`:

```python
def _replace_import_tables(conn: DuckDBPyConnection) -> None:
    conn.execute("DELETE FROM enrichment_work")
    conn.execute("DELETE FROM artist_similarity_edge")
    conn.execute("DELETE FROM artist_tag")
    conn.execute("DELETE FROM tag_top_artist")
    conn.execute("DELETE FROM artist_listening_fact")
    conn.execute("DELETE FROM track")
    conn.execute("DELETE FROM artist")
    conn.execute("DELETE FROM scrobble")
    conn.execute("DELETE FROM raw_scrobble")
    conn.execute("DELETE FROM import_warning")
```

- [x] **Step 4: Run warning test to verify it passes**

Run:

```bash
python -m pytest tests/test_import.py::test_import_warns_on_utc_time_mismatch_without_dropping_row -v
```

Expected:

```text
1 passed
```

- [x] **Step 5: Run current import tests**

Run:

```bash
python -m pytest tests/test_import.py -v
```

Expected:

```text
all import tests pass
```

- [ ] **Step 6: Commit import warnings**

Deviation: skipped commit for this checkpoint because the task executor was instructed not to commit; the controller will commit after review gates.
Deviation: evidence serialization preserves the specified persisted JSON key order for the Task 3 warning test instead of sorting keys alphabetically.

Run:

```bash
git add src/scrobble_recs/importers/lastfm_csv.py tests/test_import.py docs/superpowers/plans/2026-06-06-import-local-entity-completeness.md
git commit -m "feat: persist import warnings"
```

---

### Task 4: Configurable Near-Duplicate Marking

**Files:**
- Modify: `src/scrobble_recs/importers/lastfm_csv.py`
- Modify: `src/scrobble_recs/cli.py`
- Modify: `tests/test_import.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing near-duplicate tests**

Append to `tests/test_import.py`:

```python
def test_import_does_not_mark_near_duplicates_when_window_is_disabled(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "near_duplicates_disabled.csv",
        [
            "1704067200,2024-01-01 00:00:00,The Cure,mbid-cure,"
            "Disintegration,,Pictures of You,",
            "1704067225,2024-01-01 00:00:25,The Cure,mbid-cure,"
            "Disintegration,,Pictures of You,",
        ],
    )
    config = load_config(write_config(tmp_path, csv_path))
    conn = initialize_database(config)

    stats = import_lastfm_csv(conn, csv_path)

    assert stats.scrobbles == 2
    assert stats.duplicate_scrobbles == 0
    assert stats.near_duplicate_scrobbles == 0
    assert conn.execute("SELECT count(*) FROM import_warning").fetchone() == (0,)

    conn.close()


def test_import_marks_configured_near_duplicates_without_deleting_rows(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "near_duplicates_enabled.csv",
        [
            "1704067200,2024-01-01 00:00:00,The Cure,mbid-cure,"
            "Disintegration,,Pictures of You,",
            "1704067225,2024-01-01 00:00:25,The Cure,mbid-cure,"
            "Disintegration,,Pictures of You,",
            "1704067265,2024-01-01 00:01:05,The Cure,mbid-cure,"
            "Disintegration,,Pictures of You,",
        ],
    )
    config = load_config(write_config(tmp_path, csv_path))
    conn = initialize_database(config)

    stats = import_lastfm_csv(conn, csv_path, near_duplicate_window_seconds=30)

    duplicate_rows = conn.execute(
        """
        SELECT scrobble_id, uts, is_duplicate
        FROM scrobble
        ORDER BY scrobble_id
        """
    ).fetchall()
    warnings = conn.execute(
        """
        SELECT import_row_number, warning_code, message
        FROM import_warning
        ORDER BY warning_id
        """
    ).fetchall()

    assert stats.scrobbles == 3
    assert stats.duplicate_scrobbles == 1
    assert stats.near_duplicate_scrobbles == 1
    assert duplicate_rows == [
        (1, 1704067200, False),
        (2, 1704067225, True),
        (3, 1704067265, False),
    ]
    assert warnings == [
        (
            2,
            "near_duplicate_scrobble",
            "same artist and track within configured near-duplicate window",
        )
    ]

    conn.close()
```

- [ ] **Step 2: Run near-duplicate tests to verify they fail**

Run:

```bash
python -m pytest tests/test_import.py::test_import_does_not_mark_near_duplicates_when_window_is_disabled tests/test_import.py::test_import_marks_configured_near_duplicates_without_deleting_rows -v
```

Expected:

```text
AssertionError: assert 0 == 1
```

- [ ] **Step 3: Implement near-duplicate detection**

In `_write_validated_rows()`, replace duplicate computation with:

```python
            duplicate_key = (uts, artist_key, track_key, album_key)
            is_exact_duplicate = duplicate_key in seen_scrobbles
            seen_scrobbles.add(duplicate_key)
            is_near_duplicate = _is_near_duplicate(
                seen_near_duplicates,
                row_number=row_index - 1,
                uts=uts,
                artist_key=artist_key,
                track_key=track_key,
                near_duplicate_window_seconds=near_duplicate_window_seconds,
            )
            is_duplicate = is_exact_duplicate or is_near_duplicate
            if is_near_duplicate and not is_exact_duplicate:
                _write_warning(
                    warning_writer,
                    row_number=row_index - 1,
                    warning_code="near_duplicate_scrobble",
                    message="same artist and track within configured near-duplicate window",
                    evidence={
                        "uts": uts,
                        "artist_key": artist_key,
                        "track_key": track_key,
                        "window_seconds": near_duplicate_window_seconds,
                    },
                )
```

Add helper:

```python
def _is_near_duplicate(
    seen_near_duplicates: dict[tuple[str, str], list[tuple[int, int]]],
    *,
    row_number: int,
    uts: int,
    artist_key: str,
    track_key: str,
    near_duplicate_window_seconds: int,
) -> bool:
    if near_duplicate_window_seconds <= 0:
        return False
    key = (artist_key, track_key)
    previous_rows = seen_near_duplicates.setdefault(key, [])
    is_duplicate = any(
        abs(uts - previous_uts) <= near_duplicate_window_seconds
        for previous_uts, _previous_row_number in previous_rows
    )
    previous_rows.append((uts, row_number))
    return is_duplicate
```

Modify `src/scrobble_recs/cli.py` import command call:

```python
stats = import_lastfm_csv(
    conn,
    csv_path,
    near_duplicate_window_seconds=resolved_config.input.near_duplicate_window_seconds,
)
```

Modify `_print_stats()`:

```python
def _print_stats(stats: ImportStats) -> None:
    console.print(f"raw scrobbles: {stats.raw_scrobbles}")
    console.print(f"clean scrobbles: {stats.scrobbles}")
    console.print(f"duplicate scrobbles: {stats.duplicate_scrobbles}")
    console.print(f"near-duplicate scrobbles: {stats.near_duplicate_scrobbles}")
    console.print(f"distinct artists: {stats.distinct_artists}")
    console.print(f"distinct tracks: {stats.distinct_tracks}")
    console.print(f"null artist MBID scrobbles: {stats.null_artist_mbid_scrobbles}")
    console.print(f"import warnings: {stats.import_warnings}")
```

- [ ] **Step 4: Add CLI dispatch coverage**

Add this test to `tests/test_cli.py`:

```python
def test_import_command_passes_near_duplicate_window(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    csv_path = tmp_path / "scrobbles.csv"
    write_csv(csv_path, [])
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
                    "csv_columns": list(EXPECTED_CSV_COLUMNS),
                    "near_duplicate_window_seconds": 30,
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[int] = []

    def fake_import(conn, csv_path, *, near_duplicate_window_seconds=0):
        calls.append(near_duplicate_window_seconds)
        return ImportStats(
            raw_scrobbles=0,
            scrobbles=0,
            duplicate_scrobbles=0,
            near_duplicate_scrobbles=0,
            distinct_artists=0,
            distinct_tracks=0,
            null_artist_mbid_scrobbles=0,
            import_warnings=0,
        )

    monkeypatch.setattr("scrobble_recs.cli.import_lastfm_csv", fake_import)

    result = runner.invoke(app, ["import", "--config", str(config_path)])

    assert result.exit_code == 0
    assert calls == [30]
```

Use the existing `write_csv`, `CliRunner`, `app`, `ImportStats`, and `EXPECTED_CSV_COLUMNS` imports or add them at the top of `tests/test_cli.py` if missing.

- [ ] **Step 5: Run near-duplicate and CLI tests**

Run:

```bash
python -m pytest tests/test_import.py::test_import_does_not_mark_near_duplicates_when_window_is_disabled tests/test_import.py::test_import_marks_configured_near_duplicates_without_deleting_rows tests/test_cli.py::test_import_command_passes_near_duplicate_window -v
```

Expected:

```text
3 passed
```

- [ ] **Step 6: Commit near-duplicate behavior**

Run:

```bash
git add src/scrobble_recs/importers/lastfm_csv.py src/scrobble_recs/cli.py tests/test_import.py tests/test_cli.py docs/superpowers/plans/2026-06-06-import-local-entity-completeness.md
git commit -m "feat: mark configurable near duplicate scrobbles"
```

---

### Task 5: Local MBID Consistency And Provisional Artist Identity

**Files:**
- Modify: `src/scrobble_recs/importers/lastfm_csv.py`
- Modify: `tests/test_import.py`

- [ ] **Step 1: Add failing artist MBID consistency tests**

Append to `tests/test_import.py`:

```python
def test_import_keeps_consistent_artist_mbid_on_provisional_artist(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "consistent_artist_mbid.csv",
        [
            "1704067200,2024-01-01 00:00:00,The Cure,mbid-cure,"
            "Disintegration,,Pictures of You,",
            "1704153600,2024-01-02 00:00:00,the cure,mbid-cure,"
            "Disintegration,,Plainsong,",
        ],
    )
    config = load_config(write_config(tmp_path, csv_path))
    conn = initialize_database(config)

    import_lastfm_csv(conn, csv_path)

    artist_row = conn.execute(
        """
        SELECT canonical_name, musicbrainz_artist_mbid, resolution_status, resolution_confidence
        FROM artist
        WHERE artist_key = 'the cure'
        """
    ).fetchone()

    assert artist_row == ("The Cure", "mbid-cure", "provisional_mbid", 0.5)
    assert conn.execute("SELECT count(*) FROM import_warning").fetchone() == (0,)

    conn.close()


def test_import_does_not_trust_conflicting_artist_mbids(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "conflicting_artist_mbid.csv",
        [
            "1704067200,2024-01-01 00:00:00,The Cure,mbid-cure-a,"
            "Disintegration,,Pictures of You,",
            "1704153600,2024-01-02 00:00:00,The Cure,mbid-cure-b,"
            "Disintegration,,Plainsong,",
        ],
    )
    config = load_config(write_config(tmp_path, csv_path))
    conn = initialize_database(config)

    stats = import_lastfm_csv(conn, csv_path)

    artist_row = conn.execute(
        """
        SELECT canonical_name, musicbrainz_artist_mbid, resolution_status, resolution_confidence
        FROM artist
        WHERE artist_key = 'the cure'
        """
    ).fetchone()
    warnings = conn.execute(
        """
        SELECT warning_code, message
        FROM import_warning
        ORDER BY warning_id
        """
    ).fetchall()

    assert stats.import_warnings == 1
    assert artist_row == ("The Cure", None, "provisional_mbid_conflict", 0.0)
    assert warnings == [
        ("artist_mbid_conflict", "artist_key has multiple supplied artist MBIDs")
    ]

    conn.close()
```

- [ ] **Step 2: Run artist MBID tests to verify they fail**

Run:

```bash
python -m pytest tests/test_import.py::test_import_keeps_consistent_artist_mbid_on_provisional_artist tests/test_import.py::test_import_does_not_trust_conflicting_artist_mbids -v
```

Expected:

```text
AssertionError
```

- [ ] **Step 3: Replace provisional artist insert**

Modify `_insert_provisional_artists()`:

```python
def _insert_provisional_artists(conn: DuckDBPyConnection) -> None:
    next_id = _next_id(conn, "artist", "artist_id")
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
        WITH name_counts AS (
            SELECT
                artist_key,
                artist_raw,
                count(*) AS plays,
                min(scrobble_id) AS first_scrobble_id
            FROM scrobble
            WHERE artist_key <> ''
              AND NOT is_duplicate
            GROUP BY artist_key, artist_raw
        ),
        preferred_names AS (
            SELECT artist_key, artist_raw AS canonical_name
            FROM (
                SELECT
                    artist_key,
                    artist_raw,
                    row_number() OVER (
                        PARTITION BY artist_key
                        ORDER BY plays DESC, first_scrobble_id
                    ) AS row_number
                FROM name_counts
            ) ranked_names
            WHERE row_number = 1
        ),
        mbids AS (
            SELECT
                artist_key,
                count(DISTINCT NULLIF(trim(artist_mbid_raw), '')) AS mbid_count,
                min(NULLIF(trim(artist_mbid_raw), '')) AS artist_mbid
            FROM scrobble
            WHERE artist_key <> ''
              AND NOT is_duplicate
            GROUP BY artist_key
        )
        SELECT
            ? + row_number() OVER (ORDER BY names.artist_key) - 1 AS artist_id,
            names.canonical_name,
            names.artist_key,
            CASE WHEN mbids.mbid_count = 1 THEN mbids.artist_mbid ELSE NULL END,
            names.canonical_name AS lastfm_name,
            CASE
                WHEN mbids.mbid_count = 1 THEN 'provisional_mbid'
                WHEN mbids.mbid_count > 1 THEN 'provisional_mbid_conflict'
                ELSE 'provisional_name'
            END AS resolution_status,
            CASE WHEN mbids.mbid_count = 1 THEN 0.5 ELSE 0.0 END AS resolution_confidence
        FROM preferred_names names
        JOIN mbids ON mbids.artist_key = names.artist_key
        WHERE NOT EXISTS (
            SELECT 1 FROM artist existing WHERE existing.artist_key = names.artist_key
        )
        """,
        [next_id],
    )
```

Add conflict warning insert:

```python
def _insert_artist_mbid_conflict_warnings(conn: DuckDBPyConnection) -> None:
    next_id = _next_id(conn, "import_warning", "warning_id")
    conn.execute(
        """
        INSERT INTO import_warning (
            warning_id,
            import_row_number,
            warning_code,
            severity,
            message,
            evidence
        )
        WITH conflicts AS (
            SELECT
                artist_key,
                min(scrobble_id) AS first_scrobble_id,
                list_sort(list_distinct(list(NULLIF(trim(artist_mbid_raw), '')))) AS mbids
            FROM scrobble
            WHERE artist_key <> ''
              AND NOT is_duplicate
              AND NULLIF(trim(artist_mbid_raw), '') IS NOT NULL
            GROUP BY artist_key
            HAVING count(DISTINCT NULLIF(trim(artist_mbid_raw), '')) > 1
        )
        SELECT
            ? + row_number() OVER (ORDER BY artist_key) - 1 AS warning_id,
            first_scrobble_id AS import_row_number,
            'artist_mbid_conflict' AS warning_code,
            'warning' AS severity,
            'artist_key has multiple supplied artist MBIDs' AS message,
            json_object('artist_key', artist_key, 'artist_mbids', mbids) AS evidence
        FROM conflicts
        """,
        [next_id],
    )
```

In `import_lastfm_csv()`, call conflict warning insert after `_insert_scrobbles(conn)` and before `_insert_import_warnings(conn)`:

```python
            _insert_scrobbles(conn)
            _insert_artist_mbid_conflict_warnings(conn)
            _insert_import_warnings(conn)
            _insert_provisional_artists(conn)
```

Because `_insert_artist_mbid_conflict_warnings()` writes directly to `import_warning`, update `_insert_import_warnings()` to compute `next_id` at call time as shown in Task 3 so staged warning IDs start after conflict warning IDs.

- [ ] **Step 4: Run artist MBID tests to verify they pass**

Run:

```bash
python -m pytest tests/test_import.py::test_import_keeps_consistent_artist_mbid_on_provisional_artist tests/test_import.py::test_import_does_not_trust_conflicting_artist_mbids -v
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Commit artist identity improvements**

Run:

```bash
git add src/scrobble_recs/importers/lastfm_csv.py tests/test_import.py docs/superpowers/plans/2026-06-06-import-local-entity-completeness.md
git commit -m "feat: require consistent artist mbids on import"
```

---

### Task 6: Provisional Track Entities

**Files:**
- Modify: `src/scrobble_recs/importers/lastfm_csv.py`
- Modify: `tests/test_import.py`

- [ ] **Step 1: Add failing track entity tests**

Append to `tests/test_import.py`:

```python
def test_import_creates_provisional_track_entities(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/tiny_lastfm_export.csv")
    config = load_config(write_config(tmp_path, fixture))
    conn = initialize_database(config)

    stats = import_lastfm_csv(conn, fixture)

    track_rows = conn.execute(
        """
        SELECT a.canonical_name, t.canonical_title, t.track_key, t.musicbrainz_recording_mbid,
               t.resolution_status, t.resolution_confidence
        FROM track t
        JOIN artist a ON a.artist_id = t.artist_id
        ORDER BY a.canonical_name, t.canonical_title
        """
    ).fetchall()

    assert stats.distinct_tracks == 5
    assert track_rows == [
        ("Cocteau Twins", "Cherry-coloured Funk", "cherry coloured funk", None, "provisional_name", 0.0),
        ("Pixies", "Debaser", "debaser", None, "provisional_name", 0.0),
        ("Slowdive", "Alison", "alison", None, "provisional_name", 0.0),
        ("The Cure", "Boys Don't Cry", "boys don t cry", None, "provisional_name", 0.0),
        ("The Cure", "Pictures of You", "pictures of you", None, "provisional_name", 0.0),
    ]

    conn.close()


def test_import_does_not_trust_conflicting_track_mbids(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "conflicting_track_mbid.csv",
        [
            "1704067200,2024-01-01 00:00:00,The Cure,mbid-cure,"
            "Disintegration,,Pictures of You,recording-a",
            "1704153600,2024-01-02 00:00:00,The Cure,mbid-cure,"
            "Disintegration,,Pictures of You,recording-b",
        ],
    )
    config = load_config(write_config(tmp_path, csv_path))
    conn = initialize_database(config)

    stats = import_lastfm_csv(conn, csv_path)

    track_row = conn.execute(
        """
        SELECT canonical_title, musicbrainz_recording_mbid, resolution_status, resolution_confidence
        FROM track
        """
    ).fetchone()
    warnings = conn.execute(
        """
        SELECT warning_code, message
        FROM import_warning
        ORDER BY warning_id
        """
    ).fetchall()

    assert stats.import_warnings == 1
    assert track_row == ("Pictures of You", None, "provisional_mbid_conflict", 0.0)
    assert warnings == [
        ("track_mbid_conflict", "artist_key and track_key have multiple supplied track MBIDs")
    ]

    conn.close()
```

- [ ] **Step 2: Run track tests to verify they fail**

Run:

```bash
python -m pytest tests/test_import.py::test_import_creates_provisional_track_entities tests/test_import.py::test_import_does_not_trust_conflicting_track_mbids -v
```

Expected:

```text
AssertionError
```

- [ ] **Step 3: Implement track warning and insert functions**

Add conflict warning insert:

```python
def _insert_track_mbid_conflict_warnings(conn: DuckDBPyConnection) -> None:
    next_id = _next_id(conn, "import_warning", "warning_id")
    conn.execute(
        """
        INSERT INTO import_warning (
            warning_id,
            import_row_number,
            warning_code,
            severity,
            message,
            evidence
        )
        WITH conflicts AS (
            SELECT
                artist_key,
                track_key,
                min(scrobble_id) AS first_scrobble_id,
                list_sort(list_distinct(list(NULLIF(trim(track_mbid_raw), '')))) AS mbids
            FROM scrobble
            WHERE artist_key <> ''
              AND track_key <> ''
              AND NOT is_duplicate
              AND NULLIF(trim(track_mbid_raw), '') IS NOT NULL
            GROUP BY artist_key, track_key
            HAVING count(DISTINCT NULLIF(trim(track_mbid_raw), '')) > 1
        )
        SELECT
            ? + row_number() OVER (ORDER BY artist_key, track_key) - 1 AS warning_id,
            first_scrobble_id AS import_row_number,
            'track_mbid_conflict' AS warning_code,
            'warning' AS severity,
            'artist_key and track_key have multiple supplied track MBIDs' AS message,
            json_object(
                'artist_key', artist_key,
                'track_key', track_key,
                'track_mbids', mbids
            ) AS evidence
        FROM conflicts
        """,
        [next_id],
    )
```

Add `_insert_provisional_tracks()`:

```python
def _insert_provisional_tracks(conn: DuckDBPyConnection) -> None:
    next_id = _next_id(conn, "track", "track_id")
    conn.execute(
        """
        INSERT INTO track (
            track_id,
            artist_id,
            canonical_title,
            track_key,
            musicbrainz_recording_mbid,
            lastfm_track_name,
            resolution_status,
            resolution_confidence
        )
        WITH title_counts AS (
            SELECT
                s.artist_key,
                s.track_key,
                s.track_raw,
                count(*) AS plays,
                min(s.scrobble_id) AS first_scrobble_id
            FROM scrobble s
            WHERE s.artist_key <> ''
              AND s.track_key <> ''
              AND NOT s.is_duplicate
            GROUP BY s.artist_key, s.track_key, s.track_raw
        ),
        preferred_titles AS (
            SELECT artist_key, track_key, track_raw AS canonical_title
            FROM (
                SELECT
                    artist_key,
                    track_key,
                    track_raw,
                    row_number() OVER (
                        PARTITION BY artist_key, track_key
                        ORDER BY plays DESC, first_scrobble_id
                    ) AS row_number
                FROM title_counts
            ) ranked_titles
            WHERE row_number = 1
        ),
        mbids AS (
            SELECT
                artist_key,
                track_key,
                count(DISTINCT NULLIF(trim(track_mbid_raw), '')) AS mbid_count,
                min(NULLIF(trim(track_mbid_raw), '')) AS track_mbid
            FROM scrobble
            WHERE artist_key <> ''
              AND track_key <> ''
              AND NOT is_duplicate
            GROUP BY artist_key, track_key
        )
        SELECT
            ? + row_number() OVER (ORDER BY a.artist_id, titles.track_key) - 1 AS track_id,
            a.artist_id,
            titles.canonical_title,
            titles.track_key,
            CASE WHEN mbids.mbid_count = 1 THEN mbids.track_mbid ELSE NULL END,
            titles.canonical_title AS lastfm_track_name,
            CASE
                WHEN mbids.mbid_count = 1 THEN 'provisional_mbid'
                WHEN mbids.mbid_count > 1 THEN 'provisional_mbid_conflict'
                ELSE 'provisional_name'
            END AS resolution_status,
            CASE WHEN mbids.mbid_count = 1 THEN 0.5 ELSE 0.0 END AS resolution_confidence
        FROM preferred_titles titles
        JOIN artist a ON a.artist_key = titles.artist_key
        JOIN mbids
          ON mbids.artist_key = titles.artist_key
         AND mbids.track_key = titles.track_key
        WHERE NOT EXISTS (
            SELECT 1
            FROM track existing
            WHERE existing.artist_id = a.artist_id
              AND existing.track_key = titles.track_key
        )
        """,
        [next_id],
    )
```

Update `import_lastfm_csv()` order:

```python
            _insert_scrobbles(conn)
            _insert_artist_mbid_conflict_warnings(conn)
            _insert_track_mbid_conflict_warnings(conn)
            _insert_import_warnings(conn)
            _insert_provisional_artists(conn)
            _insert_provisional_tracks(conn)
            _rebuild_artist_listening_facts(conn)
```

Keep `_replace_import_tables()` deleting `track` before `artist`.

- [ ] **Step 4: Run track tests to verify they pass**

Run:

```bash
python -m pytest tests/test_import.py::test_import_creates_provisional_track_entities tests/test_import.py::test_import_does_not_trust_conflicting_track_mbids -v
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Run import test file**

Run:

```bash
python -m pytest tests/test_import.py -v
```

Expected:

```text
all import tests pass
```

- [ ] **Step 6: Commit track entities**

Run:

```bash
git add src/scrobble_recs/importers/lastfm_csv.py tests/test_import.py docs/superpowers/plans/2026-06-06-import-local-entity-completeness.md
git commit -m "feat: create provisional track entities on import"
```

---

### Task 7: Doctor, README, Implementation Log, And Verification

**Files:**
- Modify: `src/scrobble_recs/admin/doctor.py`
- Modify: `tests/test_admin_doctor.py`
- Modify: `README.md`
- Modify: `docs/implementation-log.md`

- [ ] **Step 1: Add failing doctor warning test**

Append to `tests/test_admin_doctor.py`:

```python
def test_run_doctor_reports_import_warnings(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    conn = initialize_database(config)
    try:
        conn.execute(
            """
            INSERT INTO import_warning (
                warning_id,
                import_row_number,
                warning_code,
                severity,
                message,
                evidence
            )
            VALUES (
                1,
                2,
                'timestamp_mismatch',
                'warning',
                'utc_time does not match uts',
                '{}'
            )
            """
        )
        result = run_doctor(config, conn)
    finally:
        conn.close()

    checks = {check.name: check for check in result.checks}
    assert checks["import_warnings"].status is DiagnosticStatus.WARN
    assert checks["import_warnings"].message == "warnings=1"
```

- [ ] **Step 2: Run doctor test to verify it fails**

Run:

```bash
python -m pytest tests/test_admin_doctor.py::test_run_doctor_reports_import_warnings -v
```

Expected:

```text
KeyError: 'import_warnings'
```

- [ ] **Step 3: Implement doctor warning check**

Modify `run_doctor()` checks list:

```python
    checks = [
        _config_check(config),
        _database_check(config),
        _csv_check(config.input.lastfm_csv),
        _cache_path_check(config.app.cache_dir),
        _lastfm_api_key_check(config),
        _rate_limit_check(config),
        _api_cache_check(conn, resolved_now),
        _import_stats_check(conn),
        _import_warnings_check(conn),
    ]
```

Add helper:

```python
def _import_warnings_check(conn: DuckDBPyConnection) -> DiagnosticCheck:
    warning_count = get_import_stats(conn).import_warnings
    if warning_count == 0:
        return DiagnosticCheck("import_warnings", DiagnosticStatus.OK, "warnings=0")
    return DiagnosticCheck(
        "import_warnings",
        DiagnosticStatus.WARN,
        f"warnings={warning_count}",
    )
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
python -m pytest tests/test_admin_doctor.py::test_run_doctor_reports_import_warnings tests/test_import.py tests/test_config.py -v
```

Expected:

```text
all selected tests pass
```

- [ ] **Step 5: Update README**

Add this section near the import command documentation:

```markdown
## Import Completeness

Import keeps both raw rows and cleaned local entities. Each distinct normalized
artist becomes a provisional `artist`, and each distinct normalized
artist/track pair becomes a provisional `track`. Supplied MusicBrainz IDs are
used only when they are consistent across matching local rows; conflicting
artist or track MBIDs are ignored for provisional identity and recorded as
local import warnings.

The importer rejects malformed CSV structure, missing `uts`, missing `artist`,
missing `track`, non-integer `uts`, duplicate header rows inside the file, and
impossible timestamps. Non-fatal data quality issues such as `utc_time`
mismatches, conflicting MBIDs, and configured near duplicates are stored in
DuckDB in `import_warning` and shown by `doctor`.

Near-duplicate marking is disabled by default. Enable the spec's 30-second
rule by adding this to the config input block:

```json
"near_duplicate_window_seconds": 30
```

Near duplicates are marked with `scrobble.is_duplicate = true`; rows are not
deleted from `raw_scrobble` or `scrobble`.
```

- [ ] **Step 6: Update implementation log**

Append to `docs/implementation-log.md`:

```markdown
## 2026-06-06: Import and local entity completeness

Changed:

- Added local `track` entities for distinct imported artist/track pairs.
- Added persisted `import_warning` rows for non-fatal import data quality issues.
- Added `input.near_duplicate_window_seconds` with near-duplicate marking disabled by default.
- Added configured near-duplicate marking for same artist and track within the configured window.
- Added timestamp mismatch warnings for `utc_time` values that disagree with `uts`.
- Changed provisional artist and track MBID assignment to require locally consistent supplied MBIDs.
- Added artist and track MBID conflict warnings.
- Updated doctor and CLI import stats to surface import warnings.

Verified:

- `python -m pytest tests/test_import.py tests/test_config.py tests/test_admin_doctor.py tests/test_cli.py -v`
- `python -m pytest`
- `python -m ruff check .`
- `python -m ruff format --check .`
- `python -m mypy src`
```

- [ ] **Step 7: Run focused verification**

Run:

```bash
python -m pytest tests/test_import.py tests/test_config.py tests/test_admin_doctor.py tests/test_cli.py -v
```

Expected:

```text
all selected tests pass
```

- [ ] **Step 8: Run full verification**

Run:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

Expected:

```text
pytest passes
ruff check reports All checks passed!
ruff format reports all files already formatted
mypy reports Success: no issues found
```

- [ ] **Step 9: Commit docs and verification**

Run:

```bash
git add src/scrobble_recs/admin/doctor.py tests/test_admin_doctor.py README.md docs/implementation-log.md docs/superpowers/plans/2026-06-06-import-local-entity-completeness.md
git commit -m "docs: record import entity completeness"
```

---

## Self Review

Spec coverage:

- Section 8.3 is covered by Task 1 and Task 6 through the `track` schema and provisional track import.
- Section 9.1 is covered by Task 3 and the existing importer behavior: structural CSV failures remain fatal, while timestamp mismatch is persisted as a warning.
- Section 9.2 is covered by Task 2 and Task 4 through `near_duplicate_window_seconds`, disabled-by-default behavior, duplicate marking, and warning persistence.
- Section 9.3 is covered by Task 5 and Task 6: artist and track MBIDs are only trusted when locally consistent, conflicts are warned, and unresolved local identities remain usable.
- Section 21 is covered by preserving `artist_listening_fact` rebuilds and adding track entities that make distinct track inspection local and deterministic.

Design consistency:

- The plan preserves the current set-based staging importer instead of returning to per-row DuckDB writes.
- The near-duplicate default is `0` so existing imports keep current behavior unless the user opts into the ambiguous 30-second rule.
- Warnings are persisted in DuckDB, which keeps doctor, CLI, and future TUI inspection local-first.
- `track` is derived after `artist`, so existing recommendation code that depends on `artist_id` remains stable.

Testing quality:

- Tests use temporary DuckDB files and deterministic CSV rows.
- Tests assert behavior: warning persistence, duplicate marking, MBID conflict handling, provisional entity rows, config validation, CLI dispatch, and doctor diagnostics.
- No test uses network access or real API responses.

Execution notes:

- The worktree currently has existing modified and added files from other work. Do not revert or stage unrelated changes while executing this plan.
- If another migration is added before this plan is executed, rename `006_import_entities.sql` to the next sortable migration number and update this plan before implementation.
