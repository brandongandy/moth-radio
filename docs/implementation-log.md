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

## 2026-06-04: Task 2 multi-region seed selection

Changed:

- Added review-driven coverage for overlapping lifetime/recent seed regions that previously underfilled the unique seed list.
- Added re-review coverage for two duplicate-producing backfill regions that share the same backfill artist.
- Added duplicate-aware backfill for underfilled region selections while preserving proportional allocation against the caller limit.
- Fixed shared backfill artists to accumulate each missing backfill region's weighted score and region label without double-counting a region already assigned to that artist.
- Limited each region's backfill pass to the number of region slots lost to duplicate initial picks, preventing later selected artists from receiving unrelated region weights.
- Prioritized unselected replacement seeds before applying deferred backfill region scores to already-selected artists, so duplicate backfill accounting cannot underfill the unique seed list when eligible replacements exist.
- Treated an explicitly empty seed region sequence as no seed picks instead of falling back to the default recent region.
- Preserved final seed ranking by accumulated weighted score descending, then name, capped to the caller limit.

Verified:

- `python -m pytest tests/test_profile.py::test_select_seed_artists_backfills_duplicate_region_picks -v` before implementation -> failed because only the two overlapping artists were returned instead of four unique eligible seeds
- `python -m pytest tests/test_profile.py::test_select_seed_artists_backfills_duplicate_region_picks -v` after implementation -> 1 passed
- `python -m pytest tests/test_profile.py::test_select_seed_artists_accumulates_shared_backfill_region -v` before re-review fix -> failed because `Shared Backfill` had seed weight `4.0` instead of `6.0`
- `python -m pytest tests/test_profile.py::test_select_seed_artists_empty_regions_selects_no_seeds -v` before re-review fix -> failed because explicit empty regions fell back to the default recent pick
- `python -m pytest tests/test_profile.py::test_select_seed_artists_accumulates_shared_backfill_region -v` after re-review fix -> 1 passed
- `python -m pytest tests/test_profile.py::test_select_seed_artists_empty_regions_selects_no_seeds -v` after re-review fix -> 1 passed
- `python -m pytest tests/test_profile.py::test_select_seed_artists_limits_backfill_to_missing_region_slots -v` before final review fix -> failed because `Lifetime Only` had seed weight `16.0` instead of `9.0`
- `python -m pytest tests/test_profile.py::test_select_seed_artists_limits_backfill_to_missing_region_slots -v` after final review fix -> 1 passed
- `python -m pytest tests/test_profile.py::test_select_seed_artists_prioritizes_unique_backfill_before_shared_region -v` before final spec fix -> failed because `Recent Backfill` was omitted even though it had a positive region score
- `python -m pytest tests/test_profile.py::test_select_seed_artists_prioritizes_unique_backfill_before_shared_region -v` after final spec fix -> 1 passed
- `python -m pytest tests/test_profile.py -v` -> 10 passed
- `python -m pytest tests/test_recommend.py -v` -> 5 passed
- `python -m mypy src` -> Success, no issues found in 21 source files
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 34 files already formatted

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

## 2026-06-04: Task 4 export persisted recommendations

Changed:

- Added CSV and JSONL export for persisted recommendation runs.
- Export rows are loaded from local DuckDB recommendation tables and joined to artist names.
- CSV output includes rank, artist, score, primary tags, knownness, obviousness, rediscovery flag, explanation summary, and source URLs without embedding full explanation JSON.
- JSONL output includes the full row with inspectable explanation JSON.
- Updated the Task 4 plan checklist and documented the `format_name` typing deviation.
- Addressed review findings by validating unsupported formats before DB reads or writes, URL-encoding Last.fm artist path segments, and adding a deterministic latest-run fallback tie-breaker.

Verified:

- `python -m pytest tests/test_export.py -v` before implementation -> failed with `ModuleNotFoundError: No module named 'scrobble_recs.export'`
- `python -m pytest tests/test_export.py -v` after implementation -> 2 passed
- `python -m pytest` -> 85 passed
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 32 files already formatted
- `python -m mypy src` -> Success, no issues found in 20 source files
- `python -m pytest tests/test_export.py -v` with review regression tests before fixes -> 3 failed and 2 passed; failures covered invalid-format precedence, Last.fm URL encoding, and latest-run tie-breaking.
- `python -m pytest tests/test_export.py -v` after review fixes -> 5 passed
- `python -m pytest` after review fixes -> 88 passed
- `python -m mypy src` after review fixes -> Success, no issues found in 20 source files
- `python -m ruff check .` after review fixes -> All checks passed
- `python -m ruff format --check .` after review fixes -> 32 files already formatted

## 2026-06-04: Milestone 3 first recommender

Changed:

- Added recommendation run and recommendation result tables.
- Added listening weight, knownness, and rediscovery score formulas.
- Added offline recommendation generation from persisted Last.fm similarity and tag data.
- Added knownness and obviousness penalties.
- Added inspectable JSON explanations for each persisted recommendation.
- Added CSV and JSONL export for persisted recommendation runs.
- Added `recommend` and `export` CLI commands.

Verified:

- `python -m pytest tests/test_knownness.py tests/test_recommend.py tests/test_export.py tests/test_cli.py -v`
- `python -m pytest`
- `python -m ruff check .`
- `python -m ruff format --check .`
- `python -m mypy src`

## 2026-06-04: Recommender quality pass

Changed:

- Added recommendation mode settings for balanced, explore, safe, rediscovery, recent, old, anti-obvious, and tag-focused modes.
- Changed seed selection to combine lifetime, recent, old-obsession, and underexplored local taste regions.
- Added mode-specific knownness thresholds, obviousness penalties, rediscovery weighting, and tag-focus boosts.
- Added default hard-hide behavior for rank 1-3 high-affinity top-tag artists, with rediscovery candidates allowed through.
- Added maximal marginal relevance diversity reranking and a same-primary-tag cap for the top 20.
- Persisted diversity penalties and richer mode/seed metadata in recommendation runs.

Verified:

- `python -m pytest tests/test_modes.py tests/test_profile.py tests/test_recommend.py tests/test_cli.py -v`
- `python -m pytest`
- `python -m ruff check .`
- `python -m ruff format --check .`
- `python -m mypy src`

## 2026-06-04: Task 1 feedback schema migration

Changed:

- Added the DuckDB `feedback_event` table with lookup indexes for feedback ID, run ID, artist ID, and action.
- Added schema migration coverage that initializes a temporary DuckDB database, asserts the feedback table exists with ordered column names and types, inserts a deterministic feedback event, and verifies duplicate feedback IDs are rejected.
- Updated the existing database initialization schema expectation to include the feedback event table.
- Updated the Task 1 feedback-loop plan checklist and documented the commit-content deviation for the implementation log and schema assertion update.

Verified:

- `python -m pytest tests/test_feedback.py::test_feedback_migration_creates_event_table -v` before migration -> failed with `AssertionError: assert 'feedback_event' in ...`
- `python -m pytest tests/test_feedback.py::test_feedback_migration_creates_event_table -v` after migration -> 1 passed
- `python -m pytest` before updating the full-schema assertion -> failed with `Extra items in the left set: 'feedback_event'`
- `python -m pytest tests/test_config.py::test_initialize_database_applies_initial_migration -v` after updating the full-schema assertion -> 1 passed
- `python -m pytest` -> 122 passed
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 35 files already formatted
- `python -m mypy src` -> Success, no issues found in 21 source files

## 2026-06-04: Task 2 feedback event API and profile

Changed:

- Added the local feedback model API for immutable `feedback_event` writes with allowed-action validation, deterministic context normalization, compact sorted JSON serialization, optional deterministic timestamps, and generated feedback IDs.
- Added deterministic feedback profile derivation from persisted events, including direct artist adjustments, tag adjustments, obviousness tag adjustments, and hidden/known/boosted/suppressed seed sets.
- Added feedback helper functions for later recommendation scoring and tag profile adjustment.
- Added behavioral tests for feedback event persistence, unsupported action rejection, and artist/tag/seed profile derivation.
- Strengthened the event persistence test after review to verify optional `created_at` persistence plus sorted JSON keys and sorted/normalized/de-duplicated context tags.
- Fixed review-reported profile loading so explicit persisted `value=0.0` remains zero instead of defaulting to `1.0`.
- Added behavioral tests for `feedback_adjustment_for_candidate`, `feedback_obviousness_for_candidate`, and `apply_feedback_to_tag_profile`, covering clamping plus tag normalization and suppression behavior.
- Updated the Task 2 feedback-loop plan checklist and documented deviations for DuckDB UUID assertion casting, the strengthened context/timestamp assertion, expanded review coverage, and including the implementation log in the commit.

