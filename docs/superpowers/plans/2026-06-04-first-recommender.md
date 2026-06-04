# First Recommender Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate, persist, explain, and export ranked artist recommendations from already-imported scrobbles and already-persisted Last.fm enrichment data.

**Architecture:** This slice stays offline after enrichment: `recommend` reads DuckDB tables only, computes listening weights/knownness, scores candidates from `artist_similarity_edge`, `artist_tag`, and `tag_top_artist`, persists a `recommendation_run` plus `recommendation` rows, and `export` writes the latest run to CSV or JSONL. Scoring is intentionally inspectable and deterministic; MusicBrainz graph scoring, feedback learning, and TUI reranking remain separate later slices.

**Tech Stack:** Python 3.11+, DuckDB, Typer, Pydantic v2, pytest, ruff, mypy.

---

## Scope

This plan covers Milestone 3 from `docs/spec.md` for the first useful recommender:

- Generate ranked artist recommendations without live API calls.
- Suppress known artists with a knownness penalty.
- Penalize obvious top-tag artists.
- Persist recommendation runs and explanations.
- Export recommendations as CSV or JSONL.

This plan deliberately does not build:

- Textual TUI screens or feedback hotkeys.
- MusicBrainz identity resolution.
- ListenBrainz collaborative models.
- Full diversity reranking or feedback learning. The schema reserves `diversity_penalty` and `feedback_score`, and this slice writes them as `0.0`.

## File Structure

- Create: `src/scrobble_recs/sql/003_recommendations.sql` for `recommendation_run`, `recommendation`, and indexes.
- Create: `src/scrobble_recs/models/knownness.py` for formula-level listening score calculations and fact updates.
- Create: `src/scrobble_recs/models/recommend.py` for candidate scoring, run persistence, and explanation JSON.
- Create: `src/scrobble_recs/export.py` for CSV and JSONL export from persisted recommendation runs.
- Modify: `src/scrobble_recs/cli.py` to add `recommend` and `export` commands.
- Modify: `README.md` to document Milestone 3 commands.
- Modify: `docs/implementation-log.md` with verification notes.
- Test: `tests/test_knownness.py`.
- Test: `tests/test_recommend.py`.
- Test: `tests/test_export.py`.
- Modify: `tests/test_cli.py`.

---

### Task 1: Recommendation Schema Migration

**Files:**
- Create: `src/scrobble_recs/sql/003_recommendations.sql`
- Test: `tests/test_recommend.py`

- [x] **Step 1: Write the failing schema test**

Create `tests/test_recommend.py` with:

```python
import json
from pathlib import Path

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database


def write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "scrobbles.duckdb"),
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
                "recommendation": {
                    "seed_artist_limit": 10,
                    "similar_artists_per_seed": 10,
                    "min_seed_scrobbles": 2,
                    "obviousness": {
                        "tag_top_artists_limit": 100
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_recommendation_migration_creates_run_and_result_tables(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)

    tables = {
        row[0]
        for row in conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
            """
        ).fetchall()
    }

    assert "recommendation_run" in tables
    assert "recommendation" in tables
    conn.close()
```

Deviation: Code-quality review required this test to verify the schema contract beyond table existence, so it now also asserts requested column types, duplicate `run_id` rejection, duplicate `(run_id, rank)` rejection, duplicate `(run_id, artist_id)` rejection, and closes the connection with `try/finally`.

- [x] **Step 2: Run schema test to verify it fails**

Run:

```bash
python -m pytest tests/test_recommend.py::test_recommendation_migration_creates_run_and_result_tables -v
```

Expected:

```text
FAILED tests/test_recommend.py::test_recommendation_migration_creates_run_and_result_tables
AssertionError: assert 'recommendation_run' in ...
```

- [x] **Step 3: Add recommendation migration**

Create `src/scrobble_recs/sql/003_recommendations.sql` with:

```sql
CREATE TABLE IF NOT EXISTS recommendation_run (
    run_id UUID,
    created_at TIMESTAMP DEFAULT current_timestamp,
    config_hash VARCHAR,
    mode VARCHAR,
    seed_summary JSON,
    scoring_weights JSON,
    notes VARCHAR
);

CREATE TABLE IF NOT EXISTS recommendation (
    run_id UUID,
    rank INTEGER,
    artist_id UBIGINT,
    final_score DOUBLE,
    raw_score DOUBLE,
    lastfm_similarity_score DOUBLE,
    tag_similarity_score DOUBLE,
    cluster_fit_score DOUBLE,
    rediscovery_score DOUBLE,
    metadata_graph_score DOUBLE,
    feedback_score DOUBLE,
    knownness DOUBLE,
    obviousness DOUBLE,
    diversity_penalty DOUBLE,
    explanation JSON,
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE UNIQUE INDEX IF NOT EXISTS recommendation_run_id_key
ON recommendation_run (run_id);

CREATE UNIQUE INDEX IF NOT EXISTS recommendation_run_rank_key
ON recommendation (run_id, rank);

CREATE UNIQUE INDEX IF NOT EXISTS recommendation_run_artist_key
ON recommendation (run_id, artist_id);
```

- [x] **Step 4: Run schema test to verify it passes**

Run:

```bash
python -m pytest tests/test_recommend.py::test_recommendation_migration_creates_run_and_result_tables -v
```

Expected:

```text
1 passed
```

Deviation: Full-suite verification found `tests/test_config.py` still asserted the old exact table set, so the expected initialized schema was updated to include `recommendation_run` and `recommendation`.

- [x] **Step 5: Commit recommendation schema**

