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

## 2026-10-16 — Task 4: normalized evidence, admission, and candidate details

- Added frozen, defensively copied `EvidenceObservation`, `CandidateEvidence`, and `AdmissionDecision` contracts. Families and relationship kinds are explicit; scores must be finite, ranks positive or explicitly absent, identity confidence bounded, and provider strength labels validated. Canonical observation ordering makes persisted evidence independent of row input order.
- Migration 016 adds normalized observations with nullable source artists, provider metadata, cache/payload provenance, identity status/confidence, and inspectable JSON. The required provider-specific `observation_key` distinguishes tags, modes, relation types/directions, and feedback events. The natural key is `(source, relationship_kind, observation_key)`; canonical UUID5 helpers make retries stable, and repeated writes retain the first fact. Migration replay is safe. Nine additive candidate-detail columns are nullable for historical rows.
- Legacy similarity/tag/chart rows are captured once in an explicitly typed view shared by normalization and the unchanged compatibility scorer. Non-finite historical edges remain available to legacy diagnostics/walk handling without constructing invalid normalized observations. Raw provider scores/counts and missing scores remain distinct from normalized legacy weights; tag affinity retains its original listener-profile calculation.
- Provider facts use inclusive `fetched_at <= feedback_cutoff`; feedback uses exclusive `created_at < feedback_cutoff`; local listening keeps its separate exclusive history cutoff. A null cutoff admits all stored rows. The same provider cutoff now also governs feedback seed expansion, seed popularity, and tag profiles. Deterministic evaluation fixtures intended to precede replay now use `2000-01-01` fetch timestamps, including two test-local inserts; assertions were retained. Dedicated tests prove boundary inclusion and exclusion of later fetches.
- Feedback observations use `local_feedback` / `personal` / `personal_feedback`, null source artist/rank/mode, the explicit value/time, and feedback ID/action/context; notes are omitted. One captured evidence summary now supplies both the run snapshot ID and freshness. Snapshot hashes include normalized natural keys/payloads and feedback identity/action/value/context/event times, canonicalize nested JSON, and omit notes. Existing legacy count/freshness inputs remain present. Snapshot IDs intentionally change when normalized or feedback evidence changes.
- Added pure source-aware admission and cumulative `retrieve_with_backoff`. Admission precedes component calibration and never compares raw provider values across sources. Last.fm retains its exact edge/tag thresholds; other sources must declare validated local strengths. Independent families are deduplicated. Hidden artists, active cooldown/strong rejection, and identities below the absolute floor remain excluded. Expired negative feedback retains its existing relevance effect.
- Stage 0 applies baseline evidence; stage 1 records the current exhaustive legacy depth scan as a no-op; stage 2 permits weaker collaborative support only with an independent family; stage 3 permits configured bounded walk support; stage 4 lowers the identity requirement only when callers explicitly pass `allow_low_confidence=True`. Production general queries leave this false. First-admitting stages are retained, and exhaustion returns fewer candidates. Attempted stages, no-op facts, requested/admitted/selected counts, and exhaustion are persisted deterministically under `seed_summary.retrieval_diagnostics`.
- Added conservative discovery controls: `minimum_independent_families=2`, `identity_safety_threshold=0.80`, `low_confidence_identity_threshold=0.50`, and `catalog_relationships_enabled=False`, with validated bounds/order. Catalog-only admission additionally requires a constructed artist query and a declared supported relationship. Executable artist queries, provider acquisition, fusion, and learned ranking remain assigned to later tasks.
- Controller-approved compatibility exception: translated legacy-only admission retains `float(stored_confidence or 0.50)` and the exact existing gate, plus an inspectable `legacy_identity_compatibility` reason when below 0.80. Translated rows record literal zero and `confidence_projection='legacy_zero_as_unknown'` when this projection applies. New/mixed evidence uses the literal current target confidence; provider observation confidence never upgrades it. This preserves default Last.fm behavior but deliberately retains legacy uncertainty until a later identity migration.
- Candidate storage now separates versioned evidence, feature values/missing signals, admission, policy effects, raw candidate/relevance/policy scores, known/new/rediscovery status, and backoff stage. Every considered candidate has evidence/admission; unavailable post-gate values remain null or empty. Legacy seed-ID `candidate_sources`, diagnostic/scalar columns, selected explanations, and atomic run/candidate/result rollback remain compatible. No provider SQL or scoring formulas were introduced into orchestration.

### Test-first evidence and verification