Verified:

- `python -m pytest tests/test_feedback.py::test_record_feedback_event_validates_action_and_persists_json_context tests/test_feedback.py::test_record_feedback_event_rejects_unknown_action tests/test_feedback.py::test_load_feedback_profile_derives_artist_tag_and_seed_adjustments -v` before implementation -> failed with `ModuleNotFoundError: No module named 'scrobble_recs.models.feedback'`
- `python -m pytest tests/test_feedback.py::test_record_feedback_event_validates_action_and_persists_json_context tests/test_feedback.py::test_record_feedback_event_rejects_unknown_action tests/test_feedback.py::test_load_feedback_profile_derives_artist_tag_and_seed_adjustments -v` after initial implementation -> 1 failed, 2 passed because DuckDB returned `run_id` as `uuid.UUID`; the test now casts `run_id` to `VARCHAR` before comparison.
- `python -m pytest tests/test_feedback.py::test_record_feedback_event_validates_action_and_persists_json_context tests/test_feedback.py::test_record_feedback_event_rejects_unknown_action tests/test_feedback.py::test_load_feedback_profile_derives_artist_tag_and_seed_adjustments -v` after UUID assertion adjustment -> 3 passed
- `python -m pytest tests/test_feedback.py -v` -> 4 passed
- `python -m ruff check src/scrobble_recs/models/feedback.py tests/test_feedback.py` -> All checks passed
- `python -m mypy src` -> Success, no issues found in 22 source files
- `python -m pytest` -> 125 passed
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 36 files already formatted
- Review follow-up: `python -m pytest tests/test_feedback.py::test_record_feedback_event_validates_action_and_persists_json_context tests/test_feedback.py::test_record_feedback_event_rejects_unknown_action tests/test_feedback.py::test_load_feedback_profile_derives_artist_tag_and_seed_adjustments -v` -> 3 passed
- Review follow-up: `python -m pytest tests/test_feedback.py -v` -> 4 passed
- Second review follow-up: `python -m pytest tests/test_feedback.py::test_load_feedback_profile_preserves_explicit_zero_value -v` before fix -> failed with `assert 0.15 == 0.0`
- Second review follow-up: `python -m pytest tests/test_feedback.py::test_load_feedback_profile_preserves_explicit_zero_value -v` after fix -> 1 passed
- Second review follow-up: `python -m pytest tests/test_feedback.py::test_load_feedback_profile_preserves_explicit_zero_value tests/test_feedback.py::test_feedback_adjustment_for_candidate_combines_sources_and_clamps tests/test_feedback.py::test_feedback_obviousness_for_candidate_weights_tags_and_clamps tests/test_feedback.py::test_apply_feedback_to_tag_profile_suppresses_and_normalizes_tags -v` -> 4 passed
- Second review follow-up: `python -m pytest tests/test_feedback.py -v` -> 8 passed
- Second review follow-up: `python -m ruff check src/scrobble_recs/models/feedback.py tests/test_feedback.py` -> All checks passed
- Second review follow-up: `python -m mypy src` -> Success, no issues found in 22 source files
- Final review follow-up: `python -m ruff format --check .` before formatting -> failed with `Would reformat: tests\test_feedback.py`
- Final review follow-up: `python -m ruff format tests/test_feedback.py` -> 1 file reformatted
- Final review follow-up: `python -m ruff format --check .` -> 36 files already formatted
- Final review follow-up: `python -m pytest tests/test_feedback.py -v` -> 8 passed
- Final review follow-up: `python -m ruff check src/scrobble_recs/models/feedback.py tests/test_feedback.py` -> All checks passed

