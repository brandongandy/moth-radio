# Recommendation Quality Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make rediscovery require a current taste match, damp weak/generic tags, and reduce repeated recommendations across persisted runs.

**Architecture:** Keep the recommender local-first and deterministic. Add config-only defaults, then apply tag hygiene while building user tag profiles, a quality gate before rediscovery contributes to scoring, and a fatigue penalty before diversity reranking. Persist gate and fatigue evidence in existing recommendation explanation JSON.

**Tech Stack:** Python, DuckDB, Pydantic config models, pytest, existing `scrobble_recs.models.recommend` and `scrobble_recs.models.profile_builder` modules.

---

## File Structure

- Modify `src/scrobble_recs/config.py`
  - Add `QualityGateConfig`, `FatigueConfig`, and `TagProfileConfig`.
  - Attach them to `RecommendationConfig`.
  - Update `default_config_document()`.
- Modify `src/scrobble_recs/models/profile_builder.py`
  - Apply tag specificity and weak-tag penalties to persisted `tag_profile`.
- Modify `src/scrobble_recs/models/recommend.py`
  - Apply the same tag hygiene to the in-memory recommendation tag profile.
  - Add candidate quality-gate decisions.
  - Add prior-run fatigue data and scoring penalty.
  - Add quality-gate and fatigue details to explanations.
- Modify `tests/test_config.py`
  - Cover new config defaults and validation.
- Modify `tests/test_profile.py`
  - Cover weak/generic tag damping.
- Modify `tests/test_recommend.py`
  - Cover rediscovery gate, rediscovery-mode threshold relaxation, fatigue, and explanation JSON.
- Modify `README.md`
  - Update the recommendation algorithm summary.
- Modify `docs/spec-v02.md`
  - Update rediscovery, tag profile, and ranking sections.
- Modify `docs/implementation-log.md`
  - Record implementation and verification.

---

### Task 1: Add Recommendation Quality Config

