# Discovery-First Recommendation Quality and Serendipity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce inspectable, discovery-first artist recommendations from a trailing 12-month profile, with permanent hides, vote cooldowns, evidence-gated calibrated ranking, and relevance-bounded graph serendipity.

**Architecture:** A `ListeningWindow` scopes the seed profile but not discovery eligibility, which always checks all locally listened artists. The recommender records candidate eligibility diagnostics, admits only direct graph-supported discoveries, calibrates eligible evidence within a run, applies a bounded local-graph serendipity bonus, then runs the existing list-level diversity selection. Evaluation creates a cutoff-bounded profile and scores future first listens without accessing future history.

**Tech Stack:** Python 3.12, DuckDB, Pydantic v2, Typer, Textual, pytest, Ruff, mypy.

## Global Constraints

- Keep the application local-first; no live API call is allowed in recommend, evaluate, or tests.
- Use temporary DuckDB files in tests and deterministic fixtures only.
- Keep `hide_artist` as a permanent exclusion.
- Apply a 30-day cooldown after a recommendation preference vote; `opened_link`, `played_externally`, and `note` are not votes.
- Standard modes are discovery-only; `rediscovery` is the only mode that may rank locally listened artists.
- The default seed profile is the 12 calendar months preceding `as_of`; `--since` and `--until` override it.
- Preserve raw scorer evidence in structured JSON; do not rely on formatted explanation strings for logic.
- Do not add ListenBrainz in this plan.
- Update README and `docs/implementation-log.md` when behavior changes.

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/scrobble_recs/models/profile_window.py` | Immutable, validated half-open listening-window value object and calendar-month resolution. |
| `src/scrobble_recs/models/profile.py` | Seed selection from all-history facts or from scoped raw-scrobble aggregates. |
| `src/scrobble_recs/models/feedback.py` | Preference-vote cooldown policy and query. |
| `src/scrobble_recs/models/recommendation_quality.py` | Direct-evidence admission decision and deterministic component calibration. |
| `src/scrobble_recs/models/serendipity.py` | Pure local-graph neighbor-cohesion and bridge-bonus calculations. |
| `src/scrobble_recs/models/recommend.py` | Orchestration, candidate diagnostics, scoring, explanations, and persistence. |
| `src/scrobble_recs/models/evaluate.py` | Leakage-free temporal holdout evaluation and comparison metrics. |
| `src/scrobble_recs/config.py` | Validated profile, evidence-gate, calibration, and serendipity configuration. |
| `src/scrobble_recs/cli.py` | `recommend --since/--until` parsing and window reporting. |
| `src/scrobble_recs/sql/013_recommendation_quality.sql` | Additive recommendation-candidate diagnostic columns. |
| `src/scrobble_recs/export.py`, `src/scrobble_recs/tui/data.py` | Read structured quality and serendipity explanation data from persisted runs. |
| `tests/test_profile_window.py`, `tests/test_recommendation_quality.py`, `tests/test_serendipity.py` | Pure behavior tests for new bounded units. |
| Existing `tests/test_profile.py`, `tests/test_feedback.py`, `tests/test_recommend.py`, `tests/test_evaluate.py`, `tests/test_cli.py`, `tests/test_export.py`, `tests/test_tui_data.py` | Integration and user-facing regression tests. |

### Task 1: Add validated profile-window configuration and CLI parsing

**Files:**

- Create: `src/scrobble_recs/models/profile_window.py`
- Modify: `src/scrobble_recs/config.py:259-405`
- Modify: `src/scrobble_recs/cli.py:701-727`
- Modify: `tests/test_config.py`
- Modify: `tests/test_cli.py`
- Create: `tests/test_profile_window.py`

**Interfaces:**

- Produces `ListeningWindow(start: datetime, end: datetime)` where `start <= played_at < end`.
- Produces `resolve_listening_window(*, as_of: datetime, default_months: int, since: datetime | None, until: datetime | None) -> ListeningWindow`.
- Extends `generate_recommendations(conn, config, *, limit, mode=None, as_of=None, history_window: ListeningWindow | None = None, progress_callback=None)` in Task 2; this task only makes the CLI construct the object.

- [ ] **Step 1: Write failing window and config tests**

```python
def test_default_window_is_twelve_calendar_months_before_as_of() -> None:
    as_of = datetime(2026, 6, 15, 12, 0, 0)
    assert resolve_listening_window(as_of=as_of, default_months=12, since=None, until=None) == ListeningWindow(
        start=datetime(2025, 6, 15, 12, 0, 0), end=as_of
    )


