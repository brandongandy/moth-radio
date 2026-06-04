# Recommender Quality Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve recommendation quality by selecting seeds from multiple taste regions, applying mode-specific ranking semantics, hiding the most obvious top-tag artists by default, and diversifying the persisted ranked list.

**Architecture:** Keep the recommender offline-first and deterministic: all quality changes operate on DuckDB facts and persisted Last.fm enrichment tables. Add a small mode-settings module, extend seed selection to combine named profile regions, apply hard-hide and mode-specific scoring before persistence, then add a final maximal marginal relevance reranker that records diversity penalties in existing recommendation rows.

**Tech Stack:** Python 3.11+, DuckDB, Typer, Pydantic v2, pytest, ruff, mypy.

---

## Scope

This plan covers the next ranking-quality slice from `docs/spec.md`:

- Section 10: profile depth through lifetime, recent, old-obsession, and underexplored taste regions.
- Section 11.1: seed selection from multiple taste regions instead of only recent-weight ordering.
- Section 12: obviousness penalty plus default hard-hide behavior for rank 1-3 high-affinity tag artists.
- Section 14: maximal marginal relevance diversity reranking and a same-primary-tag cap.
- Section 15: recommendation modes that change seed regions, scoring tolerance, obviousness behavior, and diversity strength.
- Section 23: sensible defaults for seed limits, knownness threshold, obvious top-tag hide, top-tag penalty, and run size.
- Section 24: full local algorithm step 11, diversifying ranked results before persisting.

This plan deliberately keeps these pieces out of scope:

- TUI mode controls and tag-selection widgets.
- Feedback persistence and manual boosts.
- MusicBrainz metadata graph scoring.
- ListenBrainz collaborative scoring.
- Live API calls during `recommend`.

## File Structure

- Create: `src/scrobble_recs/models/modes.py` for validated mode settings and tag-focus mode parsing.
- Modify: `src/scrobble_recs/models/profile.py` to select seed artists from named profile regions.
- Modify: `src/scrobble_recs/models/recommend.py` to consume mode settings, hard-hide obvious candidates, rerank for diversity, and persist richer run metadata.
- Modify: `src/scrobble_recs/cli.py` only if the mode error message needs to remain user-facing through existing `ValueError` handling.
- Modify: `README.md` to document quality-pass recommendation modes and obviousness behavior.
- Modify: `docs/implementation-log.md` with the completed change and verification notes.
- Test: `tests/test_modes.py`.
- Modify: `tests/test_profile.py`.
- Modify: `tests/test_recommend.py`.
- Modify: `tests/test_cli.py` only if the CLI mode validation behavior changes.

---

### Task 1: Mode Settings

**Files:**
- Create: `src/scrobble_recs/models/modes.py`
- Test: `tests/test_modes.py`

- [x] **Step 1: Write failing mode tests**

Create `tests/test_modes.py` with:

```python
import json
from pathlib import Path

import pytest

from scrobble_recs.config import load_config
from scrobble_recs.models.modes import resolve_recommendation_mode


def _config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": "tests/fixtures/tiny_lastfm_export.csv",
                    "timezone": "UTC",
                    "timestamp_column": "uts",
                    "csv_columns": [
                        "uts",
                        "utc_time",
                        "artist",
                        "artist_mbid",
                        "album",
                        "album_mbid",
                        "track",
                        "track_mbid",
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_balanced_mode_uses_sensible_defaults(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))

    mode = resolve_recommendation_mode(None, config)

    assert mode.name == "balanced"
    assert mode.seed_regions == (
        ("lifetime", 150, 1.00),
        ("recent", 75, 1.00),
        ("old_obsession", 50, 0.85),
        ("underexplored", 50, 0.75),
    )
    assert mode.diversity_lambda == pytest.approx(0.75)
    assert mode.max_knownness == pytest.approx(0.65)
    assert mode.obviousness_penalty_weight == pytest.approx(0.25)
    assert mode.hard_hide_top_tag_rank == 3
    assert mode.same_primary_tag_cap == 4
    assert mode.focus_tags == ()


@pytest.mark.parametrize(
    ("mode_name", "expected_lambda", "expected_knownness", "expected_penalty"),
    [
        ("explore", 0.55, 0.45, 0.35),
        ("safe", 0.90, 0.75, 0.15),
        ("rediscovery", 0.80, 0.80, 0.20),
        ("recent", 0.75, 0.60, 0.25),
        ("old", 0.80, 0.75, 0.20),
        ("anti-obvious", 0.65, 0.55, 0.45),
    ],
)
def test_named_modes_resolve_ranking_knobs(
    tmp_path: Path,
    mode_name: str,
    expected_lambda: float,
    expected_knownness: float,
    expected_penalty: float,
) -> None:
    config = load_config(_config_path(tmp_path))

    mode = resolve_recommendation_mode(mode_name, config)

    assert mode.name == mode_name
    assert mode.diversity_lambda == pytest.approx(expected_lambda)
    assert mode.max_knownness == pytest.approx(expected_knownness)
    assert mode.obviousness_penalty_weight == pytest.approx(expected_penalty)
    assert mode.seed_regions


def test_tag_focus_mode_parses_comma_separated_tags(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))

    mode = resolve_recommendation_mode("tag:post-punk, gothic rock", config)

    assert mode.name == "tag"
    assert mode.focus_tags == ("post-punk", "gothic rock")
    assert mode.tag_focus_boost == pytest.approx(1.60)
    assert mode.seed_regions == (
        ("lifetime", 125, 0.85),
        ("recent", 75, 1.00),
        ("underexplored", 50, 0.75),
    )


def test_unknown_mode_raises_clear_value_error(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))

    with pytest.raises(ValueError, match="unsupported recommendation mode 'fresh'"):
        resolve_recommendation_mode("fresh", config)


def test_empty_tag_focus_mode_raises_clear_value_error(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))

    with pytest.raises(ValueError, match="tag focus mode requires at least one tag"):
        resolve_recommendation_mode("tag:", config)
```

- [x] **Step 2: Run mode tests to verify they fail**

Run:

