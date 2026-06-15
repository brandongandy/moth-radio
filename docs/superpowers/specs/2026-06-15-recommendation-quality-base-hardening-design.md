# Spec: Recommendation Quality Base Hardening

Date: 2026-06-15

## Context

`scrobble-recs` is a local-first DuckDB recommendation tool. It imports Last.fm history, enriches via cache-first API calls, and generates artist recommendations from local data with persisted, inspectable evidence. This phase hardens recommendation quality without adding track-based recs or serendipity.

## Non-goals

- Track-based recommendations, track.getSimilar, recording-level tables
- Serendipity v03
- Spotify dependency
- Live API calls inside recommend/evaluate/tests
- ListenBrainz collaborative filtering

## Architecture Overview

All changes extend the existing pipeline:

```
import → enrich → build-profile → recommend → export
                                         ↓
                                    resolve (separate)
```

New columns are added via migration SQL files to existing tables. One new table (`artist_identity_resolution`) for MusicBrainz resolution provenance. No table restructures.

## Files by Deliverable

| # | Deliverable | Files |
|---|-------------|-------|
| 1 | Score bands + gating | `sql/009_score_bands.sql`, `models/recommend.py`, `export.py`, `tui/data.py`, `cli.py` |
| 2 | Trial penalty + rediscovery cleanup | `sql/010_listening_diagnostics.sql`, `models/recommend.py`, `models/knownness.py`, `tui/data.py`, `export.py` |
| 3 | Identity confidence | `sql/011_identity_fields.sql`, `models/recommend.py`, `importers/lastfm_csv.py`, `tui/data.py`, `export.py` |
| 4 | MusicBrainz resolve | `sql/012_identity_resolution.sql`, `apis/musicbrainz.py` (new), `cli.py`, `admin/doctor.py`, `enrichment/queue.py` |
| 5 | Richer explanations/export | `models/recommend.py`, `export.py`, `tui/data.py`, `cli.py` |

## Deliverable 1: Score Bands and Weak-Tail Gating

### Schema

Migration `009_score_bands.sql`:

```sql
ALTER TABLE recommendation ADD COLUMN score_band VARCHAR;
ALTER TABLE recommendation ADD COLUMN evidence_source_count INTEGER;
```

### Scoring changes (`models/recommend.py`)

New fields on `_CandidateScore`:
- `score_band: str` — "strong" | "plausible" | "weak" | "candidate_only"
- `normalized_score: float` — final_score / max_final_score_in_run
- `evidence_source_count: int` — count of evidence sources with non-zero contribution

Classification function:

```python
def _classify_score_band(normalized: float, raw: float, evidence_count: int) -> str:
    if raw <= 0 or normalized == 0:
        return "candidate_only"
    if normalized >= 0.50 and evidence_count >= 2:
        return "strong"
    if normalized >= 0.10 and evidence_count >= 1:
        return "plausible"
    return "weak"
```

Evidence sources counted: `lastfm_similarity_score > 0`, `tag_similarity_score > 0`, `feedback_score > 0`, `rediscovery_score > 0`.

### Candidate persistence change

Zero-score candidates (`final_score <= 0`) are no longer skipped. They are persisted to the recommendation table with `score_band='candidate_only'`, `rank=999999` (sentinel), and zeroed score fields. This allows export with `--include-candidates`.