def test_explicit_bounds_override_default_window() -> None:
    window = resolve_listening_window(
        as_of=datetime(2026, 6, 15),
        default_months=12,
        since=datetime(2024, 1, 1),
        until=datetime(2024, 7, 1),
    )
    assert window == ListeningWindow(datetime(2024, 1, 1), datetime(2024, 7, 1))


def test_rejects_empty_or_reversed_window() -> None:
    with pytest.raises(ValueError, match="before --until"):
        resolve_listening_window(
            as_of=datetime(2026, 1, 1), default_months=12,
            since=datetime(2025, 1, 1), until=datetime(2025, 1, 1),
        )
```

Add Pydantic tests that `profile_window_months=12` is accepted and `0` is rejected. Add a `CliRunner` test that `recommend --since 2024-01-01T00:00:00 --until 2024-07-01T00:00:00` passes this exact half-open range to the recommendation entry point; malformed ISO input must produce a Typer validation error.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/test_profile_window.py tests/test_config.py tests/test_cli.py -q`

Expected: FAIL because `ListeningWindow`, the new configuration field, and CLI options do not exist.

- [ ] **Step 3: Implement the window value object, config, and CLI options**

```python
# src/scrobble_recs/models/profile_window.py
@dataclass(frozen=True)
class ListeningWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError("--since must be before --until")

    def contains(self, played_at: datetime) -> bool:
        return self.start <= played_at < self.end


def resolve_listening_window(*, as_of: datetime, default_months: int,
                             since: datetime | None, until: datetime | None) -> ListeningWindow:
    if default_months < 1:
        raise ValueError("profile_window_months must be at least 1")
    resolved_end = until or as_of
    resolved_start = since or _subtract_calendar_months(resolved_end, default_months)
    return ListeningWindow(start=resolved_start, end=resolved_end)
```

Implement `_subtract_calendar_months` with only the standard library: calculate target year/month, use `calendar.monthrange`, and clamp the day for dates such as February 29. Add `profile_window_months: int = 12` to `RecommendationConfig` and validate `>= 1`. Add optional `since` and `until` Typer options typed as strings, parse with `datetime.fromisoformat`, pass the resolved `ListeningWindow` to `generate_recommendations`, and print `history window: <start> to <end>` after the run summary.

- [ ] **Step 4: Run focused tests and static checks**

Run: `python -m pytest tests/test_profile_window.py tests/test_config.py tests/test_cli.py -q`

Expected: PASS.

Run: `python -m ruff check src/scrobble_recs/models/profile_window.py src/scrobble_recs/config.py src/scrobble_recs/cli.py tests/test_profile_window.py`

Expected: `All checks passed!`

- [ ] **Step 5: Commit the isolated change**

```bash
git add src/scrobble_recs/models/profile_window.py src/scrobble_recs/config.py src/scrobble_recs/cli.py tests/test_profile_window.py tests/test_config.py tests/test_cli.py
git commit -m "feat: add recommendation history windows"
```

### Task 2: Scope seed selection and make standard modes discovery-only

**Files:**

- Modify: `src/scrobble_recs/models/profile.py:44-177`
- Modify: `src/scrobble_recs/models/recommend.py:126-340,646-875,1417-1619`
- Modify: `src/scrobble_recs/models/modes.py:31-160`
- Modify: `tests/test_profile.py`
- Modify: `tests/test_recommend.py`

**Interfaces:**

- Consumes `ListeningWindow` from Task 1.
- `select_seed_artists(conn, *, limit, min_scrobbles, regions=None, window: ListeningWindow | None = None) -> list[SeedArtist]` derives seed facts from `scrobble` when `window` is supplied.
- `generate_recommendations(conn, config, *, limit, mode=None, as_of=None, history_window: ListeningWindow | None = None, progress_callback=None)` resolves and snapshots a window for every run.
- Produces a discovery eligibility reason `"locally_listened"` for all-history artist IDs in non-rediscovery modes.

- [ ] **Step 1: Write failing scoped-seed and discovery behavior tests**