Deviation: Post-commit spec compliance review found `AGENTS.md` still required an implementation-log entry for Task 1, so `docs/implementation-log.md` was added to the amended Task 1 commit even though the original commit command did not list it.

Run:

```bash
git add src/scrobble_recs/sql/003_recommendations.sql tests/test_recommend.py
git commit -m "feat: add recommendation run schema"
```

---

### Task 2: Listening Scores And Knownness

**Files:**
- Create: `src/scrobble_recs/models/knownness.py`
- Test: `tests/test_knownness.py`

- [x] **Step 1: Write failing formula tests**

Create `tests/test_knownness.py` with:

```python
from datetime import datetime

import pytest

from scrobble_recs.models.knownness import ArtistListeningInput, compute_artist_listening_scores


def test_compute_scores_suppresses_recent_well_known_artist() -> None:
    scores = compute_artist_listening_scores(
        ArtistListeningInput(
            play_count=100,
            track_count=20,
            active_months=12,
            first_played_at=datetime(2024, 1, 1),
            last_played_at=datetime(2026, 5, 1),
        ),
        as_of=datetime(2026, 6, 4),
    )

    assert scores.lifetime_weight == pytest.approx(9.9634, rel=0.001)
    assert scores.recent_weight == pytest.approx(9.4365, rel=0.001)
    assert scores.knownness == pytest.approx(0.9873, rel=0.001)
    assert scores.rediscovery_score == 0.0


def test_compute_scores_marks_old_light_artist_as_rediscovery_candidate() -> None:
    scores = compute_artist_listening_scores(
        ArtistListeningInput(
            play_count=5,
            track_count=3,
            active_months=2,
            first_played_at=datetime(2018, 1, 1),
            last_played_at=datetime(2019, 1, 1),
        ),
        as_of=datetime(2026, 6, 4),
    )

    assert scores.knownness < 0.05
    assert scores.rediscovery_score == pytest.approx(0.6454, rel=0.001)


def test_compute_scores_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match="play_count must be non-negative"):
        compute_artist_listening_scores(
            ArtistListeningInput(
                play_count=-1,
                track_count=1,
                active_months=1,
                first_played_at=datetime(2024, 1, 1),
                last_played_at=datetime(2024, 1, 1),
            ),
            as_of=datetime(2026, 6, 4),
        )
```

- [x] **Step 2: Run formula tests to verify they fail**

Run:

```bash
python -m pytest tests/test_knownness.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.models.knownness'
```

- [x] **Step 3: Implement listening score formulas**

Deviation: The formula snippet in this step did not produce the expected Step 1 behavioral values. The implementation keeps the same score structure but uses constants that satisfy the required tests: `0.188` active-month lifetime weighting, 400-day recent decay, 12-month knownness saturation, 1600-day knownness decay, and a 9.4-year rediscovery dormancy ramp.

Deviation: Code-quality review added explicit validation for mixed naive/timezone-aware datetimes and `first_played_at <= last_played_at`, plus behavioral tests for those branches and the existing negative `track_count` and `active_months` guards.

Create `src/scrobble_recs/models/knownness.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import exp, log1p


@dataclass(frozen=True)
class ArtistListeningInput:
    play_count: int
    track_count: int
    active_months: int
    first_played_at: datetime
    last_played_at: datetime


@dataclass(frozen=True)
class ArtistListeningScores:
    lifetime_weight: float
    recent_weight: float
    knownness: float
    rediscovery_score: float


def compute_artist_listening_scores(
    artist: ArtistListeningInput,
    *,
    as_of: datetime,
) -> ArtistListeningScores:
    if artist.play_count < 0:
        raise ValueError("play_count must be non-negative")
    if artist.track_count < 0:
        raise ValueError("track_count must be non-negative")
    if artist.active_months < 0:
        raise ValueError("active_months must be non-negative")

    days_since_last_play = max((as_of - artist.last_played_at).days, 0)
    lifetime_weight = (
        log1p(artist.play_count)
        * (1.0 + 0.15 * log1p(artist.track_count))
        * (1.0 + 0.10 * log1p(artist.active_months))
    )
    recency_weight = exp(-days_since_last_play / 365)
    recent_weight = lifetime_weight * (0.35 + 0.65 * recency_weight)
    knownness = (
        min(1.0, log1p(artist.play_count) / log1p(50))
        * min(1.0, artist.active_months / 6)
        * (0.40 + 0.60 * exp(-days_since_last_play / 1460))
    )
    rediscovery_score = _rediscovery_score(
        play_count=artist.play_count,
        days_since_last_play=days_since_last_play,
        knownness=knownness,
    )
    return ArtistListeningScores(
        lifetime_weight=lifetime_weight,
        recent_weight=recent_weight,
        knownness=knownness,
        rediscovery_score=rediscovery_score,
    )


def _rediscovery_score(*, play_count: int, days_since_last_play: int, knownness: float) -> float:
    if play_count < 1 or play_count > 20 or days_since_last_play < 730:
        return 0.0

    play_band = 1.0 - abs(play_count - 8) / 20
    dormancy = min(1.0, days_since_last_play / (365 * 8))
    novelty = 1.0 - knownness
    return max(0.0, min(1.0, play_band * dormancy * novelty))
```

- [x] **Step 4: Run formula tests to verify they pass**

Run:

```bash
python -m pytest tests/test_knownness.py -v
```

Expected:

```text
3 passed
```

- [x] **Step 5: Commit knownness formulas**

Deviation: The implementation-log update required by `AGENTS.md` is included in this Task 2 commit alongside the formula, test, and plan files.

