# Implementation log

## 2026-10-15 — Task 1: deterministic control and rolling replay

- Captured the unchanged legacy and walk scorers in `tests/fixtures/control_recommendation_fingerprint.json`: ranked artist IDs, score bands, component values rounded to eight decimal places, candidate eligibility states/reasons, and sorted explanation keys. The existing deterministic fake Last.fm graph is built in separate temporary databases for each engine. Test-only walk settings allow its one-seed graph to produce recommendations. The fixture contains no timestamps, UUIDs, local paths, or unordered database output.
- Added frozen evaluation window/report contracts and rolling replay through the existing single-window evaluator, retaining its metric calculations and per-window historical listening/feedback cutoffs. API windows retain input order; report means weight each window equally.
- Added candidate source and identity diagnostics. Task 1's binding source ruling uses positive `appeared_from_seed_count`/`appeared_from_tag_count` as `lastfm`/`lastfm_tag` evidence; legacy `candidate_sources` contains seed IDs and is deliberately not parsed. Candidates can count in both groups; future hits intersect the same groups with the existing bounded first-listen outcomes. Provider-keyed evidence remains assigned to a later task.
- Applied the binding identity ruling: all distinct candidate artists form the denominator, `resolved_musicbrainz` is resolved, and `ambiguous_musicbrainz`/`provisional_mbid_conflict` are ambiguous. Other statuses do not count toward either rate; empty populations return zero. High identity confidence and provisional MBIDs alone do not establish resolution.
- Added `evaluate-rolling` with required repeatable cutoffs, normalized UTC sorting/deduplication, a positive horizon (default 90 days), per-window rows, and mean metrics. The existing `evaluate` command retains its behavior. Additive diagnostic fields have defaults to preserve existing `EvaluationResult` construction.
- Included `tests/test_recommend.py` as required by the task steps and commit list, despite its omission from the brief's initial Files block. No scorer, schema, provider, query, or later-slice behavior was changed.

### Test-first evidence

- Fingerprint RED: the prescribed focused test failed because the fixture did not exist. After recording only the unchanged implementation's output and reviewing the stable fields, it passed.
- Evaluation RED: the original nine evaluator tests passed while twelve new cases failed on missing rolling API/contracts or diagnostic fields. Minimal evaluator implementation was followed by focused GREEN. Fixture investigation corrected an expired 12-month seed window and an assumption that enriched data implies an empty candidate pool; scorer behavior was left intact.
- CLI RED: five rolling CLI cases failed because `evaluate-rolling` was absent; all five passed after adding the command. An initial test collection typo (missing pytest import) was corrected before recording this RED run.
- Coverage includes distinct recall and identity means, reversed API window order, empty windows/candidates, exact resolution statuses, candidates with overlapping sources, future hits on excluded candidates, malformed legacy JSON, historical scrobble/feedback cutoffs, sorted/deduplicated CLI cutoffs, custom/default horizons, and invalid/missing CLI options.

### Verification

- `.venv/bin/python -m pytest tests/test_recommend.py tests/test_evaluate.py tests/test_cli.py -v` — 138 passed.
- `.venv/bin/python -m pytest` — 401 passed, no failures (25.05 seconds).
- `.venv/bin/python -m ruff check .` — all checks passed.
- `.venv/bin/python -m ruff format --check .` — 74 files already formatted.
- `.venv/bin/python -m mypy src` — no issues in 41 source files.
- `git diff --check` — clean. Self-review confirmed that the changes reuse existing metric/cutoff logic, preserve the existing evaluation command, contain no real API keys or local user paths, and leave later tasks in their approved order.

## 2026-10-15 — Task 2: immutable recommendation query context

- Added frozen recommendation request, query context, weighted taste-region seed, target relationship, and version contracts. JSON uses UTC-naive ISO timestamps and sorted seed IDs/region names; general-query JSON omits target-only fields. `request=None` retains the general-query default. Valid artist requests raise a clear not-yet-implemented `ValueError` before producing a run; artist execution remains Task 7.
- Migration `015_recommendation_query_context.sql` adds query kind/target, context JSON, evidence snapshot ID, and feature/fusion/ranker/slate-policy columns. Historical rows backfill `query_kind='general'` and retain null context/version fields. Applying all migrations twice is safe.
- General runs capture the selected seed weights, requested history window, `local_history_end` history cutoff, `historical_cutoff` feedback cutoff, mode, novelty intent, config hash, source freshness, and version identity. Explicitly absent cutoffs stay null. Code version stays null when a trustworthy deployed revision is unavailable. The existing scoring-engine column remains separate; legacy and walk runs record `legacy-v1` and `walk-v1` rankers respectively, while the versions contract defaults to `legacy-v1`.
- Only the snapshot translates legacy selector names to product regions: lifetime/core, recent/current, old_obsession/dormant, underexplored/curiosity. Existing feedback seed regions remain inspectable. Controller-approved novelty mapping: balanced/mixed; rediscovery and old/rediscover; safe, explore, recent, anti-obvious, and tag/discover. This records current policy without choosing another engine or altering ranking.
- Evidence IDs are SHA-256 hashes of canonical source row-count/maximum-timestamp summaries for similarity edges, artist tags, tag top artists, and identity resolutions. Cutoffs include evidence exactly at the cutoff; missing/empty sources become zero/null. These are lightweight snapshot identifiers: content updates preserving both count and maximum timestamp do not change the ID. Richer observations remain Task 4.
- JSONL exports add optional full persisted context and scalar query/version provenance. CSV adds scalar provenance columns only. TUI `RunSummary` exposes optional parsed context/scalars and continues reading historical rows; no screen or live lookup was added. Existing recommendation rows, explanations, and legacy `candidate_sources` semantics are unchanged.

### Test-first evidence

- Contract/hash RED: `.venv/bin/python -m pytest tests/test_recommendation_query.py -q` — 11 failed because the query module did not exist. After implementing the contracts and hash, the same command passed all 11 tests.
- Migration/orchestrator RED: `.venv/bin/python -m pytest tests/test_recommend.py -q -k 'migration or fingerprint or mode_persists or artist_request'` — 12 failed, 3 passed, 63 deselected, exposing absent columns and the missing request argument. The migration and context persistence made these pass. An initial balanced-region fixture lacked the two-year dormancy needed by the existing seed selector; extending its history window to January 2026 demonstrated all four product regions without changing selection logic.
- Persisted-reader RED: `.venv/bin/python -m pytest tests/test_recommend.py tests/test_export.py tests/test_tui_data.py -q -k 'fingerprint or mode_persists or query_provenance or optional_persisted'` — 3 failed, 10 passed, 89 deselected because export provenance and TUI context fields were absent. Added those projections and fixed a DuckDB grouping error exposed by the focused suite; all focused tests then passed.
- The frozen Task 1 fixture remains unchanged for legacy and walk scores, ordered artists, eligibility reasons, and explanation keys. Identical repeated queries persist identical context JSON; existing full explanation/score determinism coverage remains green.

### Verification

- `.venv/bin/python -m pytest tests/test_recommendation_query.py tests/test_recommend.py tests/test_export.py tests/test_tui_data.py -v` — 113 passed (5.80 seconds).
- `.venv/bin/python -m pytest` — 425 passed, no failures (26.99 seconds).
- `.venv/bin/python -m ruff check .` — all checks passed.
- `.venv/bin/python -m ruff format --check .` — 76 files already formatted.
- `.venv/bin/python -m mypy src` — no issues in 42 source files.
- `git diff --check` — clean. Tests use local deterministic fixtures/fake clients and temporary database files; no real API keys or local user paths were added.
