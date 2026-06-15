# Recommendation Quality Base Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add score bands, trial penalty, identity confidence, MusicBrainz resolve, and richer diagnostics to the recommendation pipeline without introducing track-based recs or serendipity.

**Architecture:** Extend existing tables via migration SQL files, enrich the scoring pipeline with new penalty/band/identity computations, build a MusicBrainz API client following the Last.fm client pattern, and surface all new fields in export/TUI/CLI.

**Tech Stack:** Python 3.11+, DuckDB, Pydantic, Typer, Textual, httpx, pytest

---

## File Map

**Create:**
- `src/scrobble_recs/sql/009_score_bands.sql`
- `src/scrobble_recs/sql/010_listening_diagnostics.sql`
- `src/scrobble_recs/sql/011_identity_fields.sql`
- `src/scrobble_recs/sql/012_identity_resolution.sql`
- `src/scrobble_recs/apis/musicbrainz.py`
- `tests/test_musicbrainz.py`

**Modify:**
- `src/scrobble_recs/models/recommend.py` — `_CandidateScore` dataclass, `_ScoringData` dataclass, `_load_scoring_data`, `_score_candidates`, `_rerank_for_diversity`, `_explanation`, `_persist_run`, `_classify_score_band` (new), `_compute_trial_penalty` (new), `_rediscovery_eligible` (new)
- `src/scrobble_recs/export.py` — `_load_rows`, `_write_csv`, `_write_jsonl`, `export_recommendations` signature
- `src/scrobble_recs/tui/data.py` — `RecommendationListRow`, `ScoreBreakdown`, `RecommendationDetail`, `RecommendationFilters`, `TuiRecommendationStore`, `_status_labels`, `_classification_label`
- `src/scrobble_recs/cli.py` — `export_command`, `resolve_command`, `doctor_command`
- `src/scrobble_recs/importers/lastfm_csv.py` — add `compute_identity_confidence` call after import
- `src/scrobble_recs/admin/doctor.py` — add musicbrainz resolution check
- `src/scrobble_recs/enrichment/queue.py` — accept `musicbrainz` source
- `tests/test_recommend.py` — score band, trial penalty, identity confidence tests
- `tests/test_export.py` — new columns, `--include-candidates` tests
- `tests/test_tui_data.py` — new fields on data classes
- `tests/test_cli.py` — resolve command, doctor, export flag tests
- `tests/test_import.py` — identity confidence after import

---

## Phase 1: Schema Migrations

### Task 1.1: Score bands migration

**Files:**
- Create: `src/scrobble_recs/sql/009_score_bands.sql`

- [ ] **Step 1: Create migration file**

```sql
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS score_band VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS evidence_source_count INTEGER DEFAULT 0;
```

- [ ] **Step 2: Run tests to verify migration applies cleanly**

Run: `python -m pytest tests/test_recommend.py -v -k "test_recommendation_migration"`

Expected: PASS — the existing migration application still works (IF NOT EXISTS ensures idempotency).

- [ ] **Step 3: Commit**

```bash
git add src/scrobble_recs/sql/009_score_bands.sql
git commit -m "feat: add score_band and evidence_source_count migration"
```

### Task 1.2: Listening diagnostics migration

**Files:**
- Create: `src/scrobble_recs/sql/010_listening_diagnostics.sql`

- [ ] **Step 1: Create migration file**

```sql
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS local_scrobble_count INTEGER;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS listened_day_count INTEGER;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS first_listened_at TIMESTAMP;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS last_listened_at TIMESTAMP;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS years_since_last_listen DOUBLE;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS rediscovery_eligible BOOLEAN;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS trial_penalty DOUBLE DEFAULT 0.0;
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_recommend.py -v -k "test_recommendation_migration"`

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/scrobble_recs/sql/010_listening_diagnostics.sql
git commit -m "feat: add listening diagnostics migration"
```

### Task 1.3: Identity fields migration

**Files:**
- Create: `src/scrobble_recs/sql/011_identity_fields.sql`

- [ ] **Step 1: Create migration file**

```sql
ALTER TABLE artist ADD COLUMN IF NOT EXISTS identity_confidence DOUBLE DEFAULT 0.0;
ALTER TABLE artist ADD COLUMN IF NOT EXISTS identity_warnings VARCHAR[];

ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS candidate_artist_name VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS canonical_artist_name VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS musicbrainz_artist_mbid VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS lastfm_url VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS musicbrainz_url VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS identity_confidence DOUBLE;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS identity_warnings VARCHAR[];
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_recommend.py -v -k "test_recommendation_migration"`

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/scrobble_recs/sql/011_identity_fields.sql
git commit -m "feat: add identity confidence fields migration"
```

---

## Phase 2: Score Bands and Export Gating (Deliverable 1)

### Task 2.1: Extend _CandidateScore and _ScoringData

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`

- [ ] **Step 1: Add new fields to _CandidateScore dataclass**

At `src/scrobble_recs/models/recommend.py:56-75`, add to `_CandidateScore`:

```python
@dataclass(frozen=True)
class _CandidateScore:
    artist_id: int
    artist_name: str
    final_score: float
    raw_score: float
    lastfm_similarity_score: float
    tag_similarity_score: float
    feedback_score: float
    feedback_contributions: dict[str, float]
    feedback_effects: list[str]
    rediscovery_score: float
    knownness: float
    obviousness: float
    knownness_penalty: float
    obviousness_penalty: float
    diversity_penalty: float
    primary_tag: str | None
    quality_gate: _QualityGateDecision
    fatigue: _FatiguePenalty
    explanation: dict[str, object]
    # NEW FIELDS
    score_band: str = "candidate_only"
    normalized_score: float = 0.0
    evidence_source_count: int = 0
    trial_penalty: float = 0.0
    local_scrobble_count: int = 0
    listened_day_count: int = 0
    first_listened_at: datetime | None = None
    last_listened_at: datetime | None = None
    years_since_last_listen: float = 0.0
    rediscovery_eligible: bool = False
    identity_confidence: float = 0.50
    identity_warnings: tuple[str, ...] = ()
```

The `from __future__ import annotations` at line 1 means `datetime | None` works. The `datetime` import at line 6 (`from datetime import UTC, datetime`) is already present.

- [ ] **Step 2: Commit**

```bash
git add src/scrobble_recs/models/recommend.py
git commit -m "feat: add new fields to _CandidateScore dataclass"
```

### Task 2.2: Add _classify_score_band function and test

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`
- Modify: `tests/test_recommend.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_recommend.py`:

```python
from scrobble_recs.models.recommend import _classify_score_band


class TestScoreBand:
    def test_strong_band(self) -> None:
        result = _classify_score_band(normalized=0.55, raw=0.3, evidence_count=3)
        assert result == "strong"

    def test_plausible_band(self) -> None:
        result = _classify_score_band(normalized=0.12, raw=0.1, evidence_count=1)
        assert result == "plausible"

    def test_plausible_needs_evidence(self) -> None:
        result = _classify_score_band(normalized=0.12, raw=0.1, evidence_count=0)
        assert result == "weak"

    def test_weak_band(self) -> None:
        result = _classify_score_band(normalized=0.05, raw=0.05, evidence_count=1)
        assert result == "weak"

    def test_candidate_only_zero_raw(self) -> None:
        result = _classify_score_band(normalized=0.8, raw=0.0, evidence_count=3)
        assert result == "candidate_only"

    def test_candidate_only_zero_normalized(self) -> None:
        result = _classify_score_band(normalized=0.0, raw=0.3, evidence_count=3)
        assert result == "candidate_only"

    def test_strong_needs_two_sources(self) -> None:
        result = _classify_score_band(normalized=0.55, raw=0.3, evidence_count=1)
        assert result == "plausible"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_recommend.py::TestScoreBand -v`

Expected: FAIL with `AttributeError: module 'scrobble_recs.models.recommend' has no attribute '_classify_score_band'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/scrobble_recs/models/recommend.py` (after `_rank_penalty` at line ~1358):

```python
def _classify_score_band(*, normalized: float, raw: float, evidence_count: int) -> str:
    if raw <= 0.0 or normalized <= 0.0:
        return "candidate_only"
    if normalized >= 0.50 and evidence_count >= 2:
        return "strong"
    if normalized >= 0.10 and evidence_count >= 1:
        return "plausible"
    return "weak"
```

Note: Export from module — add `_classify_score_band` to `__all__` or just define as a module-level function (tests import it directly).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_recommend.py::TestScoreBand -v`

Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add src/scrobble_recs/models/recommend.py tests/test_recommend.py
git commit -m "feat: add _classify_score_band function"
```

### Task 2.3: Compute evidence_source_count and score_band during scoring

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`

- [ ] **Step 1: Update _score_candidates to compute evidence_source_count and score_band**

In `_score_candidates` (line ~697), update the `_CandidateScore(...)` construction call to include the new fields. The current code at lines 697-718 constructs the candidate. After computing `final_score` and before constructing `_CandidateScore`, add:

```python
evidence_count = sum(
    1 for val in (lastfm_score, tag_score, feedback.score, rediscovery)
    if val > 0.0
)
```

Then add to the constructor call:
```python
evidence_source_count=evidence_count,
```

The `score_band` and `normalized_score` need to be computed after we know the max score for the run, which happens in `_rerank_for_diversity`. So `score_band` and `normalized_score` are set after diversity reranking.

In `generate_recommendations` (line ~232-238), after `_rerank_for_diversity` returns `ranked_without_explanations`, compute the max final score and apply score bands:

```python
max_final = max((s.final_score for s in ranked_without_explanations), default=0.0)
banded = [
    replace(
        score,
        normalized_score=_display_score(score.final_score, max_final),
        score_band=_classify_score_band(
            normalized=_display_score(score.final_score, max_final),
            raw=score.raw_score,
            evidence_count=score.evidence_source_count,
        ),
    )
    for score in ranked_without_explanations
]
```

And use `banded` instead of `ranked_without_explanations` in the subsequent `_explanation` loop and `_persist_run` call.

- [ ] **Step 2: Ensure zero-score candidates are persisted with candidate_only band**

In `_score_candidates`, change the skip at line 695:

```python
# Before:
if final_score <= 0:
    continue

