# Pause Packet: CLI/Admin Hardening Task 5

## Current task

Task 5: Public Local Profile Refresh And Build-Profile Command.

## Why execution stopped

The architecture/deviation gate returned:

```text
severity: architectural
continue: no
reason: Task 5 Option B expansion was human-approved, and the local build-profile behavior otherwise aligns with §7, §8.4, and §11.3. The remaining deviation is the new persisted `profile_cluster` schema and cluster-key/evidence semantics, which are not explicitly specified in the approved schema area. Because this introduces a database migration and durable profile-state contract, it is architectural rather than local implementation detail.
impacted_concepts: build-profile persistence; DuckDB schema/migrations; genre cluster storage contract; future recommendation/profile consumers
required_human_decision: Approve `profile_cluster` as the durable storage contract for §7 genre clusters, including deterministic one-tag-per-cluster baseline behavior and collision-safe keys, or require a different representation before commit.
suggested_plan_update: Add explicit Task 5 Option B notes for `profile_cluster` columns/indexes, cluster evidence semantics, and baseline deterministic clustering; ensure the actual plan/checkpoint records this approval before continuing.
```

## What changed

- Kept the public local listening-score refresh added earlier.
- Reused that refresh helper from recommendation generation.
- Expanded `build-profile` per Option B so it now refreshes local artist scores, derives and persists local tag weights, and persists deterministic local genre clusters.
- Added `tag_profile` and `profile_cluster` persistence in `src/scrobble_recs/sql/005_profile.sql`.
- Added a `profile_cluster.cluster_key` collision fix: keys use `source:readable-slug:stable-tag-hash`.
- Added behavioral tests for score refresh, tag profile persistence, cluster persistence, cluster-key collisions, migration presence, and CLI count output.
- Updated the plan with Task 5 Option B notes, verification, review approvals, and this pause status.

No Task 5 commit was created because the gate did not permit continuation.

## Files changed

- `docs/superpowers/plans/2026-06-05-cli-admin-hardening.md`
- `src/scrobble_recs/cli.py`
- `src/scrobble_recs/models/listening.py`
- `src/scrobble_recs/models/profile_builder.py`
- `src/scrobble_recs/models/recommend.py`
- `src/scrobble_recs/sql/005_profile.sql`
- `tests/test_cli.py`
- `tests/test_config.py`
- `tests/test_profile.py`
- `docs/superpowers/plans/2026-06-05-cli-admin-hardening.task5.pause.md`

Pre-existing untracked pause file left untouched:

- `docs/superpowers/plans/2026-06-05-cli-admin-hardening.pause.md`

## Checks run

- Original Task 5 red:
  `python -m pytest tests/test_profile.py::test_refresh_artist_listening_scores_updates_weights_for_build_profile tests/test_cli.py::test_build_profile_command_refreshes_local_scores -v`
  failed before implementation with missing `scrobble_recs.models.listening`.
- Original Task 5 green:
  `python -m pytest tests/test_profile.py tests/test_recommend.py tests/test_cli.py::test_build_profile_command_refreshes_local_scores -v`
  passed with 38 tests.
- Option B red:
  `python -m pytest tests/test_profile.py::test_build_profile_persists_tag_profile_and_genre_clusters tests/test_cli.py::test_build_profile_command_prints_profile_weight_counts -v`
  failed before implementation with missing `scrobble_recs.models.profile_builder` and missing CLI tag/cluster counts.
- Option B green:
  `python -m pytest tests/test_profile.py tests/test_recommend.py tests/test_cli.py::test_build_profile_command_refreshes_local_scores tests/test_cli.py::test_build_profile_command_prints_profile_weight_counts -v`
  passed with 40 tests.
- Review fix red:
  `python -m pytest tests/test_profile.py::test_build_profile_persists_clusters_for_slug_colliding_tags -v`
  failed before the fix with duplicate `profile_cluster` key `lastfm:post-punk`.
- Parent fresh focused verification:
  `python -m pytest tests/test_profile.py tests/test_recommend.py tests/test_cli.py::test_build_profile_command_refreshes_local_scores tests/test_cli.py::test_build_profile_command_prints_profile_weight_counts tests/test_config.py::test_initialize_database_applies_initial_migration -v`
  passed with 42 tests.

## Reviews

Spec compliance review:

- Initial status: `CHANGES_REQUESTED`.
- Finding: slugified `profile_cluster.cluster_key` could collide for distinct tags such as `post punk`, `post-punk`, and `post_punk`.
- Fix: use a stable hash suffix in cluster keys and add a collision regression test.
- Re-review status: `APPROVED`.

Code quality review:

- Status: `APPROVED`.
- Findings: none.
- Notes: `build_profile()` uses a transaction with rollback, profile migrations are idempotent, and the local-first boundary is preserved.

Architecture/deviation gate:

- Severity: `architectural`.
- Continue: `no`.

## Required human decision

Choose one:

- Option A: Approve `profile_cluster` as the durable storage contract for §7 genre clusters, including columns/indexes in `005_profile.sql`, inspectable JSON evidence, deterministic one-tag-per-cluster baseline behavior, and collision-safe keys. Resume by recording that approval, rerunning the Task 5 architecture gate if needed, and committing Task 5 if permitted.
- Option B: Require a different cluster persistence representation before committing Task 5. Specify the desired table shape and semantics.
- Option C: Defer durable genre-cluster persistence to a later task and revise Task 5 so `build-profile` does not introduce `profile_cluster`; this likely reopens the §7 compliance question.

## Next task after decision

- If Option A is chosen and the gate permits continuation, commit Task 5 with `feat: add local build-profile command`, then continue to Task 6.
- If Option B or C is chosen, revise Task 5 first, then rerun spec review, code-quality review, architecture gate, and commit only if continuation is permitted.

## Exact resume prompt

Resume checkpointed plan execution for `docs/superpowers/plans/2026-06-05-cli-admin-hardening.md` in the worktree `C:\Dev\src\tools\scrobble-recs\.worktrees\cli-admin-hardening`.

The previous run paused during Task 5 because the architecture gate returned `severity: architectural` and `continue: no`: Option B full local `build-profile` behavior is implemented and review-approved, but the gate requires explicit approval for the new durable `profile_cluster` schema and deterministic one-tag-per-cluster baseline. Read `docs/superpowers/plans/2026-06-05-cli-admin-hardening.task5.pause.md`, apply my decision, then rerun Task 5 architecture gate and commit only Task 5 if the gate permits continuation. Afterward, continue the rest of the tasks in the plan.