## 2026-06-04: Task 3 feedback-aware recommendation scoring

Changed:

- Added recommendation tests for direct feedback score boosts, hidden feedback artists, saved current candidates with outgoing edges, feedback seed weights in non-balanced modes, tag feedback, mixed-case tag feedback matching, knownness overrides, obviousness feedback, negative candidate demotion, and `more_like_this` feedback as a future local seed.
- Loaded the derived feedback profile during recommendation generation.
- Applied feedback-adjusted tag profiles, hidden artist exclusion, known-artist overrides, direct artist/tag feedback scores, and feedback-neighborhood scoring.
- Persisted nonzero `feedback_score` values and added contribution-tracked feedback metadata to recommendation explanations.
- Added feedback seed promotion for local artists with outgoing Last.fm similarity edges, while preserving boosted artists that are already current candidate targets.
- Kept feedback seed weights profile-derived so mode Last.fm multipliers apply only once during scoring.
- Normalized recommendation tag keys with `casefold()` for tag-profile, candidate-tag, feedback-score, and obviousness matching.
- Added direct candidate intent as a first-class artist feedback contribution when boosted or suppressed feedback artists remain candidates.
- Loaded feedback neighborhood edges for boosted and suppressed non-hidden feedback artists so both positive and negative neighborhood effects influence future candidates.
- Included configured zero-count mode regions and feedback seed counts in persisted recommendation run seed summaries.
- Updated the Task 3 feedback-loop plan checklist and documented the seed-promotion, feedback-neighborhood loading, feedback-seed weighting, tag-normalization, contribution-tracking, seed-summary, and implementation-log commit deviations.

Verified:

- `python -m pytest tests/test_recommend.py::test_generate_recommendations_applies_direct_feedback_scores tests/test_recommend.py::test_generate_recommendations_excludes_hidden_feedback_artist tests/test_recommend.py::test_generate_recommendations_uses_more_like_this_as_future_seed -v` before implementation -> 3 failed because saved feedback was not scored, hidden artists were still returned, and feedback seeds were ignored.
- `python -m pytest tests/test_recommend.py::test_generate_recommendations_applies_direct_feedback_scores tests/test_recommend.py::test_generate_recommendations_excludes_hidden_feedback_artist tests/test_recommend.py::test_generate_recommendations_uses_more_like_this_as_future_seed -v` after initial implementation -> 1 failed, 2 passed because the default `feedback_model` weight was not enough for the direct-save ranking fixture.
- `python -m pytest tests/test_recommend.py::test_generate_recommendations_applies_direct_feedback_scores tests/test_recommend.py::test_generate_recommendations_excludes_hidden_feedback_artist tests/test_recommend.py::test_generate_recommendations_uses_more_like_this_as_future_seed -v` after the candidate seed-intent adjustment -> 3 passed.
- Review follow-up: `python -m pytest tests/test_recommend.py::test_generate_recommendations_applies_direct_feedback_scores tests/test_recommend.py::test_generate_recommendations_keeps_saved_current_candidate_with_outgoing_edges tests/test_recommend.py::test_generate_recommendations_uses_more_like_this_as_future_seed tests/test_recommend.py::test_generate_recommendations_applies_tag_feedback_scores tests/test_recommend.py::test_generate_recommendations_marks_already_known_candidates_with_knownness_penalty tests/test_recommend.py::test_generate_recommendations_applies_too_obvious_tag_feedback_penalty tests/test_recommend.py::test_generate_recommendations_demotes_suppressed_feedback_candidate -v` before follow-up implementation -> 7 failed because effects were not contribution-based, saved current candidates with outgoing edges could be promoted out of candidates, and feedback contributions were not persisted.
- Review follow-up: `python -m pytest tests/test_recommend.py::test_generate_recommendations_applies_direct_feedback_scores tests/test_recommend.py::test_generate_recommendations_keeps_saved_current_candidate_with_outgoing_edges tests/test_recommend.py::test_generate_recommendations_uses_more_like_this_as_future_seed tests/test_recommend.py::test_generate_recommendations_applies_tag_feedback_scores tests/test_recommend.py::test_generate_recommendations_marks_already_known_candidates_with_knownness_penalty tests/test_recommend.py::test_generate_recommendations_applies_too_obvious_tag_feedback_penalty tests/test_recommend.py::test_generate_recommendations_demotes_suppressed_feedback_candidate -v` after follow-up implementation -> 7 passed.
- Review follow-up: `python -m pytest tests/test_recommend.py::test_generate_recommendations_applies_direct_feedback_scores tests/test_recommend.py::test_generate_recommendations_excludes_hidden_feedback_artist tests/test_recommend.py::test_generate_recommendations_uses_more_like_this_as_future_seed -v` -> 3 passed.
- Review follow-up: `python -m pytest tests/test_recommend.py -v` -> 22 passed.
- Review follow-up: `python -m pytest` -> 137 passed.
- Second review follow-up: `python -m pytest tests/test_recommend.py::test_generate_recommendations_does_not_mode_multiply_feedback_seed_weight tests/test_recommend.py::test_generate_recommendations_matches_feedback_to_mixed_case_candidate_tags -v` before follow-up implementation -> 2 failed because safe-mode feedback seed weight was mode-multiplied to `0.30` and lowercase feedback did not match mixed-case stored candidate tags.
- Second review follow-up: `python -m pytest tests/test_recommend.py::test_generate_recommendations_does_not_mode_multiply_feedback_seed_weight tests/test_recommend.py::test_generate_recommendations_matches_feedback_to_mixed_case_candidate_tags -v` after follow-up implementation -> 2 passed.
- Second review follow-up: `python -m pytest tests/test_recommend.py::test_generate_recommendations_applies_direct_feedback_scores tests/test_recommend.py::test_generate_recommendations_excludes_hidden_feedback_artist tests/test_recommend.py::test_generate_recommendations_uses_more_like_this_as_future_seed -v` -> 3 passed.
- Second review follow-up: `python -m pytest tests/test_recommend.py -v` -> 24 passed.
- Second review follow-up: `python -m pytest` -> 139 passed.
- Final review follow-up: `python -m pytest tests/test_recommend.py::test_generate_recommendations_applies_suppressed_feedback_neighborhood -v` before follow-up implementation -> failed because the future candidate's feedback score was `0.0`; suppressed feedback source neighborhood edges were not loaded.
- Final review follow-up: `python -m pytest tests/test_recommend.py::test_generate_recommendations_applies_suppressed_feedback_neighborhood -v` after follow-up implementation -> 1 passed.
- Final review follow-up: `python -m pytest tests/test_recommend.py::test_generate_recommendations_applies_direct_feedback_scores tests/test_recommend.py::test_generate_recommendations_excludes_hidden_feedback_artist tests/test_recommend.py::test_generate_recommendations_uses_more_like_this_as_future_seed -v` -> 3 passed.
- Final review follow-up: `python -m pytest tests/test_recommend.py -v` -> 25 passed.
- Final review follow-up: `python -m pytest` -> 140 passed.
- `python -m ruff check .` -> All checks passed.
- `python -m ruff format --check .` -> 36 files already formatted.
- `python -m mypy src` -> Success, no issues found in 22 source files.

## 2026-06-04: Task 4 lightweight current-run rerank

Changed:

- Added `rerank_recommendation_run` to update persisted recommendation rows for a run without live API calls or full retraining.
- Added rerank stats for visible and hidden recommendation counts.
- Applied derived feedback scores to persisted recommendation rows, updated ranks, and wrote inspectable feedback metadata into explanations.
- Hid current-run recommendations marked by feedback actions such as `too_obvious`.
- Fixed review-reported repeat reranking so existing persisted `feedback_score` is removed from `final_score` before applying the newly derived feedback score, preventing compounding on repeated feedback reranks.
- Fixed review-reported current-run hide leakage by deriving current-run hide IDs for the specific reranked run while preserving global `hide_artist` suppression.
- Fixed the `_explanation_tag_weights` local `None` case so `python -m mypy src` passes without weakening module typing.
- Updated the Task 4 feedback-loop plan checklist and documented deviations for the non-compounding rerank adjustment plus including this implementation log in the commit.

Verified:

- `python -m pytest tests/test_feedback.py::test_rerank_recommendation_run_demotes_rejected_artist_and_updates_explanation tests/test_feedback.py::test_rerank_recommendation_run_hides_current_too_obvious_artist -v` before implementation -> failed with `ImportError: cannot import name 'rerank_recommendation_run'`
- `python -m pytest tests/test_feedback.py::test_rerank_recommendation_run_demotes_rejected_artist_and_updates_explanation tests/test_feedback.py::test_rerank_recommendation_run_hides_current_too_obvious_artist -v` after implementation -> 2 passed
- Review follow-up: `python -m pytest tests/test_feedback.py::test_rerank_recommendation_run_does_not_compound_existing_feedback_score -v` before fix -> failed because second rerank changed the recommendation score from `(1.05, 0.25)` to `(1.3, 0.25)`
- Review follow-up: `python -m pytest tests/test_feedback.py::test_rerank_recommendation_run_does_not_compound_existing_feedback_score -v` after fix -> 1 passed
- Review follow-up: `python -m pytest tests/test_feedback.py::test_rerank_recommendation_run_demotes_rejected_artist_and_updates_explanation tests/test_feedback.py::test_rerank_recommendation_run_hides_current_too_obvious_artist tests/test_feedback.py::test_rerank_recommendation_run_does_not_compound_existing_feedback_score -v` -> 3 passed
- Review follow-up: `python -m pytest tests/test_feedback.py -v` -> 11 passed
- Review follow-up: `python -m ruff check src/scrobble_recs/models/feedback.py tests/test_feedback.py` -> All checks passed
- Second review follow-up: `python -m pytest tests/test_feedback.py::test_rerank_recommendation_run_does_not_hide_current_feedback_from_other_runs -v` before fix -> failed because reranking run B hid an artist from run A's `too_obvious` feedback.
- Second review follow-up: `python -m pytest tests/test_feedback.py::test_rerank_recommendation_run_does_not_hide_current_feedback_from_other_runs -v` after fix -> 1 passed
- Second review follow-up: `python -m pytest tests/test_feedback.py::test_rerank_recommendation_run_demotes_rejected_artist_and_updates_explanation tests/test_feedback.py::test_rerank_recommendation_run_hides_current_too_obvious_artist tests/test_feedback.py::test_rerank_recommendation_run_does_not_compound_existing_feedback_score tests/test_feedback.py::test_rerank_recommendation_run_does_not_hide_current_feedback_from_other_runs -v` -> 4 passed
- Second review follow-up: `python -m pytest tests/test_feedback.py -v` -> 12 passed
- Second review follow-up: `python -m mypy src` -> Success, no issues found in 22 source files
- Second review follow-up: `python -m ruff check src/scrobble_recs/models/feedback.py tests/test_feedback.py` -> All checks passed
- Final verification: `python -m ruff format --check .` before formatting -> Would reformat `src\scrobble_recs\models\feedback.py`
- Final verification: `python -m ruff format src/scrobble_recs/models/feedback.py` -> 1 file reformatted
- Final verification: `python -m pytest` -> 144 passed
- Final verification: `python -m ruff check .` -> All checks passed
- Final verification: `python -m ruff format --check .` -> 36 files already formatted
- Final verification: `python -m mypy src` -> Success, no issues found in 22 source files

## 2026-06-04: Feedback loop

Changed:

- Added the local `feedback_event` table and indexes.
- Added feedback event validation and persistence for the spec-defined action set.
- Added derived feedback profiles for artist suppression/boosts, tag suppression/boosts, known-artist suppression, and feedback seed boosts.
- Added feedback-aware recommendation scoring, including persisted `feedback_score` values and explanation feedback metadata.
- Added lightweight current-run reranking after feedback without live API calls or full model retraining.
- Added `scrobble-recs feedback` to record an event and rerank the target run by default.
- Made the default feedback record-and-rerank CLI path atomic so failed reranks do not leave durable feedback events.

Verified:

- `python -m pytest tests/test_feedback.py tests/test_recommend.py tests/test_cli.py -v`
- `python -m pytest`
- `python -m ruff check .`
- `python -m ruff format --check .`
- `python -m mypy src`