Score band is computed after diversity reranking (so normalized_score uses the run's max across selected candidates) and stored during `_persist_run`.

### Export gating (`export.py`, `cli.py`)

- Default export query: `WHERE score_band != 'candidate_only'`
- `--include-candidates` CLI flag removes this filter
- `score_raw`, `score_normalized`, `score_band`, `evidence_source_count` added to CSV/JSONL columns

### TUI (`tui/data.py`)

- `RecommendationListRow` gains `score_band: str` — shown as classification label (`weak`, `cand`)
- `ScoreBreakdown` gains `score_band`, `normalized_score`, `evidence_source_count`
- Candidate_only rows hidden by default in list view; toggleable via filter

### Acceptance Criteria

- Normal export excludes candidate_only rows by default
- `--include-candidates` exports all rows including zero-score
- Tests cover zero-score/candidate_only classification
- No live API calls during recommendation generation

## Deliverable 2: Discovery vs Rediscovery Cleanup

### Schema

Migration `010_listening_diagnostics.sql`:

```sql
ALTER TABLE recommendation ADD COLUMN local_scrobble_count INTEGER;
ALTER TABLE recommendation ADD COLUMN listened_day_count INTEGER;
ALTER TABLE recommendation ADD COLUMN first_listened_at TIMESTAMP;
ALTER TABLE recommendation ADD COLUMN last_listened_at TIMESTAMP;
ALTER TABLE recommendation ADD COLUMN years_since_last_listen DOUBLE;
ALTER TABLE recommendation ADD COLUMN rediscovery_eligible BOOLEAN;
ALTER TABLE recommendation ADD COLUMN trial_penalty DOUBLE;
```

These mirror `artist_listening_fact` columns: `play_count`, `active_days`, `first_played_at`, `last_played_at`. They are snapshotted onto the recommendation row for export inspectability.

### Scoring changes (`models/recommend.py`)

**Extended `_load_scoring_data`:** `listening_by_artist` becomes:
```python
dict[int, tuple[float, float, int, int, datetime, datetime]]
# (knownness, rediscovery_score, play_count, active_days, first_played_at, last_played_at)
```

**New field on `_CandidateScore`:**
- `trial_penalty: float`
- `local_scrobble_count: int`
- `listened_day_count: int`
- `first_listened_at: datetime | None`
- `last_listened_at: datetime | None`
- `years_since_last_listen: float`
- `rediscovery_eligible: bool`

**Trial penalty function:**

```python
def _compute_trial_penalty(play_count: int, active_days: int,
                           years_since_last_listen: float) -> float:
    if play_count == 1 and active_days <= 1 and years_since_last_listen >= 2:
        return 0.35
    if play_count == 2 and active_days <= 1 and years_since_last_listen >= 2:
        return 0.20
    return 0.00
```

Where `years_since_last_listen = max(0, (as_of - last_played_at).days / 365.25)`.

**Rediscovery eligibility:**

```python
def _rediscovery_eligible(play_count: int, active_days: int,
                          has_positive_feedback: bool) -> bool:
    return play_count >= 3 or active_days >= 2 or has_positive_feedback
```

**Mode integration:**
- `rediscovery` mode: candidates with `rediscovery_score > 0` but `not rediscovery_eligible` are skipped (treated like quality gate failure)
- `balanced` mode: `trial_penalty` subtracted from `final_score` alongside other penalties
- `explore`/`anti-obvious`: no direct trial_penalty, but one-scrobble artists won't pass quality gates without strong evidence

### Acceptance Criteria

- One-off old artists are demoted unless they have strong independent evidence
- Rediscovery mode does not produce single-scrobble ghosts
- Balanced mode still allows good rediscoveries
- Tests cover 1-scrobble, 2-scrobble, multi-day, and feedback-positive cases

## Deliverable 3: Identity Confidence

### Schema

Migration `011_identity_fields.sql`:

```sql
ALTER TABLE artist ADD COLUMN identity_confidence DOUBLE DEFAULT 0.0;
ALTER TABLE artist ADD COLUMN identity_warnings VARCHAR[];

ALTER TABLE recommendation ADD COLUMN candidate_artist_name VARCHAR;
ALTER TABLE recommendation ADD COLUMN canonical_artist_name VARCHAR;
ALTER TABLE recommendation ADD COLUMN musicbrainz_artist_mbid VARCHAR;
ALTER TABLE recommendation ADD COLUMN lastfm_url VARCHAR;
ALTER TABLE recommendation ADD COLUMN musicbrainz_url VARCHAR;
ALTER TABLE recommendation ADD COLUMN identity_confidence DOUBLE;
ALTER TABLE recommendation ADD COLUMN identity_warnings VARCHAR[];
```

### Artist table computation (`importers/lastfm_csv.py`)

New function `compute_identity_confidence` called after `_insert_provisional_artists` and again from `build-profile`:

```python
def compute_identity_confidence(artist_name: str, mbid: str | None,
                                resolution_status: str,
                                lastfm_enrichment_mbid: str | None,
                                lastfm_url_from_enrichment: str | None,
                                canonical_name_match: bool
                                ) -> tuple[float, list[str]]:
    confidence = 0.50
    warnings: list[str] = []

    if mbid and resolution_status == 'provisional_mbid':
        confidence += 0.30
    if lastfm_enrichment_mbid or lastfm_url_from_enrichment:
        confidence += 0.15
    if canonical_name_match:
        confidence += 0.10

    name_parts = artist_name.strip().split()
    if len(name_parts) <= 1 and not mbid:
        confidence -= 0.30
        warnings.append("generic_short_name_no_mbid")
    if resolution_status == 'provisional_mbid_conflict':
        confidence -= 0.20
        warnings.append("multiple_possible_local_matches")
    if not mbid:
        warnings.append("missing_musicbrainz_mbid")

    return (max(0.0, min(1.0, confidence)), sorted(warnings))
```

`canonical_name_match` is always True for single-artist-key entities (the common case). It becomes relevant when the candidate artist name differs from the canonical (e.g., alias vs canonical).

### Scoring integration

Identity confidence applies a demotion multiplier after all penalties:
```python
confidence_multiplier = 0.5 + 0.5 * identity_confidence
final_score *= confidence_multiplier
```
A 0.0-confidence artist gets halved; a 1.0 artist is unchanged.

### Recommendation snapshot

At persist time, `_persist_run` reads `identity_confidence`, `identity_warnings`, `musicbrainz_artist_mbid`, `canonical_name` from the artist table and writes them to the recommendation row alongside constructed URLs.

### URL construction

- `lastfm_url`: `https://www.last.fm/music/{url-quoted-lastfm-name-or-canonical-name}`
- `musicbrainz_url`: `https://musicbrainz.org/artist/{mbid}` — only when MBID exists, empty string otherwise

### Acceptance Criteria

- Existing imports with consistent MBIDs improve identity confidence
- Artists without MBIDs still work
- Short/generic unresolved names are visibly flagged
- High-ranking candidate_only/generic names are demoted by confidence multiplier
- No live MusicBrainz calls from recommend

## Deliverable 4: MusicBrainz Resolve

### Schema

Migration `012_identity_resolution.sql`:

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
    warning_codes VARCHAR[],
    resolved_at TIMESTAMP DEFAULT current_timestamp,
    raw_cache_key VARCHAR
);
CREATE UNIQUE INDEX IF NOT EXISTS artist_identity_resolution_key
ON artist_identity_resolution (artist_id, source);
```

Statuses: `resolved`, `ambiguous`, `not_found`, `skipped_existing_mbid`, `error`.

### API client (`apis/musicbrainz.py` — new)

Follows `apis/lastfm.py` pattern: `ApiCache`, `RateLimiter` (1 call/sec), `httpx`, mockable in tests.

```python
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
    def search_artist(self, query: str, *, limit: int = 5) -> list[MusicBrainzArtistMatch]: ...
    def lookup_artist(self, mbid: str) -> MusicBrainzArtistResult | None: ...