- Initial evidence vocabulary/writer/loader tests: 13 expected failures because the new module was absent; implementation made them pass. Config defaults initially failed because the four fields were missing.
- Admission tests: seven expected failures for the missing pure gate, followed by green provider-local strengths, independent families, exact legacy boundaries, identity safety/reason priority, and artist-only catalog coverage.
- Snapshot/cutoff/backoff tests: three expected failures showed ignored normalized payloads, leaked post-cutoff tag evidence, and the missing coordinator. Subsequent red/green regressions covered nested JSON canonicalization, mixed-provider Last.fm strength, preserving legacy listener tag affinity, raw provider count/missing-score preservation, and individual feedback times beyond the maximum timestamp.
- A real-connection scan-count test first observed two reads of each legacy provider table, then exactly one after shared capture. A durable trace test first failed on missing `retrieval_diagnostics`, then passed with exact schema/sequence/counts and byte-identical repeated seed summaries. A final red/green safety regression prevented personal-only stage-4 evidence from inheriting the legacy zero-confidence projection. Explicit positive rediscovery feedback is recognized, and unrelated legacy tags do not veto safe personal admission. Other deterministic tests cover all stages, default-off/opt-in low confidence, fewer results, UUID5 retry identity, distinct tag/mode/catalog/feedback keys, historical nulls, target versus observation identity, pre-calibration admission, and known/rediscovery labels.
- `.venv/bin/python -m pytest tests/test_evidence.py tests/test_retrieval.py tests/test_recommendation_quality.py tests/test_config.py tests/test_recommend.py -q` — 161 passed (6.70 seconds).
- `.venv/bin/python -m pytest` — 512 passed, no failures (31.15 seconds), after the final admission and persisted-trace changes.
- `.venv/bin/python -m ruff check .` — all checks passed; `.venv/bin/python -m ruff format --check .` — 86 files already formatted; `.venv/bin/python -m mypy src` — no issues in 47 source files; `git diff --check` — clean.
- Frozen legacy/walk fingerprints and full selected explanation fixtures were not changed. Tests use only persisted local fixtures, fake clients, and temporary databases; no network requests, credentials, user paths, provider adapters, or later-task execution were added.

### Task 4 review corrections

- Closed all five Important review findings within the existing task. Backoff and ranking now use the same typed `CandidateEligibility` view of seed, local-listening/mode, hidden/cooldown, missing-name, rediscovery, walk reachability, and obviousness exclusions. Stage stop/exhaustion counts include only source-admitted candidates surviving that view. The limit-1 and limit-2 regressions first stopped at stage 0 without the available artist; both now reach stage 2, retain excluded diagnostics, and report truthful usable counts. Additional cases cover obviousness and a source-admitted but walk-unreachable candidate while another artist requires stage 3.
- Normalized snapshot identity now hashes every structured observation field, including family, source/target IDs, raw score, rank, mode, timestamp, cache/payload provenance, identity status/confidence, natural key, and canonical evidence JSON. Nine field mutations previously collided; all now change identity. Existing key-order, note exclusion, individual feedback-time and one-summary-capture tests remain green.
- Only qualifying positive personal feedback (`save`, `more_like_this`, `rediscovery_good` with positive value and the correct relationship kind) contributes direct admission or independent-family support. Reject/less-like-this, note/opened-link, zero and negative values remain inspectable without reinforcing admission. Twelve stage-0/stage-2 negative/neutral cases first admitted incorrectly and now reject; positive cases still admit.
- Translated legacy provenance is now represented by private in-memory observation types created by the loader, preserving the exact public field/JSON vocabulary. Provider JSON/source strings cannot assert that origin. Normalized writes reject reserved legacy-projection keys, and normalized reads sanitize previously stored spoofed keys. Observations loaded from normalized storage always use literal current target identity for safety, even for `source='lastfm'`, stored zero, or a concurrently passing real legacy gate. Existing valid translated legacy/walk compatibility remains intact. The spoof reproduction and three missing writer-rejection cases were observed red before this correction.
- Merged all candidate detail values into the existing batched INSERT/upsert and removed the per-candidate UPDATE loop and optional incomplete write mode. A pure row-value helper preserves sorted JSON/null/scalar bytes. Statement-count regressions first observed 3 and 1,001 extra UPDATEs; both now perform zero UPDATEs, with candidate insert batches `[3]` and `[1000, 1]`. The transaction, progress order, historical nulls, complete selected explanations and rollback remain covered.
- The advisory three current-identity-map reads remain explicitly deferred in the execution ledger; no identity-capture or transaction redesign was added to this correction.
- `.venv/bin/python -m pytest tests/test_evidence.py tests/test_config.py tests/test_retrieval.py tests/test_ranking.py tests/test_policy.py tests/test_recommendation_quality.py tests/test_recommendation_query.py tests/test_recommendation_store.py tests/test_recommend.py tests/test_evaluate.py tests/test_compare.py -q` — 271 passed (16.03 seconds).
- `.venv/bin/python -m pytest` — 556 passed, no failures (33.98 seconds). Ruff lint passed; Ruff format reported 86 files already formatted; mypy reported no issues in 47 source files; diff checks were clean. Frozen control fingerprints and complete selected explanation fixtures remain unchanged.

## 2026-10-26 — Set-based recommendation persistence

- Replaced DuckDB `executemany` candidate writes with one typed
  `INSERT ... SELECT FROM unnest(?)` statement for the complete candidate pool. Removed the
  unnecessary conflict-update path: a new run UUID cannot already have candidate rows inside the
  same transaction. Duplicate diagnostics retain the prior last-write-wins behavior by collapsing
  to one row per artist before the set insert.
- Replaced the per-result `conn.execute(INSERT ...)` loop with one typed set insert for every
  selected recommendation. Python still constructs application-owned JSON, URL, identity, and rank
  values; DuckDB receives each destination collection in one statement.
