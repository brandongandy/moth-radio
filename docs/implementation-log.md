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