```bash
python -m pytest tests/test_modes.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.models.modes'
```

- [x] **Step 3: Implement mode settings**

Create `src/scrobble_recs/models/modes.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from scrobble_recs.config import ScrobbleRecsConfig

SeedRegionName = Literal["lifetime", "recent", "old_obsession", "underexplored"]
SeedRegionSetting = tuple[SeedRegionName, int, float]


@dataclass(frozen=True)
class RecommendationModeSettings:
    name: str
    seed_regions: tuple[SeedRegionSetting, ...]
    diversity_lambda: float
    max_knownness: float
    obviousness_penalty_weight: float
    hard_hide_top_tag_rank: int
    high_affinity_tag_weight: float
    same_primary_tag_cap: int
    rediscovery_multiplier: float = 1.0
    lastfm_similarity_multiplier: float = 1.0
    tag_similarity_multiplier: float = 1.0
    tag_focus_boost: float = 1.0
    focus_tags: tuple[str, ...] = ()


def resolve_recommendation_mode(
    requested_mode: str | None,
    config: ScrobbleRecsConfig,
) -> RecommendationModeSettings:
    mode_name = (requested_mode or config.recommendation.default_mode).strip().casefold()
    if mode_name.startswith("tag:"):
        focus_tags = tuple(
            tag.strip().casefold() for tag in mode_name.removeprefix("tag:").split(",") if tag.strip()
        )
        if not focus_tags:
            raise ValueError("tag focus mode requires at least one tag")
        return RecommendationModeSettings(
            name="tag",
            seed_regions=(
                ("lifetime", 125, 0.85),
                ("recent", 75, 1.00),
                ("underexplored", 50, 0.75),
            ),
            diversity_lambda=0.70,
            max_knownness=config.recommendation.max_knownness_default,
            obviousness_penalty_weight=config.recommendation.obviousness.penalty_weight,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.75,
            same_primary_tag_cap=config.recommendation.diversity.max_same_primary_tag_in_top_20,
            tag_similarity_multiplier=1.25,
            tag_focus_boost=1.60,
            focus_tags=focus_tags,
        )

    base = {
        "balanced": RecommendationModeSettings(
            name="balanced",
            seed_regions=(
                ("lifetime", 150, 1.00),
                ("recent", 75, 1.00),
                ("old_obsession", 50, 0.85),
                ("underexplored", 50, 0.75),
            ),
            diversity_lambda=config.recommendation.diversity.lambda_,
            max_knownness=config.recommendation.max_knownness_default,
            obviousness_penalty_weight=config.recommendation.obviousness.penalty_weight,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.75,
            same_primary_tag_cap=config.recommendation.diversity.max_same_primary_tag_in_top_20,
        ),
        "explore": RecommendationModeSettings(
            name="explore",
            seed_regions=(
                ("underexplored", 100, 1.20),
                ("recent", 75, 1.00),
                ("lifetime", 75, 0.65),
                ("old_obsession", 50, 0.80),
            ),
            diversity_lambda=0.55,
            max_knownness=0.45,
            obviousness_penalty_weight=0.35,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.70,
            same_primary_tag_cap=3,
            tag_similarity_multiplier=1.10,
        ),
        "safe": RecommendationModeSettings(
            name="safe",
            seed_regions=(
                ("lifetime", 175, 1.15),
                ("recent", 75, 1.00),
            ),
            diversity_lambda=0.90,
            max_knownness=0.75,
            obviousness_penalty_weight=0.15,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.80,
            same_primary_tag_cap=5,
            lastfm_similarity_multiplier=1.20,
        ),
        "rediscovery": RecommendationModeSettings(
            name="rediscovery",
            seed_regions=(
                ("old_obsession", 125, 1.30),
                ("underexplored", 75, 1.00),
                ("recent", 50, 0.80),
            ),
            diversity_lambda=0.80,
            max_knownness=0.80,
            obviousness_penalty_weight=0.20,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.75,
            same_primary_tag_cap=4,
            rediscovery_multiplier=1.75,
        ),
        "recent": RecommendationModeSettings(
            name="recent",
            seed_regions=(
                ("recent", 175, 1.30),
                ("lifetime", 75, 0.70),
            ),
            diversity_lambda=0.75,
            max_knownness=0.60,
            obviousness_penalty_weight=0.25,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.75,
            same_primary_tag_cap=4,
        ),
        "old": RecommendationModeSettings(
            name="old",
            seed_regions=(
                ("old_obsession", 150, 1.30),
                ("lifetime", 100, 0.80),
            ),
            diversity_lambda=0.80,
            max_knownness=0.75,
            obviousness_penalty_weight=0.20,
            hard_hide_top_tag_rank=config.recommendation.obviousness.top_n_per_tag_hide,
            high_affinity_tag_weight=0.75,
            same_primary_tag_cap=4,
            rediscovery_multiplier=1.35,
        ),
        "anti-obvious": RecommendationModeSettings(
            name="anti-obvious",
            seed_regions=(
                ("underexplored", 100, 1.15),
                ("recent", 75, 1.00),
                ("old_obsession", 50, 0.90),
                ("lifetime", 50, 0.60),
            ),
            diversity_lambda=0.65,
            max_knownness=0.55,
            obviousness_penalty_weight=0.45,
            hard_hide_top_tag_rank=10,
            high_affinity_tag_weight=0.55,
            same_primary_tag_cap=3,
            tag_similarity_multiplier=1.10,
        ),
    }
    try:
        return base[mode_name]
    except KeyError as exc:
        supported = ", ".join(sorted(base)) + ", tag:<tag>[,<tag>]"
        raise ValueError(
            f"unsupported recommendation mode {requested_mode!r}; supported modes: {supported}"
        ) from exc
```

- [x] **Step 4: Run mode tests to verify they pass**

Run:

```bash
python -m pytest tests/test_modes.py -v
```

Expected:

```text
5 passed
```

- [x] **Step 5: Commit mode settings**

Run:

```bash
git add src/scrobble_recs/models/modes.py tests/test_modes.py docs/superpowers/plans/2026-06-04-recommender-quality-pass.md
git commit -m "feat: add recommendation mode settings"
```

