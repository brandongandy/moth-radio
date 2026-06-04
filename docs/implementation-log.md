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

- `python -m pytest` -> 25 passed in 1.79s
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 13 files already formatted
- `python -m mypy src` -> Success, no issues found in 9 source files
- `scrobble-recs import --config config.example.json` -> Imported 6 scrobbles
- `scrobble-recs doctor --config config.example.json` -> Config: ok; Database: ok; CSV: ok; API keys: not required for import/doctor

## 2026-06-03: Task 1 API and recommendation config models

Changed:

- Added API and recommendation config models with default values.
- Updated `config.example.json` with API and recommendation defaults.
- Added and verified `DiversityConfig.lambda_` alias behavior for JSON `lambda`.

Verified:

- `python -m pytest tests/test_config.py -v` -> 5 passed
- `python -m pytest` -> 28 passed
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 13 files already formatted
- `python -m mypy src` -> Success, no issues found in 9 source files

## 2026-06-03: Task 2 enrichment schema migration

Changed:

- Added DuckDB enrichment tables for API cache entries, artist similarity edges, artist tags, and tag top artists.
- Added schema migration coverage for the enrichment tables.
- Updated database initialization table assertions to include the enrichment schema.

Verified:

- `python -m pytest tests/test_api_cache.py::test_enrichment_migration_creates_cache_and_normalized_tables -v` -> 1 passed
- `python -m pytest tests/test_config.py tests/test_api_cache.py -v` -> 6 passed
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 14 files already formatted

## 2026-06-03: Task 3 DuckDB API cache

Changed:

- Added the cache-first API package marker.
- Added canonical API cache keys using sorted JSON parameters and SHA-256 digests.
- Added DuckDB-backed cache writes and fresh cache reads for successful, unexpired responses.
- Added transaction-protected cache replacement so failed writes do not discard existing fresh cache rows.
- Added behavioral coverage for key stability, fresh and stale reads, non-2xx misses, and replacement rollback.

Verified:

- `python -m pytest tests/test_api_cache.py -v` -> 6 passed
- `python -m pytest` -> 34 passed
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 16 files already formatted
- `python -m mypy src` -> Success, no issues found in 11 source files

## 2026-06-03: Task 4 deterministic rate limiter

Changed:

- Added deterministic API rate limiter with injectable monotonic clock and sleep function.
- Added rate limiter tests for waiting until the next allowed request and skipping sleep after the interval has elapsed.
- Added review-follow-up coverage for invalid rates and the post-sleep state transition across a third wait call.
- Hardened rate validation to reject non-finite values such as `nan` and `inf`.
- Used approximate float assertion for the computed wait duration to avoid decimal representation brittleness.

Verified:

- `python -m pytest tests/test_rate_limit.py -v` before implementation -> failed with `ModuleNotFoundError: No module named 'scrobble_recs.apis.rate_limit'`
- `python -m pytest tests/test_rate_limit.py -v` after implementation and precision assertion fix -> 2 passed
- `python -m pytest tests/test_rate_limit.py -v` before finite-rate hardening -> 2 failed for `nan` and `inf`, 6 passed
- `python -m pytest tests/test_rate_limit.py -v` after finite-rate hardening -> 8 passed
- `python -m pytest` -> 42 passed
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 18 files already formatted
- `python -m mypy src` -> Success, no issues found in 12 source files

## 2026-06-03: Task 5 cache-first Last.fm client

Changed:

- Added a cache-first Last.fm client for `artist.getSimilar`, `artist.getTopTags`, and `tag.getTopArtists`.
- Added cache-hit behavior that skips rate limiting and HTTP when a fresh response is available.
- Added Last.fm response parsing that handles singleton and list payload shapes and normalizes missing numeric or MBID values.
- Added HTTP client ownership handling with explicit `close()`, `__enter__`, and `__exit__` behavior for internally created clients.
- Added behavioral tests for cache hits, config errors, payload normalization, default timeout, and lifecycle ownership.

Verified:

- `python -m pytest tests/test_lastfm_client.py -v` -> 9 passed
- `python -m ruff check src/scrobble_recs/apis/lastfm.py tests/test_lastfm_client.py docs/implementation-log.md`
- `python -m ruff format --check .`

## 2026-06-03: Task 6 seed artist selection

Changed:

- Added the `scrobble_recs.models` package for taste profile and recommendation model helpers.
- Added `SeedArtist` and `select_seed_artists()` to select enrichment seed artists from persisted local listening facts.
- Ordered seed artists by seed weight descending, then canonical artist name ascending, with a minimum scrobble filter.
- Added behavioral tests covering seed ordering, duplicate-aware play counts from the Last.fm fixture, minimum scrobble filtering, Last.fm name fallback, and seed-weight source precedence.
- Updated the Task 6 plan checkboxes after each completed step.

Verified:

- `python -m pytest tests/test_profile.py -v` before implementation -> failed with `ModuleNotFoundError: No module named 'scrobble_recs.models'`
- `python -m pytest tests/test_profile.py -v` after implementation -> 2 passed
- `python -m pytest tests/test_profile.py -v` after review-driven coverage -> 3 passed
- `python -m pytest` -> 54 passed
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 23 files already formatted
- `python -m mypy src` -> Success, no issues found in 15 source files

## 2026-06-03: Task 7 Last.fm enrichment service

Changed:

- Added the offline-first enrichment package and Last.fm enrichment service.
- Added seed-based Last.fm similar artist, artist tag, and tag top artist persistence.
- Added idempotent artist creation and normalized writes for similarity edges, artist tags, and tag top artists.
- Added `enrichment_work` completion tracking so sparse, empty, and resumed work does not repeat completed requests or skip partial work.
- Added natural-key unique indexes and conflict-safe enrichment inserts for durable idempotence.
- Added re-import invalidation for profile-derived enrichment rows and completion markers while preserving `api_cache`.
- Added transactional limit-change replacement for completed similarity and tag top artist work so changed request limits prune stale rows without losing previous completed data on replacement failure.
- Added atomic first-run persistence so normalized rows and completion markers commit or roll back together.
- Added default Last.fm client cleanup when the enrichment service creates the client.
- Added behavioral coverage for persisted enrichment rows, resume behavior, sparse/empty completion, partial tag retry, natural-key uniqueness, re-import invalidation, successful limit-change replacement, failed replacement API preservation, failed replacement write rollback, first-run write rollback, tag-top replacement rollback, evidence JSON, timestamp shape, and client lifecycle.

Verified:

- `python -m pytest tests/test_lastfm_enrichment.py -v` -> 11 passed
- `python -m pytest` -> 66 passed
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 26 files already formatted
- `python -m mypy src` -> Success, no issues found in 17 source files

## 2026-06-03: Task 8 CLI enrich command

Changed:

- Added `scrobble-recs enrich --config <path> --source lastfm`.
- Wired the command to initialize the local DuckDB database, dispatch Last.fm enrichment, close the connection, and print enrichment stats.
- Added a CLI test that monkeypatches enrichment dispatch so no network access is required.
- Updated the Task 8 plan checkboxes after each completed step.

Verified:

- `python -m pytest tests/test_cli.py::test_enrich_command_dispatches_lastfm_source -v` before implementation -> failed with `AttributeError: 'module' object at scrobble_recs.cli has no attribute 'run_lastfm_enrichment'`
- `python -m pytest tests/test_cli.py::test_enrich_command_dispatches_lastfm_source -v` after implementation -> 1 passed
- `python -m pytest` -> 67 passed
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 26 files already formatted
- `python -m mypy src` -> Success, no issues found in 17 source files

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
- Added an early key-required failure for live Last.fm enrichment before seed selection.

Verified:

- `python -m pytest`
- `python -m ruff check .`
- `python -m ruff format --check .`
- `python -m mypy src`
- `scrobble-recs import --config config.example.json`
- `scrobble-recs doctor --config config.example.json`
- `scrobble-recs enrich --config config.example.json --source lastfm` without `LASTFM_API_KEY` fails with a clear key-required error.

## 2026-06-04: Task 1 recommendation schema migration

Changed:

- Added DuckDB `recommendation_run` and `recommendation` tables with unique indexes for run IDs, run ranks, and run artists.
- Added schema migration coverage that initializes a temporary DuckDB database, asserts the recommendation tables and column types exist, and verifies unique index behavior with duplicate inserts.
- Updated the existing database initialization schema expectation to include the recommendation tables.
- Updated the Task 1 plan checklist and noted the full-suite schema assertion deviation.

Verified:

- `python -m pytest tests/test_recommend.py::test_recommendation_migration_creates_run_and_result_tables -v` before migration -> failed with `AssertionError: assert 'recommendation_run' in ...`
- `python -m pytest tests/test_recommend.py::test_recommendation_migration_creates_run_and_result_tables -v` after migration -> 1 passed
- `python -m pytest` -> 72 passed
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 27 files already formatted
- `python -m mypy src` -> Success, no issues found in 17 source files

## 2026-06-04: Task 2 listening scores and knownness

Changed:

- Added formula-level artist listening score models for lifetime weight, recent weight, knownness, and rediscovery score.
- Added deterministic knownness tests for recent well-known suppression, old light-listen rediscovery, and negative count validation.
- Added review-driven validation for mixed naive/timezone-aware datetimes and `first_played_at` after `last_played_at`.
- Added review-driven tests for negative `track_count`, negative `active_months`, mixed timezone-awareness, and invalid play date ordering.
- Updated the Task 2 plan checklist and documented the formula-snippet mismatch deviation.

Verified:

- `python -m pytest tests/test_knownness.py -v` before implementation -> failed with `ModuleNotFoundError: No module named 'scrobble_recs.models.knownness'`
- `python -m pytest tests/test_knownness.py -v` after implementing the formula constants -> 3 passed
- `python -m pytest tests/test_knownness.py -v` after adding review-driven tests before validation -> 2 failed and 5 passed; mixed timezone-awareness raised `TypeError`, and invalid play date ordering did not raise
- `python -m pytest tests/test_knownness.py -v` after validation -> 7 passed
- `python -m pytest` -> 79 passed
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 29 files already formatted
- `python -m mypy src` -> Success, no issues found in 18 source files

## 2026-06-04: Task 3 persisted recommendation scoring

Changed:

- Added offline recommendation generation from persisted Last.fm similarity, artist tag, and tag top artist tables.
- Added persisted recommendation runs with score components, knownness and obviousness penalties, and inspectable explanation JSON.
- Added deterministic recommendation tests covering ranked run persistence, run isolation, explanation tag ordering, and stale similarity edges from non-current seeds.
- Fixed explanation tag selection to fetch all candidate tag rows, sort by local user tag weight, candidate rank, and tag name, then slice to five.
- Batch-loaded scoring inputs for candidate names, similarity edges, direct tags, tag ranks, and local listening facts, and deferred explanation construction until after top-N ranking.
- Fixed explanations to include only current seed similarity edges, ordered by actual seed contribution.

Verified:

- `python -m pytest tests/test_recommend.py -v` before implementation -> failed with `ModuleNotFoundError: No module named 'scrobble_recs.models.recommend'`
- `python -m pytest tests/test_recommend.py::test_generate_recommendations_orders_explanation_tags_before_slicing -v` before the tag-ordering fix -> failed with `AssertionError: assert 'post-punk' == 'coldwave'`
- `python -m pytest tests/test_recommend.py::test_generate_recommendations_explanation_uses_current_seed_edges_only -v` before the current-seed filter fix -> failed with `AssertionError: assert ['Cocteau Twins', 'The Cure'] == ['The Cure']`
- `python -m pytest tests/test_recommend.py -v` -> 5 passed
- `python -m mypy src` -> Success, no issues found in 19 source files
- `python -m ruff check src/scrobble_recs/models/recommend.py tests/test_recommend.py docs/implementation-log.md` -> All checks passed
- `python -m ruff format --check src/scrobble_recs/models/recommend.py tests/test_recommend.py` -> 2 files already formatted
- `python -m ruff format --check --preview docs/implementation-log.md` -> 1 file already formatted