- Preserved the transaction spanning run metadata, candidates, and selected results. A forced
  result-set failure verifies that all three tables roll back. Empty candidate/result collections
  remain valid, and candidate progress completes after the single statement returns.
- Added a real-DuckDB execution-shape regression that rejects `executemany`, requires exactly one
  candidate insert and one selected-result insert for multi-row inputs, and verifies the persisted
  row counts.

### Test-first evidence

- RED: `.venv/bin/python -m pytest tests/test_recommendation_store.py -q` — 1 failed, 5 passed;
  the guard caught candidate persistence calling `executemany`.
- Intermediate RED after the candidate fix: the same command reported 1 failed, 5 passed because
  two selected recommendations still produced two `recommendation` inserts.
- GREEN: `.venv/bin/python -m pytest tests/test_recommendation_store.py -q` — 6 passed.
- Focused pipeline verification:
  `.venv/bin/python -m pytest tests/test_recommendation_store.py tests/test_recommend.py tests/test_retrieval.py tests/test_ranking.py tests/test_policy.py -q`
  — 117 passed (5.56 seconds).
- Review correction preserved the previous last-write-wins result for duplicate candidate
  diagnostics before the set insert. Explicit empty-collection coverage verifies that no candidate
  or result insert is issued, the run still commits, and candidate progress reports `0/0`
  completion.
- The exact persistence suite also passed with DuckDB 1.0.0, the declared dependency floor:
  `PYTHONPATH="${compat_dir}:src" .venv/bin/python -m pytest tests/test_recommendation_store.py -q`
  — 8 passed.
- Final verification: `.venv/bin/python -m pytest` — 559 passed (28.46 seconds);
  `.venv/bin/python -m ruff check .` — all checks passed;
  `.venv/bin/python -m ruff format --check src tests` — 83 files already formatted;
  `.venv/bin/python -m mypy src` — no issues in 47 source files; `git diff --check` — clean.
- The broader `.venv/bin/python -m ruff format --check .` also inspected a pre-existing,
  untracked `docs/superpowers/plans/2026-10-15-hybrid-recommendation-engine.md` and reported that
  file would be reformatted. It was outside this change and left untouched.

## 2026-10-26 — Streaming candidate bulk persistence

- Replaced the single Python `list[dict]` candidate parameter with a streaming CSV staging file
  and one typed, parameterized `INSERT ... FROM read_csv(?)`. This avoids DuckDB's expensive
  Python-to-`STRUCT[]` conversion and bounds the additional persistence memory instead of
  materializing all candidate row dictionaries at once.
- Preserved one transaction across run metadata, candidates, and selected results. Candidate
  diagnostics still use last-write-wins deduplication, nullable values remain SQL nulls, and empty
  candidate collections remain valid. The selected-recommendation set insert is unchanged.
- The staging file is created with the platform's secure temporary-file facility, closed before
  DuckDB reads it, and removed in `finally` after success, database failure, serialization failure,
  or interruption. The path remains a bound parameter rather than interpolated SQL.
- Candidate progress now advances every 10,000 staged rows before the final completion event, so a
  large local run no longer displays `0/N` for the entire persistence phase.

### Test-first evidence, benchmark, and verification

- RED: `.venv/bin/python -m pytest tests/test_recommendation_store.py -q` — 3 failed, 7 passed.
  The execution guard rejected `INSERT ... FROM unnest(?)`, the injected bulk-load failure was not
  reached, and the expected incremental progress event was absent.
- Initial GREEN on DuckDB 1.5.5: the same suite passed 10 tests after streaming to CSV and loading
  with `COPY`. Compatibility RED then showed 6 failures on DuckDB 1.0.0 because that version does
  not parse a path parameter in `COPY ... FROM ?`.
- Compatibility correction: parameterized `read_csv(?)` uses the same bulk CSV scanner and works on
  the declared DuckDB floor without SQL path interpolation. The exact persistence suite passed all
  10 tests on DuckDB 1.0.0 and DuckDB 1.5.5.
- Focused integration verification:
  `.venv/bin/python -m pytest tests/test_recommendation_store.py tests/test_recommend.py tests/test_evidence.py -q`
  — 177 passed (6.64 seconds), including 1,001 candidate rows, persisted JSON/null values, cleanup,
  progress, duplicate behavior, and rollback.
- Representative 50,000-row transport benchmark with 2,519 JSON bytes per row and a 134.8 MiB
  staging file: the prior nested-parameter execution took 54.75 seconds; streaming and the typed CSV
  bulk scan took 1.35 seconds total (0.97 seconds staging, 0.38 seconds loading), about 40x faster.
- Final verification: `.venv/bin/python -m pytest -q` — 561 passed (27.08 seconds);
  `.venv/bin/python -m ruff check src tests` — all checks passed;
  `.venv/bin/python -m ruff format --check src tests` — 83 files already formatted;
  `.venv/bin/python -m mypy src` — no issues in 47 source files; `git diff --check` — clean.

## 2026-10-27 — Task 5: Cache-first ListenBrainz aggregate evidence