**Files:**
- Modify: `src/scrobble_recs/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Add these tests to `tests/test_config.py`:

```python
def test_recommendation_quality_gate_defaults_are_loaded(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": str(tmp_path / "scrobbles.csv"),
                    "timezone": "UTC",
                    "timestamp_column": "uts",
                    "csv_columns": list(EXPECTED_CSV_COLUMNS),
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.recommendation.quality_gate.enabled is True
    assert config.recommendation.quality_gate.rediscovery_requires_taste_match is True
    assert config.recommendation.quality_gate.min_lastfm_similarity_score == pytest.approx(0.01)
    assert config.recommendation.quality_gate.min_tag_similarity_score == pytest.approx(0.05)
    assert config.recommendation.quality_gate.min_positive_feedback_score == pytest.approx(0.05)
    assert config.recommendation.quality_gate.rediscovery_mode_multiplier == pytest.approx(0.75)
    assert config.recommendation.fatigue.enabled is True
    assert config.recommendation.fatigue.lookback_runs == 5
    assert config.recommendation.fatigue.latest_run_penalty == pytest.approx(0.12)
    assert config.recommendation.fatigue.repeat_appearance_penalty == pytest.approx(0.04)
    assert "soundtrack" in config.recommendation.tag_profile.weak_tags
    assert "spoken word" in config.recommendation.tag_profile.weak_tags
    assert config.recommendation.tag_profile.weak_tag_penalty == pytest.approx(0.25)
```

If `tests/test_config.py` does not already import `pytest` or `Path`, add:

```python
from pathlib import Path

import pytest
```

- [ ] **Step 2: Run the failing config test**

Run:

```bash
python -m pytest tests/test_config.py::test_recommendation_quality_gate_defaults_are_loaded -v
```

Expected: FAIL with `AttributeError` for `quality_gate`.

- [ ] **Step 3: Add config models**

In `src/scrobble_recs/config.py`, add these classes after `DiversityConfig`:

```python
class QualityGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    rediscovery_requires_taste_match: bool = True
    min_lastfm_similarity_score: float = 0.01
    min_tag_similarity_score: float = 0.05
    min_positive_feedback_score: float = 0.05
    rediscovery_mode_multiplier: float = 0.75

    @model_validator(mode="after")
    def validate_thresholds(self) -> QualityGateConfig:
        for field_name in (
            "min_lastfm_similarity_score",
            "min_tag_similarity_score",
            "min_positive_feedback_score",
        ):
            value = float(getattr(self, field_name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if not 0.0 < self.rediscovery_mode_multiplier <= 1.0:
            raise ValueError("rediscovery_mode_multiplier must be greater than 0 and at most 1")
        return self


class FatigueConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    lookback_runs: int = 5
    latest_run_penalty: float = 0.12
    repeat_appearance_penalty: float = 0.04

    @model_validator(mode="after")
    def validate_fatigue(self) -> FatigueConfig:
        if self.lookback_runs < 1:
            raise ValueError("lookback_runs must be at least 1")
        if self.latest_run_penalty < 0.0:
            raise ValueError("latest_run_penalty must be non-negative")
        if self.repeat_appearance_penalty < 0.0:
            raise ValueError("repeat_appearance_penalty must be non-negative")
        return self


class TagProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weak_tags: list[str] = Field(
        default_factory=lambda: [
            "seen live",
            "favorite",
            "favorites",
            "favourite",
            "lastfm",
            "spotify",
            "soundtrack",
            "spoken word",
            "rock",
            "indie",
            "alternative",
            "pop",
            "electronic",
            "classic rock",
            "female vocalists",
            "80s",
            "90s",
            "70s",
            "american",
            "canadian",
            "british",
            "japanese",
        ]
    )
    weak_tag_penalty: float = 0.25

    @field_validator("weak_tags")
    @classmethod
    def normalize_weak_tags(cls, value: list[str]) -> list[str]:
        return sorted({tag.strip().casefold() for tag in value if tag.strip()})

    @field_validator("weak_tag_penalty")
    @classmethod
    def weak_tag_penalty_must_be_ratio(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("weak_tag_penalty must be between 0 and 1")
        return value
```

Then add these fields to `RecommendationConfig`:

```python
quality_gate: QualityGateConfig = Field(default_factory=QualityGateConfig)
fatigue: FatigueConfig = Field(default_factory=FatigueConfig)
tag_profile: TagProfileConfig = Field(default_factory=TagProfileConfig)
```

- [ ] **Step 4: Update default config document**

In `default_config_document()`, add these keys under `"recommendation"`:

```python
"quality_gate": {
    "enabled": True,
    "rediscovery_requires_taste_match": True,
    "min_lastfm_similarity_score": 0.01,
    "min_tag_similarity_score": 0.05,
    "min_positive_feedback_score": 0.05,
    "rediscovery_mode_multiplier": 0.75,
},
"fatigue": {
    "enabled": True,
    "lookback_runs": 5,
    "latest_run_penalty": 0.12,
    "repeat_appearance_penalty": 0.04,
},
"tag_profile": {
    "weak_tags": [
        "seen live",
        "favorite",
        "favorites",
        "favourite",
        "lastfm",
        "spotify",
        "soundtrack",
        "spoken word",
        "rock",
        "indie",
        "alternative",
        "pop",
        "electronic",
        "classic rock",
        "female vocalists",
        "80s",
        "90s",
        "70s",
        "american",
        "canadian",
        "british",
        "japanese",
    ],
    "weak_tag_penalty": 0.25,
},
```

- [ ] **Step 5: Run config tests**

Run:

```bash
python -m pytest tests/test_config.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/scrobble_recs/config.py tests/test_config.py
git commit -m "Add recommendation quality config"
```

---

### Task 2: Apply Tag Specificity and Weak-Tag Damping

**Files:**
- Modify: `src/scrobble_recs/models/profile_builder.py`
- Modify: `src/scrobble_recs/models/recommend.py`
- Test: `tests/test_profile.py`
- Test: `tests/test_recommend.py`

- [ ] **Step 1: Write failing profile-builder test**

Add this test to `tests/test_profile.py`:

```python
def test_build_profile_damps_weak_and_overbroad_tags(tmp_path: Path) -> None:
    from scrobble_recs.models.profile_builder import build_profile

    config = load_config(_config_path(tmp_path))
    conn = initialize_database(config)
    try:
        _insert_profile_artist(
            conn,
            artist_id=70,
            name="Specific Anchor One",
            play_count=100,
            lifetime_weight=10.0,
            recent_weight=10.0,
            knownness=0.5,
            rediscovery_score=0.0,
            last_played_at="2026-05-01",
        )
        _insert_profile_artist(
            conn,
            artist_id=71,
            name="Specific Anchor Two",
            play_count=90,
            lifetime_weight=9.0,
            recent_weight=9.0,
            knownness=0.5,
            rediscovery_score=0.0,
            last_played_at="2026-05-01",
        )
        _insert_artist_tag(conn, artist_id=70, tag="rock", weight=1.0)
        _insert_artist_tag(conn, artist_id=70, tag="zeuhl", weight=0.8)
        _insert_artist_tag(conn, artist_id=71, tag="rock", weight=1.0)

        build_profile(conn, config, as_of=datetime(2026, 6, 5, 12, 0, 0))

        rows = {
            str(tag): float(user_weight)
            for tag, user_weight in conn.execute(
                "SELECT tag, user_weight FROM tag_profile WHERE source = 'lastfm'"
            ).fetchall()
        }
        assert rows["zeuhl"] > rows["rock"]
    finally:
        conn.close()
```

- [ ] **Step 2: Run failing profile-builder test**

Run:

```bash
python -m pytest tests/test_profile.py::test_build_profile_damps_weak_and_overbroad_tags -v
```

Expected: FAIL because `rock` is not damped below `zeuhl`.

- [ ] **Step 3: Update `build_profile()` to pass config into tag profile building**

In `src/scrobble_recs/models/profile_builder.py`, change:

```python
tag_profile = build_user_tag_profile(conn, seeds)
```

to:

```python
tag_profile = build_user_tag_profile(
    conn,
    seeds,
    weak_tags=config.recommendation.tag_profile.weak_tags,
    weak_tag_penalty=config.recommendation.tag_profile.weak_tag_penalty,
)
```

Then change the function signature:

```python
def build_user_tag_profile(
    conn: DuckDBPyConnection,
    seeds: list[SeedArtist],
    *,
    weak_tags: list[str] | None = None,
    weak_tag_penalty: float = 1.0,
) -> list[_TagProfileEntry]:
```

- [ ] **Step 4: Apply specificity and weak-tag penalty**

In `build_user_tag_profile()`, before iterating rows, add:

```python
normalized_weak_tags = {
    _normalize_tag_key(tag)
    for tag in (weak_tags or [])
    if _normalize_tag_key(tag) != ""
}
artists_by_tag: dict[str, set[int]] = {}
for artist_id, tag, _normalized_weight in rows:
    normalized_tag = _normalize_tag_key(tag)
    if normalized_tag:
        artists_by_tag.setdefault(normalized_tag, set()).add(int(artist_id))
```

Replace:

```python
contribution = seed.seed_weight * tag_weight
```

with:

```python
specificity = _tag_specificity(len(artists_by_tag.get(normalized_tag, set())))
weak_multiplier = (
    weak_tag_penalty if normalized_tag in normalized_weak_tags else 1.0
)
contribution = seed.seed_weight * tag_weight * specificity * weak_multiplier
```

Add this helper near `_normalize_tag_key()`:

```python
def _tag_specificity(seed_artist_count: int) -> float:
    if seed_artist_count < 1:
        return 0.0
    return 1.0 / math.log(2 + seed_artist_count)
```

Add `import math` at the top of `profile_builder.py`.

- [ ] **Step 5: Update recommendation in-memory tag profile**

In `src/scrobble_recs/models/recommend.py`, change:

```python
tag_profile = _load_user_tag_profile(conn, seeds)
```

to:

```python
tag_profile = _load_user_tag_profile(
    conn,
    seeds,
    weak_tags=config.recommendation.tag_profile.weak_tags,
    weak_tag_penalty=config.recommendation.tag_profile.weak_tag_penalty,
)
```

Change `_load_user_tag_profile()` signature:

```python
def _load_user_tag_profile(
    conn: DuckDBPyConnection,
    seeds: list[SeedArtist],
    *,
    weak_tags: list[str] | None = None,
    weak_tag_penalty: float = 1.0,
) -> dict[str, float]:
```

Inside `_load_user_tag_profile()`, after fetching `rows`, add:

```python
normalized_weak_tags = {
    _normalize_tag_key(tag)
    for tag in (weak_tags or [])
    if _normalize_tag_key(tag) != ""
}
artists_by_tag: dict[str, set[int]] = {}
for artist_id, tag, _normalized_weight in rows:
    seed_weight = seed_weight_by_id.get(int(artist_id))
    if seed_weight is None:
        continue
    normalized_tag = _normalize_tag_key(tag)
    if normalized_tag:
        artists_by_tag.setdefault(normalized_tag, set()).add(int(artist_id))
```

Replace:

```python
profile[normalized_tag] = profile.get(normalized_tag, 0.0) + seed_weight * float(
    normalized_weight
)
```

with:

```python
specificity = _tag_specificity(len(artists_by_tag.get(normalized_tag, set())))
weak_multiplier = (
    weak_tag_penalty if normalized_tag in normalized_weak_tags else 1.0
)
profile[normalized_tag] = profile.get(normalized_tag, 0.0) + (
    seed_weight * float(normalized_weight) * specificity * weak_multiplier
)
```

Add `from math import log, sqrt` or change the existing import to:

```python
from math import log, sqrt
```

Add this helper near `_normalize_tag_key()`:

```python
def _tag_specificity(seed_artist_count: int) -> float:
    if seed_artist_count < 1:
        return 0.0
    return 1.0 / log(2 + seed_artist_count)
```

- [ ] **Step 6: Run tag tests**

Run:

```bash
python -m pytest tests/test_profile.py::test_build_profile_damps_weak_and_overbroad_tags tests/test_profile.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/scrobble_recs/models/profile_builder.py src/scrobble_recs/models/recommend.py tests/test_profile.py
git commit -m "Damp weak tags in user profiles"
```

---

### Task 3: Add Candidate Quality Gate

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`
- Test: `tests/test_recommend.py`

- [ ] **Step 1: Write failing rediscovery-gate test**

Add this test to `tests/test_recommend.py`:

```python
def test_generate_recommendations_skips_rediscovery_without_current_taste_match(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=500,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=500, tag="post-punk", weight=100.0)
    _insert_scoring_artist(
        conn,
        artist_id=501,
        name="Old Random Local",
        play_count=5,
        lifetime_weight=0.0,
        recent_weight=0.0,
        knownness=0.02,
        rediscovery_score=0.95,
    )
    conn.execute(
        """
        INSERT INTO tag_top_artist (
            tag,
            artist_id,
            source,
            tag_rank,
            tag_count,
            fetched_at
        )
        VALUES ('spoken word', 501, 'lastfm', 91, 1.0, current_timestamp)
        """
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert stats.recommendations == 0
    assert _recommendation_names(conn, stats.run_id) == []
    conn.close()
```

- [ ] **Step 2: Run failing test**

Run:

```bash
python -m pytest tests/test_recommend.py::test_generate_recommendations_skips_rediscovery_without_current_taste_match -v
```

Expected: FAIL because `Old Random Local` is recommended.

- [ ] **Step 3: Add quality-gate decision data**

In `src/scrobble_recs/models/recommend.py`, add this dataclass after `_FeedbackScore`:

```python
@dataclass(frozen=True)
class _QualityGateDecision:
    passed: bool
    reasons: tuple[str, ...]
    thresholds: dict[str, float]
```

Add this field to `_CandidateScore`:

```python
quality_gate: _QualityGateDecision
```

- [ ] **Step 4: Add quality-gate helper**

Add this helper near `_raw_score()`:

```python
def _quality_gate_decision(
    config: ScrobbleRecsConfig,
    mode: RecommendationModeSettings,
    *,
    lastfm_similarity_score: float,
    tag_similarity_score: float,
    feedback_score: float,
    rediscovery_score: float,
) -> _QualityGateDecision:
    gate = config.recommendation.quality_gate
    if not gate.enabled:
        return _QualityGateDecision(
            passed=True,
            reasons=("disabled",),
            thresholds={
                "min_lastfm_similarity_score": 0.0,
                "min_tag_similarity_score": 0.0,
                "min_positive_feedback_score": 0.0,
            },
        )

    multiplier = gate.rediscovery_mode_multiplier if mode.name == "rediscovery" else 1.0
    min_lastfm = gate.min_lastfm_similarity_score * multiplier
    min_tag = gate.min_tag_similarity_score * multiplier
    min_feedback = gate.min_positive_feedback_score * multiplier
    reasons: list[str] = []
    if lastfm_similarity_score >= min_lastfm:
        reasons.append("lastfm_similarity")
    if tag_similarity_score >= min_tag:
        reasons.append("tag_similarity")
    if feedback_score >= min_feedback:
        reasons.append("feedback")
    if not gate.rediscovery_requires_taste_match and rediscovery_score > 0.0:
        reasons.append("rediscovery")
    return _QualityGateDecision(
        passed=bool(reasons),
        reasons=tuple(reasons),
        thresholds={
            "min_lastfm_similarity_score": min_lastfm,
            "min_tag_similarity_score": min_tag,
            "min_positive_feedback_score": min_feedback,
        },
    )
```

- [ ] **Step 5: Apply the gate before raw scoring**

In `_score_candidates()`, after computing `lastfm_score`, `tag_score`, and `feedback`, add:

```python
quality_gate = _quality_gate_decision(
    config,
    mode,
    lastfm_similarity_score=lastfm_score,
    tag_similarity_score=tag_score,
    feedback_score=feedback.score,
    rediscovery_score=rediscovery,
)
if not quality_gate.passed:
    continue
```

When constructing `_CandidateScore`, add:

```python
quality_gate=quality_gate,
```

- [ ] **Step 6: Add quality gate to explanation JSON**

In `_explanation()`, add this top-level key:

```python
"quality_gate": {
    "passed": score.quality_gate.passed,
    "reasons": list(score.quality_gate.reasons),
    "thresholds": score.quality_gate.thresholds,
    "scores": {
        "lastfm_similarity_score": score.lastfm_similarity_score,
        "tag_similarity_score": score.tag_similarity_score,
        "feedback_score": score.feedback_score,
        "rediscovery_score": score.rediscovery_score,
    },
},
```

- [ ] **Step 7: Run gate test and recommendation tests**

Run:

```bash
python -m pytest tests/test_recommend.py::test_generate_recommendations_skips_rediscovery_without_current_taste_match tests/test_recommend.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
git add src/scrobble_recs/models/recommend.py tests/test_recommend.py
git commit -m "Require taste match for rediscovery"
```

---

### Task 4: Cover Positive Gate Cases and Rediscovery Mode Relaxation

**Files:**
- Modify: `tests/test_recommend.py`
- Modify if tests reveal a bug: `src/scrobble_recs/models/recommend.py`

- [ ] **Step 1: Add positive gate tests**

Add these tests to `tests/test_recommend.py`:

```python
def test_generate_recommendations_allows_rediscovery_with_tag_match(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=510,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=510, tag="coldwave", weight=100.0)
    _insert_scoring_artist(
        conn,
        artist_id=511,
        name="Old Matching Local",
        play_count=5,
        lifetime_weight=0.0,
        recent_weight=0.0,
        knownness=0.02,
        rediscovery_score=0.95,
    )
    conn.execute(
        """
        INSERT INTO tag_top_artist (
            tag,
            artist_id,
            source,
            tag_rank,
            tag_count,
            fetched_at
        )
        VALUES ('coldwave', 511, 'lastfm', 40, 1.0, current_timestamp)
        """
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == ["Old Matching Local"]
    (explanation_json,) = conn.execute(
        "SELECT explanation FROM recommendation WHERE run_id = ? AND artist_id = 511",
        [stats.run_id],
    ).fetchone()
    explanation = json.loads(explanation_json)
    assert explanation["quality_gate"]["passed"] is True
    assert "tag_similarity" in explanation["quality_gate"]["reasons"]
    conn.close()


def test_generate_recommendations_rediscovery_mode_relaxes_gate_thresholds(
    tmp_path: Path,
) -> None:
    config_path = write_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["recommendation"]["quality_gate"] = {
        "enabled": True,
        "rediscovery_requires_taste_match": True,
        "min_lastfm_similarity_score": 0.50,
        "min_tag_similarity_score": 0.50,
        "min_positive_feedback_score": 0.05,
        "rediscovery_mode_multiplier": 0.50,
    }
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(config_path)
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=520,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=520, tag="coldwave", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=520,
        candidate_id=521,
        candidate_name="Relaxed Threshold Candidate",
        tag="noise",
        tag_rank=80,
        similarity=0.30,
    )

    balanced = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )
    rediscovery = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="rediscovery",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, balanced.run_id) == []
    assert _recommendation_names(conn, rediscovery.run_id) == ["Relaxed Threshold Candidate"]
    conn.close()
```

- [ ] **Step 2: Run the new tests**

Run:

```bash
python -m pytest tests/test_recommend.py::test_generate_recommendations_allows_rediscovery_with_tag_match tests/test_recommend.py::test_generate_recommendations_rediscovery_mode_relaxes_gate_thresholds -v
```

Expected: PASS. The second test uses a non-matching candidate tag so balanced mode cannot pass through tag similarity; rediscovery mode passes through the relaxed Last.fm similarity threshold.

- [ ] **Step 3: Run all recommendation tests**

Run:

```bash
python -m pytest tests/test_recommend.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

Run:

```bash
git add tests/test_recommend.py src/scrobble_recs/models/recommend.py
git commit -m "Cover quality gate pass cases"
```

---

### Task 5: Add Cross-Run Fatigue

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`
- Test: `tests/test_recommend.py`

- [ ] **Step 1: Write failing fatigue test**

Add this test to `tests/test_recommend.py`:

```python
def test_generate_recommendations_demotes_recently_recommended_artist(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=530,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=530, tag="coldwave", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=530,
        candidate_id=531,
        candidate_name="Repeated Candidate",
        tag="coldwave",
        tag_rank=40,
        similarity=0.80,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=530,
        candidate_id=532,
        candidate_name="Fresh Candidate",
        tag="coldwave",
        tag_rank=41,
        similarity=0.78,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000530")
    conn.execute(
        """
        INSERT INTO recommendation (
            run_id,
            rank,
            artist_id,
            final_score,
            raw_score,
            explanation
        )
        VALUES (
            '00000000-0000-0000-0000-000000000530',
            1,
            531,
            0.9,
            0.9,
            '{}'
        )
        """
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == [
        "Fresh Candidate",
        "Repeated Candidate",
    ]
    (explanation_json,) = conn.execute(
        "SELECT explanation FROM recommendation WHERE run_id = ? AND artist_id = 531",
        [stats.run_id],
    ).fetchone()
    explanation = json.loads(explanation_json)
    assert explanation["fatigue"]["prior_run_appearances"] == 1
    assert explanation["fatigue"]["latest_run_rank"] == 1
    assert explanation["fatigue"]["penalty"] > 0.0
    conn.close()
```

- [ ] **Step 2: Run failing fatigue test**

Run:

```bash
python -m pytest tests/test_recommend.py::test_generate_recommendations_demotes_recently_recommended_artist -v
```

Expected: FAIL because prior runs do not affect ranking.

- [ ] **Step 3: Add fatigue data structures**

In `src/scrobble_recs/models/recommend.py`, add this dataclass after `_QualityGateDecision`:

```python
@dataclass(frozen=True)
class _FatiguePenalty:
    prior_run_appearances: int
    latest_run_rank: int | None
    penalty: float
```

Add this field to `_CandidateScore`:

```python
fatigue: _FatiguePenalty
```

- [ ] **Step 4: Load fatigue rows**

Add this helper near `_load_scoring_data()`:

```python
def _load_fatigue_by_artist(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    feedback_profile: FeedbackProfile,
) -> dict[int, _FatiguePenalty]:
    fatigue = config.recommendation.fatigue
    if not fatigue.enabled:
        return {}
    recent_runs = conn.execute(
        """
        SELECT run_id
        FROM recommendation_run
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [fatigue.lookback_runs],
    ).fetchall()
    run_ids = [str(row[0]) for row in recent_runs]
    if not run_ids:
        return {}
    rows = conn.execute(
        """
        SELECT
            artist_id,
            count(*) AS appearances,
            min(CASE WHEN run_id = ? THEN rank ELSE NULL END) AS latest_rank
        FROM recommendation
        WHERE run_id IN (SELECT unnest(?))
        GROUP BY artist_id
        """,
        [run_ids[0], run_ids],
    ).fetchall()
    result: dict[int, _FatiguePenalty] = {}
    for artist_id, appearances, latest_rank in rows:
        resolved_artist_id = int(artist_id)
        if feedback_profile.artist_adjustments.get(resolved_artist_id, 0.0) > 0.0:
            continue
        latest_penalty = fatigue.latest_run_penalty if latest_rank is not None else 0.0
        repeat_penalty = fatigue.repeat_appearance_penalty * max(0, int(appearances) - 1)
        result[resolved_artist_id] = _FatiguePenalty(
            prior_run_appearances=int(appearances),
            latest_run_rank=None if latest_rank is None else int(latest_rank),
            penalty=latest_penalty + repeat_penalty,
        )
    return result
```

- [ ] **Step 5: Apply fatigue during scoring**

In `generate_recommendations()`, after loading `feedback_profile`, add:

```python
fatigue_by_artist = _load_fatigue_by_artist(conn, config, feedback_profile)
```

Pass `fatigue_by_artist` into `_score_candidates()` and update its signature:

```python
fatigue_by_artist: dict[int, _FatiguePenalty],
```

Inside `_score_candidates()`, before `final_score`, add:

```python
fatigue = fatigue_by_artist.get(
    artist_id,
    _FatiguePenalty(prior_run_appearances=0, latest_run_rank=None, penalty=0.0),
)
```

Change:

```python
final_score = raw_score - knownness_penalty - obviousness_penalty
```

to:

```python
final_score = raw_score - knownness_penalty - obviousness_penalty - fatigue.penalty
```

When constructing `_CandidateScore`, add:

```python
fatigue=fatigue,
```

- [ ] **Step 6: Add fatigue to explanation JSON**

In `_explanation()`, add:

```python
"fatigue": {
    "prior_run_appearances": score.fatigue.prior_run_appearances,
    "latest_run_rank": score.fatigue.latest_run_rank,
    "penalty": score.fatigue.penalty,
},
```

- [ ] **Step 7: Run fatigue test**

Run:

```bash
python -m pytest tests/test_recommend.py::test_generate_recommendations_demotes_recently_recommended_artist -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
git add src/scrobble_recs/models/recommend.py tests/test_recommend.py
git commit -m "Demote recent repeated recommendations"
```

---

### Task 6: Positive Feedback Reduces Fatigue

**Files:**
- Modify: `tests/test_recommend.py`
- Modify if tests reveal a bug: `src/scrobble_recs/models/recommend.py`

- [ ] **Step 1: Add positive-feedback fatigue test**

Add this test to `tests/test_recommend.py`:

```python
def test_generate_recommendations_positive_feedback_bypasses_fatigue(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=540,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=540, tag="coldwave", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=540,
        candidate_id=541,
        candidate_name="Saved Repeated Candidate",
        tag="coldwave",
        tag_rank=40,
        similarity=0.80,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=540,
        candidate_id=542,
        candidate_name="Fresh Candidate",
        tag="coldwave",
        tag_rank=41,
        similarity=0.78,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000540")
    conn.execute(
        """
        INSERT INTO recommendation (
            run_id,
            rank,
            artist_id,
            final_score,
            raw_score,
            explanation
        )
        VALUES (
            '00000000-0000-0000-0000-000000000540',
            1,
            541,
            0.9,
            0.9,
            '{}'
        )
        """
    )
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000540",
        artist_id=541,
        action="save",
        feedback_id="00000000-0000-0000-0000-000000000541",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id)[0] == "Saved Repeated Candidate"
    (explanation_json,) = conn.execute(
        "SELECT explanation FROM recommendation WHERE run_id = ? AND artist_id = 541",
        [stats.run_id],
    ).fetchone()
    explanation = json.loads(explanation_json)
    assert explanation["fatigue"]["penalty"] == 0.0
    conn.close()
```

- [ ] **Step 2: Run the test**

Run:

```bash
python -m pytest tests/test_recommend.py::test_generate_recommendations_positive_feedback_bypasses_fatigue -v
```

Expected: PASS if Task 5 used `feedback_profile.artist_adjustments > 0.0`; otherwise FAIL and fix `_load_fatigue_by_artist()` to skip positively adjusted artists.

- [ ] **Step 3: Run recommendation tests**

Run:

```bash
python -m pytest tests/test_recommend.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

Run:

```bash
git add tests/test_recommend.py src/scrobble_recs/models/recommend.py
git commit -m "Preserve saved repeated recommendations"
```

---

### Task 7: Update Docs and Implementation Log

**Files:**
- Modify: `README.md`
- Modify: `docs/spec-v02.md`
- Modify: `docs/implementation-log.md`

- [ ] **Step 1: Update README recommendation summary**

In `README.md`, update the "Recommendation Algorithm" section so it states:

```markdown
Rediscovery does not act as a standalone recommendation source. An old,
lightly played artist must still match the current profile through seed
similarity, tag fit, or positive feedback before rediscovery can boost it.
Broad Last.fm tags are damped so generic tags such as `rock`, `indie`,
`soundtrack`, and `spoken word` do not dominate more specific taste evidence.
Artists shown in recent recommendation runs receive a fatigue penalty unless
positive feedback says they should remain prominent.
```

- [ ] **Step 2: Update v0.2 spec**

In `docs/spec-v02.md`, update the rediscovery, user tag profile, and hybrid
ranking sections with this language:

```markdown
Rediscovery candidates must pass the recommendation quality gate before their
rediscovery score contributes to ranking. The gate requires current taste
evidence from Last.fm seed similarity, tag similarity, or positive feedback.
Rediscovery mode may relax gate thresholds, but it must not remove the gate by
default.

Tag profile construction applies tag specificity and weak-tag penalties. Weak
tags are not deleted; their contribution is multiplied down so broad Last.fm
labels and community artifacts cannot swamp specific tags.

Final scoring subtracts fatigue for artists shown in recent persisted runs.
Positive feedback can bypass fatigue for the affected artist.
```

- [ ] **Step 3: Update implementation log**

Append this section to `docs/implementation-log.md`:

```markdown
## 2026-06-07: Recommendation quality gate

Changed:

- Added recommendation quality-gate config, fatigue config, and weak tag-profile config.
- Dampened weak/generic tags and tags shared by many seed artists.
- Required rediscovery recommendations to have a current taste match.
- Added cross-run fatigue for repeated recommendations.
- Added quality-gate and fatigue evidence to recommendation explanations.

Verified:

- `python -m pytest tests/test_config.py -v`
- `python -m pytest tests/test_profile.py -v`
- `python -m pytest tests/test_recommend.py -v`
- Full verification commands listed below before final completion.
```

- [ ] **Step 4: Commit docs**

Run:

```bash
git add README.md docs/spec-v02.md docs/implementation-log.md
git commit -m "Document recommendation quality gate"
```

---

### Task 8: Final Verification

**Files:**
- No source edits expected.

- [ ] **Step 1: Run full test suite**

Run:

```bash
python -m pytest
```

Expected: PASS.

- [ ] **Step 2: Run Ruff checks**

Run:

```bash
python -m ruff check .
python -m ruff format --check .
```

Expected: PASS.

- [ ] **Step 3: Run mypy**

Run:

```bash
python -m mypy src
```

Expected: PASS.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git status --short
git diff --stat
```

Expected: only intentional files changed, or a clean tree if all task commits were made.

Do not delete the unrelated untracked item `1` unless the user explicitly says it is disposable.

---

## Self-Review

Spec coverage:

- Rediscovery requires current taste match: Task 3 and Task 4.
- Tag specificity and weak tags: Task 2.
- Repeated recommendations: Task 5 and Task 6.
- Explanation JSON: Task 3 and Task 5.
- Docs and implementation log: Task 7.
- Verification: Task 8.

Placeholder scan:

- No `TBD`, `TODO`, or "implement later" placeholders remain.
- Every task has concrete files, test commands, expected results, and commit commands.

Type consistency:

- Config fields are consistently named `quality_gate`, `fatigue`, and `tag_profile`.
- Gate dataclass is consistently `_QualityGateDecision`.
- Fatigue dataclass is consistently `_FatiguePenalty`.
- Explanation keys are consistently `quality_gate` and `fatigue`.