```python
def test_seed_selection_uses_only_scrobbles_in_explicit_window(conn) -> None:
    _insert_scrobbles(conn, artist="Past Favorite", played_at=datetime(2023, 1, 1), count=20)
    _insert_scrobbles(conn, artist="Current Favorite", played_at=datetime(2025, 8, 1), count=5)
    seeds = select_seed_artists(conn, limit=10, min_scrobbles=1,
        window=ListeningWindow(datetime(2025, 6, 1), datetime(2026, 6, 1)))
    assert [seed.name for seed in seeds] == ["Current Favorite"]


def test_standard_mode_excludes_artist_with_any_local_history(tmp_path: Path) -> None:
    # Candidate has a high Last.fm edge but one pre-window local listen.
    stats = generate_recommendations(conn, config, limit=10, mode="balanced",
        as_of=datetime(2026, 6, 1), history_window=ListeningWindow(datetime(2025, 6, 1), datetime(2026, 6, 1)))
    assert "Previously Heard Candidate" not in _recommendation_names(conn, stats.run_id)
    assert _candidate_reason(conn, stats.run_id, "Previously Heard Candidate") == "locally_listened"


def test_rediscovery_mode_can_rank_eligible_locally_listened_artist(tmp_path: Path) -> None:
    stats = generate_recommendations(conn, config, limit=10, mode="rediscovery", as_of=datetime(2026, 6, 1))
    assert "Previously Heard Candidate" in _recommendation_names(conn, stats.run_id)
```

Also assert the persisted `recommendation_run.seed_summary` contains `history_window.start`, `history_window.end`, and `history_window.source`.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/test_profile.py tests/test_recommend.py -q`

Expected: FAIL because selection uses `artist_listening_fact` across all history and standard modes still score rediscoveries.

- [ ] **Step 3: Implement scoped seed aggregates and discovery exclusion**

```python
def _load_seed_rows(conn: DuckDBPyConnection, *, min_scrobbles: int,
                    window: ListeningWindow | None) -> list[_SeedRow]:
    if window is None:
        return _load_all_history_seed_rows(conn, min_scrobbles=min_scrobbles)
    rows = conn.execute(
        """
        SELECT a.artist_id, a.canonical_name, a.artist_key,
               coalesce(nullif(a.lastfm_name, ''), a.canonical_name),
               count(*) AS play_count, count(DISTINCT s.track_key) AS track_count,
               count(DISTINCT date_trunc('month', s.played_at)) AS active_months,
               min(s.played_at), max(s.played_at)
        FROM scrobble s JOIN artist a ON a.artist_key = s.artist_key
        WHERE NOT s.is_duplicate AND s.played_at >= ? AND s.played_at < ?
        GROUP BY ALL HAVING count(*) >= ?
        """, [window.start, window.end, min_scrobbles]
    ).fetchall()
    return _seed_rows_from_window_rows(rows, as_of=window.end)
```

Use `compute_artist_listening_scores` on each scoped aggregate to populate lifetime/recent/rediscovery-like seed values, without updating global facts. In `_score_candidates`, compute `is_discovery = local_play_count == 0` from the all-history `artist_listening_fact` mapping. For `mode.name != "rediscovery"`, record `locally_listened` and skip ranking when it is false. Do not add rediscovery score to normal-mode raw scores. Persist the resolved window in `seed_summary["history_window"]`.

- [ ] **Step 4: Run focused tests and the existing recommender regression suite**

Run: `python -m pytest tests/test_profile.py tests/test_recommend.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the isolated change**

```bash
git add src/scrobble_recs/models/profile.py src/scrobble_recs/models/recommend.py src/scrobble_recs/models/modes.py tests/test_profile.py tests/test_recommend.py
git commit -m "feat: scope discovery recommendations to history window"
```

### Task 3: Add preference-vote cooldowns and persisted candidate diagnostics

**Files:**

- Create: `src/scrobble_recs/sql/013_recommendation_quality.sql`
- Modify: `src/scrobble_recs/models/feedback.py`
- Modify: `src/scrobble_recs/models/recommend.py`
- Modify: `tests/test_feedback.py`
- Modify: `tests/test_recommend.py`

**Interfaces:**