- Replaced placeholder ListenBrainz settings with validated public API, rate/timeout/cache/retry,
  mode/popularity, and acquisition bounds. Acquisition and candidate generation default off.
  Obsolete dump/personalized-API fields now produce an actionable removal error. Updated generated
  defaults and the checked-in example so it remains loadable.
- Added the public artist-radio client with injected HTTP/time boundaries, configured timeout,
  exponential transient retries, explicit stale-success policy, and owned-client cleanup. The cache
  stores ordered artist groups and the original payload hash so fresh/stale reads retain provider
  response positions. Malformed successes never replace usable cache entries.
- Added bounded enrichment over resolved, high-confidence local MBID seeds, with local planning,
  queue status/retry routing, and atomic membership replacement plus queue completion. Matching
  MBIDs reuse canonical artists; normalized-name conflicts preserve separate artists and warnings.
  Existing identity confidence/status are never upgraded by aggregate membership.
- Persisted unranked collaborative observations with null score/rank, provider mode and request
  bounds, original response position, bounded recording MBIDs/listen counts, fetch time, cache key,
  and payload hash. Explicit reruns refresh existing rows and remove obsolete memberships for that
  seed/request without duplicating evidence.
- Kept optional provider observations out of production admission/calibration unless the separate
  candidate-generation switch is explicitly enabled. Evaluation measures distinct source/family
  membership and unique candidate/future-hit additions independently of that production pool.
  `evaluate --ablate-source listenbrainz` filters measurements/retrieval while preserving stored
  evidence and its snapshot ID. No fusion/RRF, artist queries, or representative-recording lookup
  behavior was introduced.
- Documented the optional aggregate workflow, settings, queue semantics, provenance, attribution,
  and same-snapshot ablation. Tests use deterministic fixtures, fake provider clients or MockTransport,
  and temporary DuckDB files; no live API calls, keys, or local user paths are committed.

### Test-first evidence

- Config RED: 13 expected failures for missing bounded fields and obsolete-field migration errors;
  GREEN: 13 passed after replacing the placeholder settings.
- Client RED: missing-module discovery followed by an explicit unimplemented constructor yielded
  17 expected failures; GREEN: 17 passed for the request contract, fresh/stale order preservation,
  bounded transport/429/503 retries, stale policy, immediate 4xx failure, malformed payload rejection,
  configured timeout, and HTTP ownership.
- Enrichment RED: 5 expected failures against unimplemented entrypoints; GREEN: all 5 passed for
  seed safety, identity matching/conflicts, null rank/score and provenance, replacement/idempotence,
  queue failure/retry, disabled acquisition, and acquisition bounds. Real HTTP-cache integration,
  recording limits, and ineligible pending seeds add regression coverage.
- Integration RED: 8 expected CLI/evaluation/control failures exposed unsupported source routing,
  missing source metrics/ablation, and changed Last.fm admission from stored optional observations;
  GREEN: all 8 passed after routing and production gating.
- Additional RED/GREEN regressions preserved existing ambiguous identity confidence/status,
  retained cache/payload provenance in evidence JSON, and detected normalized-name conflicts across
  separately created provider identities. Disabled and failed-provider controls compare complete
  persisted recommendation tuples and explanation bytes against the Last.fm-only baseline with
  optional rows actually present.

### Verification

All commands used the repository environment with `PYTHONPATH=src`:

- `.venv/bin/python -m pytest tests/test_listenbrainz_client.py tests/test_listenbrainz_enrichment.py tests/test_config.py tests/test_cli.py tests/test_evaluate.py tests/test_recommend.py -q`
  — 225 passed (10.88 seconds).
- `.venv/bin/python -m pytest -q` — 611 passed (28.41 seconds), no failures or warnings.
- `.venv/bin/python -m ruff check .` — all checks passed.
- `.venv/bin/python -m ruff format --check .` — 90 files already formatted.
- `.venv/bin/python -m mypy src` — no issues in 49 source files.
- `git diff --check` — clean. Frozen legacy/walk control fingerprints and selected explanation
  fixtures remain unchanged. Self-review confirmed no new schema, dependency, API credentials,
  network-bound test, or later-task behavior.

### Task 5 review fixes

- Fixed completed refreshes starving later bounded batches. Planning now finishes adding/recovering
  queued work, then starts a completed-work refresh cycle only when no queued/running rows remain.
  A two-seed, batch-size-one regression drains both seeds across repeated calls, refreshes both
  again, and verifies updated evidence remains one observation per seed.
- Fixed skipped rows becoming permanently stranded. Planning requeues a skipped row only when its
  seed is currently eligible. A real-DuckDB regression exercises queued → skipped → eligible →
  complete, proves still-unsafe rows stay skipped, and preserves a separate failed row's attempts
  and error until explicit retry.
- RED: `.venv/bin/python -m pytest tests/test_listenbrainz_enrichment.py -q -k bounded_batches_drain`
  — 1 failed, 10 deselected: a completed row was requeued while another seed was pending.
  GREEN after the fairness fix: `-k 'bounded_batches_drain or rerun_refreshes'`
  — 2 passed, 9 deselected.