---

### Task 2: Multi-Region Seed Selection

**Files:**
- Modify: `src/scrobble_recs/models/profile.py`
- Modify: `tests/test_profile.py`

- [ ] **Step 1: Add failing profile-region tests**

Append these tests to `tests/test_profile.py`:

```python
from scrobble_recs.models.modes import resolve_recommendation_mode


def test_select_seed_artists_combines_profile_regions(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))
    mode = resolve_recommendation_mode("balanced", config)
    conn = initialize_database(config)
    _insert_profile_artist(
        conn,
        artist_id=10,
        name="Lifetime Anchor",
        play_count=90,
        lifetime_weight=9.0,
        recent_weight=2.0,
        knownness=0.95,
        rediscovery_score=0.0,
        last_played_at="2025-01-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=11,
        name="Recent Anchor",
        play_count=12,
        lifetime_weight=3.0,
        recent_weight=8.0,
        knownness=0.50,
        rediscovery_score=0.0,
        last_played_at="2026-05-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=12,
        name="Old Obsession",
        play_count=80,
        lifetime_weight=8.0,
        recent_weight=1.0,
        knownness=0.35,
        rediscovery_score=0.60,
        last_played_at="2019-01-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=13,
        name="Underexplored Spark",
        play_count=5,
        lifetime_weight=4.0,
        recent_weight=3.5,
        knownness=0.10,
        rediscovery_score=0.30,
        last_played_at="2024-06-01",
    )

    seeds = select_seed_artists(
        conn,
        limit=4,
        min_scrobbles=3,
        regions=mode.seed_regions,
    )

    assert [seed.name for seed in seeds] == [
        "Old Obsession",
        "Lifetime Anchor",
        "Recent Anchor",
        "Underexplored Spark",
    ]
    assert seeds[0].regions == ("lifetime", "old_obsession")
    assert seeds[1].regions == ("lifetime",)
    assert seeds[2].regions == ("recent",)
    assert seeds[3].regions == ("underexplored",)
    assert seeds[0].seed_weight > seeds[1].seed_weight
    conn.close()


def test_select_seed_artists_supports_recent_mode_region_bias(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))
    mode = resolve_recommendation_mode("recent", config)
    conn = initialize_database(config)
    _insert_profile_artist(
        conn,
        artist_id=20,
        name="Huge Old Artist",
        play_count=200,
        lifetime_weight=10.0,
        recent_weight=1.0,
        knownness=0.90,
        rediscovery_score=0.0,
        last_played_at="2018-01-01",
    )
    _insert_profile_artist(
        conn,
        artist_id=21,
        name="Smaller Recent Artist",
        play_count=20,
        lifetime_weight=4.0,
        recent_weight=8.0,
        knownness=0.60,
        rediscovery_score=0.0,
        last_played_at="2026-05-01",
    )

    seeds = select_seed_artists(
        conn,
        limit=2,
        min_scrobbles=3,
        regions=mode.seed_regions,
    )

    assert [seed.name for seed in seeds] == ["Smaller Recent Artist", "Huge Old Artist"]
    assert seeds[0].regions == ("recent",)
    conn.close()


def _insert_profile_artist(
    conn,
    *,
    artist_id: int,
    name: str,
    play_count: int,
    lifetime_weight: float,
    recent_weight: float,
    knownness: float,
    rediscovery_score: float,
    last_played_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO artist (
            artist_id,
            canonical_name,
            artist_key,
            lastfm_name
        )
        VALUES (?, ?, ?, ?)
        """,
        [artist_id, name, name.casefold().replace(" ", "-"), name],
    )
    conn.execute(
        """
        INSERT INTO artist_listening_fact (
            artist_id,
            play_count,
            track_count,
            active_months,
            first_played_at,
            last_played_at,
            lifetime_weight,
            recent_weight,
            knownness,
            rediscovery_score
        )
        VALUES (?, ?, 5, 5, '2018-01-01', ?, ?, ?, ?, ?)
        """,
        [
            artist_id,
            play_count,
            last_played_at,
            lifetime_weight,
            recent_weight,
            knownness,
            rediscovery_score,
        ],
    )
```

- [ ] **Step 2: Run profile tests to verify they fail**

Run:

```bash
python -m pytest tests/test_profile.py -v
```

Expected:

```text
TypeError: select_seed_artists() got an unexpected keyword argument 'regions'
```

- [ ] **Step 3: Update seed model and selection algorithm**

Modify `src/scrobble_recs/models/profile.py` to:

```python
from __future__ import annotations

from dataclasses import dataclass
from math import log1p
from typing import Sequence

from duckdb import DuckDBPyConnection

from scrobble_recs.models.modes import SeedRegionSetting


@dataclass(frozen=True)
class SeedArtist:
    artist_id: int
    name: str
    artist_key: str
    lastfm_name: str
    play_count: int
    seed_weight: float
    regions: tuple[str, ...] = ()


@dataclass
class _SeedAccumulator:
    artist_id: int
    name: str
    artist_key: str
    lastfm_name: str
    play_count: int
    seed_weight: float
    regions: set[str]


def select_seed_artists(
    conn: DuckDBPyConnection,
    *,
    limit: int,
    min_scrobbles: int,
    regions: Sequence[SeedRegionSetting] | None = None,
) -> list[SeedArtist]:
    if limit < 1:
        return []
    resolved_regions: Sequence[SeedRegionSetting] = regions or (("recent", limit, 1.0),)
    rows = _load_seed_rows(conn, min_scrobbles=min_scrobbles)
    selected: dict[int, _SeedAccumulator] = {}
    for region_name, region_limit, multiplier in resolved_regions:
        ranked = sorted(
            rows,
            key=lambda row: (-_region_score(row, region_name), row["name"]),
        )
        taken = 0
        for row in ranked:
            region_score = _region_score(row, region_name)
            if region_score <= 0.0:
                continue
            accumulator = selected.get(int(row["artist_id"]))
            weighted_score = region_score * multiplier
            if accumulator is None:
                selected[int(row["artist_id"])] = _SeedAccumulator(
                    artist_id=int(row["artist_id"]),
                    name=str(row["name"]),
                    artist_key=str(row["artist_key"]),
                    lastfm_name=str(row["lastfm_name"]),
                    play_count=int(row["play_count"]),
                    seed_weight=weighted_score,
                    regions={region_name},
                )
            else:
                accumulator.seed_weight += weighted_score
                accumulator.regions.add(region_name)
            taken += 1
            if taken >= region_limit:
                break

    ranked_seeds = sorted(
        selected.values(),
        key=lambda item: (-item.seed_weight, item.name),
    )[:limit]
    return [
        SeedArtist(
            artist_id=item.artist_id,
            name=item.name,
            artist_key=item.artist_key,
            lastfm_name=item.lastfm_name,
            play_count=item.play_count,
            seed_weight=item.seed_weight,
            regions=tuple(sorted(item.regions)),
        )
        for item in ranked_seeds
    ]


def _load_seed_rows(conn: DuckDBPyConnection, *, min_scrobbles: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            a.artist_id,
            a.canonical_name,
            a.artist_key,
            coalesce(nullif(a.lastfm_name, ''), a.canonical_name) AS lastfm_name,
            f.play_count,
            coalesce(f.lifetime_weight, ln(1 + f.play_count)) AS lifetime_weight,
            coalesce(f.recent_weight, f.lifetime_weight, ln(1 + f.play_count)) AS recent_weight,
            coalesce(f.knownness, 0.0) AS knownness,
            coalesce(f.rediscovery_score, 0.0) AS rediscovery_score,
            f.last_played_at
        FROM artist a
        JOIN artist_listening_fact f ON f.artist_id = a.artist_id
        WHERE f.play_count >= ?
        """,
        [min_scrobbles],
    ).fetchall()
    return [
        {
            "artist_id": int(row[0]),
            "name": str(row[1]),
            "artist_key": str(row[2]),
            "lastfm_name": str(row[3]),
            "play_count": int(row[4]),
            "lifetime_weight": float(row[5]),
            "recent_weight": float(row[6]),
            "knownness": float(row[7]),
            "rediscovery_score": float(row[8]),
        }
        for row in rows
    ]


def _region_score(row: dict[str, object], region_name: str) -> float:
    lifetime_weight = float(row["lifetime_weight"])
    recent_weight = float(row["recent_weight"])
    knownness = float(row["knownness"])
    rediscovery_score = float(row["rediscovery_score"])
    play_count = int(row["play_count"])
    if region_name == "lifetime":
        return lifetime_weight
    if region_name == "recent":
        return recent_weight
    if region_name == "old_obsession":
        return lifetime_weight * rediscovery_score
    if region_name == "underexplored":
        if play_count > 30:
            return 0.0
        return lifetime_weight * max(0.0, 1.0 - knownness)
    return log1p(play_count)
```

- [ ] **Step 4: Run profile tests to verify they pass**

Run:

```bash
python -m pytest tests/test_profile.py -v
```

Expected:

```text
5 passed
```

- [ ] **Step 5: Run recommendation tests to catch integration failures**

Run:

```bash
python -m pytest tests/test_recommend.py -v
```

Expected:

```text
all existing recommendation tests pass
```

If this fails because `generate_recommendations()` has not passed `regions` yet, keep the fallback behavior in `select_seed_artists()` as shown in Step 3 and defer the recommender integration to Task 3.

- [ ] **Step 6: Commit multi-region seed selection**

Run:

```bash
git add src/scrobble_recs/models/profile.py tests/test_profile.py docs/superpowers/plans/2026-06-04-recommender-quality-pass.md
git commit -m "feat: select seeds from profile regions"
```

---

### Task 3: Mode-Aware Scoring And Obviousness Hard Hide

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`
- Modify: `tests/test_recommend.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing hard-hide and mode tests**

Append these tests to `tests/test_recommend.py`:

```python
def test_generate_recommendations_hard_hides_obvious_high_affinity_artist(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=100,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=100, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=100,
        candidate_id=101,
        candidate_name="Obvious Rank Two",
        tag="post-punk",
        tag_rank=2,
        similarity=0.95,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=100,
        candidate_id=102,
        candidate_name="Less Obvious Rank Twenty",
        tag="post-punk",
        tag_rank=20,
        similarity=0.75,
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    names = _recommendation_names(conn, stats.run_id)
    assert names == ["Less Obvious Rank Twenty"]
    conn.close()


def test_generate_recommendations_keeps_obvious_rediscovery_candidate(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=110,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=110, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=110,
        candidate_id=111,
        candidate_name="Dormant Rank Two",
        tag="post-punk",
        tag_rank=2,
        similarity=0.95,
    )
    conn.execute(
        """
        INSERT INTO artist_listening_fact (
            artist_id,
            play_count,
            track_count,
            active_months,
            first_played_at,
            last_played_at
        )
        VALUES (111, 5, 3, 2, '2018-01-01', '2019-01-01')
        """
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == ["Dormant Rank Two"]
    (explanation_json,) = conn.execute(
        "SELECT explanation FROM recommendation WHERE run_id = ? AND rank = 1",
        [stats.run_id],
    ).fetchone()
    explanation = json.loads(explanation_json)
    assert explanation["classification"]["is_obvious"] is True
    assert explanation["classification"]["is_rediscovery"] is True
    conn.close()


def test_anti_obvious_mode_uses_stricter_top_tag_hide(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=120,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=120, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=120,
        candidate_id=121,
        candidate_name="Rank Eight Mainstream",
        tag="post-punk",
        tag_rank=8,
        similarity=0.95,
    )

    balanced = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )
    anti_obvious = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="anti-obvious",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, balanced.run_id) == ["Rank Eight Mainstream"]
    assert _recommendation_names(conn, anti_obvious.run_id) == []
    conn.close()


def test_tag_focus_mode_boosts_matching_candidate_tags(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=130,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=130, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=130,
        candidate_id=131,
        candidate_name="Lower Similarity Gothic Fit",
        tag="gothic rock",
        tag_rank=25,
        similarity=0.55,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=130,
        candidate_id=132,
        candidate_name="Higher Similarity Punk Fit",
        tag="punk",
        tag_rank=25,
        similarity=0.70,
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="tag:gothic rock",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == [
        "Lower Similarity Gothic Fit",
        "Higher Similarity Punk Fit",
    ]
    conn.close()


def _insert_scoring_artist(
    conn,
    *,
    artist_id: int,
    name: str,
    play_count: int,
    lifetime_weight: float,
    recent_weight: float,
    knownness: float,
    rediscovery_score: float,
) -> None:
    conn.execute(
        """
        INSERT INTO artist (
            artist_id,
            canonical_name,
            artist_key,
            lastfm_name
        )
        VALUES (?, ?, ?, ?)
        """,
        [artist_id, name, name.casefold().replace(" ", "-"), name],
    )
    conn.execute(
        """
        INSERT INTO artist_listening_fact (
            artist_id,
            play_count,
            track_count,
            active_months,
            first_played_at,
            last_played_at,
            lifetime_weight,
            recent_weight,
            knownness,
            rediscovery_score
        )
        VALUES (?, ?, 10, 10, '2024-01-01', '2026-05-01', ?, ?, ?, ?)
        """,
        [
            artist_id,
            play_count,
            lifetime_weight,
            recent_weight,
            knownness,
            rediscovery_score,
        ],
    )


def _insert_artist_tag(conn, *, artist_id: int, tag: str, weight: float) -> None:
    conn.execute(
        """
        INSERT INTO artist_tag (
            artist_id,
            tag,
            source,
            tag_count,
            normalized_weight,
            fetched_at
        )
        VALUES (?, ?, 'lastfm', ?, ?, current_timestamp)
        """,
        [artist_id, tag, weight, weight / 100.0],
    )


def _insert_candidate_from_seed(
    conn,
    *,
    seed_id: int,
    candidate_id: int,
    candidate_name: str,
    tag: str,
    tag_rank: int,
    similarity: float,
) -> None:
    conn.execute(
        """
        INSERT INTO artist (
            artist_id,
            canonical_name,
            artist_key,
            lastfm_name
        )
        VALUES (?, ?, ?, ?)
        """,
        [candidate_id, candidate_name, candidate_name.casefold().replace(" ", "-"), candidate_name],
    )
    conn.execute(
        """
        INSERT INTO artist_similarity_edge (
            source_artist_id,
            target_artist_id,
            source,
            source_score,
            normalized_score,
            evidence,
            fetched_at
        )
        VALUES (?, ?, 'lastfm', ?, ?, '{}', current_timestamp)
        """,
        [seed_id, candidate_id, similarity, similarity],
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
        VALUES (?, ?, 'lastfm', ?, 1.0, current_timestamp)
        """,
        [tag, candidate_id, tag_rank],
    )


def _recommendation_names(conn, run_id: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """
            SELECT a.canonical_name
            FROM recommendation r
            JOIN artist a ON a.artist_id = r.artist_id
            WHERE r.run_id = ?
            ORDER BY r.rank
            """,
            [run_id],
        ).fetchall()
    ]
```

Append this CLI validation test to `tests/test_cli.py`:

```python
def test_recommend_command_prints_invalid_mode_error(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)

    def fake_generate(conn, config, *, limit, mode=None):
        raise ValueError("unsupported recommendation mode 'fresh'")

    monkeypatch.setattr("scrobble_recs.cli.generate_recommendations", fake_generate)

    result = runner.invoke(
        app,
        ["recommend", "--config", str(config_path), "--limit", "5", "--mode", "fresh"],
    )

    assert result.exit_code == 1
    assert "Error: unsupported recommendation mode 'fresh'" in result.output
```

- [ ] **Step 2: Run focused tests to verify they fail**

Run:

```bash
python -m pytest tests/test_recommend.py::test_generate_recommendations_hard_hides_obvious_high_affinity_artist tests/test_recommend.py::test_generate_recommendations_keeps_obvious_rediscovery_candidate tests/test_recommend.py::test_anti_obvious_mode_uses_stricter_top_tag_hide tests/test_recommend.py::test_tag_focus_mode_boosts_matching_candidate_tags tests/test_cli.py::test_recommend_command_prints_invalid_mode_error -v
```

Expected:

```text
at least one recommendation behavior test fails because mode settings and hard-hide behavior are not wired into generate_recommendations()
```

- [ ] **Step 3: Wire mode settings into recommendation generation**

Modify imports in `src/scrobble_recs/models/recommend.py`:

```python
from scrobble_recs.models.modes import RecommendationModeSettings, resolve_recommendation_mode
```

Modify `_CandidateScore`:

```python
@dataclass(frozen=True)
class _CandidateScore:
    artist_id: int
    artist_name: str
    final_score: float
    raw_score: float
    lastfm_similarity_score: float
    tag_similarity_score: float
    rediscovery_score: float
    knownness: float
    obviousness: float
    knownness_penalty: float
    obviousness_penalty: float
    diversity_penalty: float
    primary_tag: str | None
    explanation: dict[str, object]
```

Modify the start of `generate_recommendations()`:

```python
    mode_settings = resolve_recommendation_mode(mode, config)
    resolved_mode = mode_settings.name
    resolved_as_of = as_of or datetime.now(tz=UTC).replace(tzinfo=None)
    _refresh_artist_listening_scores(conn, resolved_as_of)
    seeds = select_seed_artists(
        conn,
        limit=config.recommendation.seed_artist_limit,
        min_scrobbles=config.recommendation.min_seed_scrobbles,
        regions=mode_settings.seed_regions,
    )
```

Modify the scoring call:

```python
    scores = _score_candidates(config, mode_settings, seeds, seed_ids, tag_profile, scoring_data)
```

Replace `_score_candidates()` signature and scoring body with:

```python
def _score_candidates(
    config: ScrobbleRecsConfig,
    mode: RecommendationModeSettings,
    seeds: list[SeedArtist],
    seed_ids: set[int],
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
) -> list[_CandidateScore]:
    seed_weight_by_id = {seed.artist_id: seed.seed_weight for seed in seeds}
    total_seed_weight = sum(seed_weight_by_id.values()) or 1.0
    scores: list[_CandidateScore] = []
    focused_tag_profile = _apply_tag_focus(tag_profile, mode)
    for artist_id in scoring_data.candidates:
        if artist_id in seed_ids:
            continue
        artist_name = scoring_data.artist_names.get(artist_id)
        if artist_name is None:
            continue
        lastfm_score = _lastfm_similarity_score(
            artist_id,
            seed_weight_by_id,
            total_seed_weight,
            scoring_data.similarity_by_target,
        )
        tag_score = _tag_similarity_score(artist_id, focused_tag_profile, scoring_data)
        knownness, rediscovery = scoring_data.listening_by_artist.get(artist_id, (0.0, 0.0))
        obviousness = _obviousness(artist_id, focused_tag_profile, scoring_data)
        if _hard_hides_obvious_candidate(
            artist_id,
            mode,
            focused_tag_profile,
            scoring_data,
            rediscovery_score=rediscovery,
        ):
            continue
        weights = config.recommendation.weights
        raw_score = _raw_score(
            weights,
            lastfm_score * mode.lastfm_similarity_multiplier,
            tag_score * mode.tag_similarity_multiplier,
            rediscovery * mode.rediscovery_multiplier,
        )
        knownness_penalty = 0.40 * max(0.0, knownness - mode.max_knownness)
        obviousness_penalty = mode.obviousness_penalty_weight * obviousness
        final_score = raw_score - knownness_penalty - obviousness_penalty
        if final_score <= 0:
            continue
        scores.append(
            _CandidateScore(
                artist_id=artist_id,
                artist_name=artist_name,
                final_score=final_score,
                raw_score=raw_score,
                lastfm_similarity_score=lastfm_score,
                tag_similarity_score=tag_score,
                rediscovery_score=rediscovery,
                knownness=knownness,
                obviousness=obviousness,
                knownness_penalty=knownness_penalty,
                obviousness_penalty=obviousness_penalty,
                diversity_penalty=0.0,
                primary_tag=_primary_tag(artist_id, focused_tag_profile, scoring_data),
                explanation={},
            )
        )
    return scores
```

Add helper functions:

```python
def _apply_tag_focus(
    tag_profile: dict[str, float],
    mode: RecommendationModeSettings,
) -> dict[str, float]:
    if not mode.focus_tags:
        return tag_profile
    focused = dict(tag_profile)
    for tag in mode.focus_tags:
        focused[tag] = max(focused.get(tag, 0.0), 1.0) * mode.tag_focus_boost
    max_weight = max(focused.values(), default=0.0)
    if max_weight <= 0.0:
        return {}
    return {tag: weight / max_weight for tag, weight in focused.items()}


def _hard_hides_obvious_candidate(
    artist_id: int,
    mode: RecommendationModeSettings,
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
    *,
    rediscovery_score: float,
) -> bool:
    if rediscovery_score > 0.0:
        return False
    for tag, rank in scoring_data.tag_ranks_by_artist.get(artist_id, []):
        if rank <= mode.hard_hide_top_tag_rank and tag_profile.get(tag, 0.0) >= mode.high_affinity_tag_weight:
            return True
    return False


def _primary_tag(
    artist_id: int,
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
) -> str | None:
    direct_tags = scoring_data.direct_tags_by_artist.get(artist_id, {})
    if direct_tags:
        return sorted(direct_tags.items(), key=lambda row: (-row[1], row[0]))[0][0]
    ranked_tags = scoring_data.tag_ranks_by_artist.get(artist_id, [])
    if not ranked_tags:
        return None
    return sorted(ranked_tags, key=lambda row: (-tag_profile.get(row[0], 0.0), row[1], row[0]))[0][0]
```

Modify `_persist_run()` row values to store `score.diversity_penalty` instead of `0.0`:

```python
                    score.diversity_penalty,
```

Modify `_explanation()` penalties:

```python
        "penalties": {
            "knownness": score.knownness_penalty,
            "obviousness": score.obviousness_penalty,
            "diversity": score.diversity_penalty,
        },
```

Modify the `_persist_run()` call to include mode settings:

```python
    _persist_run(conn, run_id, resolved_mode, config, mode_settings, seeds, ranked)
```

Modify `_persist_run()` signature and metadata payload:

```python
def _persist_run(
    conn: DuckDBPyConnection,
    run_id: str,
    mode: str,
    config: ScrobbleRecsConfig,
    mode_settings: RecommendationModeSettings,
    seeds: list[SeedArtist],
    ranked: list[_CandidateScore],
) -> None:
```

Use this seed summary JSON:

```python
                json.dumps(
                    {
                        "seed_count": len(seeds),
                        "regions": {
                            region: sum(1 for seed in seeds if region in seed.regions)
                            for region, _limit, _multiplier in mode_settings.seed_regions
                        },
                        "top_seeds": [
                            {
                                "artist": seed.name,
                                "seed_weight": seed.seed_weight,
                                "regions": list(seed.regions),
                            }
                            for seed in seeds[:10]
                        ],
                    }
                ),
```

Use this scoring weights JSON:

```python
                json.dumps(
                    {
                        "base": config.recommendation.weights.model_dump(mode="json"),
                        "mode": {
                            "name": mode_settings.name,
                            "diversity_lambda": mode_settings.diversity_lambda,
                            "max_knownness": mode_settings.max_knownness,
                            "obviousness_penalty_weight": mode_settings.obviousness_penalty_weight,
                            "hard_hide_top_tag_rank": mode_settings.hard_hide_top_tag_rank,
                            "same_primary_tag_cap": mode_settings.same_primary_tag_cap,
                            "focus_tags": list(mode_settings.focus_tags),
                        },
                    },
                    sort_keys=True,
                ),
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run:

```bash
python -m pytest tests/test_recommend.py::test_generate_recommendations_hard_hides_obvious_high_affinity_artist tests/test_recommend.py::test_generate_recommendations_keeps_obvious_rediscovery_candidate tests/test_recommend.py::test_anti_obvious_mode_uses_stricter_top_tag_hide tests/test_recommend.py::test_tag_focus_mode_boosts_matching_candidate_tags tests/test_cli.py::test_recommend_command_prints_invalid_mode_error -v
```

Expected:

```text
5 passed
```

- [ ] **Step 5: Run existing recommendation tests**

Run:

```bash
python -m pytest tests/test_recommend.py tests/test_cli.py -v
```

Expected:

```text
all selected tests pass
```

- [ ] **Step 6: Commit mode-aware scoring**

Run:

```bash
git add src/scrobble_recs/models/recommend.py tests/test_recommend.py tests/test_cli.py docs/superpowers/plans/2026-06-04-recommender-quality-pass.md
git commit -m "feat: apply modes and obviousness hiding"
```

---

### Task 4: Diversity Reranking

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`
- Modify: `tests/test_recommend.py`