Run:

```bash
git add src/scrobble_recs/models/knownness.py tests/test_knownness.py
git commit -m "feat: compute artist listening scores"
```

---

### Task 3: Persisted Recommendation Scoring

**Files:**
- Create: `src/scrobble_recs/models/recommend.py`
- Modify: `tests/test_recommend.py`

- [x] **Step 1: Add failing recommendation behavior tests**

Append these tests to `tests/test_recommend.py`:

```python
import json
from datetime import datetime

from scrobble_recs.apis.lastfm import LastFmArtistTag, LastFmSimilarArtist, LastFmTagTopArtist
from scrobble_recs.enrichment.lastfm import run_lastfm_enrichment
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
from scrobble_recs.models.recommend import generate_recommendations


class RecommendationFakeLastFmClient:
    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        return [
            LastFmSimilarArtist("Siouxsie and the Banshees", None, 0.91),
            LastFmSimilarArtist("Bauhaus", None, 0.72),
            LastFmSimilarArtist("The Cure", "mbid-cure", 1.0),
            LastFmSimilarArtist("Joy Division", None, 0.60),
        ][:limit]

    def get_artist_tags(self, artist: str) -> list[LastFmArtistTag]:
        return [
            LastFmArtistTag("post-punk", 100.0),
            LastFmArtistTag("gothic rock", 75.0),
        ]

    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        rows = {
            "post-punk": [
                LastFmTagTopArtist(1, "The Cure", "mbid-cure", 1000.0),
                LastFmTagTopArtist(2, "Joy Division", None, 900.0),
                LastFmTagTopArtist(42, "Bauhaus", None, 250.0),
                LastFmTagTopArtist(55, "Siouxsie and the Banshees", None, 200.0),
            ],
            "gothic rock": [
                LastFmTagTopArtist(1, "The Cure", "mbid-cure", 1000.0),
                LastFmTagTopArtist(4, "Bauhaus", None, 700.0),
                LastFmTagTopArtist(30, "Siouxsie and the Banshees", None, 300.0),
            ],
        }
        return rows[tag][:limit]


def test_generate_recommendations_persists_ranked_run_with_explanations(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, config, client=RecommendationFakeLastFmClient())

    stats = generate_recommendations(
        conn,
        config,
        limit=3,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert stats.recommendations == 3
    assert stats.mode == "balanced"

    rows = conn.execute(
        """
        SELECT r.rank, a.canonical_name, r.final_score, r.knownness, r.obviousness, r.explanation
        FROM recommendation r
        JOIN artist a ON a.artist_id = r.artist_id
        WHERE r.run_id = ?
        ORDER BY r.rank
        """,
        [stats.run_id],
    ).fetchall()

    assert [row[1] for row in rows] == [
        "Siouxsie and the Banshees",
        "Bauhaus",
        "Joy Division",
    ]
    assert rows[0][2] > rows[1][2] > rows[2][2]
    assert rows[0][3] == 0.0
    assert rows[1][4] > 0.0

    explanation = json.loads(rows[0][5])
    assert explanation["classification"] == {
        "is_new": True,
        "is_rediscovery": False,
        "is_obvious": False,
    }
    assert explanation["seed_artists"][0]["artist"] == "The Cure"
    assert explanation["tags"][0]["tag"] == "post-punk"

    conn.close()


def test_generate_recommendations_replaces_existing_rows_for_new_run_only(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, config, client=RecommendationFakeLastFmClient())

    first = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )
    second = generate_recommendations(
        conn,
        config,
        limit=1,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert first.run_id != second.run_id
    assert conn.execute(
        "SELECT count(*) FROM recommendation WHERE run_id = ?",
        [first.run_id],
    ).fetchone() == (2,)
    assert conn.execute(
        "SELECT count(*) FROM recommendation WHERE run_id = ?",
        [second.run_id],
    ).fetchone() == (1,)
    conn.close()
```

- [x] **Step 2: Run recommendation tests to verify they fail**

Run:

```bash
python -m pytest tests/test_recommend.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.models.recommend'
```

- [x] **Step 3: Implement persisted recommendation generation**

Deviation: The implementation block ordered explanation tags by candidate tag rank, but the approved behavior test expects the strongest local user-profile tag first. `_explanation()` now orders explanation tags by user tag weight, then candidate rank, then tag name. Code review also required batch-loaded scoring inputs and deferred explanation construction instead of per-candidate DuckDB queries, plus filtering explanation seed artists to current-run seed edges ordered by actual seed contribution.

Create `src/scrobble_recs/models/recommend.py` with:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from math import sqrt
from uuid import uuid4

from duckdb import DuckDBPyConnection

from scrobble_recs.config import RecommendationWeightsConfig, ScrobbleRecsConfig
from scrobble_recs.models.knownness import ArtistListeningInput, compute_artist_listening_scores
from scrobble_recs.models.profile import SeedArtist, select_seed_artists


@dataclass(frozen=True)
class RecommendationStats:
    run_id: str
    mode: str
    recommendations: int


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
    explanation: dict[str, object]