- RED: `.venv/bin/python -m pytest tests/test_listenbrainz_enrichment.py -q -k skipped_seed_recovers`
  — 1 failed, 11 deselected: restoring eligibility queued zero rows.
  GREEN after recovery: `-k 'skipped_seed_recovers or bounded_batches_drain or queue_plan_failure or rerun_refreshes'`
  — 4 passed, 8 deselected.
- Final commands used `PYTHONPATH=src` and the repository Python environment:
  `.venv/bin/python -m pytest tests/test_listenbrainz_enrichment.py tests/test_listenbrainz_client.py tests/test_cli.py -q`
  — 83 passed (3.68 seconds); `.venv/bin/python -m pytest -q`
  — 613 passed (31.68 seconds), no warnings or failures. Ruff lint passed; Ruff format reported
  90 files already formatted; mypy reported no issues in 49 source files; `git diff --check` was clean.
- Self-review confirmed that pending work takes precedence without changing queue priority or
  depending on wall-clock ordering; completed work still refreshes/idempotently replaces evidence
  after draining; skipped recovery uses current identity safety; failed rows are never reset by
  planning. No later-task work or new schema/config behavior was introduced.

### Task 5 review fix round 2 — Interrupted running work

- A new synchronous ListenBrainz enrichment invocation now recovers persisted `running` rows for
  its own provider/endpoint to `queued` before planning. The unfinished attempt is returned to the
  retry budget, so recovery works even with `max_queue_attempts=1`. Completed refreshes continue to
  wait for the recovered batch to drain; failed and skipped rows retain their existing semantics.
- Recovery is scoped to worker entry, not planning. The app calls this worker synchronously and
  has no concurrent ListenBrainz worker path. A mid-request planning regression verifies genuinely
  active work remains running and still blocks completed refreshes while that request executes.
- RED: `.venv/bin/python -m pytest tests/test_listenbrainz_enrichment.py -q -k interrupted_running_seed`
  — 1 failed, 12 deselected: after a real `KeyboardInterrupt` left complete/running queue rows,
  the resumed call made zero requests instead of one. This remained red with a one-attempt budget.
- GREEN: `-k 'interrupted_running_seed or bounded_batches_drain or skipped_seed_recovers or queue_plan_failure'`
  — 4 passed, 9 deselected. The interrupted seed resumes first, then both seeds refresh across
  bounded calls with no duplicate evidence. Active-request planning adds a separate guard regression.
- Final verification used `PYTHONPATH=src` and the repository Python environment:
  `.venv/bin/python -m pytest tests/test_listenbrainz_enrichment.py tests/test_listenbrainz_client.py tests/test_cli.py -q`
  — 85 passed (3.90 seconds); `.venv/bin/python -m pytest -q`
  — 615 passed (32.04 seconds), no warnings or failures. Ruff lint passed; Ruff format reported
  90 files already formatted; mypy reported no issues in 49 source files; diff checks were clean.
- Self-review confirmed recovery touches only this provider/endpoint's interrupted work at a new
  synchronous call, preserves pending-batch fairness and explicit failed retries, and needs no
  generic queue redesign, schema, worker lease, timestamp heuristic, or later-task behavior.

## Task 6 — Source-aware MusicBrainz catalog relationships

- Added replay-safe migration `017_musicbrainz_catalog.sql` for per-artist aliases, including
  locale, primary status, partial dates, type, source, and original fetch time. Missing locale
  uses an empty-string sentinel so the per-source/artist/normalized-alias/locale unique key
  deduplicates it; shared aliases across artists never merge identities.
- Preserved identity search/lookup and the identity-only `resolve` command. Added a separate
  cache-first `aliases+artist-rels` artist lookup with frozen catalog/alias/relationship models.
  Parsing skips malformed relation rows and missing/invalid targets, preserves directions and
  partial dates, and retains unknown types. Malformed collection containers fail lookup rather
  than becoming successful empty refreshes. Stale fallbacks preserve the successful cache row,
  fetch time, and canonical-JSON payload hash. UTC timestamps retain existing naïve UTC storage
  using the supported clock API.
- Added bounded, nonrecursive catalog enrichment over resolved/high-confidence local seeds.
  Exact MBIDs identify targets; conflicting normalized names remain separate identities with
  inspectable warnings. Multiple local MBID matches remain separate, with deterministic target
  selection and warnings; duplicate source MBIDs are ineligible. Existing identity confidence
  and resolution status are preserved. Aliases and catalog evidence update transactionally with
  queue completion; failed lookup or persistence retains the prior catalog.
- Ported the fixed Task 5 queue lifecycle for MusicBrainz `artist-catalog` work: pending batches
  drain before refresh, eligible skipped rows recover, failed rows need explicit retry, and
  synchronous worker entry recovers interrupted attempts within its own provider/endpoint.
  Planning leaves active requests and the refresh barrier untouched. Disabled acquisition
  neither requests HTTP nor mutates catalog queue state.
- Added default-off `recommendation.catalog_evidence` policy and an explicit initial allow-list:
  `member of band`, `collaboration`, `supporting musician`, and
  `instrumental supporting musician`. Unknown relations persist for inspection but current
  policy excludes them before candidate introduction, admission, and family agreement.
  Allow-list changes take effect without reacquisition. Every acquired catalog observation has
  null raw score/source rank; supported facts supply membership, explanation, and family
  reinforcement without synthetic strength, score, or provider rank.