- Produces `COOLDOWN_FEEDBACK_ACTIONS: frozenset[str]` and `load_cooldown_artist_ids(conn, *, as_of: datetime, days: int) -> set[int>`.
- `recommendation_candidate` gains `eligibility_state VARCHAR`, `eligibility_reasons JSON`, and `quality_gate JSON`.
- Candidate diagnostics use states `eligible`, `excluded`, and `rejected` with machine-readable reason codes.

- [ ] **Step 1: Write failing cooldown and migration tests**

```python
@pytest.mark.parametrize("action", sorted(COOLDOWN_FEEDBACK_ACTIONS))
def test_preference_vote_starts_thirty_day_cooldown(conn, action: str) -> None:
    record_feedback_event(conn, run_id=RUN_ID, artist_id=42, action=action,
        created_at=datetime(2026, 5, 15))
    assert 42 in load_cooldown_artist_ids(conn, as_of=datetime(2026, 6, 13), days=30)
    assert 42 not in load_cooldown_artist_ids(conn, as_of=datetime(2026, 6, 15), days=30)


@pytest.mark.parametrize("action", ["opened_link", "played_externally", "note"])
def test_operational_feedback_does_not_start_cooldown(conn, action: str) -> None:
    record_feedback_event(conn, run_id=RUN_ID, artist_id=42, action=action)
    assert 42 not in load_cooldown_artist_ids(conn, as_of=datetime(2026, 6, 1), days=30)


def test_hide_artist_remains_excluded_after_cooldown_expiry(tmp_path: Path) -> None:
    # A hide from 31 days ago must still leave the candidate absent from output.
    assert "Hidden Candidate" not in _recommendation_names(conn, stats.run_id)
```

Add an integration test that an otherwise eligible candidate with `less_like_this` yesterday is absent and has diagnostic state `excluded` with reason `feedback_cooldown`; verify the `013` migration adds all three columns.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/test_feedback.py tests/test_recommend.py -q`

Expected: FAIL because cooldown state and diagnostic columns are absent.

- [ ] **Step 3: Implement the policy, migration, and diagnostic persistence**

```sql
-- src/scrobble_recs/sql/013_recommendation_quality.sql
ALTER TABLE recommendation_candidate ADD COLUMN IF NOT EXISTS eligibility_state VARCHAR;
ALTER TABLE recommendation_candidate ADD COLUMN IF NOT EXISTS eligibility_reasons JSON;
ALTER TABLE recommendation_candidate ADD COLUMN IF NOT EXISTS quality_gate JSON;
```

```python
COOLDOWN_FEEDBACK_ACTIONS = frozenset({
    "save", "reject", "already_know", "too_obvious", "wrong_genre",
    "more_like_this", "less_like_this", "hide_tag", "boost_tag",
    "rediscovery_good", "rediscovery_bad",
})

def load_cooldown_artist_ids(conn: DuckDBPyConnection, *, as_of: datetime, days: int) -> set[int]:
    cutoff = as_of - timedelta(days=days)
    rows = conn.execute(
        "SELECT DISTINCT artist_id FROM feedback_event WHERE action IN (SELECT unnest(?)) AND created_at > ?",
        [sorted(COOLDOWN_FEEDBACK_ACTIONS), cutoff],
    ).fetchall()
    return {int(row[0]) for row in rows}
```

Add `feedback_cooldown_days: int = 30` to `RecommendationConfig` with `>= 1` validation. Load cooldown IDs once at run start. Replace the current pre-score candidate insertion with an upsert after candidate evaluation that writes state, ordered reason list, and structured gate payload. Keep `hide_artist` in `FeedbackProfile.hidden_artist_ids`; never let a cooldown expiry remove that permanent exclusion.

- [ ] **Step 4: Run focused tests and verify all migrations remain idempotent**

Run: `python -m pytest tests/test_feedback.py tests/test_recommend.py -q`

Expected: PASS.

Run: `python -m pytest tests/test_admin_doctor.py tests/test_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the isolated change**

```bash
git add src/scrobble_recs/sql/013_recommendation_quality.sql src/scrobble_recs/models/feedback.py src/scrobble_recs/models/recommend.py src/scrobble_recs/config.py tests/test_feedback.py tests/test_recommend.py
git commit -m "feat: apply recommendation feedback cooldowns"
```

### Task 4: Require direct graph evidence and calibrate eligible scorer components

**Files:**