# After: still skip for the ranking flow
if final_score <= 0:
    continue
```

No — per the spec, zero-score candidates should be persisted as candidate_only. Change this to collect them separately:

Add a list at the top of `_score_candidates`:
```python
candidate_only_scores: list[_CandidateScore] = []
```

Change the `if final_score <= 0: continue` to:
```python
if final_score <= 0:
    candidate_only_scores.append(
        _CandidateScore(
            artist_id=artist_id,
            artist_name=artist_name,
            final_score=0.0,
            raw_score=raw_score,
            lastfm_similarity_score=lastfm_score,
            tag_similarity_score=tag_score,
            feedback_score=feedback.score,
            feedback_contributions=feedback.contributions,
            feedback_effects=feedback.effects,
            rediscovery_score=rediscovery,
            knownness=knownness,
            obviousness=obviousness,
            knownness_penalty=knownness_penalty,
            obviousness_penalty=obviousness_penalty,
            diversity_penalty=0.0,
            primary_tag=_primary_tag(artist_id, tag_profile, scoring_data),
            quality_gate=quality_gate,
            fatigue=fatigue,
            explanation={},
            score_band="candidate_only",
            normalized_score=0.0,
            evidence_source_count=evidence_count,
            trial_penalty=0.0,
            local_scrobble_count=int(scoring_data.listening_by_artist.get(artist_id, (0.0, 0.0, 0, 0, None, None))[2]),
            listened_day_count=int(scoring_data.listening_by_artist.get(artist_id, (0.0, 0.0, 0, 0, None, None))[3]),
            first_listened_at=scoring_data.listening_by_artist.get(artist_id, (0.0, 0.0, 0, 0, None, None))[4],
            last_listened_at=scoring_data.listening_by_artist.get(artist_id, (0.0, 0.0, 0, 0, None, None))[5],
            years_since_last_listen=0.0,
            rediscovery_eligible=False,
            identity_confidence=0.50,
            identity_warnings=(),
        )
    )
    continue
```

Return `scores + candidate_only_scores` at the end.

But wait — this would flood the result with thousands of candidate_only rows. The spec says "Default export/TUI behavior should hide candidate_only unless an explicit debug/include flag is set." We should impose a reasonable limit on candidate_only rows. Let's cap at 500 (same as default run_limit) or a proportion of scored candidates.

Add a limit at the return:
```python
candidate_only_scores.sort(key=lambda s: s.raw_score, reverse=True)
return scores + candidate_only_scores[:min(len(scores) * 2, 500)]
```

- [ ] **Step 3: Run existing tests to verify no regressions**

Run: `python -m pytest tests/test_recommend.py -v -k "not TestScoreBand"`

Expected: All existing tests PASS

- [ ] **Step 4: Commit**

```bash
git add src/scrobble_recs/models/recommend.py
git commit -m "feat: compute score_band, normalized_score, and persist candidate_only rows"
```

### Task 2.4: Update _persist_run to write new columns

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`

- [ ] **Step 1: Update _persist_run INSERT statement**

In `_persist_run` (line ~1297-1335), update the INSERT to include new columns. The current INSERT is:

```sql
INSERT INTO recommendation (
    run_id, rank, artist_id,
    final_score, raw_score, lastfm_similarity_score,
    tag_similarity_score, cluster_fit_score,
    rediscovery_score, metadata_graph_score,
    feedback_score, knownness, obviousness,
    diversity_penalty, explanation
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

Change to:

```sql
INSERT INTO recommendation (
    run_id, rank, artist_id,
    final_score, raw_score, lastfm_similarity_score,
    tag_similarity_score, cluster_fit_score,
    rediscovery_score, metadata_graph_score,
    feedback_score, knownness, obviousness,
    diversity_penalty, explanation,
    score_band, evidence_source_count,
    local_scrobble_count, listened_day_count,
    first_listened_at, last_listened_at,
    years_since_last_listen, rediscovery_eligible,
    trial_penalty, candidate_artist_name,
    canonical_artist_name, musicbrainz_artist_mbid,
    lastfm_url, musicbrainz_url,
    identity_confidence, identity_warnings
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

Update the params list to match. Use sentinel values for fields not yet computed (identity, trial_penalty, listening diagnostics) — they'll be filled in by later tasks.

For now, set defaults:
```python
score_band = getattr(score, 'score_band', 'plausible')  # computed in Phase 2
evidence_source_count = getattr(score, 'evidence_source_count', 0)
local_scrobble_count = getattr(score, 'local_scrobble_count', 0)
listened_day_count = getattr(score, 'listened_day_count', 0)
first_listened_at = getattr(score, 'first_listened_at', None)
last_listened_at = getattr(score, 'last_listened_at', None)
years_since_last_listen = getattr(score, 'years_since_last_listen', 0.0)
rediscovery_eligible = getattr(score, 'rediscovery_eligible', False)
trial_penalty = getattr(score, 'trial_penalty', 0.0)
candidate_artist_name = score.artist_name
canonical_artist_name = score.artist_name  # Will be improved in Phase 4
musicbrainz_artist_mbid_val = ""  # Will be populated in Phase 4
lastfm_url_val = f"https://www.last.fm/music/{quote(score.artist_name, safe='')}"
musicbrainz_url_val = ""  # Will be populated in Phase 4
identity_confidence_val = 0.50  # Will be populated in Phase 4
identity_warnings_val = []  # Will be populated in Phase 4
```

Add `from urllib.parse import quote` at the top of the file.

For `candidate_only` rows with `rank=999999`:
```python
rank_value = 999999 if score.score_band == "candidate_only" else rank
```

The `rank` loop at line ~1296 uses `for rank, score in enumerate(ranked, start=1)`. Candidate_only scores should not interfere with normal rank numbering. Instead, persist scored candidates with normal ranks (1..N), then append candidate_only rows with sentinel ranks.

Actually, let's keep it simpler: candidate_only scores should have `rank = None` (NULL in SQL). In `_persist_run`, handle two categories:

```python
for rank, score in enumerate(ranked, start=1):
    # ... existing insert with rank
# After the main loop
for i, score in enumerate(candidate_only_scores, start=1):
    # Insert with rank = 999000 + i (sentinel, sorts after regular recs)
```

Wait — the `ranked` list passed to `_persist_run` currently only contains scored candidates (from `_rerank_for_diversity`). We need to also pass or separately handle candidate_only scores. The simplest approach: append candidate_only after diversity rerank and before explanations.

In `generate_recommendations`, after `_rerank_for_diversity` returns and we build banded scores, append the candidate_only scores. But candidate_only scores aren't in `ranked_without_explanations` — they're in a separate list from `_score_candidates`.

Let me adjust: have `_score_candidates` return both lists, or append in `generate_recommendations`.

Simplest approach: In `_score_candidates`, return `(scored, candidate_only)` tuple. In `generate_recommendations`, store the candidate_only list, and pass it to `_persist_run` separately.

Change `_score_candidates` return type from `list[_CandidateScore]` to `tuple[list[_CandidateScore], list[_CandidateScore]]`.

Actually, that's a big signature change. Let me instead modify `generate_recommendations` to separately track candidate_only:

In `_score_candidates`, just append them to the scores list (don't filter), but mark them. In `_rerank_for_diversity`, they'll sort to the bottom because final_score=0. Then in `_persist_run`, handle rank specially for candidate_only.

Simplest: just let them go through the whole pipeline. Rerank will put them last. Give them rank accordingly (they get ranks after the last real recommendation).

OK, let me not overthink this. The spec says rank=999999 sentinel. Let me just do that in `_persist_run`:

```python
for rank, score in enumerate(ranked, start=1):
    effective_rank = 999999 if score.score_band == "candidate_only" else rank
    # ... insert with effective_rank
```

This is the cleanest approach. Done.

- [ ] **Step 2: Run tests to verify persistence works**

Run: `python -m pytest tests/test_recommend.py -v -x`

Expected: All existing tests PASS (the new columns have defaults so old INSERT paths still work; but wait — we changed the INSERT to require more params. Existing tests that directly INSERT into recommendation will break. Let me handle that.

Actually, looking at existing tests like `test_export.py::_insert_export_row`, they do direct INSERT. Those tests won't run against the new schema unless we update their INSERT statements. We'll handle that in the export task.

For now, just make sure the pipeline tests pass.

Run: `python -m pytest tests/test_recommend.py::test_generate_recommendations_basic -v`

- [ ] **Step 3: Commit**

```bash
git add src/scrobble_recs/models/recommend.py
git commit -m "feat: persist score_band and new columns to recommendation table"
```

### Task 2.5: Update export with new columns and --include-candidates flag

**Files:**
- Modify: `src/scrobble_recs/export.py`
- Modify: `src/scrobble_recs/cli.py`
- Modify: `tests/test_export.py`

- [ ] **Step 1: Update export_recommendations to accept include_candidates**

Change `export_recommendations` signature:

```python
def export_recommendations(
    conn: DuckDBPyConnection,
    output_path: Path,
    *,
    format_name: str,
    run_id: str | None = None,
    include_candidates: bool = False,
) -> int:
```

Pass `include_candidates` to `_load_rows`.

- [ ] **Step 2: Update _load_rows to filter by score_band and read new columns**

```python
def _load_rows(conn: DuckDBPyConnection, run_id: str, *, include_candidates: bool = False) -> list[dict[str, object]]:
    where_clause = "" if include_candidates else "AND r.score_band != 'candidate_only'"
    rows = conn.execute(
        f"""
        SELECT
            r.rank,
            a.canonical_name,
            r.final_score,
            max(r.final_score) OVER () AS max_final_score,
            r.raw_score,
            r.score_band,
            r.evidence_source_count,
            r.knownness,
            r.obviousness,
            r.rediscovery_score,
            r.local_scrobble_count,
            r.listened_day_count,
            r.first_listened_at,
            r.last_listened_at,
            r.years_since_last_listen,
            r.rediscovery_eligible,
            r.trial_penalty,
            r.candidate_artist_name,
            r.musicbrainz_artist_mbid,
            r.lastfm_url,
            r.musicbrainz_url,
            r.identity_confidence,
            r.identity_warnings,
            r.lastfm_similarity_score,
            r.tag_similarity_score,
            r.feedback_score,
            r.explanation
        FROM recommendation r
        JOIN artist a ON a.artist_id = r.artist_id
        WHERE r.run_id = ?
          {where_clause}
        ORDER BY r.rank
        """,
        [run_id],
    ).fetchall()
    # ... build result dicts with all fields
```

Build the result dict with all new fields. Include `explanation_structured` as the full parsed JSON.

- [ ] **Step 3: Update _write_csv with new fieldnames**

```python
def _write_csv(output_path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "rank", "artist", "score_normalized", "score_raw", "score_band",
        "evidence_source_count", "primary_tags", "knownness", "obviousness",
        "is_rediscovery", "rediscovery_eligible", "why",
        "lastfm_url", "musicbrainz_url",
        "local_scrobble_count", "listened_day_count", "first_listened_at",
        "last_listened_at", "years_since_last_listen", "trial_penalty",
        "candidate_artist_name", "canonical_artist_name",
        "musicbrainz_artist_mbid", "identity_confidence", "identity_warnings",
        "top_seed_artists", "top_seed_edge_scores", "matching_tags",
        "tag_fit_score", "lastfm_similarity_score", "rediscovery_score",
        "feedback_score", "knownness_penalty", "obviousness_penalty",
        "fatigue_penalty", "candidate_sources",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
```

- [ ] **Step 4: Update _write_jsonl to include all fields**

JSONL already writes `json.dumps(row, sort_keys=True)`, so it naturally includes all new keys.

- [ ] **Step 5: Update export CLI command**

In `cli.py` line 466-496, add `--include-candidates`:

```python
@app.command("export")
def export_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Output file path.")],
    format_name: Annotated[
        str,
        typer.Option("--format", help="Export format: csv or jsonl."),
    ] = "csv",
    run_id: Annotated[str | None, typer.Option("--run-id", help="Recommendation run UUID.")] = None,
    include_candidates: Annotated[
        bool,
        typer.Option("--include-candidates", help="Include candidate_only (zero-score) rows."),
    ] = False,
) -> None:
    # ... existing validation ...
    exported = export_recommendations(
        conn,
        output,
        format_name=resolved_format,
        run_id=run_id,
        include_candidates=include_candidates,
    )
    console.print(f"Exported {exported} recommendations to {output}")
```

- [ ] **Step 6: Update existing export tests for new schema**

Update `test_export.py::_insert_export_row` to include the new columns in its INSERT statement. Add defaults or explicit values.

- [ ] **Step 7: Write test for --include-candidates**

```python
def test_export_includes_candidate_only_with_flag(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000020"
    created_at = datetime(2026, 6, 4, 12, 0, 0)
    # Insert recommendation_run
    conn.execute(
        "INSERT INTO recommendation_run (run_id, config_hash, mode, seed_summary, scoring_weights) "
        "VALUES (?, 'hash', 'balanced', '{}', '{}')",
        [run_id, created_at],
    )
    # Insert artist
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key) VALUES (701, 'Good Artist', 'good artist')",
    )
    # Insert scored recommendation
    conn.execute(
        """INSERT INTO recommendation (
            run_id, rank, artist_id, final_score, raw_score,
            lastfm_similarity_score, tag_similarity_score, cluster_fit_score,
            rediscovery_score, metadata_graph_score, feedback_score,
            knownness, obviousness, diversity_penalty, explanation,
            score_band, evidence_source_count
        ) VALUES (?, 1, 701, 0.9, 0.8, 0.7, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ?, 'strong', 2)""",
        [run_id, json.dumps({"summary": "Good"})],
    )
    # Insert candidate_only row
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key) VALUES (702, 'Weak Artist', 'weak artist')",
    )
    conn.execute(
        """INSERT INTO recommendation (
            run_id, rank, artist_id, final_score, raw_score,
            lastfm_similarity_score, tag_similarity_score, cluster_fit_score,
            rediscovery_score, metadata_graph_score, feedback_score,
            knownness, obviousness, diversity_penalty, explanation,
            score_band, evidence_source_count
        ) VALUES (?, 999999, 702, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ?, 'candidate_only', 0)""",
        [run_id, json.dumps({"summary": "Weak"})],
    )

    # Default export excludes candidate_only
    output_default = tmp_path / "default.jsonl"
    export_recommendations(conn, output_default, format_name="jsonl", run_id=run_id)
    lines_default = output_default.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines_default) == 1
    assert "Good Artist" in lines_default[0]

    # With flag includes candidate_only
    output_full = tmp_path / "full.jsonl"
    export_recommendations(conn, output_full, format_name="jsonl", run_id=run_id, include_candidates=True)
    lines_full = output_full.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines_full) == 2
    assert "Weak Artist" in output_full.read_text(encoding="utf-8")

    conn.close()
```

- [ ] **Step 8: Run tests**

Run: `python -m pytest tests/test_export.py -v`

Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add src/scrobble_recs/export.py src/scrobble_recs/cli.py tests/test_export.py
git commit -m "feat: add new export columns and --include-candidates flag"
```

---

## Phase 3: Trial Penalty and Rediscovery Eligibility (Deliverable 2)

### Task 3.1: Extend _load_scoring_data to carry listening diagnostics

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`

- [ ] **Step 1: Update _load_scoring_data to include play_count, active_days, timestamps**

Change `listening_by_artist` dict type from:
```python
dict[int, tuple[float, float]]
```
to:
```python
dict[int, tuple[float, float, int, int, datetime | None, datetime | None]]
```

Update the query in `_load_scoring_data` (line ~502-513):

```python
listening_by_artist = {
    int(artist_id): (
        float(knownness),
        float(rediscovery_score),
        int(play_count or 0),
        int(active_days or 0),
        first_played_at,
        last_played_at,
    )
    for artist_id, knownness, rediscovery_score, play_count, active_days, first_played_at, last_played_at in conn.execute(
        """
        SELECT
            artist_id,
            coalesce(knownness, 0.0),
            coalesce(rediscovery_score, 0.0),
            coalesce(play_count, 0),
            coalesce(active_days, 0),
            first_played_at,
            last_played_at
        FROM artist_listening_fact
        """
    ).fetchall()
}
```

- [ ] **Step 2: Update _ScoringData docstring/comment to reflect new tuple shape**

No changes to the dataclass — it's a dict, so the type annotation alone documents the change.

- [ ] **Step 3: Commit**

```bash
git add src/scrobble_recs/models/recommend.py
git commit -m "feat: extend _load_scoring_data with listening diagnostics"
```

### Task 3.2: Add trial_penalty and rediscovery_eligible computation

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`
- Modify: `tests/test_recommend.py`

- [ ] **Step 1: Write tests for _compute_trial_penalty and _rediscovery_eligible**

Add to `tests/test_recommend.py`:

```python
from scrobble_recs.models.recommend import _compute_trial_penalty, _is_rediscovery_eligible


class TestTrialPenalty:
    def test_single_scrobble_single_day_old(self) -> None:
        result = _compute_trial_penalty(play_count=1, active_days=1, years_since_last_listen=3.0)
        assert result == pytest.approx(0.35)

    def test_two_scrobbles_single_day_old(self) -> None:
        result = _compute_trial_penalty(play_count=2, active_days=1, years_since_last_listen=3.0)
        assert result == pytest.approx(0.20)

    def test_single_scrobble_recent_no_penalty(self) -> None:
        result = _compute_trial_penalty(play_count=1, active_days=1, years_since_last_listen=1.0)
        assert result == 0.0

    def test_multi_scrobble_no_penalty(self) -> None:
        result = _compute_trial_penalty(play_count=3, active_days=1, years_since_last_listen=3.0)
        assert result == 0.0

    def test_single_scrobble_multi_day_no_penalty(self) -> None:
        result = _compute_trial_penalty(play_count=1, active_days=2, years_since_last_listen=3.0)
        assert result == 0.0

    def test_five_scrobbles_no_penalty(self) -> None:
        result = _compute_trial_penalty(play_count=5, active_days=3, years_since_last_listen=5.0)
        assert result == 0.0


class TestRediscoveryEligible:
    def test_eligible_by_play_count(self) -> None:
        assert _is_rediscovery_eligible(play_count=3, active_days=1, has_positive_feedback=False)

    def test_eligible_by_active_days(self) -> None:
        assert _is_rediscovery_eligible(play_count=1, active_days=2, has_positive_feedback=False)

    def test_eligible_by_feedback(self) -> None:
        assert _is_rediscovery_eligible(play_count=1, active_days=1, has_positive_feedback=True)

    def test_not_eligible_single_scrobble_single_day(self) -> None:
        assert not _is_rediscovery_eligible(play_count=1, active_days=1, has_positive_feedback=False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_recommend.py::TestTrialPenalty tests/test_recommend.py::TestRediscoveryEligible -v`

Expected: FAIL — functions not defined

- [ ] **Step 3: Implement functions**

Add to `src/scrobble_recs/models/recommend.py`:

```python
def _compute_trial_penalty(*, play_count: int, active_days: int, years_since_last_listen: float) -> float:
    if years_since_last_listen < 2.0:
        return 0.0
    if play_count == 1 and active_days <= 1:
        return 0.35
    if play_count == 2 and active_days <= 1:
        return 0.20
    return 0.0


def _is_rediscovery_eligible(*, play_count: int, active_days: int, has_positive_feedback: bool) -> bool:
    return play_count >= 3 or active_days >= 2 or has_positive_feedback
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_recommend.py::TestTrialPenalty tests/test_recommend.py::TestRediscoveryEligible -v`

Expected: 10 PASS

- [ ] **Step 5: Commit**

```bash
git add src/scrobble_recs/models/recommend.py tests/test_recommend.py
git commit -m "feat: add _compute_trial_penalty and _is_rediscovery_eligible"
```

### Task 3.3: Integrate trial penalty into _score_candidates

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`

- [ ] **Step 1: Compute trial penalty and rediscovery eligible in _score_candidates**

In `_score_candidates`, after extracting `knownness, rediscovery = scoring_data.listening_by_artist.get(artist_id, (0.0, 0.0))`, extract the listening diagnostics:

```python
listening = scoring_data.listening_by_artist.get(artist_id, (0.0, 0.0, 0, 0, None, None))
knownness, rediscovery, local_play_count, local_active_days, first_played, last_played = listening
```

Compute `years_since_last_listen`:
```python
years_since_last_listen = 0.0
if resolved_as_of is not None and last_played is not None:
    years_since_last_listen = max(0.0, (resolved_as_of - last_played).days / 365.25)
```

Wait — `resolved_as_of` is available in `_score_candidates`? Let me check... Currently `_score_candidates` doesn't receive `as_of`. It receives `config`, `mode`, `seeds`, `seed_ids`, `tag_profile`, `scoring_data`, `feedback_profile`, `fatigue_by_artist`, `progress_callback`. We need to pass `as_of` through.

Add `as_of: datetime` parameter to `_score_candidates` signature. In `generate_recommendations`, pass it:

```python
scores = _score_candidates(
    config, mode_settings, seeds, seed_ids,
    effective_tag_profile, scoring_data,
    feedback_profile, fatigue_by_artist,
    as_of=resolved_as_of,
    progress_callback=progress_callback,
)
```

Now compute:
```python
has_positive_feedback = artist_id in feedback_profile.boosted_seed_artist_ids
trial_penalty = _compute_trial_penalty(
    play_count=local_play_count,
    active_days=local_active_days,
    years_since_last_listen=years_since_last_listen,
)
rediscovery_eligible = _is_rediscovery_eligible(
    play_count=local_play_count,
    active_days=local_active_days,
    has_positive_feedback=has_positive_feedback,
)
```

Apply rediscovery mode filter:
```python
# In rediscovery mode, skip candidates that are ineligible for rediscovery
if mode.name == "rediscovery" and rediscovery > 0.0 and not rediscovery_eligible:
    continue
```

Apply trial penalty to final_score (balanced mode):
```python
if mode.name == "balanced":
    final_score -= trial_penalty
```

Note: the trial_penalty subtraction should happen BEFORE the `if final_score <= 0` check so trial-penalized candidates can fall to candidate_only.

- [ ] **Step 2: Add new fields to _CandidateScore construction in _score_candidates**

Update the `_CandidateScore(...)` call to include:
```python
trial_penalty=trial_penalty,
local_scrobble_count=local_play_count,
listened_day_count=local_active_days,
first_listened_at=first_played,
last_listened_at=last_played,
years_since_last_listen=years_since_last_listen,
rediscovery_eligible=rediscovery_eligible,
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_recommend.py -v -x`

Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add src/scrobble_recs/models/recommend.py
git commit -m "feat: integrate trial penalty and rediscovery eligibility into scoring"
```

---

## Phase 4: Identity Confidence (Deliverable 3)

### Task 4.1: Implement compute_identity_confidence

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py` (or new module)
- Modify: `tests/test_recommend.py`

- [ ] **Step 1: Write tests**

Add to `tests/test_recommend.py`:

```python
from scrobble_recs.models.recommend import compute_identity_confidence


class TestIdentityConfidence:
    def test_full_confidence_with_mbid(self) -> None:
        conf, warnings = compute_identity_confidence(
            artist_name="Radiohead",
            mbid="a74b1b7f-71a5-4011-9441-d0b5e4122711",
            resolution_status="provisional_mbid",
            lastfm_enrichment_mbid="a74b1b7f-71a5-4011-9441-d0b5e4122711",
            lastfm_url_from_enrichment=None,
            canonical_name_match=True,
        )
        assert conf == pytest.approx(1.0)  # 0.50 + 0.30 + 0.15 + 0.10 = 1.05, clamped to 1.0
        assert "missing_musicbrainz_mbid" not in warnings

    def test_missing_mbid_warning(self) -> None:
        conf, warnings = compute_identity_confidence(
            artist_name="Radiohead",
            mbid=None,
            resolution_status="provisional_name",
            lastfm_enrichment_mbid=None,
            lastfm_url_from_enrichment=None,
            canonical_name_match=True,
        )
        assert conf == pytest.approx(0.60)  # 0.50 + 0.10
        assert "missing_musicbrainz_mbid" in warnings

    def test_short_generic_name_penalty(self) -> None:
        conf, warnings = compute_identity_confidence(
            artist_name="Bauhaus",
            mbid=None,
            resolution_status="provisional_name",
            lastfm_enrichment_mbid=None,
            lastfm_url_from_enrichment=None,
            canonical_name_match=True,
        )
        assert conf == pytest.approx(0.30)  # 0.50 + 0.10 - 0.30
        assert "generic_short_name_no_mbid" in warnings

    def test_mbid_conflict_penalty(self) -> None:
        conf, warnings = compute_identity_confidence(
            artist_name="Some Artist Name",
            mbid=None,
            resolution_status="provisional_mbid_conflict",
            lastfm_enrichment_mbid=None,
            lastfm_url_from_enrichment=None,
            canonical_name_match=True,
        )
        assert conf == pytest.approx(0.40)  # 0.50 + 0.10 - 0.20
        assert "multiple_possible_local_matches" in warnings
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_recommend.py::TestIdentityConfidence -v`

Expected: FAIL

- [ ] **Step 3: Implement function**

Add to `src/scrobble_recs/models/recommend.py`:

```python
def compute_identity_confidence(
    *,
    artist_name: str,
    mbid: str | None,
    resolution_status: str,
    lastfm_enrichment_mbid: str | None = None,
    lastfm_url_from_enrichment: str | None = None,
    canonical_name_match: bool = True,
) -> tuple[float, list[str]]:
    confidence = 0.50
    warnings: list[str] = []

    if mbid and resolution_status == "provisional_mbid":
        confidence += 0.30
    if lastfm_enrichment_mbid or lastfm_url_from_enrichment:
        confidence += 0.15
    if canonical_name_match:
        confidence += 0.10

    name_parts = artist_name.strip().split()
    if len(name_parts) <= 1 and not mbid:
        confidence -= 0.30
        warnings.append("generic_short_name_no_mbid")
    if resolution_status == "provisional_mbid_conflict":
        confidence -= 0.20
        warnings.append("multiple_possible_local_matches")
    if not mbid:
        warnings.append("missing_musicbrainz_mbid")

    return (max(0.0, min(1.0, confidence)), sorted(warnings))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_recommend.py::TestIdentityConfidence -v`

Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/scrobble_recs/models/recommend.py tests/test_recommend.py
git commit -m "feat: add compute_identity_confidence function"
```

### Task 4.2: Compute identity confidence after import

**Files:**
- Modify: `src/scrobble_recs/importers/lastfm_csv.py`

- [ ] **Step 1: Add identity confidence computation after import**

In `import_lastfm_csv`, after `_insert_provisional_artists` (line 95) and before `_insert_provisional_tracks` (line 96), add:

```python
_insert_provisional_artists(conn, next_artist_id)
_update_identity_confidence(conn)
```

Where `_update_identity_confidence` is a new function:

```python
def _update_identity_confidence(conn: DuckDBPyConnection) -> None:
    rows = conn.execute(
        """
        SELECT artist_id, canonical_name, musicbrainz_artist_mbid, resolution_status
        FROM artist
        """
    ).fetchall()
    for artist_id, canonical_name, mbid, resolution_status in rows:
        name = str(canonical_name)
        mbid_val = str(mbid) if mbid else None
        status = str(resolution_status) if resolution_status else "provisional_name"

        confidence = 0.50
        warnings: list[str] = []

        if mbid_val and status == "provisional_mbid":
            confidence += 0.30
        # canonical_name_match is always True
        confidence += 0.10

        name_parts = name.strip().split()
        if len(name_parts) <= 1 and not mbid_val:
            confidence -= 0.30
            warnings.append("generic_short_name_no_mbid")
        if status == "provisional_mbid_conflict":
            confidence -= 0.20
            warnings.append("multiple_possible_local_matches")
        if not mbid_val:
            warnings.append("missing_musicbrainz_mbid")

        identity_confidence = max(0.0, min(1.0, confidence))
        conn.execute(
            """
            UPDATE artist
            SET identity_confidence = ?,
                identity_warnings = ?
            WHERE artist_id = ?
            """,
            [identity_confidence, warnings, int(artist_id)],
        )
```

Note: DuckDB may not support Python list for `VARCHAR[]`. Instead, use `list_value(...)` or serialize to JSON. Let's serialize to JSON string and parse on read. Or better, construct as DuckDB list:

```python
conn.execute(
    """
    UPDATE artist
    SET identity_confidence = ?,
        identity_warnings = array_agg(w) FROM (SELECT unnest(?) AS w)
    WHERE artist_id = ?
    """,
    [identity_confidence, warnings, int(artist_id)],
)
```

Actually the simplest approach for VARCHAR[] in DuckDB is:
```sql
UPDATE artist SET identity_warnings = list_value(?) WHERE artist_id = ?
```
But that requires dynamic SQL construction. Let me use a simpler approach: serialize warnings as a JSON string in a VARCHAR column. We can change the schema to `identity_warnings VARCHAR` (storing JSON array) instead of `VARCHAR[]`.

Actually, let me revert the migration to use `VARCHAR` for identity_warnings (storing JSON-serialized list). This avoids DuckDB array complexity.

- [ ] **Step 2: Update migration 011**

Change `identity_warnings VARCHAR[]` to `identity_warnings VARCHAR` in both tables. The value stored is `json.dumps(["warn1", "warn2"])`.

Update `011_identity_fields.sql`:
```sql
ALTER TABLE artist ADD COLUMN IF NOT EXISTS identity_confidence DOUBLE DEFAULT 0.0;
ALTER TABLE artist ADD COLUMN IF NOT EXISTS identity_warnings VARCHAR;

ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS candidate_artist_name VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS canonical_artist_name VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS musicbrainz_artist_mbid VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS lastfm_url VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS musicbrainz_url VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS identity_confidence DOUBLE;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS identity_warnings VARCHAR;
```

- [ ] **Step 3: Update _update_identity_confidence to store JSON warnings**

```python
import json

def _update_identity_confidence(conn: DuckDBPyConnection) -> None:
    rows = conn.execute(
        """
        SELECT artist_id, canonical_name, musicbrainz_artist_mbid, resolution_status
        FROM artist
        """
    ).fetchall()
    for artist_id, canonical_name, mbid, resolution_status in rows:
        name = str(canonical_name)
        mbid_val = str(mbid) if mbid else None
        status = str(resolution_status) if resolution_status else "provisional_name"

        confidence = 0.50
        warnings: list[str] = []

        if mbid_val and status == "provisional_mbid":
            confidence += 0.30
        confidence += 0.10  # canonical_name_match

        name_parts = name.strip().split()
        if len(name_parts) <= 1 and not mbid_val:
            confidence -= 0.30
            warnings.append("generic_short_name_no_mbid")
        if status == "provisional_mbid_conflict":
            confidence -= 0.20
            warnings.append("multiple_possible_local_matches")
        if not mbid_val:
            warnings.append("missing_musicbrainz_mbid")

        identity_confidence = max(0.0, min(1.0, confidence))
        conn.execute(
            "UPDATE artist SET identity_confidence = ?, identity_warnings = ? WHERE artist_id = ?",
            [identity_confidence, json.dumps(warnings), int(artist_id)],
        )
```

- [ ] **Step 4: Run import tests**

Run: `python -m pytest tests/test_import.py -v`

Expected: All PASS (new columns on artist table won't break existing INSERT statements since they have defaults)

- [ ] **Step 5: Commit**

```bash
git add src/scrobble_recs/importers/lastfm_csv.py src/scrobble_recs/sql/011_identity_fields.sql
git commit -m "feat: compute identity confidence after import"
```

### Task 4.3: Apply identity confidence multiplier in scoring

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`

- [ ] **Step 1: Read identity_confidence from artist table in scoring pipeline**

In `_load_scoring_data`, also read `identity_confidence` from the artist table:

```python
identity_by_artist = {
    int(artist_id): float(conf or 0.50)
    for artist_id, conf in conn.execute(
        "SELECT artist_id, coalesce(identity_confidence, 0.50) FROM artist"
    ).fetchall()
}
```

Store it in `_ScoringData`:
```python
@dataclass(frozen=True)
class _ScoringData:
    artist_names: dict[int, str]
    candidates: set[int]
    similarity_by_target: dict[int, list[tuple[int, float]]]
    feedback_similarity_by_target: dict[int, list[tuple[int, float]]]
    direct_tags_by_artist: dict[int, dict[str, float]]
    tag_ranks_by_artist: dict[int, list[tuple[str, int]]]
    listening_by_artist: dict[int, tuple[float, float, int, int, datetime | None, datetime | None]]
    identity_confidence_by_artist: dict[int, float]  # NEW
```

Update the return in `_load_scoring_data`:
```python
return _ScoringData(
    artist_names=artist_names,
    candidates=candidates - seed_ids,
    similarity_by_target=similarity_by_target,
    feedback_similarity_by_target=feedback_similarity_by_target,
    direct_tags_by_artist=direct_tags_by_artist,
    tag_ranks_by_artist=tag_ranks_by_artist,
    listening_by_artist=listening_by_artist,
    identity_confidence_by_artist=identity_by_artist,
)
```

- [ ] **Step 2: Apply identity confidence multiplier in _score_candidates**

After computing `final_score` and subtracting penalties, but before `if final_score <= 0: continue`:

```python
identity_conf = scoring_data.identity_confidence_by_artist.get(artist_id, 0.50)
confidence_multiplier = 0.5 + 0.5 * identity_conf
final_score *= confidence_multiplier
```

Add `identity_confidence=identity_conf` to the _CandidateScore construction.

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_recommend.py -v -x`

Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/scrobble_recs/models/recommend.py
git commit -m "feat: apply identity confidence multiplier in scoring"
```

### Task 4.4: Snapshot identity fields in _persist_run

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`

- [ ] **Step 1: Read artist identity data in _persist_run and write to recommendation**

In `_persist_run`, before the INSERT loop, load artist identity data:

```python
artist_identity = {
    int(artist_id): (str(canonical_name or ""), str(mbid or "") if mbid else "", float(conf or 0.50), str(warnings or "[]"))
    for artist_id, canonical_name, mbid, conf, warnings in conn.execute(
        """
        SELECT artist_id, canonical_name,
               coalesce(musicbrainz_artist_mbid, ''),
               coalesce(identity_confidence, 0.50),
               coalesce(identity_warnings, '[]')
        FROM artist
        """
    ).fetchall()
}
```

In the INSERT loop, for each score:
```python
identity = artist_identity.get(score.artist_id, ("", "", 0.50, "[]"))
canonical_name_val = identity[0]
mbid_val = identity[1]
identity_conf_val = identity[2]
identity_warnings_val = identity[3]

# URLs
from urllib.parse import quote
lastfm_url_val = f"https://www.last.fm/music/{quote(score.artist_name, safe='')}"
musicbrainz_url_val = f"https://musicbrainz.org/artist/{mbid_val}" if mbid_val else ""
```

Add these to the INSERT params list.

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_recommend.py -v -x`

Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add src/scrobble_recs/models/recommend.py
git commit -m "feat: snapshot identity fields in _persist_run"
```

---

## Phase 5: Richer Explanations (Deliverable 5)

### Task 5.1: Enhance _explanation function

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`

- [ ] **Step 1: Update _explanation to include all new fields**

Replace the current `_explanation` return dict (lines 1101-1180) with an enhanced version that includes:

```python
def _explanation(
    score: _CandidateScore,
    scoring_data: _ScoringData,
    *,
    seed_weight_by_id: dict[int, float],
    tag_profile: dict[str, float],
) -> dict[str, object]:
    seed_rows = [
        (
            source_artist_id,
            scoring_data.artist_names.get(source_artist_id, ""),
            similarity,
            seed_weight_by_id.get(source_artist_id, 0.0) * similarity,
        )
        for source_artist_id, similarity in scoring_data.similarity_by_target.get(
            score.artist_id, []
        )
        if source_artist_id in seed_weight_by_id
    ]
    seed_rows.sort(key=lambda row: (-row[3], row[1]))
    tag_rows = scoring_data.tag_ranks_by_artist.get(score.artist_id, [])
    ordered_tag_rows = sorted(
        tag_rows,
        key=lambda row: (-tag_profile.get(row[0], 0.0), row[1], row[0]),
    )

    top_seed_names = [name for _, name, _, _ in seed_rows[:5]]
    top_seed_scores = [sim for _, _, sim, _ in seed_rows[:5]]
    matching_tag_names = [str(tag) for tag, _ in ordered_tag_rows[:5]]

    identity_warnings_list: list[str] = []
    if score.identity_warnings:
        identity_warnings_list = list(score.identity_warnings)

    evidence_sources: list[str] = []
    if score.lastfm_similarity_score > 0:
        evidence_sources.append("lastfm_similarity")
    if score.tag_similarity_score > 0:
        evidence_sources.append("tag_similarity")
    if score.feedback_score > 0:
        evidence_sources.append("feedback")
    if score.rediscovery_score > 0:
        evidence_sources.append("rediscovery")

    # Build human-readable summary
    summary_parts = []
    if matching_tag_names:
        summary_parts.append(f"Matched tags: {', '.join(matching_tag_names[:3])}.")
    if top_seed_names:
        summary_parts.append(f"Nearest seeds: {', '.join(top_seed_names[:3])}.")
    if evidence_sources:
        summary_parts.append(f"Evidence: {' + '.join(evidence_sources)}.")
    cautions = []
    if score.trial_penalty > 0:
        cautions.append("old one-day local listen")
    if identity_warnings_list:
        cautions.append("identity unresolved")
    if score.fatigue.penalty > 0:
        cautions.append("appeared in recent runs")
    if cautions:
        summary_parts.append(f"Caution: {'; '.join(cautions)}.")

    return {
        "summary": " ".join(summary_parts),
        "top_seed_artists": top_seed_names,
        "top_seed_edge_scores": top_seed_scores,
        "matching_tags": matching_tag_names,
        "tag_fit_score": score.tag_similarity_score,
        "lastfm_similarity_score": score.lastfm_similarity_score,
        "rediscovery_score": score.rediscovery_score,
        "feedback_score": score.feedback_score,
        "knownness_penalty": score.knownness_penalty,
        "obviousness_penalty": score.obviousness_penalty,
        "fatigue_penalty": score.fatigue.penalty,
        "trial_penalty": score.trial_penalty,
        "identity_confidence": score.identity_confidence,
        "identity_warnings": identity_warnings_list,
        "candidate_sources": evidence_sources,
        "quality_gate": {
            "passed": score.quality_gate.passed,
            "reasons": list(score.quality_gate.reasons),
            "thresholds": score.quality_gate.thresholds,
            "scores": {
                "lastfm_similarity_score": score.lastfm_similarity_score,
                "tag_similarity_score": score.tag_similarity_score,
                "feedback_score": score.feedback_score,
                "rediscovery_score": score.rediscovery_score,
            },
        },
        "seed_artists": [
            {
                "artist": seed_name,
                "seed_weight": seed_weight_by_id.get(source_id, 0.0),
                "source": "lastfm_artist_similarity",
                "similarity": similarity,
            }
            for source_id, seed_name, similarity, _contribution in seed_rows[:5]
        ],
        "tags": [
            {
                "tag": str(tag),
                "user_weight": tag_profile.get(str(tag), 0.0),
                "candidate_weight": _rank_affinity(int(rank)),
                "candidate_rank_for_tag": int(rank),
            }
            for tag, rank in ordered_tag_rows[:5]
        ],
        "penalties": {
            "knownness": score.knownness_penalty,
            "obviousness": score.obviousness_penalty,
            "diversity": score.diversity_penalty,
            "trial": score.trial_penalty,
        },
        "fatigue": {
            "prior_run_appearances": score.fatigue.prior_run_appearances,
            "latest_run_rank": score.fatigue.latest_run_rank,
            "penalty": score.fatigue.penalty,
        },
        "feedback": {
            "score": score.feedback_score,
            "effects": score.feedback_effects,
            "contributions": score.feedback_contributions,
        },
        "classification": {
            "is_new": score.knownness == 0.0,
            "is_rediscovery": score.rediscovery_score > 0.0,
            "is_obvious": score.obviousness >= 0.75,
            "score_band": score.score_band,
            "rediscovery_eligible": score.rediscovery_eligible,
        },
    }
```

- [ ] **Step 2: Run existing tests**

Run: `python -m pytest tests/test_recommend.py -v -x`

Expected: All PASS (existing explanation tests may need adjustment if they check exact summary strings)

- [ ] **Step 3: Commit**

```bash
git add src/scrobble_recs/models/recommend.py
git commit -m "feat: enhance _explanation with all diagnostic fields"
```

### Task 5.2: Update export row construction for all new fields

**Files:**
- Modify: `src/scrobble_recs/export.py`

- [ ] **Step 1: Update _load_rows to extract all new fields from explanation**

In `_load_rows`, after parsing `explanation = json.loads(str(explanation_json))`, populate all new fields:

```python
result.append(
    {
        "rank": int(rank),
        "artist": str(artist or ""),
        "score_normalized": _display_score(float(final_score or 0.0), float(max_score or 0.0)),
        "score_raw": float(raw_score or 0.0),
        "score_band": str(score_band or ""),
        "evidence_source_count": int(evidence_count or 0),
        "primary_tags": ", ".join(str(tag["tag"]) for tag in tags[:3]),
        "knownness": float(knownness or 0.0),
        "obviousness": float(obviousness or 0.0),
        "is_rediscovery": bool(float(rediscovery_score or 0.0) > 0.0),
        "rediscovery_eligible": bool(rediscovery_eligible),
        "why": str(explanation.get("summary", "")),
        "lastfm_url": str(lastfm_url or f"https://www.last.fm/music/{quote(artist_name, safe='')}"),
        "musicbrainz_url": str(musicbrainz_url or ""),
        "local_scrobble_count": int(local_scrobbles or 0),
        "listened_day_count": int(listened_days or 0),
        "first_listened_at": str(first_listened_at) if first_listened_at else "",
        "last_listened_at": str(last_listened_at) if last_listened_at else "",
        "years_since_last_listen": float(years_since or 0.0),
        "trial_penalty": float(trial_pen or 0.0),
        "candidate_artist_name": str(candidate_name or artist_name),
        "canonical_artist_name": str(canonical_name or artist_name),
        "musicbrainz_artist_mbid": str(mbid or ""),
        "identity_confidence": float(identity_conf or 0.50),
        "identity_warnings": str(identity_warns or "[]"),
        "top_seed_artists": ", ".join(str(s) for s in explanation.get("top_seed_artists", [])),
        "top_seed_edge_scores": ", ".join(str(s) for s in explanation.get("top_seed_edge_scores", [])),
        "matching_tags": ", ".join(str(t) for t in explanation.get("matching_tags", [])),
        "tag_fit_score": float(explanation.get("tag_fit_score", 0.0)),
        "lastfm_similarity_score": float(explanation.get("lastfm_similarity_score", 0.0)),
        "rediscovery_score": float(rediscovery_score or 0.0),
        "feedback_score": float(explanation.get("feedback_score", 0.0)),
        "knownness_penalty": float(explanation.get("knownness_penalty", 0.0)),
        "obviousness_penalty": float(explanation.get("obviousness_penalty", 0.0)),
        "fatigue_penalty": float(explanation.get("fatigue_penalty", 0.0)),
        "candidate_sources": ", ".join(str(s) for s in explanation.get("candidate_sources", [])),
        "explanation": explanation,
    }
)
```

- [ ] **Step 2: Run export tests**

Run: `python -m pytest tests/test_export.py -v`

Expected: All PASS (may need updates to _insert_export_row helper)

- [ ] **Step 3: Commit**

```bash
git add src/scrobble_recs/export.py tests/test_export.py
git commit -m "feat: add all diagnostic fields to export output"
```

---

## Phase 6: TUI Updates (Deliverable 5)

### Task 6.1: Update TUI data classes

**Files:**
- Modify: `src/scrobble_recs/tui/data.py`
- Modify: `tests/test_tui_data.py`

- [ ] **Step 1: Add new fields to RecommendationListRow**

```python
@dataclass(frozen=True)
class RecommendationListRow:
    run_id: str
    artist_id: int
    rank: int
    artist: str
    score: float
    primary_tags: tuple[str, ...]
    knownness: float
    obviousness: float
    why_short: str
    feedback_state: str
    classification: str
    # NEW
    score_band: str = ""
    identity_confidence: float = 0.50
    identity_warnings: tuple[str, ...] = ()
    trial_penalty: float = 0.0
```

- [ ] **Step 2: Add new fields to ScoreBreakdown**

```python
@dataclass(frozen=True)
class ScoreBreakdown:
    display_score: float
    final_score: float
    raw_score: float
    lastfm_similarity_score: float
    tag_similarity_score: float
    cluster_fit_score: float
    rediscovery_score: float
    metadata_graph_score: float
    feedback_score: float
    knownness: float
    obviousness: float
    diversity_penalty: float
    # NEW
    score_band: str = ""
    normalized_score: float = 0.0
    evidence_source_count: int = 0
    trial_penalty: float = 0.0
    identity_confidence: float = 0.50
    identity_warnings: tuple[str, ...] = ()
```

- [ ] **Step 3: Add new fields to RecommendationDetail**

```python
@dataclass(frozen=True)
class RecommendationDetail:
    run_id: str
    artist_id: int
    artist: str
    rank: int
    summary: str
    seed_artists: tuple[str, ...]
    tags: tuple[str, ...]
    penalties: dict[str, float]
    classification: dict[str, bool]
    score_breakdown: ScoreBreakdown
    status_labels: tuple[str, ...]
    feedback_history: tuple[str, ...]
    # NEW
    score_band: str = ""
    identity_confidence: float = 0.50
    identity_warnings: tuple[str, ...] = ()
    local_scrobble_count: int = 0
    listened_day_count: int = 0
    years_since_last_listen: float = 0.0
    rediscovery_eligible: bool = False
    trial_penalty: float = 0.0
    top_seed_artists: tuple[str, ...] = ()
```

- [ ] **Step 4: Update _list_row to populate new fields**

```python
def _list_row(
    *,
    run_id: str,
    artist_id: int,
    rank: int,
    artist: str,
    final_score: float,
    knownness: float,
    obviousness: float,
    feedback_score: float,
    explanation: dict[str, Any],
    # NEW from query
    score_band: str = "",
    identity_confidence: float = 0.50,
    identity_warnings: str = "[]",
    trial_penalty: float = 0.0,
) -> RecommendationListRow:
    identity_warnings_tuple = tuple(
        json.loads(identity_warnings) if identity_warnings and identity_warnings != "[]" else []
    ) if isinstance(identity_warnings, str) else ()
    return RecommendationListRow(
        run_id=run_id,
        artist_id=artist_id,
        rank=rank,
        artist=artist,
        score=final_score,
        primary_tags=_tag_names(explanation)[:3],
        knownness=knownness,
        obviousness=obviousness,
        why_short=str(explanation.get("summary", "")),
        feedback_state=_feedback_state(feedback_score),
        classification=_classification_label(explanation, score_band),
        score_band=score_band,
        identity_confidence=identity_confidence,
        identity_warnings=identity_warnings_tuple,
        trial_penalty=trial_penalty,
    )
```

- [ ] **Step 5: Update _classification_label to use score_band**

```python
def _classification_label(explanation: dict[str, Any], score_band: str = "") -> str:
    classification = _bool_map(explanation.get("classification", {}))
    if score_band == "candidate_only":
        return "cand"
    if score_band == "weak":
        return "weak"
    if classification.get("is_rediscovery"):
        return "redis"
    if classification.get("is_obvious"):
        return "obvious"
    if classification.get("is_new"):
        return "new"
    return "sim"
```

- [ ] **Step 6: Update list_recommendations query to include new columns**

In `TuiRecommendationStore.list_recommendations`, add new columns to the SELECT:

```sql
SELECT
    CAST(r.run_id AS VARCHAR),
    r.artist_id,
    r.rank,
    a.canonical_name,
    r.final_score,
    max(r.final_score) OVER () AS max_final_score,
    r.knownness,
    r.obviousness,
    r.feedback_score,
    r.explanation,
    r.score_band,
    r.identity_confidence,
    r.identity_warnings,
    r.trial_penalty
FROM recommendation r
JOIN artist a ON a.artist_id = r.artist_id
WHERE r.run_id = ?
ORDER BY r.rank
```

- [ ] **Step 7: Update get_detail to populate new fields**

In `TuiRecommendationStore.get_detail`, query `score_band`, `trial_penalty`, `identity_confidence`, `identity_warnings`, `local_scrobble_count`, `listened_day_count`, `years_since_last_listen`, `rediscovery_eligible` from the recommendation table and populate the new `RecommendationDetail` fields.

Also parse `top_seed_artists` from explanation:
```python
top_seed_artists = tuple(
    str(s) for s in explanation.get("top_seed_artists", [])
)
```

- [ ] **Step 8: Update _status_labels to include identity warnings**

```python
def _status_labels(
    explanation: dict[str, Any],
    score_breakdown: ScoreBreakdown,
) -> tuple[str, ...]:
    labels = []
    if not _seed_names(explanation) or score_breakdown.lastfm_similarity_score == 0.0:
        labels.append("partial_similarity")
    if not _tag_names(explanation) or score_breakdown.tag_similarity_score == 0.0:
        labels.append("partial_tags")
    if score_breakdown.identity_warnings:
        labels.extend(f"id:{w}" for w in score_breakdown.identity_warnings)
    if score_breakdown.score_band in ("weak", "candidate_only"):
        labels.append(f"band:{score_breakdown.score_band}")
    return tuple(labels) or ("complete",)
```

- [ ] **Step 9: Run TUI tests**

Run: `python -m pytest tests/test_tui_data.py -v`

Expected: All PASS (may need updates to test assertions)

- [ ] **Step 10: Commit**

```bash
git add src/scrobble_recs/tui/data.py tests/test_tui_data.py
git commit -m "feat: add all diagnostic fields to TUI data classes"
```

---

## Phase 7: MusicBrainz Resolve (Deliverable 4)

### Task 7.1: Create MusicBrainz API client

**Files:**
- Create: `src/scrobble_recs/apis/musicbrainz.py`
- Create: `tests/test_musicbrainz.py`

- [ ] **Step 1: Write tests for MusicBrainzClient**

Create `tests/test_musicbrainz.py`:

```python
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.apis.musicbrainz import (
    MusicBrainzArtistMatch,
    MusicBrainzArtistResult,
    MusicBrainzClient,
)
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import MusicBrainzApiConfig


class FakeHttpClient:
    def __init__(self, response_json: dict, status_code: int = 200):
        self.response_json = response_json
        self.status_code = status_code
        self.requests: list = []

    def get(self, url: str, headers: dict | None = None, timeout: float | None = None):
        self.requests.append(url)
        return FakeResponse(self.status_code, json.dumps(self.response_json))


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)


def test_search_artist_returns_matches(tmp_path: Path):
    config = MusicBrainzApiConfig(enabled=True)
    fake_response = {
        "artists": [
            {
                "id": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
                "name": "Radiohead",
                "disambiguation": "English rock band",
                "country": "GB",
                "type": "Group",
                "score": "100",
            }
        ]
    }
    http = FakeHttpClient(fake_response)
    conn = _init_test_cache(tmp_path)
    cache = ApiCache(conn)
    client = MusicBrainzClient(
        config=config,
        cache=cache,
        http_client=http,
        rate_limiter=RateLimiter(999.0),
    )
    results = client.search_artist("Radiohead", limit=5)
    assert len(results) == 1
    assert results[0].mbid == "a74b1b7f-71a5-4011-9441-d0b5e4122711"
    assert results[0].name == "Radiohead"
    assert results[0].score == 100.0


def test_search_artist_caches_responses(tmp_path: Path):
    config = MusicBrainzApiConfig(enabled=True)
    fake_response = {
        "artists": [
            {
                "id": "abc123",
                "name": "Test Artist",
                "score": "90",
            }
        ]
    }
    http = FakeHttpClient(fake_response)
    conn = _init_test_cache(tmp_path)
    cache = ApiCache(conn)
    client = MusicBrainzClient(
        config=config,
        cache=cache,
        http_client=http,
        rate_limiter=RateLimiter(999.0),
    )
    # First call hits HTTP
    client.search_artist("Test Artist", limit=5)
    assert len(http.requests) == 1

    # Second call should hit cache
    client.search_artist("Test Artist", limit=5)
    assert len(http.requests) == 1  # No additional HTTP call


def test_lookup_artist_returns_result(tmp_path: Path):
    config = MusicBrainzApiConfig(enabled=True)
    fake_response = {
        "id": "abc123",
        "name": "Test Artist",
        "disambiguation": "",
        "country": "US",
        "type": "Person",
        "life-span": {"begin": "1980", "end": ""},
    }
    http = FakeHttpClient(fake_response)
    conn = _init_test_cache(tmp_path)
    cache = ApiCache(conn)
    client = MusicBrainzClient(
        config=config,
        cache=cache,
        http_client=http,
        rate_limiter=RateLimiter(999.0),
    )
    result = client.lookup_artist("abc123")
    assert result is not None
    assert result.mbid == "abc123"
    assert result.name == "Test Artist"
    assert result.country == "US"


def test_lookup_artist_returns_none_on_404(tmp_path: Path):
    config = MusicBrainzApiConfig(enabled=True)
    http = FakeHttpClient({"error": "not found"}, status_code=404)
    conn = _init_test_cache(tmp_path)
    cache = ApiCache(conn)
    client = MusicBrainzClient(
        config=config,
        cache=cache,
        http_client=http,
        rate_limiter=RateLimiter(999.0),
    )
    result = client.lookup_artist("nonexistent")
    assert result is None


def _init_test_cache(tmp_path: Path):
    import duckdb
    conn = duckdb.connect(str(tmp_path / "test.duckdb"))
    conn.execute("""
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
        )
    """)
    return conn
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_musicbrainz.py -v`

Expected: FAIL — module not found

- [ ] **Step 3: Implement MusicBrainzClient**

Create `src/scrobble_recs/apis/musicbrainz.py`:

```python
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from scrobble_recs.apis.cache import ApiCache, JsonObject
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import MusicBrainzApiConfig

_SOURCE = "musicbrainz"


@dataclass(frozen=True)
class MusicBrainzArtistMatch:
    mbid: str
    name: str
    disambiguation: str | None
    country: str | None
    type: str | None
    score: float


@dataclass(frozen=True)
class MusicBrainzArtistResult:
    mbid: str
    name: str
    disambiguation: str
    country: str
    type: str
    life_span_begin: str
    life_span_end: str


class MusicBrainzClient:
    def __init__(
        self,
        *,
        config: MusicBrainzApiConfig,
        cache: ApiCache,
        http_client: Any = None,
        rate_limiter: RateLimiter | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.Client(
            timeout=30.0,
            headers={"User-Agent": config.user_agent},
        )
        self._rate_limiter = rate_limiter or RateLimiter(config.rate_limit_per_second)
        self._now = now or datetime.utcnow

    def search_artist(self, query: str, *, limit: int = 5) -> list[MusicBrainzArtistMatch]:
        params: JsonObject = {"query": f'artist:"{query}"', "limit": str(limit), "fmt": "json"}
        data = self._request("artist", params)
        if data is None:
            return []
        artists = data.get("artists", [])
        results: list[MusicBrainzArtistMatch] = []
        for artist in artists:
            if not isinstance(artist, dict):
                continue
            mbid = str(artist.get("id", ""))
            if not mbid:
                continue
            results.append(
                MusicBrainzArtistMatch(
                    mbid=mbid,
                    name=str(artist.get("name", "")),
                    disambiguation=artist.get("disambiguation"),
                    country=artist.get("country"),
                    type=artist.get("type"),
                    score=float(artist.get("score", 0)),
                )
            )
        return results

    def lookup_artist(self, mbid: str) -> MusicBrainzArtistResult | None:
        params: JsonObject = {"fmt": "json"}
        data = self._request(f"artist/{mbid}", params)
        if data is None:
            return None
        life_span = data.get("life-span", {}) or {}
        return MusicBrainzArtistResult(
            mbid=str(data.get("id", mbid)),
            name=str(data.get("name", "")),
            disambiguation=str(data.get("disambiguation", "")),
            country=str(data.get("country", "")),
            type=str(data.get("type", "")),
            life_span_begin=str(life_span.get("begin", "")),
            life_span_end=str(life_span.get("end", "")),
        )

    def _request(self, endpoint: str, params: JsonObject) -> JsonObject | None:
        now = self._now()
        cached = self._cache.get_fresh(_SOURCE, endpoint, params, now=now)
        if cached is not None:
            return cached

        stale = self._cache.get_stale_success(_SOURCE, endpoint, params)
        if stale is not None and self._config.max_retries > 0:
            # Return stale on network error per config
            pass

        self._rate_limiter.acquire()
        url = f"{self._config.base_url.rstrip('/')}/{endpoint}"
        try:
            response = self._http_client.get(url, params=params, timeout=30.0)
            status = response.status_code
            data = response.json() if 200 <= status < 300 else {}
            error_msg: str | None = None
            if status >= 400:
                error_msg = response.text[:500]
                data = {}
            expires_at = now + timedelta(days=self._config.cache_ttl_days)
            self._cache.write_response(
                source=_SOURCE,
                endpoint=endpoint,
                request_url=str(response.url),
                request_params=params,
                response_status=status,
                response_json=data,
                fetched_at=now,
                expires_at=expires_at,
                error_message=error_msg,
            )
            if status >= 400:
                if stale is not None:
                    return stale
                return None
            return data
        except Exception:
            if stale is not None:
                return stale
            return None
        finally:
            if self._owns_http_client:
                pass  # Don't close shared client

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_musicbrainz.py -v`

Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/scrobble_recs/apis/musicbrainz.py tests/test_musicbrainz.py
git commit -m "feat: add MusicBrainz API client with caching"
```

### Task 7.2: Create identity resolution migration

**Files:**
- Create: `src/scrobble_recs/sql/012_identity_resolution.sql`

- [ ] **Step 1: Create migration file**

```sql
CREATE TABLE IF NOT EXISTS artist_identity_resolution (
    artist_id UBIGINT,
    source VARCHAR,
    external_id VARCHAR,
    canonical_name VARCHAR,
    disambiguation VARCHAR,
    country_or_area VARCHAR,
    type VARCHAR,
    confidence DOUBLE,
    status VARCHAR,
    warning_codes VARCHAR,
    resolved_at TIMESTAMP DEFAULT current_timestamp,
    raw_cache_key VARCHAR
);

CREATE UNIQUE INDEX IF NOT EXISTS artist_identity_resolution_key
ON artist_identity_resolution (artist_id, source);
```

- [ ] **Step 2: Run tests to verify migration applies**

Run: `python -m pytest tests/test_recommend.py -v -k "test_recommendation_migration"`

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/scrobble_recs/sql/012_identity_resolution.sql
git commit -m "feat: add artist_identity_resolution migration"
```

### Task 7.3: Implement resolve command logic

**Files:**
- Modify: `src/scrobble_recs/cli.py`
- Modify: `src/scrobble_recs/admin/doctor.py`

- [ ] **Step 1: Update resolve command in cli.py**

Replace the stub (lines 397-429) with:

```python
@app.command("resolve")
def resolve_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    source: Annotated[
        str,
        typer.Option("--source", help="Resolution source to run."),
    ] = "musicbrainz",
) -> None:
    if source != "musicbrainz":
        raise typer.BadParameter("only --source musicbrainz is supported")

    resolved_config = load_config(config)
    if not resolved_config.apis.musicbrainz.enabled:
        console.print("MusicBrainz resolution is disabled in config. Set apis.musicbrainz.enabled=true")
        raise typer.Exit(code=1)

    conn = initialize_database(resolved_config)
    try:
        from scrobble_recs.apis.cache import ApiCache
        from scrobble_recs.apis.musicbrainz import MusicBrainzClient
        from scrobble_recs.apis.rate_limit import RateLimiter

        cache = ApiCache(conn)
        client = MusicBrainzClient(
            config=resolved_config.apis.musicbrainz,
            cache=cache,
            rate_limiter=RateLimiter(resolved_config.apis.musicbrainz.rate_limit_per_second),
        )

        unresolved = conn.execute(
            """
            SELECT artist_id, canonical_name, musicbrainz_artist_mbid, resolution_status
            FROM artist
            WHERE (identity_confidence IS NULL OR identity_confidence < 0.80)
              AND (resolution_status IS NULL
                   OR resolution_status NOT IN ('resolved_musicbrainz'))
            ORDER BY identity_confidence NULLS FIRST, canonical_name
            """
        ).fetchall()

        total = len(unresolved)
        console.print(f"unresolved artists: {total}")

        resolved = 0
        ambiguous = 0
        not_found = 0
        skipped = 0
        errors = 0

        for i, (artist_id, canonical_name, existing_mbid, resolution_status) in enumerate(unresolved):
            name = str(canonical_name)
            artist_id_int = int(artist_id)
            existing_mbid_val = str(existing_mbid) if existing_mbid else ""

            # Skip if already has consistent MBID
            if existing_mbid_val and resolution_status == "provisional_mbid":
                # Already has good MBID, just update confidence
                conn.execute(
                    "UPDATE artist SET identity_confidence = 0.80, resolution_status = 'resolved_musicbrainz' WHERE artist_id = ?",
                    [artist_id_int],
                )
                skipped += 1
                continue

            try:
                matches = client.search_artist(name, limit=5)
                if not matches:
                    conn.execute(
                        """INSERT INTO artist_identity_resolution
                           (artist_id, source, canonical_name, status, warning_codes)
                           VALUES (?, 'musicbrainz', ?, 'not_found', ?)""",
                        [artist_id_int, name, json.dumps(["no_results"])],
                    )
                    not_found += 1
                    continue

                best = matches[0]
                if best.score < 80:
                    conn.execute(
                        """INSERT INTO artist_identity_resolution
                           (artist_id, source, external_id, canonical_name, disambiguation,
                            country_or_area, type, confidence, status, warning_codes, raw_cache_key)
                           VALUES (?, 'musicbrainz', ?, ?, ?, ?, ?, ?, 'ambiguous', ?, ?)""",
                        [
                            artist_id_int, best.mbid, best.name,
                            best.disambiguation, best.country, best.type,
                            float(best.score) / 100.0,
                            json.dumps(["low_confidence"]),
                            "",
                        ],
                    )
                    ambiguous += 1
                    continue

                # Resolve: lookup full details
                result = client.lookup_artist(best.mbid)
                if result is None:
                    errors += 1
                    continue

                # Store resolution
                conn.execute(
                    """INSERT INTO artist_identity_resolution
                       (artist_id, source, external_id, canonical_name, disambiguation,
                        country_or_area, type, confidence, status, raw_cache_key)
                       VALUES (?, 'musicbrainz', ?, ?, ?, ?, ?, ?, 'resolved', ?)""",
                    [
                        artist_id_int, result.mbid, result.name,
                        result.disambiguation, result.country, result.type,
                        1.0, "",
                    ],
                )

                # Update artist
                identity_conf = 0.50 + 0.30 + 0.10  # base + mbid + name match
                name_parts = name.strip().split()
                warnings = []
                if len(name_parts) <= 1:
                    identity_conf -= 0.30
                    warnings.append("generic_short_name_no_mbid")

                conn.execute(
                    """UPDATE artist
                       SET musicbrainz_artist_mbid = ?,
                           identity_confidence = ?,
                           identity_warnings = ?,
                           resolution_status = 'resolved_musicbrainz'
                       WHERE artist_id = ?""",
                    [result.mbid, max(0.0, min(1.0, identity_conf)), json.dumps(warnings), artist_id_int],
                )
                resolved += 1

            except Exception:
                errors += 1

            # Progress report every 10 artists
            if (i + 1) % 10 == 0:
                console.print(f"  progress: {i + 1}/{total} (resolved={resolved}, ambiguous={ambiguous}, not_found={not_found}, skipped={skipped}, errors={errors})")

        console.print(f"resolved: {resolved}")
        console.print(f"ambiguous: {ambiguous}")
        console.print(f"not_found: {not_found}")
        console.print(f"skipped (existing mbid): {skipped}")
        console.print(f"errors: {errors}")

    finally:
        conn.close()
```

- [ ] **Step 2: Add doctor check for resolution stats**

In `admin/doctor.py`, add a check:

```python
def _check_musicbrainz_resolution(conn: DuckDBPyConnection) -> DiagnosticCheck:
    try:
        counts = conn.execute(
            """
            SELECT coalesce(status, 'no_entry'), count(*)
            FROM artist_identity_resolution
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        summary = {str(s): int(c) for s, c in counts}
        total = sum(summary.values())
        return DiagnosticCheck(
            name="musicbrainz_resolution",
            status=DiagnosticStatus.OK,
            message=f"{total} resolution entries: {summary}",
        )
    except Exception:
        return DiagnosticCheck(
            name="musicbrainz_resolution",
            status=DiagnosticStatus.ERROR,
            message="could not query artist_identity_resolution",
        )
```

Then add `_check_musicbrainz_resolution(conn)` to the `checks` list in `run_doctor`. Per the existing pattern in `admin/doctor.py` (which uses `DiagnosticStatus.OK`/`.WARN`/`.ERROR`, all uppercase):

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_cli.py tests/test_admin_doctor.py -v -x`

Expected: All PASS (resolve stub test may need update)

- [ ] **Step 4: Commit**

```bash
git add src/scrobble_recs/cli.py src/scrobble_recs/admin/doctor.py
git commit -m "feat: implement MusicBrainz resolve command and doctor check"
```

---

## Phase 8: Integration and Cleanup

### Task 8.1: Full test suite pass

- [ ] **Step 1: Run full test suite**

```bash
python -m pytest
```

- [ ] **Step 2: Fix any regressions**

Check for:
- Tests that construct `_CandidateScore` without new fields → add defaults or update
- Tests that INSERT into `recommendation` with old column list → update
- Tests that check exact explanation summary strings → may need loosening

- [ ] **Step 3: Run full test suite again**

```bash
python -m pytest
```

Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "fix: update tests for new schema and scoring fields"
```

### Task 8.2: Lint and type check

- [ ] **Step 1: Run ruff check**

```bash
python -m ruff check .
```

Expected: All checks passed

- [ ] **Step 2: Run ruff format check**

```bash
python -m ruff format --check .
```

Expected: All files already formatted

- [ ] **Step 3: Run mypy**

```bash
python -m mypy src
```

Expected: Success, no issues found

- [ ] **Step 4: Fix any issues**

### Task 8.3: Documentation updates

- [ ] **Step 1: Update README**

Add mention of:
- Score bands (strong/plausible/weak/candidate_only)
- `--include-candidates` export flag
- `resolve --source musicbrainz` command
- New export columns documenting diagnostic fields

- [ ] **Step 2: Update implementation-log.md**

Add entry:
```
## 2026-06-15: Recommendation quality base hardening

Changed:
- Added score band classification (strong/plausible/weak/candidate_only) with export gating.
- Added trial penalty for single-scrobble/single-day old artists.
- Added rediscovery eligibility (3+ scrobbles, 2+ active days, or positive feedback).
- Added identity confidence heuristics using imported MBIDs and Last.fm enrichment data.
- Implemented MusicBrainz resolve command with cache-first API client.
- Enhanced export with 30+ diagnostic fields and richer human-readable explanations.
- Updated TUI with score bands, identity confidence, trial penalty, and extended detail view.
- Added `--include-candidates` export flag for zero-score candidate inspection.
```

- [ ] **Step 3: Commit**

```bash
git add README.md docs/implementation-log.md
git commit -m "docs: update README and implementation log for quality hardening"
```

### Task 8.4: Final verification

- [ ] **Step 1: Final test run**

```bash
python -m pytest
```

- [ ] **Step 2: Final lint/type run**

```bash
python -m ruff check . && python -m ruff format --check . && python -m mypy src
```

- [ ] **Step 3: Commit any remaining changes**

```bash
git status
git add -A
git commit -m "chore: final cleanup and verification"
```