- `enrich --source musicbrainz` resolves first, then acquires catalog facts even when no unresolved
  identities remain. CLI planning/status/retry, bounded progress, summary statistics, generated
  configuration, example configuration, attribution, and README behavior are updated.
- The real CLI regression caught a DuckDB natural-index conflict when deleting/reinserting
  observations after reopening the database. Retained relationship observations now update
  in place and obsolete observations are removed in the same scoped transaction. Relationship
  keys omit provider display names so a renamed target updates its evidence without changing
  the durable fact identity. Alias, relation, and observation helpers were extracted while green.

### TDD and verification

All Python verification commands used `PYTHONPATH=src` and the repository virtualenv interpreter.

- RED alias migration test: 1 failed because `artist_alias` did not exist; GREEN after migration:
  1 passed. Tests replay migrations and exercise actual unique constraints with temporary DuckDB.
- RED expanded client contract: 3 failed, 9 passed because `lookup_artist_catalog` was missing;
  GREEN: 13 passed including migration. Later malformed-collection regression failed on a
  falsely successful refresh, then passed after rejecting invalid containers.
- RED initial enrichment contracts: 10 failed, 1 passed at the explicit missing-module assertion;
  policy-default test separately failed at missing `catalog_evidence`. GREEN: 12 passed covering
  identity safety, replacement, rollback via a real constraint failure, bounds, and queue lifecycle.
- RED catalog retrieval policy: 3 failed, 2 passed; GREEN focused catalog tests: 5 passed.
  A fixture's one-seed threshold was corrected to two so the independent-family regression
  actually isolates catalog reinforcement. Full evidence/client/enrichment run: 116 passed.
- RED real CLI integration failed at absent catalog statistics, then exposed the reopen conflict;
  GREEN MusicBrainz CLI/enrichment tests: 16 passed. The retry assertion was corrected to use
  the existing `enrich-retry` command name.
- RED stable-key/source-ambiguity regressions: 2 failed, 13 passed; GREEN: 15 passed.
  RED progress callback regression failed at the missing keyword; GREEN focused provider/
  enrichment/evidence/CLI suite: 177 passed. Latest helper refactor: 20 passed, 52 deselected.
- Focused client/enrichment/evidence/CLI/TUI command:
  `python -m pytest tests/test_musicbrainz.py tests/test_musicbrainz_enrichment.py tests/test_evidence.py tests/test_cli.py tests/test_tui_app.py -q`
  — 201 passed (13.72 seconds). Recommendation generation and Textual browsing are verified
  against persisted catalog data with both synchronous and asynchronous HTTP clients replaced
  by raising fakes. Tests use deterministic local fixtures, mocked HTTP/time boundaries, and
  temporary database files; no live API was called.
- Frozen control check:
  `python -m pytest tests/test_recommend.py::test_control_recommendation_fingerprint_matches_fixture -q`
  — 1 passed (0.45 seconds). Disabled-catalog tests also compare complete retrieval, admission,
  ranked explanations, and policy objects with stored catalog observations present.
- Initial full-suite run: `python -m pytest -q` — 641 passed, 1 failed (30.85 seconds).
  `tests/test_config.py::test_initialize_database_applies_initial_migration` enumerated the exact
  old table set; its assertion was updated to include the new additive `artist_alias` table.
  Targeted schema check then passed (0.13 seconds). No production change was needed.
- `python -m ruff check .` — all checks passed; `python -m ruff format --check .` — 92 files
  already formatted; `python -m mypy src` — no issues in 50 source files; `git diff --check` — clean.
- Self-review confirmed exact-MBID target identity, shared-alias independence, canonical cache
  provenance, atomic/idempotent replacement after reopening, provider-scoped queue recovery,
  unsupported-type neutrality, default-off control stability, resolution-before-catalog order,
  and persisted-only recommendation/TUI behavior. No fusion, artist-query, representative-item,
  experiment, learned-ranking, dependency, API credential, or user-path changes were introduced.
- Final full-suite rerun after the schema assertion update: `python -m pytest -q` — 642 passed
  (30.94 seconds), no failures or warnings. Final Ruff lint/format, mypy, and diff checks passed.

### Task 6 review fix round 1 — MusicBrainz cross-phase pacing

- Fixed I1: `enrich --source musicbrainz` now owns one MusicBrainz client/limiter across identity
  resolution and catalog acquisition, and closes it after both phases in a `finally` block.
  The resolver accepts an injected client without closing it; standalone `resolve` still creates
  and closes its own client. Already-resolved catalog refresh and cache-first lookups are retained.
- Offline fake-clock CLI regression first reproduced RED: identity and catalog requests both
  occurred at time 0, violating the configured one-second interval (1 failed, 0.39 seconds).
  GREEN after sharing the client: 1 passed (0.53 seconds), with exactly one fake one-second sleep.
  The same regression verifies cached refresh makes no HTTP request or extra sleep, all owned
  HTTP clients close, and standalone resolution performs identity acquisition only.