- Create: `src/scrobble_recs/models/recommendation_quality.py`
- Modify: `src/scrobble_recs/config.py`
- Modify: `src/scrobble_recs/models/recommend.py`
- Create: `tests/test_recommendation_quality.py`
- Modify: `tests/test_recommend.py`

**Interfaces:**

- Produces `DirectEvidence`, `QualityDecision`, `evaluate_discovery_evidence`, and `calibrate_positive_components`.
- `evaluate_discovery_evidence(edges, tag_score, config) -> QualityDecision` admits only two-seed support or a strong edge with tag reinforcement.
- `calibrate_positive_components(rows: Sequence[Mapping[str, float]]) -> list[dict[str, float]]` returns raw-key-aligned 0-1 rank-percentile values; zero/negative inputs stay zero.

- [ ] **Step 1: Write failing pure-unit and recommender behavior tests**

```python
def test_tag_only_candidate_is_rejected() -> None:
    decision = evaluate_discovery_evidence(edges=(), tag_score=0.80, config=_gate())
    assert decision.passed is False
    assert decision.reasons == ("missing_direct_graph_evidence",)


def test_two_independent_seed_edges_are_admitted() -> None:
    decision = evaluate_discovery_evidence(edges=((1, 0.10), (2, 0.08)), tag_score=0.0, config=_gate())
    assert decision.passed is True
    assert decision.reasons == ("independent_seed_support",)


def test_strong_edge_requires_reinforcing_tag() -> None:
    assert not evaluate_discovery_evidence(edges=((1, 0.40),), tag_score=0.0, config=_gate()).passed
    assert evaluate_discovery_evidence(edges=((1, 0.40),), tag_score=0.10, config=_gate()).passed


def test_calibration_keeps_zero_at_zero_and_orders_positive_values() -> None:
    calibrated = calibrate_positive_components([
        {"graph": 0.0, "tag": 0.50}, {"graph": 0.001, "tag": 0.10}, {"graph": 0.010, "tag": 0.20},
    ])
    assert [row["graph"] for row in calibrated] == [0.0, 0.0, 1.0]
    assert calibrated[0]["tag"] > calibrated[1]["tag"]
```

Add recommender fixtures proving a broad tag-top artist with no seed edge is rejected and an artist with two seed edges is ranked. Assert stored explanation contains both `raw_components` and `calibrated_components`.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/test_recommendation_quality.py tests/test_recommend.py -q`

Expected: FAIL because current quality gate accepts any single tag, feedback, or similarity threshold and ranks raw components directly.

- [ ] **Step 3: Implement quality-gate and calibration units, then integrate them**

```python
@dataclass(frozen=True)
class DirectEvidence:
    distinct_seed_count: int
    strongest_edge: float
    qualifying_edges: tuple[tuple[int, float], ...]


def evaluate_discovery_evidence(*, edges: Sequence[tuple[int, float]], tag_score: float,
                                minimum_seed_edge: float, strong_seed_edge: float,
                                minimum_seed_count: int, minimum_tag_reinforcement: float) -> QualityDecision:
    qualifying = tuple(sorted(((seed_id, edge) for seed_id, edge in edges if edge >= minimum_seed_edge),
                              key=lambda item: (-item[1], item[0])))
    evidence = DirectEvidence(
        distinct_seed_count=len({seed_id for seed_id, _edge in qualifying}),
        strongest_edge=max((edge for _seed_id, edge in qualifying), default=0.0),
        qualifying_edges=qualifying,
    )
    if evidence.distinct_seed_count >= minimum_seed_count:
        return QualityDecision(True, ("independent_seed_support",), evidence)
    if qualifying and evidence.strongest_edge >= strong_seed_edge and tag_score >= minimum_tag_reinforcement:
        return QualityDecision(True, ("strong_edge_with_tag_reinforcement",), evidence)
    return QualityDecision(False, ("missing_direct_graph_evidence",), evidence)