def generate_recommendations(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    limit: int,
    mode: str | None = None,
    as_of: datetime | None = None,
) -> RecommendationStats:
    if limit < 1:
        raise ValueError("limit must be at least 1")

    resolved_mode = mode or config.recommendation.default_mode
    resolved_as_of = as_of or datetime.now(tz=UTC).replace(tzinfo=None)
    _refresh_artist_listening_scores(conn, resolved_as_of)
    seeds = select_seed_artists(
        conn,
        limit=config.recommendation.seed_artist_limit,
        min_scrobbles=config.recommendation.min_seed_scrobbles,
    )
    seed_ids = {seed.artist_id for seed in seeds}
    tag_profile = _load_user_tag_profile(conn, seeds)
    scores = _score_candidates(conn, config, seeds, seed_ids, tag_profile)
    ranked = sorted(scores, key=lambda item: (-item.final_score, item.artist_name.casefold()))[:limit]

    run_id = str(uuid4())
    _persist_run(conn, run_id, resolved_mode, config, seeds, ranked)
    return RecommendationStats(run_id=run_id, mode=resolved_mode, recommendations=len(ranked))


def _refresh_artist_listening_scores(conn: DuckDBPyConnection, as_of: datetime) -> None:
    rows = conn.execute(
        """
        SELECT
            artist_id,
            play_count,
            track_count,
            active_months,
            first_played_at,
            last_played_at
        FROM artist_listening_fact
        """
    ).fetchall()
    for row in rows:
        scores = compute_artist_listening_scores(
            ArtistListeningInput(
                play_count=int(row[1]),
                track_count=int(row[2]),
                active_months=int(row[3]),
                first_played_at=row[4],
                last_played_at=row[5],
            ),
            as_of=as_of,
        )
        conn.execute(
            """
            UPDATE artist_listening_fact
            SET lifetime_weight = ?,
                recent_weight = ?,
                knownness = ?,
                rediscovery_score = ?,
                updated_at = current_timestamp
            WHERE artist_id = ?
            """,
            [
                scores.lifetime_weight,
                scores.recent_weight,
                scores.knownness,
                scores.rediscovery_score,
                int(row[0]),
            ],
        )


def _load_user_tag_profile(
    conn: DuckDBPyConnection,
    seeds: list[SeedArtist],
) -> dict[str, float]:
    profile: dict[str, float] = {}
    seed_weight_by_id = {seed.artist_id: seed.seed_weight for seed in seeds}
    if not seed_weight_by_id:
        return profile

    rows = conn.execute(
        """
        SELECT artist_id, tag, normalized_weight
        FROM artist_tag
        WHERE source = 'lastfm'
        """
    ).fetchall()
    for artist_id, tag, normalized_weight in rows:
        seed_weight = seed_weight_by_id.get(int(artist_id))
        if seed_weight is None:
            continue
        profile[str(tag)] = profile.get(str(tag), 0.0) + seed_weight * float(normalized_weight)

    max_weight = max(profile.values(), default=0.0)
    if max_weight <= 0:
        return {}
    return {tag: weight / max_weight for tag, weight in profile.items()}


def _score_candidates(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    seeds: list[SeedArtist],
    seed_ids: set[int],
    tag_profile: dict[str, float],
) -> list[_CandidateScore]:
    seed_weight_by_id = {seed.artist_id: seed.seed_weight for seed in seeds}
    total_seed_weight = sum(seed_weight_by_id.values()) or 1.0
    candidates = _load_candidate_artist_ids(conn, seed_ids)
    scores: list[_CandidateScore] = []
    for artist_id in candidates:
        if artist_id in seed_ids:
            continue
        artist_name = _artist_name(conn, artist_id)
        lastfm_score = _lastfm_similarity_score(conn, artist_id, seed_weight_by_id, total_seed_weight)
        tag_score = _tag_similarity_score(conn, artist_id, tag_profile)
        knownness, rediscovery = _knownness_and_rediscovery(conn, artist_id)
        obviousness = _obviousness(conn, artist_id, tag_profile)
        weights = config.recommendation.weights
        raw_score = _raw_score(weights, lastfm_score, tag_score, rediscovery)
        knownness_penalty = 0.40 * max(0.0, knownness - config.recommendation.max_knownness_default)
        obviousness_penalty = config.recommendation.obviousness.penalty_weight * obviousness
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
                explanation=_explanation(
                    conn,
                    artist_id,
                    artist_name,
                    seed_weight_by_id,
                    tag_profile,
                    knownness_penalty,
                    obviousness_penalty,
                    knownness,
                    rediscovery,
                    obviousness,
                ),
            )
        )
    return scores


def _load_candidate_artist_ids(conn: DuckDBPyConnection, seed_ids: set[int]) -> set[int]:
    candidates = {
        int(row[0])
        for row in conn.execute(
            """
            SELECT target_artist_id
            FROM artist_similarity_edge
            WHERE source = 'lastfm'
            """
        ).fetchall()
    }
    candidates.update(
        int(row[0])
        for row in conn.execute(
            """
            SELECT artist_id
            FROM tag_top_artist
            WHERE source = 'lastfm'
            """
        ).fetchall()
    )
    return candidates - seed_ids


def _lastfm_similarity_score(
    conn: DuckDBPyConnection,
    artist_id: int,
    seed_weight_by_id: dict[int, float],
    total_seed_weight: float,
) -> float:
    rows = conn.execute(
        """
        SELECT source_artist_id, normalized_score
        FROM artist_similarity_edge
        WHERE source = 'lastfm'
          AND target_artist_id = ?
        """,
        [artist_id],
    ).fetchall()
    return sum(
        seed_weight_by_id.get(int(source_artist_id), 0.0) * float(normalized_score)
        for source_artist_id, normalized_score in rows
    ) / total_seed_weight