- Focused verification, using `PYTHONPATH=src` and the repository Python environment:
  `python -m pytest tests/test_cli.py tests/test_musicbrainz.py tests/test_musicbrainz_enrichment.py tests/test_rate_limit.py -q`
  — 94 passed (4.13 seconds). No live API calls or real sleeps were used by the new regression.
- Deferred minor README wording and tracked queue-duplication debt were left outside this fix.
- Final verification: `python -m pytest -q` — 643 passed (34.86 seconds), no warnings or failures;
  Ruff lint passed; Ruff format reported 92 files formatted; mypy found no issues in 50 source
  files; `git diff --check` was clean. Verification used the same `PYTHONPATH=src` interpreter.


## Task 7 — Artist-conditioned discovery through the shared engine

The initial numerical-control description below is historical; fix round 1 replaces its production
default with the existing deterministic scorer and moves the variation behind an explicit default-off
challenger. The fix-round entry records the current behavior.

- Added exact local target resolution (MBID, positive ID, normalized exact canonical name),
  explicit ambiguity errors, and strict-window relationship snapshots. Every continuous metric,
  threshold, low-depth sampled band, relationship version, and run-appearance exposure provenance
  is persisted. Exhaustive state precedence and finite recent-trend/missing-key conventions match
  the pinned local decisions. The existing 400-day knownness decay was extracted without changing
  general arithmetic.
- Added `RecommendationRequest(query_kind="artist", target_artist_id=...)` to the existing
  retrieval → ranking → policy → store pipeline. Target-only outgoing walk reachability, direct
  graph neighbors, shared semantic facets, target radio membership, and current enabled/allow-listed
  catalog evidence establish the pool before personal taste or feedback can rerank it. Both catalog
  policy switches and both ListenBrainz switches retain their existing roles. Provider scores/ranks
  are not synthesized or rewritten. Sparse pools stay sparse.
- Versioned artist controls use radius 1/1/2/2, popularity preference +0.10/0/-0.10/-0.05, and
  obviousness weights 0.05/0.10/0.20/0.15 for unheard/sampled/established/dormant respectively.
  Radius is bounded by configured walk depth; structural unranked membership has target relevance
  0.25. Initial relevance uses 0.75 target relevance + 0.15 personal agreement + 0.10 existing
  components, with 0.12 known-neighbor reduction. These are recorded initial controls, not claimed
  optima. Full settings, target features, known/new labels, and policy reasons are inspectable JSON.
- Artist eligibility permits known neighbors but preserves hidden/cooldown/unsafe identity and
  explicit negative artist feedback exclusions at retrieval and ranking. Self-review regression
  tests additionally pin unsafe provider-observation identity, weak older negative feedback,
  mode-independent known/new labels, future-evidence/history/feedback exclusion, and catalog flags.
- Added `recommend --artist REFERENCE`, target ID/name/state output, and `evaluate-rolling --task`.
  Artist replay selects pre-cutoff targets meeting the existing minimum seed play threshold inside
  each configured history window, ordered by ID. One run is generated per target-window; artist
  means use run count, distinct target count is separate, and empty populations report zeros.
  General result serialization and metric arithmetic remain unchanged. Artist reports serialize
  their task discriminator and report local horizon adoption grades (first=1, repeat=2, multi-day=3)
  with a separate graded NDCG field. No Task 8 fusion or Task 9 exposure schema was introduced.
- README and example configuration describe shipped behavior and initial numerical conventions.
  Corrected the deferred quick-start MusicBrainz wording while updating adjacent workflow docs.

### TDD and verification

- Relationship/config API RED: collection failed on the absent `ArtistRelationshipConfig` and
  relationship module. GREEN after implementation: 33 relationship/knownness/query tests passed.
  Boundary additions exercise exact 730-day dormancy and singleton missing-track replay behavior.
- Shared artist pipeline RED: 16 failures at the explicit artist-not-implemented rejection.
  GREEN: 16 passed through real temporary DuckDB, both scorers, all modes, bounded retrieval,
  known-neighbor preference, strict safety, source gates, sparse results, and deterministic JSON.
- Replay/CLI RED: 9 failures for absent artist/task options, replay keyword, and adoption helpers.
  The initial broad replay/CLI/evidence/recommend run had 260 passed and 3 failed on a missing
  CLI JSON import; after fixing that import, the focused relationship/artist/CLI/replay/control
  run passed 131 tests.
  The old unsupported-artist assertion now checks a missing local target instead; the existing
  standalone catalog admission contract remains intact.
- Self-review RED: unsafe provider identity introduced a candidate; old weak negative feedback
  restored two neighbors; rediscovery labeled a known artist `rediscovery`; serialized task/target
  output and target-obviousness fields were absent; disabled direct catalog admission leaked a
  catalog-only neighbor. Minimal shared-stage fixes made the focused artist suite pass (22 tests).
  Recent positive-feedback cooldown and strict future cutoffs also remain covered.
- Verification uses `PYTHONPATH=src` with the repository virtualenv interpreter, deterministic
  fixtures, temporary DuckDB files, and raising provider-client boundaries. No live API calls.