```

Add `DiscoveryEvidenceConfig` with defaults `minimum_seed_edge=0.05`, `strong_seed_edge=0.30`, `minimum_seed_count=2`, and `minimum_tag_reinforcement=0.05`; validate all ratios and `strong_seed_edge >= minimum_seed_edge`. Add `CalibrationConfig(enabled=True)`.

Collect raw component dictionaries for candidates only after exclusions and direct-evidence admission. Calibrate `lastfm_similarity`, `tag_similarity`, and `feedback` across that eligible set before `_raw_score`; do not calibrate penalties. Replace the old tag-only `_quality_gate_decision` admission path for standard modes. In rediscovery mode retain its explicit eligibility rules, but require direct evidence when `rediscovery_requires_taste_match` is true. Write raw and calibrated maps in the explanation and candidate diagnostic JSON.

- [ ] **Step 4: Run focused tests and the complete recommender suite**

Run: `python -m pytest tests/test_recommendation_quality.py tests/test_recommend.py -q`

Expected: PASS.

Run: `python -m ruff check src/scrobble_recs/models/recommendation_quality.py src/scrobble_recs/models/recommend.py tests/test_recommendation_quality.py`

Expected: `All checks passed!`

- [ ] **Step 5: Commit the isolated change**

```bash
git add src/scrobble_recs/models/recommendation_quality.py src/scrobble_recs/config.py src/scrobble_recs/models/recommend.py tests/test_recommendation_quality.py tests/test_recommend.py
git commit -m "feat: gate and calibrate discovery candidates"
```

### Task 5: Add bounded local-graph serendipity reranking

**Files:**

- Create: `src/scrobble_recs/models/serendipity.py`
- Modify: `src/scrobble_recs/config.py`
- Modify: `src/scrobble_recs/models/recommend.py`
- Create: `tests/test_serendipity.py`
- Modify: `tests/test_recommend.py`

**Interfaces:**

- Produces `SerendipityEvidence(neighbors, cohesion, component_ids, is_bridge, raw_bonus, calibrated_bonus)`.
- `score_serendipity(candidate_edges, seed_graph, config) -> SerendipityEvidence` is pure and returns zero bonus when fewer than two qualifying neighbors exist.
- The recommendation pipeline applies `min(max_bonus, raw_bonus + bridge_bonus)` only after Task 4 eligibility and before diversity reranking.

- [ ] **Step 1: Write failing graph-behavior tests**

```python
def test_relevant_cross_cluster_candidate_gets_bridge_bonus() -> None:
    graph = {1: {2: 0.9}, 2: {1: 0.9}, 3: {4: 0.9}, 4: {3: 0.9}}
    result = score_serendipity(candidate_edges=((1, 0.8), (3, 0.8)), seed_graph=graph, config=_serendipity())
    assert result.is_bridge is True
    assert result.cohesion == 0.0
    assert result.raw_bonus > 0.0


def test_deeply_clustered_candidate_gets_smaller_bonus() -> None:
    graph = {1: {2: 0.9}, 2: {1: 0.9}}
    result = score_serendipity(candidate_edges=((1, 0.8), (2, 0.8)), seed_graph=graph, config=_serendipity())
    assert result.is_bridge is False
    assert result.cohesion == pytest.approx(0.9)
    assert result.raw_bonus < _serendipity().max_bonus


def test_single_neighbor_never_receives_serendipity_bonus() -> None:
    assert score_serendipity(candidate_edges=((1, 0.8),), seed_graph={}, config=_serendipity()).raw_bonus == 0.0
```

Add a recommendation-level fixture with equal calibrated relevance for a bridge and a deep-cluster candidate. Assert the bridge is ranked first only when serendipity is enabled, and that no low-evidence candidate can receive a bonus.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/test_serendipity.py tests/test_recommend.py -q`

Expected: FAIL because no local seed graph or serendipity score exists.

- [ ] **Step 3: Implement the graph calculation and bounded integration**

```python
def score_serendipity(*, candidate_edges: Sequence[tuple[int, float]],
                      seed_graph: Mapping[int, Mapping[int, float]],
                      minimum_neighbor_edge: float, minimum_neighbors: int,
                      max_bonus: float, bridge_bonus: float) -> SerendipityEvidence:
    neighbors = tuple(seed for seed, edge in candidate_edges if edge >= minimum_neighbor_edge)
    if len(neighbors) < minimum_neighbors:
        return SerendipityEvidence(neighbors, 0.0, (), False, 0.0, 0.0)
    cohesion = _mean_pair_weight(neighbors, seed_graph)
    component_ids = _connected_components(seed_graph, neighbors)
    is_bridge = len(set(component_ids)) > 1
    raw_bonus = max(0.0, min(max_bonus, max_bonus * (1.0 - cohesion) + (bridge_bonus if is_bridge else 0.0)))
    return SerendipityEvidence(neighbors, cohesion, component_ids, is_bridge, raw_bonus, raw_bonus)
```