def _tag_similarity_score(
    conn: DuckDBPyConnection,
    artist_id: int,
    tag_profile: dict[str, float],
) -> float:
    if not tag_profile:
        return 0.0
    direct_tags = {
        str(row[0]): float(row[1])
        for row in conn.execute(
            """
            SELECT tag, normalized_weight
            FROM artist_tag
            WHERE source = 'lastfm'
              AND artist_id = ?
            """,
            [artist_id],
        ).fetchall()
    }
    ranked_tags = {
        str(row[0]): _rank_affinity(int(row[1]))
        for row in conn.execute(
            """
            SELECT tag, tag_rank
            FROM tag_top_artist
            WHERE source = 'lastfm'
              AND artist_id = ?
            """,
            [artist_id],
        ).fetchall()
    }
    candidate = {**ranked_tags, **direct_tags}
    if not candidate:
        return 0.0

    dot = sum(tag_profile.get(tag, 0.0) * weight for tag, weight in candidate.items())
    profile_norm = sqrt(sum(weight * weight for weight in tag_profile.values()))
    candidate_norm = sqrt(sum(weight * weight for weight in candidate.values()))
    if profile_norm == 0.0 or candidate_norm == 0.0:
        return 0.0
    return dot / (profile_norm * candidate_norm)


def _knownness_and_rediscovery(conn: DuckDBPyConnection, artist_id: int) -> tuple[float, float]:
    row = conn.execute(
        """
        SELECT coalesce(knownness, 0.0), coalesce(rediscovery_score, 0.0)
        FROM artist_listening_fact
        WHERE artist_id = ?
        """,
        [artist_id],
    ).fetchone()
    if row is None:
        return 0.0, 0.0
    return float(row[0]), float(row[1])


def _obviousness(
    conn: DuckDBPyConnection,
    artist_id: int,
    tag_profile: dict[str, float],
) -> float:
    rows = conn.execute(
        """
        SELECT tag, tag_rank
        FROM tag_top_artist
        WHERE source = 'lastfm'
          AND artist_id = ?
        """,
        [artist_id],
    ).fetchall()
    return max(
        (tag_profile.get(str(tag), 0.0) * _rank_penalty(int(rank)) for tag, rank in rows),
        default=0.0,
    )


def _raw_score(
    weights: RecommendationWeightsConfig,
    lastfm_similarity_score: float,
    tag_similarity_score: float,
    rediscovery_score: float,
) -> float:
    return (
        weights.lastfm_similarity * lastfm_similarity_score
        + weights.tag_similarity * tag_similarity_score
        + weights.rediscovery * rediscovery_score
    )


def _explanation(
    conn: DuckDBPyConnection,
    artist_id: int,
    artist_name: str,
    seed_weight_by_id: dict[int, float],
    tag_profile: dict[str, float],
    knownness_penalty: float,
    obviousness_penalty: float,
    knownness: float,
    rediscovery: float,
    obviousness: float,
) -> dict[str, object]:
    seed_rows = conn.execute(
        """
        SELECT source_artist.canonical_name, edge.normalized_score
        FROM artist_similarity_edge edge
        JOIN artist source_artist ON source_artist.artist_id = edge.source_artist_id
        WHERE edge.source = 'lastfm'
          AND edge.target_artist_id = ?
        ORDER BY edge.normalized_score DESC, source_artist.canonical_name ASC
        LIMIT 5
        """,
        [artist_id],
    ).fetchall()
    tag_rows = conn.execute(
        """
        SELECT tag, tag_rank
        FROM tag_top_artist
        WHERE source = 'lastfm'
          AND artist_id = ?
        ORDER BY tag_rank ASC, tag ASC
        LIMIT 5
        """,
        [artist_id],
    ).fetchall()
    return {
        "summary": (
            f"{artist_name} is similar to high-weight seed artists"
            " and fits tags from the local listening profile."
        ),
        "seed_artists": [
            {
                "artist": str(seed_name),
                "seed_weight": seed_weight_by_id.get(_artist_id(conn, str(seed_name)), 0.0),
                "source": "lastfm_artist_similarity",
                "similarity": float(similarity),
            }
            for seed_name, similarity in seed_rows
        ],
        "tags": [
            {
                "tag": str(tag),
                "user_weight": tag_profile.get(str(tag), 0.0),
                "candidate_weight": _rank_affinity(int(rank)),
                "candidate_rank_for_tag": int(rank),
            }
            for tag, rank in tag_rows
        ],
        "penalties": {
            "knownness": knownness_penalty,
            "obviousness": obviousness_penalty,
            "diversity": 0.0,
        },
        "classification": {
            "is_new": knownness == 0.0,
            "is_rediscovery": rediscovery > 0.0,
            "is_obvious": obviousness >= 0.75,
        },
    }


def _persist_run(
    conn: DuckDBPyConnection,
    run_id: str,
    mode: str,
    config: ScrobbleRecsConfig,
    seeds: list[SeedArtist],
    ranked: list[_CandidateScore],
) -> None:
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            """
            INSERT INTO recommendation_run (
                run_id,
                config_hash,
                mode,
                seed_summary,
                scoring_weights,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                _config_hash(config),
                mode,
                json.dumps(
                    {
                        "seed_count": len(seeds),
                        "top_seeds": [
                            {"artist": seed.name, "seed_weight": seed.seed_weight}
                            for seed in seeds[:10]
                        ],
                    }
                ),
                config.recommendation.weights.model_dump_json(),
                "offline lastfm/tag first recommender",
            ],
        )
        for rank, score in enumerate(ranked, start=1):
            conn.execute(
                """
                INSERT INTO recommendation (
                    run_id,
                    rank,
                    artist_id,
                    final_score,
                    raw_score,
                    lastfm_similarity_score,
                    tag_similarity_score,
                    cluster_fit_score,
                    rediscovery_score,
                    metadata_graph_score,
                    feedback_score,
                    knownness,
                    obviousness,
                    diversity_penalty,
                    explanation
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    rank,
                    score.artist_id,
                    score.final_score,
                    score.raw_score,
                    score.lastfm_similarity_score,
                    score.tag_similarity_score,
                    0.0,
                    score.rediscovery_score,
                    0.0,
                    0.0,
                    score.knownness,
                    score.obviousness,
                    0.0,
                    json.dumps(score.explanation),
                ],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _artist_name(conn: DuckDBPyConnection, artist_id: int) -> str:
    row = conn.execute(
        "SELECT canonical_name FROM artist WHERE artist_id = ?",
        [artist_id],
    ).fetchone()
    if row is None:
        raise ValueError(f"artist_id {artist_id} does not exist")
    return str(row[0])