```

Endpoints:
- Search: `artist/?query=artist:{query}&limit={limit}&fmt=json`
- Lookup: `artist/{mbid}?fmt=json`

Rate limit: 1 call per second (per `MusicBrainzApiConfig` default).

### CLI command (`cli.py`)

Replaces the current stub (lines 397-429):

```
scrobble-recs resolve --config config.json --source musicbrainz
```

Behavior:
1. Find artists with `identity_confidence < 0.80` and `resolution_status != 'resolved_musicbrainz'`
2. Skip artists already in enrichment_queue with completed musicbrainz entries
3. Enqueue via `enrichment_queue` (source=musicbrainz, endpoint=artist_search)
4. Process queue: search by canonical_name, lookup top match
5. Store results in `artist_identity_resolution`
6. On resolution: update `artist.musicbrainz_artist_mbid`, recompute `identity_confidence`, set `resolution_status = 'resolved_musicbrainz'`
7. Never overwrite an existing consistent MBID (`resolution_status = 'provisional_mbid'`)
8. Auto-skip low-confidence ambiguous names (score < 80)

### Queue integration (`enrichment/queue.py`)

Extend `--source` validation to accept `musicbrainz`. The processing loop dispatches to `MusicBrainzClient` when `source='musicbrainz'`.

### Doctor integration (`admin/doctor.py`)

New diagnostic check:
```
musicbrainz_resolution: counts artists by artist_identity_resolution.status
```

### Acceptance Criteria

- `resolve --source musicbrainz` runs independently of recommend
- `recommend` uses stored identity data if present, never calls MusicBrainz
- Tests mock MusicBrainz responses
- Doctor reports resolution counts by status

## Deliverable 5: Richer Explanations and Export

### Explanation changes (`models/recommend.py`)

The `_explanation` function produces an enhanced dict:

```python
{
    "summary": "Matched tags: dream pop, indie pop. "
               "Nearest seeds: Alvvays, Beach House, Men I Trust. "
               "Evidence: Last.fm similarity from 3 seeds + tag fit. "
               "Caution: old one-day local listen; identity unresolved.",
    "top_seed_artists": ["Alvvays", "Beach House", "Men I Trust"],
    "top_seed_edge_scores": [0.85, 0.72, 0.68],
    "matching_tags": ["dream pop", "indie pop", "shoegaze"],
    "tag_fit_score": 0.62,
    "lastfm_similarity_score": 0.78,
    "rediscovery_score": 0.0,
    "feedback_score": 0.0,
    "knownness_penalty": 0.0,
    "obviousness_penalty": 0.0,
    "fatigue_penalty": 0.0,
    "trial_penalty": 0.35,
    "identity_confidence": 0.30,
    "identity_warnings": ["generic_short_name_no_mbid", "missing_musicbrainz_mbid"],
    "candidate_sources": ["lastfm_similarity", "tag_similarity"],
    # existing fields: quality_gate, seed_artists, tags, penalties, fatigue, feedback, classification
}
```

Human-readable `summary` generated from structured fields via template.

### Export columns (`export.py`)

Full column list (CSV) / keys (JSONL):

```
rank, artist, score_normalized, score_raw, score_band, evidence_source_count,
primary_tags, knownness, obviousness, is_rediscovery, rediscovery_eligible,
why, lastfm_url, musicbrainz_url,
local_scrobble_count, listened_day_count, first_listened_at, last_listened_at,
years_since_last_listen, trial_penalty,
candidate_artist_name, canonical_artist_name, musicbrainz_artist_mbid,
identity_confidence, identity_warnings,
top_seed_artists, top_seed_edge_scores, matching_tags, tag_fit_score,
lastfm_similarity_score, rediscovery_score, feedback_score,
knownness_penalty, obviousness_penalty, fatigue_penalty, candidate_sources,
explanation
```

JSONL includes the full `explanation` dict.

### TUI changes (`tui/data.py`)

**`RecommendationListRow` additions:**
- `score_band: str`
- `identity_confidence: float`
- `trial_penalty: float`
- `identity_warnings: tuple[str, ...]`

**`ScoreBreakdown` additions:**
- `score_band`, `normalized_score`, `evidence_source_count`
- `trial_penalty`, `identity_confidence`, `identity_warnings`

**`RecommendationDetail` additions:**
- `score_band`, `identity_confidence`, `identity_warnings`
- `local_scrobble_count`, `listened_day_count`, `years_since_last_listen`
- `rediscovery_eligible`, `trial_penalty`
- `top_seed_artists` (separate from existing `seed_artists` which shows similarity edges)

**List labels:** Classification column extended with `weak` and `cand` for weak/candidate_only bands.

**Detail panel:** Score breakdown section shows all new fields. Identity section shows confidence, warnings, MBID, URLs.

### CLI changes (`cli.py`)

- `export`: add `--include-candidates` flag
- `resolve`: reimplement from stub
- `doctor`: add identity resolution status check

### Acceptance Criteria

- Exported recs are fully inspectable without opening raw JSON
- TUI detail view shows all score components, identity info, listening diagnostics
- Tests assert presence and correctness of explanation fields
- Human-readable summary reflects actual evidence composition

## Implementation Order

1. Schema migrations: `009_score_bands.sql`, `010_listening_diagnostics.sql`, `011_identity_fields.sql`
2. Score band classification + export gating (Deliverable 1)
3. Trial penalty + rediscovery eligibility (Deliverable 2)
4. Identity confidence heuristics (Deliverable 3)
5. MusicBrainz resolve client + command + `012_identity_resolution.sql` (Deliverable 4)
6. Richer explanations + full export columns + TUI debug view (Deliverable 5)
7. README and `docs/implementation-log.md` updates
8. Full test suite: `pytest`, `ruff check`, `ruff format --check`, `mypy src`

## Testing Strategy

- Use existing `tests/fixtures/tiny_lastfm_export.csv` and deterministic data
- Mock `LastFmClient` pattern extended to `MusicBrainzClient` for resolve tests
- Score band tests: verify classification thresholds, export filtering, --include-candidates
- Trial penalty tests: 1-scrobble, 2-scrobble, multi-day, feedback-positive cases
- Identity confidence tests: short names, MBID-present, MBID-absent, conflict cases
- Resolve tests: mock MusicBrainz search/lookup responses, verify resolution table writes
- Export tests: assert all new columns present, correct values
- TUI data tests: verify data classes carry new fields, detail view construction