- Final focused command: `python -m pytest tests/test_relationship.py tests/test_artist_recommend.py
  tests/test_cli.py tests/test_evaluate.py
  tests/test_recommend.py::test_control_recommendation_fingerprint_matches_fixture -q` —
  132 passed (10.64 seconds), including the unchanged frozen control fixture.
- Final full suite after code/test edits: `python -m pytest -q` — 691 passed (35.25 seconds),
  no warnings or failures. `python -m ruff check .` passed; `python -m ruff format --check .`
  reported 95 formatted files; `python -m mypy src` found no issues in 51 source files;
  `git diff --check` was clean. Configuration-only cleanup retained the example's existing
  decimal formatting. No credentials or local user paths were added to implementation files.
- Self-review covered resolution ambiguity, normalized timestamp/cutoff math, all state boundaries,
  neighborhood leakage, repeated safety checks, known labels, sparse slates, deterministic
  persistence, general-control stability, evaluation denominators, source gates, and network-free
  CLI/replay execution. The numerical artist controls remain initial experimental defaults.


### Task 7 review fix round 1 — cutoff, policy accounting, invalid evidence, and control profile

- I1: derive one effective artist listening window ending at the minimum requested end, as-of,
  historical cutoff, and local-history cutoff. Use it for personal seed/profile construction,
  persisted query context, relationship snapshot, and local listening inputs. Unequal-bound tests
  reproduce future-only scrobbles changing persisted seeds/features/rank order; all three bound
  variants now remain unchanged under both control and challenger profiles.
- I2: remove relationship popularity and known-neighbor reductions from raw relevance. The shared
  policy seam applies enabled adjustments and records signed per-candidate components/reasons in
  final-score units. Existing knownness/obviousness/fatigue/trial adjustments, challenger effects,
  clipping, diversity, and slate exclusion account for the full difference between relevance and
  final score. Excluded candidates retain their reasons and amounts too.
- I3: filter nonfinite Last.fm similarities consistently before artist scoring, graph features,
  feedback-neighborhood scoring, and provenance. Persist invalid-row IDs/reasons with string values
  in run retrieval diagnostics. Valid semantic neighbors remain usable when their legacy edge is
  NaN or positive/negative Infinity, under either configured scorer and either artist profile.
- I4: default artist production now uses `artist-control-v1`: the selected existing scorer,
  component weights, calibration/mode/identity/serendipity controls, walk depth and slate policies
  over the bounded target neighborhood. No structural membership prior, new hop floor, relevance
  blend, relationship popularity matrix, or additional known-neighbor reduction is active by default.
  Membership-only zero-relevance candidates remain persisted diagnostics and do not pad the slate.
- Consolidated the experimental variation in validated `recommendation.artist_challenger` config,
  version `artist-challenger-v1`, default `enabled=false`. Bounded relevance weights must sum to one;
  structural/hop/novelty values and every state's radius/popularity/obviousness controls are explicit.
  Runs persist the active profile, actual settings and relevance contributions; disabling the profile
  restores the existing control. README/example config distinguish control from optional evaluation
  settings. No automatic promotion, Task 8 fusion, Task 9 exposures, or Task 11 workflow was added.
- TDD: I1 first reproduced 3 unequal-bound failures, then 3 passed; I3 first reproduced 6 JSON
  persistence failures for NaN/±Infinity edges, then 6 passed; I2 first reproduced different raw
  relevance for equal-strength known/new neighbors, then accounting passed. Expanded exclusion
  tests exposed 2 missing per-effect reason failures, then passed. I4 first reproduced 4 missing
  control/default-off/zero-prior failures; 14 focused control/config tests then passed. Old tests
  asserting the rejected default numerical variation now explicitly enable the challenger.
- Latest focused artist suite: 50 passed, including control restoration, enabled-effect flags,
  actual tunable/contribution persistence, existing knownness preference, both scorer settings,
  profile-separated cutoff/invalid-evidence regressions and selected/excluded policy accounting.
- A final zero-relevance membership regression exposed a misleading `slate_limit` reason;
  it now records `nonpositive_policy_score`. RED: 1 failed; GREEN accounting/zero-prior checks:
  5 passed. General policy reasons/serialization remain unchanged.
- Final broad focused command covering artist/relationship/query/config/evaluation/CLI/policy/
  ranking/store/recommendation tests: 336 passed (18.85 seconds), including the unchanged frozen
  general control fingerprint. Full `python -m pytest -q`: 730 passed (41.05 seconds), no failures
  or warnings. Ruff lint passed; Ruff format reported 95 formatted files; mypy found no issues
  in 51 source files; diff checks were clean. Commands used `PYTHONPATH=src` and the repository
  virtualenv interpreter. No live API calls or new network dependencies were introduced.
- Self-review verified effective history bounds under both profiles, finite source-local values
  throughout scoring/provenance, explicit disabled-by-default challenger controls, actual
  contribution persistence, selected/excluded/diversity policy accounting, control restoration,
  existing knownness preference, and byte-stable general control behavior. All four Important
  findings are addressed; no scope expansion or unresolved contract decision remains.