def _artist_id(conn: DuckDBPyConnection, artist_name: str) -> int:
    row = conn.execute(
        "SELECT artist_id FROM artist WHERE canonical_name = ?",
        [artist_name],
    ).fetchone()
    if row is None:
        return -1
    return int(row[0])


def _rank_affinity(rank: int) -> float:
    if rank <= 0:
        return 0.0
    if rank <= 3:
        return 1.0
    if rank <= 10:
        return 0.75
    if rank <= 25:
        return 0.40
    if rank <= 100:
        return 0.15
    return 0.0


def _rank_penalty(rank: int) -> float:
    return _rank_affinity(rank)


def _config_hash(config: ScrobbleRecsConfig) -> str:
    payload = config.recommendation.model_dump_json(by_alias=True)
    return sha256(payload.encode("utf-8")).hexdigest()
```

- [x] **Step 4: Run recommendation tests and fix strict typing failures**

Run:

```bash
python -m pytest tests/test_recommend.py -v
python -m mypy src
```

Expected:

```text
3 passed
Success: no issues found
```

If mypy reports that DuckDB row values are `Any`, add explicit `int(...)`, `float(...)`, or `str(...)` conversions at the read boundary in `src/scrobble_recs/models/recommend.py`.

- [x] **Step 5: Commit recommendation generation**

Run:

```bash
git add src/scrobble_recs/models/recommend.py tests/test_recommend.py
git commit -m "feat: generate persisted recommendations"
```

---

### Task 4: Export Persisted Recommendations

**Files:**
- Create: `src/scrobble_recs/export.py`
- Test: `tests/test_export.py`

- [x] **Step 1: Write failing export tests**

Create `tests/test_export.py` with:

```python
import csv
import json
from datetime import datetime
from pathlib import Path

from scrobble_recs.apis.lastfm import LastFmArtistTag, LastFmSimilarArtist, LastFmTagTopArtist
from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.enrichment.lastfm import run_lastfm_enrichment
from scrobble_recs.export import export_recommendations
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
from scrobble_recs.models.recommend import generate_recommendations


class ExportFakeLastFmClient:
    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        return [LastFmSimilarArtist("Bauhaus", None, 0.72)]

    def get_artist_tags(self, artist: str) -> list[LastFmArtistTag]:
        return [LastFmArtistTag("gothic rock", 100.0)]

    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        return [LastFmTagTopArtist(4, "Bauhaus", None, 700.0)]


