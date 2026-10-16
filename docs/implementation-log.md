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

## 2026-10-15 — Task 3: explicit recommendation pipeline stages

- Extracted `retrieval.py`, `ranking.py`, `policy.py`, and `recommendation_store.py`. `recommend.py` now resolves requests/windows/seeds/profiles, captures the unchanged query context, emits progress, and calls the stages. It contains no provider SQL, recommendation formulas, MMR, or inserts. Existing CLI progress/stats imports and the identity-confidence helper remain compatible through re-exports.
- Added frozen, defensively copied retrieval, relevance, ranked-candidate, policy, and run-payload contracts. Explicit typed legacy details retain the full graph/walk state, identity/listening facts, feedback, gate evidence, complete candidate diagnostics, explanation inputs, and result scalars. No schema, version, source-provider vocabulary, or query-context change was needed.
- Candidate scores retain the legacy weighted raw score; relevance exposes the identity-adjusted score plus serendipity before policy penalties. Policy decisions report final scores, adjustments, and inclusion/exclusion/movement reasons. Legacy admission stays before candidate-set calibration through policy-owned helpers, preserving the calibration population and floating-point arithmetic order. Ranking respects a narrowed supplied retrieval pool without mutating it.
- Retrieval keeps direct edges, tag-only membership, feedback-seed reachability, historical listening cutoffs, and bounded walk behavior. Optional absent tag/feedback/listening/walk signals are named explicitly. Exact score and MMR ties now use artist IDs, as ruled by the controller; non-tied behavior and both frozen control fixtures remain unchanged.
- The store's public entry point owns one transaction covering run, candidate, and recommendation rows. Candidate insertion/progress and result insertion failures roll back all three tables. Legacy candidate sources remain seed IDs, and candidate-only rank offsets, URLs, JSON ordering, full explanations, and run bytes remain compatible.
- Controller-approved runtime compatibility inputs carry the real `as_of`, effective mode, and history-window source without changing persisted `QueryContext`. Bare retrieval calls use the context's history-window end as a deterministic `as_of`; callers needing a distinct runtime time or tag-focus mode supply the typed compatibility inputs. The orchestrator always supplies its actual values. A test pins an explicit window ending in 2025 with runtime scoring in 2030.
- Feedback behavior is unchanged: current quality/admission rejections, hidden artists, and active feedback cooldowns remain excluded. Historical `reject`/`less_like_this` votes after cooldown remain negative relevance rather than newly invented permanent bans. Task 2's duplicate evidence-summary read remains assigned to Task 4.

### Test-first evidence

- Boundary RED: the four new stage test files initially produced 8 failures because the stage modules were absent; after the extraction and copied contracts, all 8 passed.
- Integration RED: moving the candidate-failure injection to the store exposed that orchestration still used its old persistence helper (`DID NOT RAISE`). Wiring the public stages made the strengthened rollback test pass; the focused recommendation/stage suite then passed 88 tests, including unchanged full control explanations, run determinism, and exact progress order.
- Policy RED: two cases exposed missing policy-owned admission and a zero fatigue adjustment folded into relevance. After extracting those helpers and exposing pre-policy relevance, the policy/recommendation suite passed 82 tests.
- Subsequent RED/GREEN cycles caught ranking ignoring a narrowed retrieval tuple, payloads retaining caller-owned sequences/configuration, a generic tag-cap reason, and a missing listener tag-profile indicator. Each now has a deterministic regression test. A walk characterization initially assumed invalid edges removed nodes entirely; inspection confirmed the unchanged walker retains a zero-personalized-mass/non-finite-lift node, which ranking excludes. The test was corrected without changing walker behavior.
- Coverage includes direct/tag/walk/feedback/empty retrieval, exclusive historical cutoffs, eligibility-only and narrowed-pool calibration, legacy/walk non-finite projection/rejection, missing signals, nonmutation, ID ties with and without MMR, fatigue adjustments, tag caps, current quality rejection, hidden/cooldown exclusions, a forced second-result insertion failure after one real result was inserted, and unchanged legacy source encoding.

### Verification

- `.venv/bin/python -m pytest tests/test_retrieval.py tests/test_ranking.py tests/test_policy.py tests/test_recommendation_store.py tests/test_recommend.py -q` — 109 passed (5.83 seconds).
- `.venv/bin/python -m pytest` — 456 passed, no failures (27.80 seconds).
- `.venv/bin/python -m ruff check .` — all checks passed.
- `.venv/bin/python -m ruff format --check .` — 84 files already formatted.
- `.venv/bin/python -m mypy src` — no issues in 46 source files.
- `git diff --check` — clean. All tests use deterministic local fixtures/fakes and temporary database files. No real credentials, committed local user paths, live API calls, or later-task behavior were introduced.

### Review corrections and final verification

- Closed the compatibility-ownership gaps: ranking now takes one defensive snapshot of the legacy retrieval graph and shares that owned snapshot across its returned records; the run payload separately owns its retrieval snapshot. Mutating caller-owned profiles, feedback, names, edges, or diagnostics after constructing either boundary no longer changes the returned ranking or the candidate evidence written by the store. No graph is copied once per candidate, and the public dict annotations remain unchanged.
- Removed eager full explanation/path assembly from ranking. Ranked records expose inexpensive eligibility, raw/calibrated components, identity confidence, and serendipity information. The unchanged post-policy assembler builds each selected full explanation exactly once. Deterministic operation tests cover pools of 8 and 64 retained walk scores with a two-item slate: ranking performs zero full explanation/path searches, and finalization performs exactly two searches and two graph-edge traversals in both cases. Frozen selected explanations and progress ordering remain unchanged.
- Missing walk signals are now candidate-specific: absent nodes, negative hop distance, and non-finite lift explicitly name `walk` as missing while reachable nodes retain their walk signal. This changes only the new feature boundary, not legacy persisted explanation payloads.
- Review TDD RED/GREEN: two ownership cases first failed on mutated returned records/payloads, then passed after defensive snapshots; two operation-bound cases first recorded 8 and 64 eager searches, then passed with zero ranking searches and two selected searches; three missing-walk cases first omitted the signal, then passed for missing/negative-hop/non-finite nodes.
- `.venv/bin/python -m pytest tests/test_ranking.py tests/test_recommendation_store.py -q -k 'shared_compatibility_snapshot or owns_retrieval_evidence or full_walk_explanation_work or missing_walk_signal'` — 7 passed, 16 deselected (0.59 seconds).
- `.venv/bin/python -m pytest tests/test_retrieval.py tests/test_ranking.py tests/test_policy.py tests/test_recommendation_store.py tests/test_recommend.py -q` — 116 passed (5.34 seconds).
- `.venv/bin/python -m pytest` — 463 passed, no failures (25.87 seconds).
- Ruff lint passed; Ruff format reported 84 files already formatted; mypy reported no issues in 46 source files; `git diff --check` remained clean. Review corrections remain in the single amended Task 3 commit and do not include later-task or schema changes.