Build `seed_graph` from Last.fm edges with both endpoints in `seed_ids`, retaining the maximum directed weight in either direction. Add a `SerendipityConfig` with `enabled=True`, `minimum_neighbor_edge=0.05`, `minimum_neighbors=2`, `max_bonus=0.15`, and `bridge_bonus=0.05`; reject invalid ratios and `bridge_bonus > max_bonus`. Store the evidence on `_CandidateScore`, add its calibrated/bounded score to final score, and include every field in the structured explanation. Do not modify candidate eligibility or diversity math.

- [ ] **Step 4: Run focused tests and full recommender tests**

Run: `python -m pytest tests/test_serendipity.py tests/test_recommend.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the isolated change**

```bash
git add src/scrobble_recs/models/serendipity.py src/scrobble_recs/config.py src/scrobble_recs/models/recommend.py tests/test_serendipity.py tests/test_recommend.py
git commit -m "feat: rerank discoveries by graph serendipity"
```

### Task 6: Expose persisted quality and serendipity evidence in export and TUI data

**Files:**

- Modify: `src/scrobble_recs/export.py`
- Modify: `src/scrobble_recs/tui/data.py`
- Modify: `tests/test_export.py`
- Modify: `tests/test_tui_data.py`

**Interfaces:**

- Consumes explanation keys `eligibility`, `quality_gate`, `raw_components`, `calibrated_components`, and `serendipity` from Tasks 3-5.
- `ScoreBreakdown` gains `raw_components: dict[str, float]`, `calibrated_components: dict[str, float]`, and `serendipity_bonus: float`.
- Export includes `history_window_start`, `history_window_end`, `quality_gate_reasons`, `serendipity_bonus`, `serendipity_cohesion`, and `serendipity_bridge`.

- [ ] **Step 1: Write failing export and TUI projection tests**

```python
def test_csv_export_includes_quality_and_serendipity_columns(tmp_path: Path) -> None:
    export_recommendations(conn, run_id=RUN_ID, output_path=output, format="csv")
    row = next(csv.DictReader(output.open(encoding="utf-8")))
    assert row["quality_gate_reasons"] == "independent_seed_support"
    assert float(row["serendipity_bonus"]) == pytest.approx(0.12)
    assert row["serendipity_bridge"] == "true"


def test_recommendation_detail_projects_structured_quality_evidence(conn) -> None:
    detail = RecommendationRepository(conn).recommendation_detail(RUN_ID, artist_id=42)
    assert detail.score_breakdown.calibrated_components["lastfm_similarity"] == pytest.approx(0.75)
    assert detail.score_breakdown.serendipity_bonus == pytest.approx(0.12)
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/test_export.py tests/test_tui_data.py -q`

Expected: FAIL because export and data objects only project legacy score fields.

- [ ] **Step 3: Implement explicit projections without changing persisted-run semantics**

```python
quality = _as_mapping(explanation.get("quality_gate"))
serendipity = _as_mapping(explanation.get("serendipity"))
row["quality_gate_reasons"] = ";".join(_as_string_list(quality.get("reasons")))
row["serendipity_bonus"] = _as_float(serendipity.get("bonus"))
row["serendipity_cohesion"] = _as_float(serendipity.get("cohesion"))
row["serendipity_bridge"] = str(bool(serendipity.get("is_bridge"))).lower()
```

Read the history-window fields from the persisted run's `seed_summary` once per export. Extend TUI detail records only; keep recommendation list queries tied to persisted `recommendation` rows and never initiate API calls. Invalid/missing JSON must project empty maps and zero values, matching existing defensive parsing behavior.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_export.py tests/test_tui_data.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the isolated change**

```bash
git add src/scrobble_recs/export.py src/scrobble_recs/tui/data.py tests/test_export.py tests/test_tui_data.py
git commit -m "feat: expose recommendation quality evidence"
```

### Task 7: Make temporal evaluation leakage-free and document the new behavior

**Files:**

- Modify: `src/scrobble_recs/models/evaluate.py`
- Modify: `src/scrobble_recs/cli.py`
- Modify: `tests/test_evaluate.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`
- Modify: `docs/implementation-log.md`

**Interfaces:**

- `evaluate_recommendations(conn, config, *, train_until: datetime, test_from: datetime, test_until: datetime) -> EvaluationResult` uses `[train_until - 12 months, train_until)` for seeds and `[test_from, test_until)` for outcomes.
- `EvaluationResult` adds `precision_at_50`, `eligible_candidate_coverage`, `novelty_rate`, and `cooldown_or_repeat_rate`.

- [ ] **Step 1: Write failing leakage and metric tests**

```python
def test_evaluation_never_uses_future_scrobbles_as_seeds(conn, config) -> None:
    # Future-only artist has the highest play count but must not occur in seed_summary.
    result = evaluate_recommendations(conn, config,
        train_until=datetime(2025, 1, 1), test_from=datetime(2025, 1, 1), test_until=datetime(2025, 4, 1))
    summary = _latest_seed_summary(conn)
    assert "Future Favorite" not in {item["artist"] for item in summary["top_seeds"]}
    assert 0.0 <= result.precision_at_50 <= 1.0