def write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "scrobbles.duckdb"),
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
                "recommendation": {
                    "seed_artist_limit": 10,
                    "similar_artists_per_seed": 10,
                    "min_seed_scrobbles": 2,
                    "obviousness": {
                        "tag_top_artists_limit": 100
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _prepared_run(tmp_path: Path) -> tuple[object, str]:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, config, client=ExportFakeLastFmClient())
    stats = generate_recommendations(
        conn,
        config,
        limit=1,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )
    return conn, stats.run_id


def test_export_recommendations_writes_csv(tmp_path: Path) -> None:
    conn, run_id = _prepared_run(tmp_path)
    output = tmp_path / "recs.csv"

    exported = export_recommendations(conn, output, format_name="csv", run_id=run_id)

    assert exported == 1
    with output.open("r", encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    assert rows[0]["rank"] == "1"
    assert rows[0]["artist"] == "Bauhaus"
    assert rows[0]["primary_tags"] == "gothic rock"
    assert rows[0]["why"].startswith("Bauhaus is similar")
    conn.close()


def test_export_recommendations_writes_jsonl(tmp_path: Path) -> None:
    conn, run_id = _prepared_run(tmp_path)
    output = tmp_path / "recs.jsonl"

    exported = export_recommendations(conn, output, format_name="jsonl", run_id=run_id)

    assert exported == 1
    row = json.loads(output.read_text(encoding="utf-8").strip())
    assert row["rank"] == 1
    assert row["artist"] == "Bauhaus"
    assert row["explanation"]["tags"][0]["tag"] == "gothic rock"
    conn.close()
```

- [x] **Step 2: Run export tests to verify they fail**

Run:

```bash
python -m pytest tests/test_export.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.export'
```

- [x] **Step 3: Implement CSV and JSONL export**

Note: `export_recommendations()` accepts `format_name: str` while still exporting `ExportFormat`
so unsupported formats can reach the required runtime `ValueError`.

Review fix: format validation now happens before run lookup or filesystem writes, Last.fm artist
URLs are path-segment encoded, and latest-run fallback uses `created_at DESC, run_id DESC`.

Create `src/scrobble_recs/export.py` with:

```python
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Literal

from duckdb import DuckDBPyConnection

ExportFormat = Literal["csv", "jsonl"]


def export_recommendations(
    conn: DuckDBPyConnection,
    output_path: Path,
    *,
    format_name: ExportFormat,
    run_id: str | None = None,
) -> int:
    resolved_run_id = run_id or _latest_run_id(conn)
    rows = _load_rows(conn, resolved_run_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if format_name == "csv":
        _write_csv(output_path, rows)
    elif format_name == "jsonl":
        _write_jsonl(output_path, rows)
    else:
        raise ValueError("format_name must be 'csv' or 'jsonl'")
    return len(rows)


def _latest_run_id(conn: DuckDBPyConnection) -> str:
    row = conn.execute(
        """
        SELECT CAST(run_id AS VARCHAR)
        FROM recommendation_run
        ORDER BY created_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise ValueError("no recommendation runs exist")
    return str(row[0])


def _load_rows(conn: DuckDBPyConnection, run_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            r.rank,
            a.canonical_name,
            r.final_score,
            r.knownness,
            r.obviousness,
            r.rediscovery_score,
            r.explanation
        FROM recommendation r
        JOIN artist a ON a.artist_id = r.artist_id
        WHERE r.run_id = ?
        ORDER BY r.rank
        """,
        [run_id],
    ).fetchall()
    result: list[dict[str, object]] = []
    for rank, artist, score, knownness, obviousness, rediscovery_score, explanation_json in rows:
        explanation = json.loads(str(explanation_json))
        tags = explanation.get("tags", [])
        primary_tags = ", ".join(str(tag["tag"]) for tag in tags[:3])
        result.append(
            {
                "rank": int(rank),
                "artist": str(artist),
                "score": float(score),
                "primary_tags": primary_tags,
                "knownness": float(knownness),
                "obviousness": float(obviousness),
                "is_rediscovery": bool(float(rediscovery_score) > 0.0),
                "why": str(explanation.get("summary", "")),
                "lastfm_url": f"https://www.last.fm/music/{str(artist).replace(' ', '+')}",
                "musicbrainz_url": "",
                "explanation": explanation,
            }
        )
    return result


def _write_csv(output_path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "rank",
        "artist",
        "score",
        "primary_tags",
        "knownness",
        "obviousness",
        "is_rediscovery",
        "why",
        "lastfm_url",
        "musicbrainz_url",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in fieldnames})


def _write_jsonl(output_path: Path, rows: list[dict[str, object]]) -> None:
    with output_path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, sort_keys=True) + "\n")
```

- [x] **Step 4: Run export tests to verify they pass**

Run:

```bash
python -m pytest tests/test_export.py -v
```

Expected:

```text
2 passed
```

- [x] **Step 5: Commit export support**

Note: Task execution also stages this plan file and `docs/implementation-log.md` because the user
explicitly requested plan checkbox updates and the repo done rule requires the implementation log.

Run:

```bash
git add src/scrobble_recs/export.py tests/test_export.py
git commit -m "feat: export persisted recommendations"
```

---

### Task 5: Recommend And Export CLI Commands

**Files:**
- Modify: `src/scrobble_recs/cli.py`
- Modify: `tests/test_cli.py`

- [x] **Step 1: Add failing CLI tests**

Append these tests to `tests/test_cli.py`:

```python
def test_recommend_command_generates_persisted_run(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    calls: list[tuple[str, int, str | None]] = []

    class FakeRecommendationStats:
        run_id = "00000000-0000-0000-0000-000000000001"
        mode = "balanced"
        recommendations = 5

    def fake_generate(conn, config, *, limit, mode=None, as_of=None):
        calls.append((config.app.name, limit, mode))
        return FakeRecommendationStats()

    monkeypatch.setattr("scrobble_recs.cli.generate_recommendations", fake_generate)

    result = runner.invoke(app, ["recommend", "--config", str(config_path), "--limit", "5"])

    assert result.exit_code == 0
    assert calls == [("scrobble-recs", 5, None)]
    assert "Recommendation run complete" in result.output
    assert "run id: 00000000-0000-0000-0000-000000000001" in result.output
    assert "recommendations: 5" in result.output


def test_export_command_writes_requested_format(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    output = tmp_path / "recs.csv"
    calls: list[tuple[Path, str, str | None]] = []

    def fake_export(conn, output_path, *, format_name, run_id=None):
        calls.append((output_path, format_name, run_id))
        output_path.write_text("rank,artist\n", encoding="utf-8")
        return 3

    monkeypatch.setattr("scrobble_recs.cli.export_recommendations", fake_export)

    result = runner.invoke(
        app,
        [
            "export",
            "--config",
            str(config_path),
            "--format",
            "csv",
            "--output",
            str(output),
            "--run-id",
            "00000000-0000-0000-0000-000000000001",
        ],
    )

    assert result.exit_code == 0
    assert calls == [(output, "csv", "00000000-0000-0000-0000-000000000001")]
    assert "Exported 3 recommendations" in result.output
```

- [x] **Step 2: Run CLI tests to verify they fail**

Run:

```bash
python -m pytest tests/test_cli.py::test_recommend_command_generates_persisted_run tests/test_cli.py::test_export_command_writes_requested_format -v
```

Expected:

```text
FAILED tests/test_cli.py::test_recommend_command_generates_persisted_run
FAILED tests/test_cli.py::test_export_command_writes_requested_format
```

- [x] **Step 3: Add CLI imports**

Modify `src/scrobble_recs/cli.py` imports to include:

```python
from typing import Annotated, cast

from scrobble_recs.export import ExportFormat, export_recommendations
from scrobble_recs.models.recommend import RecommendationStats, generate_recommendations
```

- [x] **Step 4: Add recommend and export commands**

Add these commands after `enrich_command` in `src/scrobble_recs/cli.py`:

```python
@app.command("recommend")
def recommend_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    limit: Annotated[int, typer.Option("--limit", help="Number of recommendations to persist.")] = 500,
    mode: Annotated[str | None, typer.Option("--mode", help="Recommendation mode.")] = None,
) -> None:
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        stats = generate_recommendations(conn, resolved_config, limit=limit, mode=mode)
    finally:
        conn.close()

    console.print("Recommendation run complete")
    _print_recommendation_stats(stats)


@app.command("export")
def export_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Output file path.")],
    format_name: Annotated[
        str,
        typer.Option("--format", help="Export format: csv or jsonl."),
    ] = "csv",
    run_id: Annotated[str | None, typer.Option("--run-id", help="Recommendation run UUID.")] = None,
) -> None:
    if format_name not in {"csv", "jsonl"}:
        raise typer.BadParameter("format must be csv or jsonl")

    resolved_format = cast(ExportFormat, format_name)
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        exported = export_recommendations(
            conn,
            output,
            format_name=resolved_format,
            run_id=run_id,
        )
    finally:
        conn.close()

    console.print(f"Exported {exported} recommendations to {output}")
