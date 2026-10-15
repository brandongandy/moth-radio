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