- [ ] **Step 1: Add failing diversity tests**

Append these tests to `tests/test_recommend.py`:

```python
def test_generate_recommendations_caps_same_primary_tag_in_top_twenty(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    config.recommendation.diversity.max_same_primary_tag_in_top_20 = 2
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=200,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=200, tag="post-punk", weight=100.0)
    for offset, similarity in enumerate([0.95, 0.90, 0.85, 0.80], start=1):
        _insert_candidate_from_seed(
            conn,
            seed_id=200,
            candidate_id=200 + offset,
            candidate_name=f"Post Punk Candidate {offset}",
            tag="post-punk",
            tag_rank=25 + offset,
            similarity=similarity,
        )
    _insert_candidate_from_seed(
        conn,
        seed_id=200,
        candidate_id=210,
        candidate_name="Dream Pop Candidate",
        tag="dream pop",
        tag_rank=30,
        similarity=0.70,
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    top_names = _recommendation_names(conn, stats.run_id)
    assert top_names[:3] == [
        "Post Punk Candidate 1",
        "Post Punk Candidate 2",
        "Dream Pop Candidate",
    ]
    assert sum(1 for name in top_names[:5] if name.startswith("Post Punk")) == 2
    conn.close()


def test_generate_recommendations_persists_diversity_penalty_for_similar_followups(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=220,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=220, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=220,
        candidate_id=221,
        candidate_name="First Similar Candidate",
        tag="post-punk",
        tag_rank=30,
        similarity=0.95,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=220,
        candidate_id=222,
        candidate_name="Second Similar Candidate",
        tag="post-punk",
        tag_rank=31,
        similarity=0.90,
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    rows = conn.execute(
        """
        SELECT a.canonical_name, r.diversity_penalty, r.explanation
        FROM recommendation r
        JOIN artist a ON a.artist_id = r.artist_id
        WHERE r.run_id = ?
        ORDER BY r.rank
        """,
        [stats.run_id],
    ).fetchall()
    assert rows[0][0] == "First Similar Candidate"
    assert rows[0][1] == pytest.approx(0.0)
    assert rows[1][0] == "Second Similar Candidate"
    assert rows[1][1] > 0.0
    explanation = json.loads(rows[1][2])
    assert explanation["penalties"]["diversity"] == pytest.approx(rows[1][1])
    conn.close()
```

- [ ] **Step 2: Run diversity tests to verify they fail**

Run:

```bash
python -m pytest tests/test_recommend.py::test_generate_recommendations_caps_same_primary_tag_in_top_twenty tests/test_recommend.py::test_generate_recommendations_persists_diversity_penalty_for_similar_followups -v
```

Expected:

```text
at least one test fails because diversity reranking currently slices by final_score only
```

- [ ] **Step 3: Implement diversity reranking**

Modify `generate_recommendations()` after scoring:

```python
    ranked_without_explanations = _rerank_for_diversity(
        scores,
        mode_settings,
        scoring_data,
        limit=limit,
        enabled=config.recommendation.diversity.enabled,
    )
```

Add helper functions to `src/scrobble_recs/models/recommend.py`:

```python
def _rerank_for_diversity(
    scores: list[_CandidateScore],
    mode: RecommendationModeSettings,
    scoring_data: _ScoringData,
    *,
    limit: int,
    enabled: bool,
) -> list[_CandidateScore]:
    remaining = sorted(scores, key=lambda item: (-item.final_score, item.artist_name.casefold()))
    if not enabled:
        return remaining[:limit]

    selected: list[_CandidateScore] = []
    primary_tag_counts: dict[str, int] = {}
    while remaining and len(selected) < limit:
        best_index: int | None = None
        best_score: float | None = None
        best_penalty = 0.0
        for index, candidate in enumerate(remaining):
            if _would_break_primary_tag_cap(candidate, selected, primary_tag_counts, mode):
                continue
            similarity = max(
                (
                    _candidate_similarity(candidate, already_selected, scoring_data)
                    for already_selected in selected
                ),
                default=0.0,
            )
            diversity_penalty = (1.0 - mode.diversity_lambda) * similarity
            rerank_score = mode.diversity_lambda * candidate.final_score - diversity_penalty
            if best_score is None or rerank_score > best_score or (
                rerank_score == best_score and candidate.artist_name.casefold() < remaining[best_index].artist_name.casefold()
            ):
                best_index = index
                best_score = rerank_score
                best_penalty = diversity_penalty
        if best_index is None:
            break
        chosen = remaining.pop(best_index)
        chosen = replace(
            chosen,
            final_score=chosen.final_score - best_penalty,
            diversity_penalty=best_penalty,
        )
        selected.append(chosen)
        if chosen.primary_tag is not None and len(selected) <= 20:
            primary_tag_counts[chosen.primary_tag] = primary_tag_counts.get(chosen.primary_tag, 0) + 1
    return selected


def _would_break_primary_tag_cap(
    candidate: _CandidateScore,
    selected: list[_CandidateScore],
    primary_tag_counts: dict[str, int],
    mode: RecommendationModeSettings,
) -> bool:
    if candidate.primary_tag is None or len(selected) >= 20:
        return False
    return primary_tag_counts.get(candidate.primary_tag, 0) >= mode.same_primary_tag_cap


def _candidate_similarity(
    left: _CandidateScore,
    right: _CandidateScore,
    scoring_data: _ScoringData,
) -> float:
    left_tags = _candidate_tag_vector(left.artist_id, scoring_data)
    right_tags = _candidate_tag_vector(right.artist_id, scoring_data)
    if not left_tags or not right_tags:
        return 0.75 if left.primary_tag is not None and left.primary_tag == right.primary_tag else 0.0
    shared_tags = set(left_tags).intersection(right_tags)
    dot = sum(left_tags[tag] * right_tags[tag] for tag in shared_tags)
    left_norm = sqrt(sum(weight * weight for weight in left_tags.values()))
    right_norm = sqrt(sum(weight * weight for weight in right_tags.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    cosine = dot / (left_norm * right_norm)
    if left.primary_tag is not None and left.primary_tag == right.primary_tag:
        return max(cosine, 0.75)
    return cosine


def _candidate_tag_vector(
    artist_id: int,
    scoring_data: _ScoringData,
) -> dict[str, float]:
    direct_tags = scoring_data.direct_tags_by_artist.get(artist_id, {})
    ranked_tags = {
        tag: _rank_affinity(rank)
        for tag, rank in scoring_data.tag_ranks_by_artist.get(artist_id, [])
    }
    return {**ranked_tags, **direct_tags}
```