def test_evaluation_outcomes_are_limited_to_horizon(conn, config) -> None:
    result = evaluate_recommendations(conn, config,
        train_until=datetime(2025, 1, 1), test_from=datetime(2025, 1, 1), test_until=datetime(2025, 2, 1))
    assert result.test_until == "2025-02-01T00:00:00"
```

Add CLI formatting assertions for the new metrics and update every `EvaluationResult` constructor fixture with `precision_at_50`, `eligible_candidate_coverage`, `novelty_rate`, and `cooldown_or_repeat_rate` values.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/test_evaluate.py tests/test_cli.py -q`

Expected: FAIL because evaluation currently builds from all-time aggregates and has no test horizon or new metrics.

- [ ] **Step 3: Implement cutoff-bounded evaluation and documentation**

```python
window = resolve_listening_window(as_of=train_until,
    default_months=config.recommendation.profile_window_months, since=None, until=train_until)
stats = generate_recommendations(conn, config, limit=100, as_of=train_until,
    history_window=window)
test_artists = _first_listened_artist_ids(conn, start=test_from, end=test_until,
    exclude_before=train_until)
precision_at_50 = _precision_at_k(test_artists, rec_artist_ids, 50)
```

Define a positive outcome as an artist whose first non-duplicate local scrobble is in the test horizon and has no scrobble before `train_until`. Compute eligible-candidate coverage from persisted candidate diagnostics, novelty from recommendations with no pre-cutoff listen, and repeat rate from cooldown/permanent exclusion diagnostic counts. Update the evaluate command to require or derive a bounded horizon. Document default 12-month profiles, `recommend --since/--until`, permanent hides, 30-day votes, discovery-only behavior, and explanation/export fields in README. Add a dated entry to the implementation log listing test commands and their results.

- [ ] **Step 4: Run complete verification**

Run: `python -m pytest`

Expected: PASS.

Run: `python -m ruff check .`

Expected: `All checks passed!`

Run: `python -m ruff format --check .`

Expected: all files already formatted.

Run: `python -m mypy src`

Expected: success with no type errors.

- [ ] **Step 5: Commit the isolated change**

```bash
git add src/scrobble_recs/models/evaluate.py src/scrobble_recs/cli.py tests/test_evaluate.py tests/test_cli.py README.md docs/implementation-log.md
git commit -m "feat: evaluate discovery recommendation quality"
```

## Plan Self-Review

- Spec coverage: Tasks 1-2 implement the 12-month/default and explicit profile ranges plus discovery-first behavior. Task 3 implements cooldowns, permanent hides, and diagnostics. Task 4 implements graph-only quality admission and component calibration. Task 5 implements bounded declustering/bridge serendipity. Task 6 exposes structured persisted evidence. Task 7 implements temporal holdout comparison inputs and required documentation.
- Placeholder scan: no `TBD`, `TODO`, deferred implementation step, or unspecified test remains in task instructions.
- Type consistency: `ListeningWindow` flows from CLI to seed selection and recommendation generation; quality and serendipity units consume tuple seed-edge inputs and produce structured explanation payloads; evaluation invokes the same windowed recommendation API.