```

Add this helper after `_print_enrichment_stats`:

```python
def _print_recommendation_stats(stats: RecommendationStats) -> None:
    console.print(f"run id: {stats.run_id}")
    console.print(f"mode: {stats.mode}")
    console.print(f"recommendations: {stats.recommendations}")
```

Deviation: Review-driven CLI error handling was added beyond the original happy-path command
block, with focused validation tests for non-positive `--limit` and service `ValueError` output.

- [x] **Step 5: Run CLI tests to verify they pass**

Run:

```bash
python -m pytest tests/test_cli.py -v
```

Expected:

```text
5 passed
```

Deviation: The plan expected `5 passed`, but current `python -m pytest tests/test_cli.py -v`
produces `11 passed`: five original CLI tests, two Task 5 plan tests, and four
review-driven validation/error tests.

- [x] **Step 6: Commit CLI commands**

Run:

```bash
git add src/scrobble_recs/cli.py tests/test_cli.py
git commit -m "feat: add recommend and export commands"
```

---

### Task 6: Verification, Docs, And Implementation Log

**Files:**
- Modify: `README.md`
- Modify: `docs/implementation-log.md`

- [ ] **Step 1: Run focused recommendation and export tests**

Run:

```bash
python -m pytest tests/test_knownness.py tests/test_recommend.py tests/test_export.py tests/test_cli.py -v
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
All checks passed!
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

Append this section to `README.md`:

```markdown
## Milestone 3

This slice adds the first persisted recommender:

- Recomputes local listening weights, knownness, and rediscovery scores.
- Generates recommendations from persisted Last.fm similarity and tag enrichment.
- Penalizes known and obvious top-tag artists.
- Persists recommendation runs with inspectable JSON explanations.
- Exports persisted runs to CSV or JSONL.

```bash
scrobble-recs recommend --config config.example.json --limit 500
scrobble-recs export --config config.example.json --format csv --output recs.csv
scrobble-recs export --config config.example.json --format jsonl --output recs.jsonl
```

`recommend` does not call live APIs. Run `enrich --source lastfm` first to populate the local graph.
```

- [ ] **Step 7: Update implementation log**

Append this entry to `docs/implementation-log.md`:

```markdown
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
```

- [ ] **Step 8: Commit docs and verification**

Run:

```bash
git add README.md docs/implementation-log.md
git commit -m "docs: record first recommender milestone"
```

---

## Self Review

Spec coverage:

- Recommendation schema from spec section 8.7 is covered by Task 1.
- Artist weight, recent weight, knownness, and rediscovery formulas from spec section 10 are covered by Task 2 and Task 3.
- Candidate generation from Last.fm similarity and tag top artists from spec section 11 is covered by Task 3.
- Obviousness penalty from spec section 12 is covered by Task 3.
- Final score fields from spec section 13 are persisted by Task 3, with cluster, metadata graph, feedback, and diversity fields stored as `0.0` in this first slice.
- Explainable recommendation JSON from spec section 16.5 is covered by Task 3.
- CSV and JSONL export from spec section 22 is covered by Task 4.
- CLI contract for `recommend` and `export` from spec section 7 is covered by Task 5.
- Documentation and implementation log updates are covered by Task 6.

Deferred requirements:

- Diversity reranking is deferred to a focused follow-up because the first recommender can already rank, suppress known artists, penalize obvious artists, explain results, and export runs.
- Feedback scoring is deferred until the feedback schema and TUI actions are implemented.
- Cluster fit and metadata graph scoring are stored as zero-valued components until their data sources exist.
- TUI browsing and feedback remain Milestone 4.
- MusicBrainz identity resolution remains Milestone 5.
- ListenBrainz collaborative modeling remains Milestone 6.

Placeholder scan:

- The plan contains no TBD or open-ended implementation instructions.
- Code-writing steps include concrete code blocks for files being created or modified.
- Test-running steps include commands and expected outcomes.

Type consistency:

- `RecommendationStats` is defined before CLI use.
- `generate_recommendations()` accepts `conn`, `config`, `limit`, `mode`, and optional `as_of`, matching tests and CLI.
- `export_recommendations()` accepts `conn`, `output_path`, `format_name`, and optional `run_id`, matching tests and CLI.
- `ArtistListeningInput` and `compute_artist_listening_scores()` are used consistently by tests and recommendation generation.