- [ ] **Step 4: Run diversity tests to verify they pass**

Run:

```bash
python -m pytest tests/test_recommend.py::test_generate_recommendations_caps_same_primary_tag_in_top_twenty tests/test_recommend.py::test_generate_recommendations_persists_diversity_penalty_for_similar_followups -v
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Run all recommendation tests**

Run:

```bash
python -m pytest tests/test_recommend.py -v
```

Expected:

```text
all recommendation tests pass
```

- [ ] **Step 6: Commit diversity reranking**

Run:

```bash
git add src/scrobble_recs/models/recommend.py tests/test_recommend.py docs/superpowers/plans/2026-06-04-recommender-quality-pass.md
git commit -m "feat: diversify recommendation ranking"
```

---

### Task 5: Docs, Example Config, And Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/implementation-log.md`
- Modify: `config.example.json` only if Task 1 changes default config fields.

- [ ] **Step 1: Run focused quality-pass tests**

Run:

```bash
python -m pytest tests/test_modes.py tests/test_profile.py tests/test_recommend.py tests/test_cli.py -v
```

Expected:

```text
all selected tests pass
```

- [ ] **Step 2: Run full pytest suite**

Run:

```bash
python -m pytest
```

Expected:

```text
all tests pass
```

- [ ] **Step 3: Run ruff check**

Run:

```bash
python -m ruff check .
```

Expected:

```text
All checks passed!
```

- [ ] **Step 4: Run ruff format check**

Run:

```bash
python -m ruff format --check .
```

Expected:

```text
all files already formatted
```

- [ ] **Step 5: Run mypy**

Run:

```bash
python -m mypy src
```

Expected:

```text
Success: no issues found
```

- [ ] **Step 6: Update README**

Append this section to `README.md` after the Milestone 3 section:

```markdown
## Recommender Quality Pass

`recommend` supports mode-specific ranking from local data:

```bash
scrobble-recs recommend --config config.example.json --mode balanced --limit 500
scrobble-recs recommend --config config.example.json --mode explore --limit 500
scrobble-recs recommend --config config.example.json --mode safe --limit 500
scrobble-recs recommend --config config.example.json --mode rediscovery --limit 500
scrobble-recs recommend --config config.example.json --mode recent --limit 500
scrobble-recs recommend --config config.example.json --mode old --limit 500
scrobble-recs recommend --config config.example.json --mode anti-obvious --limit 500
scrobble-recs recommend --config config.example.json --mode "tag:post-punk,gothic rock" --limit 500
```

The recommender now selects seeds from multiple local taste regions, applies
mode-specific knownness and obviousness behavior, hides the most obvious
high-affinity top-tag artists unless they are rediscovery candidates, and
reranks the persisted list for diversity. It still reads only local DuckDB data
during `recommend`; run `enrich --source lastfm` first when the local graph needs
new Last.fm similarity or tag data.
```

- [ ] **Step 7: Update implementation log**

Append this entry to `docs/implementation-log.md`:

```markdown
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
```

- [ ] **Step 8: Commit docs and verification**

Run:

```bash
git add README.md docs/implementation-log.md docs/superpowers/plans/2026-06-04-recommender-quality-pass.md
git commit -m "docs: record recommender quality pass"
```

---

## Self Review

Spec coverage:

- Section 10 profile depth is covered by Task 2 through lifetime, recent, old-obsession, and underexplored seed regions.
- Section 11.1 seed artist selection is covered by Task 2 and integrated into generation in Task 3.
- Section 12 obviousness is covered by Task 3 with penalty weighting and default hard-hide for high-affinity top-tag ranks.
- Section 14 diversity reranking is covered by Task 4 with MMR and a top-20 primary-tag cap.
- Section 15 recommendation modes are covered by Task 1 and consumed by Task 3.
- Section 23 sensible defaults are preserved through existing config defaults and resolved mode settings in Task 1.
- Section 24 algorithm step 11 is covered by Task 4 before recommendation rows are persisted.

Design consistency:

- `RecommendationModeSettings` is defined before profile and recommendation modules consume it.
- `select_seed_artists()` remains backward compatible because `regions` defaults to the existing recent-weight behavior.
- `generate_recommendations()` keeps the public signature used by CLI tests.
- `recommendation.diversity_penalty` already exists in the schema, so no migration is needed.
- The implementation stays local-first because all tests construct DuckDB rows and no API client is used during recommendation generation.

Testing quality:

- New tests assert ranking behavior, hidden candidates, rediscovery exceptions, tag-focus ordering, seed region composition, persisted diversity penalties, and CLI error propagation.
- Tests use deterministic DuckDB rows and temporary database files.
- No test depends on live API responses or network access.

Execution note:

- The worktree currently has unrelated user-owned changes in `.gitignore` and `artifacts/recenttracks-tallain-1780422643.csv`; do not revert or stage those files while executing this plan.
