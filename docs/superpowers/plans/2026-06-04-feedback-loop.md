# Feedback Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local feedback loop that records recommendation feedback, applies feedback scoring to future runs, and reranks the current persisted run without live API calls.

**Architecture:** Store every feedback action as an immutable DuckDB event, then derive a deterministic feedback profile from those events when scoring or reranking. Future `recommend` runs consume feedback as artist suppression/boosts, tag suppression/boosts, and similarity-neighborhood boosts; the current run can be reranked in place by updating persisted recommendation rows and explanations. This keeps the loop local-first, inspectable, and compatible with the existing recommendation tables.

**Tech Stack:** Python 3.11+, DuckDB, Typer, Pydantic v2, pytest, ruff, mypy.

---

## Scope

This plan covers the feedback-loop slice from `docs/spec.md`:

- Section 8.8: `feedback_event` schema and allowed feedback actions.
- Section 13 feedback scoring: direct artist, tag, obviousness, knownness, and neighborhood adjustments.
- Section 17: write feedback immediately, rerank lightly, and affect future artist seeds, tag profile, similarity-neighborhood scoring, and mode behavior.

This plan deliberately keeps these pieces out of scope:

- Textual TUI feedback widgets and keybindings.
- Undo UI, although the event table and event API are compatible with adding undo later.
- Full model retraining after feedback.
- Live enrichment triggered by feedback.
- Collaborative ListenBrainz feedback learning.

## File Structure

- Create: `src/scrobble_recs/sql/004_feedback.sql` for the feedback event table and lookup indexes.
- Create: `src/scrobble_recs/models/feedback.py` for action validation, event persistence, derived feedback profiles, scoring adjustments, and lightweight rerank.
- Modify: `src/scrobble_recs/models/recommend.py` to consume feedback profiles during seed selection, tag-profile construction, candidate generation, scoring, persistence, and explanations.
- Modify: `src/scrobble_recs/cli.py` to add a `feedback` command that records one event and reranks the target run by default.
- Modify: `README.md` to document the local feedback workflow.
- Modify: `docs/implementation-log.md` after implementation and verification.
- Test: `tests/test_feedback.py`.
- Modify: `tests/test_recommend.py`.
- Modify: `tests/test_cli.py`.

---

### Task 1: Feedback Schema

**Files:**
- Create: `src/scrobble_recs/sql/004_feedback.sql`
- Test: `tests/test_feedback.py`

- [ ] **Step 1: Write failing schema tests**

Create `tests/test_feedback.py` with:

```python
import json
from pathlib import Path

import pytest
from duckdb import ConstraintException

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
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_feedback_migration_creates_event_table(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
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

        assert "feedback_event" in tables
        assert _column_types(conn, "feedback_event") == {
            "feedback_id": "UUID",
            "created_at": "TIMESTAMP",
            "run_id": "UUID",
            "artist_id": "UBIGINT",
            "action": "VARCHAR",
            "value": "DOUBLE",
            "context": "JSON",
            "note": "VARCHAR",
        }

        conn.execute(
            """
            INSERT INTO feedback_event (
                feedback_id,
                run_id,
                artist_id,
                action,
                value,
                context,
                note
            )
            VALUES (
                '00000000-0000-0000-0000-000000000101',
                '00000000-0000-0000-0000-000000000001',
                10,
                'more_like_this',
                1.0,
                '{"tags":["post-punk"]}',
                'good fit'
            )
            """
        )

        with pytest.raises(ConstraintException):
            conn.execute(
                """
                INSERT INTO feedback_event (
                    feedback_id,
                    run_id,
                    artist_id,
                    action,
                    value,
                    context
                )
                VALUES (
                    '00000000-0000-0000-0000-000000000101',
                    '00000000-0000-0000-0000-000000000001',
                    11,
                    'reject',
                    1.0,
                    '{}'
                )
                """
            )
    finally:
        conn.close()


def _column_types(conn, table_name: str) -> dict[str, str]:
    return {
        str(column_name): str(data_type)
        for column_name, data_type in conn.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'main'
              AND table_name = ?
            ORDER BY ordinal_position
            """,
            [table_name],
        ).fetchall()
    }
```

- [ ] **Step 2: Run schema test to verify it fails**

Run:

```bash
python -m pytest tests/test_feedback.py::test_feedback_migration_creates_event_table -v
```

Expected:

```text
AssertionError: assert 'feedback_event' in ...
```

- [ ] **Step 3: Add feedback migration**

Create `src/scrobble_recs/sql/004_feedback.sql` with:

```sql
CREATE TABLE IF NOT EXISTS feedback_event (
    feedback_id UUID,
    created_at TIMESTAMP DEFAULT current_timestamp,
    run_id UUID,
    artist_id UBIGINT,
    action VARCHAR,
    value DOUBLE,
    context JSON,
    note VARCHAR
);

CREATE UNIQUE INDEX IF NOT EXISTS feedback_event_id_key
ON feedback_event (feedback_id);

CREATE INDEX IF NOT EXISTS feedback_event_run_key
ON feedback_event (run_id);

CREATE INDEX IF NOT EXISTS feedback_event_artist_key
ON feedback_event (artist_id);

CREATE INDEX IF NOT EXISTS feedback_event_action_key
ON feedback_event (action);
```

- [ ] **Step 4: Run schema test to verify it passes**

Run:

```bash
python -m pytest tests/test_feedback.py::test_feedback_migration_creates_event_table -v
```

Expected:

```text
1 passed
```

- [ ] **Step 5: Commit feedback schema**

Run:

```bash
git add src/scrobble_recs/sql/004_feedback.sql tests/test_feedback.py docs/superpowers/plans/2026-06-04-feedback-loop.md
git commit -m "feat: add feedback event schema"
```

---

### Task 2: Feedback Event API And Derived Profile

**Files:**
- Create: `src/scrobble_recs/models/feedback.py`
- Modify: `tests/test_feedback.py`

- [ ] **Step 1: Add failing event and profile tests**

Append these tests to `tests/test_feedback.py`:

```python
from datetime import datetime

from scrobble_recs.models.feedback import (
    FeedbackProfile,
    load_feedback_profile,
    record_feedback_event,
)


def test_record_feedback_event_validates_action_and_persists_json_context(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_artist(conn, artist_id=10, name="Bauhaus")
    _insert_run(conn, run_id="00000000-0000-0000-0000-000000000001")

    event = record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000001",
        artist_id=10,
        action="more_like_this",
        value=1.0,
        context={"tags": ["gothic rock"]},
        note="good lead",
        created_at=datetime(2026, 6, 4, 12, 0, 0),
        feedback_id="00000000-0000-0000-0000-000000000201",
    )

    assert event.feedback_id == "00000000-0000-0000-0000-000000000201"
    assert event.action == "more_like_this"
    row = conn.execute(
        """
        SELECT run_id, artist_id, action, value, context, note
        FROM feedback_event
        WHERE feedback_id = ?
        """,
        [event.feedback_id],
    ).fetchone()
    assert row == (
        "00000000-0000-0000-0000-000000000001",
        10,
        "more_like_this",
        1.0,
        '{"tags":["gothic rock"]}',
        "good lead",
    )
    conn.close()


def test_record_feedback_event_rejects_unknown_action(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)

    with pytest.raises(ValueError, match="unsupported feedback action 'favorite'"):
        record_feedback_event(
            conn,
            run_id="00000000-0000-0000-0000-000000000001",
            artist_id=10,
            action="favorite",
        )

    assert conn.execute("SELECT count(*) FROM feedback_event").fetchone() == (0,)
    conn.close()


def test_load_feedback_profile_derives_artist_tag_and_seed_adjustments(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_artist(conn, artist_id=20, name="Candidate A")
    _insert_artist(conn, artist_id=21, name="Candidate B")
    _insert_run(conn, run_id="00000000-0000-0000-0000-000000000002")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000002",
        artist_id=20,
        action="more_like_this",
        context={"tags": ["post-punk", "coldwave"]},
        feedback_id="00000000-0000-0000-0000-000000000301",
    )
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000002",
        artist_id=21,
        action="hide_artist",
        context={"tags": ["arena rock"]},
        feedback_id="00000000-0000-0000-0000-000000000302",
    )
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000002",
        artist_id=21,
        action="boost_tag",
        context={"tags": ["coldwave"]},
        feedback_id="00000000-0000-0000-0000-000000000303",
    )

    profile = load_feedback_profile(conn)

    assert isinstance(profile, FeedbackProfile)
    assert profile.hidden_artist_ids == {21}
    assert profile.boosted_seed_artist_ids == {20}
    assert profile.artist_adjustments[20] == pytest.approx(0.15)
    assert profile.artist_adjustments[21] == pytest.approx(-1.0)
    assert profile.tag_adjustments["post-punk"] == pytest.approx(0.10)
    assert profile.tag_adjustments["coldwave"] == pytest.approx(0.25)
    conn.close()


def _insert_artist(conn, *, artist_id: int, name: str) -> None:
    conn.execute(
        """
        INSERT INTO artist (artist_id, canonical_name, artist_key, lastfm_name)
        VALUES (?, ?, ?, ?)
        """,
        [artist_id, name, name.casefold().replace(" ", "-"), name],
    )


def _insert_run(conn, *, run_id: str) -> None:
    conn.execute(
        """
        INSERT INTO recommendation_run (
            run_id,
            config_hash,
            mode,
            seed_summary,
            scoring_weights
        )
        VALUES (?, 'hash', 'balanced', '{}', '{}')
        """,
        [run_id],
    )
```

- [ ] **Step 2: Run event/profile tests to verify they fail**

Run:

```bash
python -m pytest tests/test_feedback.py::test_record_feedback_event_validates_action_and_persists_json_context tests/test_feedback.py::test_record_feedback_event_rejects_unknown_action tests/test_feedback.py::test_load_feedback_profile_derives_artist_tag_and_seed_adjustments -v
```

Expected:

```text
ModuleNotFoundError: No module named 'scrobble_recs.models.feedback'
```

- [ ] **Step 3: Implement feedback model**

Create `src/scrobble_recs/models/feedback.py` with:

```python
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast
from uuid import uuid4

from duckdb import DuckDBPyConnection

FeedbackAction = Literal[
    "save",
    "reject",
    "already_know",
    "too_obvious",
    "wrong_genre",
    "more_like_this",
    "less_like_this",
    "hide_artist",
    "hide_tag",
    "boost_tag",
    "rediscovery_good",
    "rediscovery_bad",
    "opened_link",
    "played_externally",
    "note",
]

ALLOWED_FEEDBACK_ACTIONS: frozenset[str] = frozenset(
    {
        "save",
        "reject",
        "already_know",
        "too_obvious",
        "wrong_genre",
        "more_like_this",
        "less_like_this",
        "hide_artist",
        "hide_tag",
        "boost_tag",
        "rediscovery_good",
        "rediscovery_bad",
        "opened_link",
        "played_externally",
        "note",
    }
)

DIRECT_ARTIST_ADJUSTMENTS: dict[str, float] = {
    "save": 0.20,
    "reject": -0.25,
    "already_know": -0.20,
    "too_obvious": -0.20,
    "wrong_genre": -0.20,
    "more_like_this": 0.15,
    "less_like_this": -0.15,
    "hide_artist": -1.00,
    "rediscovery_good": 0.10,
    "rediscovery_bad": -0.10,
}

TAG_ADJUSTMENTS: dict[str, float] = {
    "more_like_this": 0.10,
    "less_like_this": -0.10,
    "wrong_genre": -0.20,
    "hide_tag": -1.00,
    "boost_tag": 0.15,
    "too_obvious": -0.05,
    "rediscovery_good": 0.05,
    "rediscovery_bad": -0.05,
}

OBVIOUSNESS_TAG_ADJUSTMENTS: dict[str, float] = {
    "too_obvious": 0.10,
}


@dataclass(frozen=True)
class FeedbackEvent:
    feedback_id: str
    run_id: str
    artist_id: int
    action: FeedbackAction
    value: float
    context: dict[str, Any]
    note: str | None


@dataclass(frozen=True)
class FeedbackProfile:
    artist_adjustments: dict[int, float] = field(default_factory=dict)
    tag_adjustments: dict[str, float] = field(default_factory=dict)
    tag_obviousness_adjustments: dict[str, float] = field(default_factory=dict)
    hidden_artist_ids: set[int] = field(default_factory=set)
    current_run_hidden_artist_ids: set[int] = field(default_factory=set)
    known_artist_ids: set[int] = field(default_factory=set)
    boosted_seed_artist_ids: set[int] = field(default_factory=set)
    suppressed_seed_artist_ids: set[int] = field(default_factory=set)


def record_feedback_event(
    conn: DuckDBPyConnection,
    *,
    run_id: str,
    artist_id: int,
    action: str,
    value: float = 1.0,
    context: dict[str, Any] | None = None,
    note: str | None = None,
    created_at: datetime | None = None,
    feedback_id: str | None = None,
) -> FeedbackEvent:
    if action not in ALLOWED_FEEDBACK_ACTIONS:
        raise ValueError(f"unsupported feedback action {action!r}")
    resolved_feedback_id = feedback_id or str(uuid4())
    resolved_context = _normalized_context(context or {})
    params: list[object] = [
        resolved_feedback_id,
        run_id,
        artist_id,
        action,
        value,
        json.dumps(resolved_context, sort_keys=True, separators=(",", ":")),
        note,
    ]
    created_at_sql = ""
    if created_at is not None:
        created_at_sql = "created_at,"
        params.insert(1, created_at)
    conn.execute(
        f"""
        INSERT INTO feedback_event (
            feedback_id,
            {created_at_sql}
            run_id,
            artist_id,
            action,
            value,
            context,
            note
        )
        VALUES (
            ?,
            {"?," if created_at is not None else ""}
            ?,
            ?,
            ?,
            ?,
            ?,
            ?
        )
        """,
        params,
    )
    return FeedbackEvent(
        feedback_id=resolved_feedback_id,
        run_id=run_id,
        artist_id=artist_id,
        action=cast(FeedbackAction, action),
        value=value,
        context=resolved_context,
        note=note,
    )


def load_feedback_profile(conn: DuckDBPyConnection) -> FeedbackProfile:
    profile = FeedbackProfile()
    rows = conn.execute(
        """
        SELECT artist_id, action, value, context
        FROM feedback_event
        ORDER BY created_at, feedback_id
        """
    ).fetchall()
    for artist_id_raw, action_raw, value_raw, context_raw in rows:
        artist_id = int(artist_id_raw)
        action = str(action_raw)
        value = float(value_raw or 1.0)
        context = _parse_context(context_raw)
        _apply_artist_feedback(profile, artist_id=artist_id, action=action, value=value)
        tags = _context_tags(context)
        for tag in tags:
            _add_float(
                profile.tag_adjustments,
                tag,
                TAG_ADJUSTMENTS.get(action, 0.0) * value,
            )
            _add_float(
                profile.tag_obviousness_adjustments,
                tag,
                OBVIOUSNESS_TAG_ADJUSTMENTS.get(action, 0.0) * value,
            )
    return profile


def feedback_adjustment_for_candidate(
    profile: FeedbackProfile,
    *,
    artist_id: int,
    candidate_tags: dict[str, float],
    neighborhood_score: float = 0.0,
) -> float:
    score = profile.artist_adjustments.get(artist_id, 0.0)
    score += neighborhood_score
    for tag, weight in candidate_tags.items():
        score += profile.tag_adjustments.get(tag, 0.0) * weight
    return max(-1.0, min(1.0, score))


def feedback_obviousness_for_candidate(
    profile: FeedbackProfile,
    *,
    candidate_tags: dict[str, float],
) -> float:
    score = 0.0
    for tag, weight in candidate_tags.items():
        score += profile.tag_obviousness_adjustments.get(tag, 0.0) * weight
    return max(0.0, min(1.0, score))


def apply_feedback_to_tag_profile(
    tag_profile: dict[str, float],
    feedback_profile: FeedbackProfile,
) -> dict[str, float]:
    adjusted = dict(tag_profile)
    for tag, delta in feedback_profile.tag_adjustments.items():
        adjusted[tag] = max(0.0, adjusted.get(tag, 0.0) + delta)
    max_weight = max(adjusted.values(), default=0.0)
    if max_weight <= 0.0:
        return {}
    return {tag: weight / max_weight for tag, weight in adjusted.items() if weight > 0.0}


def _apply_artist_feedback(
    profile: FeedbackProfile,
    *,
    artist_id: int,
    action: str,
    value: float,
) -> None:
    _add_float(
        profile.artist_adjustments,
        artist_id,
        DIRECT_ARTIST_ADJUSTMENTS.get(action, 0.0) * value,
    )
    if action in {"hide_artist"}:
        profile.hidden_artist_ids.add(artist_id)
    if action in {"hide_artist", "too_obvious", "already_know"}:
        profile.current_run_hidden_artist_ids.add(artist_id)
    if action in {"already_know"}:
        profile.known_artist_ids.add(artist_id)
    if action in {"save", "more_like_this", "rediscovery_good"}:
        profile.boosted_seed_artist_ids.add(artist_id)
    if action in {"reject", "less_like_this", "wrong_genre", "rediscovery_bad"}:
        profile.suppressed_seed_artist_ids.add(artist_id)


def _add_float(target: dict[Any, float], key: Any, value: float) -> None:
    if value == 0.0:
        return
    target[key] = target.get(key, 0.0) + value


def _normalized_context(context: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(context)
    if "tags" in normalized:
        normalized["tags"] = _context_tags(normalized)
    return normalized


def _parse_context(raw: object) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    parsed = json.loads(str(raw))
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _context_tags(context: dict[str, Any]) -> list[str]:
    raw_tags = context.get("tags", [])
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    if not isinstance(raw_tags, list):
        return []
    return sorted(
        {
            str(tag).strip().casefold()
            for tag in raw_tags
            if str(tag).strip()
        }
    )
```

- [ ] **Step 4: Run event/profile tests to verify they pass**

Run:

```bash
python -m pytest tests/test_feedback.py::test_record_feedback_event_validates_action_and_persists_json_context tests/test_feedback.py::test_record_feedback_event_rejects_unknown_action tests/test_feedback.py::test_load_feedback_profile_derives_artist_tag_and_seed_adjustments -v
```

Expected:

```text
3 passed
```

- [ ] **Step 5: Run all feedback tests**

Run:

```bash
python -m pytest tests/test_feedback.py -v
```

Expected:

```text
4 passed
```

- [ ] **Step 6: Commit feedback model**

Run:

```bash
git add src/scrobble_recs/models/feedback.py tests/test_feedback.py docs/superpowers/plans/2026-06-04-feedback-loop.md
git commit -m "feat: derive local feedback profile"
```

---

### Task 3: Feedback-Aware Recommendation Scoring

**Files:**
- Modify: `src/scrobble_recs/models/recommend.py`
- Modify: `tests/test_recommend.py`

- [ ] **Step 1: Add failing scoring tests**

Append these tests to `tests/test_recommend.py`:

```python
from scrobble_recs.models.feedback import record_feedback_event


def test_generate_recommendations_applies_direct_feedback_scores(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=300,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=300, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=300,
        candidate_id=301,
        candidate_name="Lower Similarity Saved",
        tag="post-punk",
        tag_rank=30,
        similarity=0.62,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=300,
        candidate_id=302,
        candidate_name="Higher Similarity Neutral",
        tag="post-punk",
        tag_rank=31,
        similarity=0.70,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000301")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000301",
        artist_id=301,
        action="save",
        context={"tags": ["post-punk"]},
        feedback_id="00000000-0000-0000-0000-000000000401",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == [
        "Lower Similarity Saved",
        "Higher Similarity Neutral",
    ]
    row = conn.execute(
        """
        SELECT r.feedback_score, r.explanation
        FROM recommendation r
        WHERE r.run_id = ?
          AND r.artist_id = 301
        """,
        [stats.run_id],
    ).fetchone()
    assert row[0] > 0.0
    explanation = json.loads(row[1])
    assert explanation["feedback"]["score"] == pytest.approx(row[0])
    assert "artist" in explanation["feedback"]["effects"]
    conn.close()


def test_generate_recommendations_excludes_hidden_feedback_artist(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=310,
        name="Seed Source",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=310, tag="post-punk", weight=100.0)
    _insert_candidate_from_seed(
        conn,
        seed_id=310,
        candidate_id=311,
        candidate_name="Hidden Artist",
        tag="post-punk",
        tag_rank=30,
        similarity=0.90,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=310,
        candidate_id=312,
        candidate_name="Visible Artist",
        tag="post-punk",
        tag_rank=31,
        similarity=0.70,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000302")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000302",
        artist_id=311,
        action="hide_artist",
        feedback_id="00000000-0000-0000-0000-000000000402",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == ["Visible Artist"]
    conn.close()


def test_generate_recommendations_uses_more_like_this_as_future_seed(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_scoring_artist(
        conn,
        artist_id=320,
        name="Original Seed",
        play_count=40,
        lifetime_weight=8.0,
        recent_weight=8.0,
        knownness=0.9,
        rediscovery_score=0.0,
    )
    _insert_artist_tag(conn, artist_id=320, tag="post-punk", weight=100.0)
    _insert_scoring_artist(
        conn,
        artist_id=321,
        name="Feedback Seed",
        play_count=0,
        lifetime_weight=0.0,
        recent_weight=0.0,
        knownness=0.0,
        rediscovery_score=0.0,
    )
    _insert_candidate_from_seed(
        conn,
        seed_id=321,
        candidate_id=322,
        candidate_name="Feedback Neighborhood Artist",
        tag="coldwave",
        tag_rank=40,
        similarity=0.95,
    )
    _insert_run_for_feedback(conn, run_id="00000000-0000-0000-0000-000000000303")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000303",
        artist_id=321,
        action="more_like_this",
        context={"tags": ["coldwave"]},
        feedback_id="00000000-0000-0000-0000-000000000403",
    )

    stats = generate_recommendations(
        conn,
        config,
        limit=2,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
    )

    assert _recommendation_names(conn, stats.run_id) == ["Feedback Neighborhood Artist"]
    row = conn.execute(
        "SELECT feedback_score FROM recommendation WHERE run_id = ? AND artist_id = 322",
        [stats.run_id],
    ).fetchone()
    assert row[0] > 0.0
    conn.close()


def _insert_run_for_feedback(conn, *, run_id: str) -> None:
    conn.execute(
        """
        INSERT INTO recommendation_run (
            run_id,
            config_hash,
            mode,
            seed_summary,
            scoring_weights
        )
        VALUES (?, 'hash', 'balanced', '{}', '{}')
        """,
        [run_id],
    )
```

- [ ] **Step 2: Run scoring tests to verify they fail**

Run:

```bash
python -m pytest tests/test_recommend.py::test_generate_recommendations_applies_direct_feedback_scores tests/test_recommend.py::test_generate_recommendations_excludes_hidden_feedback_artist tests/test_recommend.py::test_generate_recommendations_uses_more_like_this_as_future_seed -v
```

Expected:

```text
at least one test fails because recommendation generation currently persists feedback_score as 0.0 and ignores feedback events
```

- [ ] **Step 3: Wire feedback into recommendation generation**

Modify imports in `src/scrobble_recs/models/recommend.py`:

```python
from scrobble_recs.models.feedback import (
    FeedbackProfile,
    apply_feedback_to_tag_profile,
    feedback_adjustment_for_candidate,
    feedback_obviousness_for_candidate,
    load_feedback_profile,
)
```

Modify `_CandidateScore`:

```python
    feedback_score: float
```

Modify `_ScoringData`:

```python
    feedback_similarity_by_target: dict[int, list[tuple[int, float]]]
```

In `generate_recommendations()`, load feedback before seeds and include boosted seeds:

```python
    feedback_profile = load_feedback_profile(conn)
    seeds = select_seed_artists(
        conn,
        limit=config.recommendation.seed_artist_limit,
        min_scrobbles=config.recommendation.min_seed_scrobbles,
        regions=mode_settings.seed_regions,
    )
    seeds = _add_feedback_seed_artists(conn, seeds, feedback_profile, mode_settings)
    seed_ids = {seed.artist_id for seed in seeds}
    tag_profile = _load_user_tag_profile(conn, seeds)
    effective_tag_profile = apply_feedback_to_tag_profile(
        _apply_tag_focus(tag_profile, mode_settings),
        feedback_profile,
    )
    scoring_data = _load_scoring_data(conn, seed_ids, feedback_profile)
    scores = _score_candidates(
        config,
        mode_settings,
        seeds,
        seed_ids,
        effective_tag_profile,
        scoring_data,
        feedback_profile,
    )
```

Add helper:

```python
def _add_feedback_seed_artists(
    conn: DuckDBPyConnection,
    seeds: list[SeedArtist],
    feedback_profile: FeedbackProfile,
    mode: RecommendationModeSettings,
) -> list[SeedArtist]:
    existing_seed_ids = {seed.artist_id for seed in seeds}
    feedback_seed_ids = sorted(
        feedback_profile.boosted_seed_artist_ids
        - feedback_profile.hidden_artist_ids
        - feedback_profile.suppressed_seed_artist_ids
        - existing_seed_ids
    )
    if not feedback_seed_ids:
        return seeds
    rows = conn.execute(
        """
        SELECT
            artist_id,
            canonical_name,
            artist_key,
            coalesce(nullif(lastfm_name, ''), canonical_name) AS lastfm_name
        FROM artist
        WHERE artist_id IN (
            SELECT unnest(?)
        )
        ORDER BY canonical_name
        """,
        [feedback_seed_ids],
    ).fetchall()
    feedback_seeds = [
        SeedArtist(
            artist_id=int(row[0]),
            name=str(row[1]),
            artist_key=str(row[2]),
            lastfm_name=str(row[3]),
            play_count=0,
            seed_weight=max(0.25, feedback_profile.artist_adjustments.get(int(row[0]), 0.0))
            * mode.lastfm_similarity_multiplier,
            regions=("feedback",),
        )
        for row in rows
    ]
    return seeds + feedback_seeds
```

Modify `_load_scoring_data()` signature and feedback edge loading:

```python
def _load_scoring_data(
    conn: DuckDBPyConnection,
    seed_ids: set[int],
    feedback_profile: FeedbackProfile,
) -> _ScoringData:
```

After loading `similarity_by_target`, add:

```python
    feedback_similarity_by_target: dict[int, list[tuple[int, float]]] = {}
    feedback_seed_ids = (
        feedback_profile.boosted_seed_artist_ids
        - feedback_profile.hidden_artist_ids
        - feedback_profile.suppressed_seed_artist_ids
    )
    if feedback_seed_ids:
        for source_artist_id, target_artist_id, normalized_score in conn.execute(
            """
            SELECT source_artist_id, target_artist_id, normalized_score
            FROM artist_similarity_edge
            WHERE source = 'lastfm'
              AND source_artist_id IN (SELECT unnest(?))
            """,
            [sorted(feedback_seed_ids)],
        ).fetchall():
            source_id = int(source_artist_id)
            target_id = int(target_artist_id)
            candidates.add(target_id)
            feedback_similarity_by_target.setdefault(target_id, []).append(
                (source_id, float(normalized_score))
            )
```

Return `feedback_similarity_by_target=feedback_similarity_by_target`.

Modify `_score_candidates()` signature:

```python
def _score_candidates(
    config: ScrobbleRecsConfig,
    mode: RecommendationModeSettings,
    seeds: list[SeedArtist],
    seed_ids: set[int],
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
    feedback_profile: FeedbackProfile,
) -> list[_CandidateScore]:
```

Remove the existing `focused_tag_profile = _apply_tag_focus(tag_profile, mode)` line from `_score_candidates()`. The `tag_profile` argument is already focus-adjusted and feedback-adjusted before this function is called.

Inside the candidate loop, add:

```python
        if artist_id in feedback_profile.hidden_artist_ids:
            continue
```

Build candidate tags, feedback neighborhood score, feedback score, and knownness override:

```python
        candidate_tags = _candidate_tag_vector(artist_id, scoring_data)
        feedback_neighborhood_score = _feedback_neighborhood_score(
            artist_id,
            scoring_data,
            feedback_profile,
        )
        feedback_score = feedback_adjustment_for_candidate(
            feedback_profile,
            artist_id=artist_id,
            candidate_tags=candidate_tags,
            neighborhood_score=feedback_neighborhood_score,
        )
        if artist_id in feedback_profile.known_artist_ids:
            knownness = max(knownness, 1.0)
```

Adjust obviousness and raw score:

```python
        obviousness = (
            min(
                1.0,
                _obviousness(artist_id, tag_profile, scoring_data)
                + feedback_obviousness_for_candidate(
                    feedback_profile,
                    candidate_tags=candidate_tags,
                ),
            )
            if obviousness_enabled
            else 0.0
        )
        raw_score = _raw_score(
            weights,
            lastfm_score * mode.lastfm_similarity_multiplier,
            tag_score * mode.tag_similarity_multiplier,
            rediscovery * mode.rediscovery_multiplier,
            feedback_score,
        )
```

Persist `feedback_score=feedback_score` in `_CandidateScore`.

Modify `_raw_score()`:

```python
def _raw_score(
    weights: RecommendationWeightsConfig,
    lastfm_similarity_score: float,
    tag_similarity_score: float,
    rediscovery_score: float,
    feedback_score: float,
) -> float:
    return (
        weights.lastfm_similarity * lastfm_similarity_score
        + weights.tag_similarity * tag_similarity_score
        + weights.rediscovery * rediscovery_score
        + weights.feedback_model * feedback_score
    )
```

Add helper:

```python
def _feedback_neighborhood_score(
    artist_id: int,
    scoring_data: _ScoringData,
    feedback_profile: FeedbackProfile,
) -> float:
    score = 0.0
    for source_artist_id, similarity in scoring_data.feedback_similarity_by_target.get(
        artist_id,
        [],
    ):
        source_adjustment = feedback_profile.artist_adjustments.get(source_artist_id, 0.0)
        if source_adjustment > 0.0:
            score += 0.15 * similarity
        elif source_adjustment < 0.0:
            score -= 0.15 * similarity
    return score
```

Modify `_explanation()` to include feedback:

```python
        "feedback": {
            "score": score.feedback_score,
            "effects": _feedback_effect_labels(score.feedback_score),
        },
```

Add:

```python
def _feedback_effect_labels(feedback_score: float) -> list[str]:
    if feedback_score > 0.0:
        return ["artist", "tag", "neighborhood"]
    if feedback_score < 0.0:
        return ["suppression"]
    return []
```

Modify `_persist_run()` to insert `score.feedback_score` instead of `0.0` for `feedback_score`.

- [ ] **Step 4: Run scoring tests to verify they pass**

Run:

```bash
python -m pytest tests/test_recommend.py::test_generate_recommendations_applies_direct_feedback_scores tests/test_recommend.py::test_generate_recommendations_excludes_hidden_feedback_artist tests/test_recommend.py::test_generate_recommendations_uses_more_like_this_as_future_seed -v
```

Expected:

```text
3 passed
```

- [ ] **Step 5: Run recommendation tests**

Run:

```bash
python -m pytest tests/test_recommend.py -v
```

Expected:

```text
all recommendation tests pass
```

- [ ] **Step 6: Commit feedback-aware scoring**

Run:

```bash
git add src/scrobble_recs/models/recommend.py tests/test_recommend.py docs/superpowers/plans/2026-06-04-feedback-loop.md
git commit -m "feat: score recommendations with feedback"
```

---

### Task 4: Lightweight Current-Run Rerank

**Files:**
- Modify: `src/scrobble_recs/models/feedback.py`
- Modify: `tests/test_feedback.py`

- [ ] **Step 1: Add failing rerank tests**

Append these tests to `tests/test_feedback.py`:

```python
from scrobble_recs.models.feedback import rerank_recommendation_run


def test_rerank_recommendation_run_demotes_rejected_artist_and_updates_explanation(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000501"
    _insert_artist(conn, artist_id=40, name="First Artist")
    _insert_artist(conn, artist_id=41, name="Second Artist")
    _insert_run(conn, run_id=run_id)
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=40,
        rank=1,
        final_score=0.80,
        explanation={"summary": "First", "tags": [{"tag": "post-punk", "candidate_weight": 1.0}]},
    )
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=41,
        rank=2,
        final_score=0.70,
        explanation={"summary": "Second", "tags": [{"tag": "coldwave", "candidate_weight": 1.0}]},
    )
    record_feedback_event(
        conn,
        run_id=run_id,
        artist_id=40,
        action="reject",
        context={"tags": ["post-punk"]},
        feedback_id="00000000-0000-0000-0000-000000000601",
    )

    stats = rerank_recommendation_run(conn, run_id)

    assert stats.run_id == run_id
    assert stats.recommendations == 2
    rows = conn.execute(
        """
        SELECT rank, artist_id, feedback_score, explanation
        FROM recommendation
        WHERE run_id = ?
        ORDER BY rank
        """,
        [run_id],
    ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [(1, 41), (2, 40)]
    assert rows[1][2] < 0.0
    explanation = json.loads(rows[1][3])
    assert explanation["feedback"]["score"] == pytest.approx(rows[1][2])
    assert "suppression" in explanation["feedback"]["effects"]
    conn.close()


def test_rerank_recommendation_run_hides_current_too_obvious_artist(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000502"
    _insert_artist(conn, artist_id=50, name="Too Obvious Artist")
    _insert_artist(conn, artist_id=51, name="Remaining Artist")
    _insert_run(conn, run_id=run_id)
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=50,
        rank=1,
        final_score=0.80,
        explanation={"summary": "Too obvious", "tags": [{"tag": "post-punk", "candidate_weight": 1.0}]},
    )
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=51,
        rank=2,
        final_score=0.70,
        explanation={"summary": "Remaining", "tags": [{"tag": "coldwave", "candidate_weight": 1.0}]},
    )
    record_feedback_event(
        conn,
        run_id=run_id,
        artist_id=50,
        action="too_obvious",
        context={"tags": ["post-punk"]},
        feedback_id="00000000-0000-0000-0000-000000000602",
    )

    stats = rerank_recommendation_run(conn, run_id)

    assert stats.recommendations == 1
    assert _feedback_recommendation_names(conn, run_id) == ["Remaining Artist"]
    conn.close()


def _insert_recommendation(
    conn,
    *,
    run_id: str,
    artist_id: int,
    rank: int,
    final_score: float,
    explanation: dict[str, object],
) -> None:
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
        VALUES (?, ?, ?, ?, ?, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ?)
        """,
        [run_id, rank, artist_id, final_score, final_score, json.dumps(explanation)],
    )


def _feedback_recommendation_names(conn, run_id: str) -> list[str]:
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

- [ ] **Step 2: Run rerank tests to verify they fail**

Run:

```bash
python -m pytest tests/test_feedback.py::test_rerank_recommendation_run_demotes_rejected_artist_and_updates_explanation tests/test_feedback.py::test_rerank_recommendation_run_hides_current_too_obvious_artist -v
```

Expected:

```text
ImportError: cannot import name 'rerank_recommendation_run'
```

- [ ] **Step 3: Implement lightweight rerank**

Append to `src/scrobble_recs/models/feedback.py`:

```python
@dataclass(frozen=True)
class FeedbackRerankStats:
    run_id: str
    recommendations: int
    hidden: int


@dataclass(frozen=True)
class _RerankRow:
    artist_id: int
    final_score: float
    feedback_score: float
    adjusted_score: float
    explanation: dict[str, Any]
    hidden: bool


def rerank_recommendation_run(
    conn: DuckDBPyConnection,
    run_id: str,
) -> FeedbackRerankStats:
    rows = _load_rerank_rows(conn, run_id)
    if not rows:
        raise ValueError(f"recommendation run {run_id!r} has no recommendations")
    profile = load_feedback_profile(conn)
    adjusted = [_apply_feedback_to_rerank_row(row, profile) for row in rows]
    visible = sorted(
        (row for row in adjusted if not row.hidden),
        key=lambda row: (-row.adjusted_score, row.artist_id),
    )
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute("UPDATE recommendation SET rank = -rank WHERE run_id = ?", [run_id])
        hidden_count = len(adjusted) - len(visible)
        for rank, row in enumerate(visible, start=1):
            conn.execute(
                """
                UPDATE recommendation
                SET rank = ?,
                    final_score = ?,
                    feedback_score = ?,
                    explanation = ?
                WHERE run_id = ?
                  AND artist_id = ?
                """,
                [
                    rank,
                    row.adjusted_score,
                    row.feedback_score,
                    json.dumps(row.explanation, sort_keys=True),
                    run_id,
                    row.artist_id,
                ],
            )
        conn.execute(
            """
            DELETE FROM recommendation
            WHERE run_id = ?
              AND rank < 0
            """,
            [run_id],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return FeedbackRerankStats(
        run_id=run_id,
        recommendations=len(visible),
        hidden=hidden_count,
    )


def _load_rerank_rows(conn: DuckDBPyConnection, run_id: str) -> list[_RerankRow]:
    loaded = []
    for artist_id, final_score, feedback_score, explanation_json in conn.execute(
        """
        SELECT artist_id, final_score, coalesce(feedback_score, 0.0), explanation
        FROM recommendation
        WHERE run_id = ?
        ORDER BY rank
        """,
        [run_id],
    ).fetchall():
        loaded.append(
            _RerankRow(
                artist_id=int(artist_id),
                final_score=float(final_score or 0.0),
                feedback_score=float(feedback_score or 0.0),
                adjusted_score=float(final_score or 0.0),
                explanation=_parse_context(explanation_json),
                hidden=False,
            )
        )
    return loaded


def _apply_feedback_to_rerank_row(
    row: _RerankRow,
    profile: FeedbackProfile,
) -> _RerankRow:
    candidate_tags = _explanation_tag_weights(row.explanation)
    feedback_score = feedback_adjustment_for_candidate(
        profile,
        artist_id=row.artist_id,
        candidate_tags=candidate_tags,
    )
    hidden = row.artist_id in profile.hidden_artist_ids or _has_current_hide_feedback(
        profile,
        row.artist_id,
    )
    explanation = dict(row.explanation)
    explanation["feedback"] = {
        "score": feedback_score,
        "effects": _feedback_effect_labels(feedback_score, hidden=hidden),
    }
    return _RerankRow(
        artist_id=row.artist_id,
        final_score=row.final_score,
        feedback_score=feedback_score,
        adjusted_score=row.final_score + feedback_score,
        explanation=explanation,
        hidden=hidden,
    )


def _has_current_hide_feedback(profile: FeedbackProfile, artist_id: int) -> bool:
    return artist_id in profile.current_run_hidden_artist_ids


def _explanation_tag_weights(explanation: dict[str, Any]) -> dict[str, float]:
    tags = explanation.get("tags", [])
    if not isinstance(tags, list):
        return {}
    weights: dict[str, float] = {}
    for item in tags:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag", "")).strip().casefold()
        if not tag:
            continue
        raw_weight = item.get("candidate_weight", item.get("user_weight", 1.0))
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            weight = 1.0
        weights[tag] = max(weights.get(tag, 0.0), weight)
    return weights


def _feedback_effect_labels(feedback_score: float, *, hidden: bool = False) -> list[str]:
    effects: list[str] = []
    if hidden:
        effects.append("hidden")
    if feedback_score > 0.0:
        effects.append("boost")
    if feedback_score < 0.0:
        effects.append("suppression")
    return effects
```

- [ ] **Step 4: Run rerank tests to verify they pass**

Run:

```bash
python -m pytest tests/test_feedback.py::test_rerank_recommendation_run_demotes_rejected_artist_and_updates_explanation tests/test_feedback.py::test_rerank_recommendation_run_hides_current_too_obvious_artist -v
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Run all feedback tests**

Run:

```bash
python -m pytest tests/test_feedback.py -v
```

Expected:

```text
all feedback tests pass
```

- [ ] **Step 6: Commit lightweight rerank**

Run:

```bash
git add src/scrobble_recs/models/feedback.py tests/test_feedback.py docs/superpowers/plans/2026-06-04-feedback-loop.md
git commit -m "feat: rerank recommendations after feedback"
```

---

### Task 5: Feedback CLI

**Files:**
- Modify: `src/scrobble_recs/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing CLI tests**

Append these tests to `tests/test_cli.py`:

```python
def test_feedback_command_records_event_and_reranks(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    calls: list[tuple[str, int, str, dict[str, object]]] = []
    reranks: list[str] = []

    class FakeEvent:
        feedback_id = "00000000-0000-0000-0000-000000000701"
        action = "more_like_this"

    class FakeRerankStats:
        run_id = "00000000-0000-0000-0000-000000000001"
        recommendations = 4
        hidden = 1

    def fake_record(conn, *, run_id, artist_id, action, value=1.0, context=None, note=None):
        calls.append((run_id, artist_id, action, context or {}))
        return FakeEvent()

    def fake_rerank(conn, run_id):
        reranks.append(run_id)
        return FakeRerankStats()

    monkeypatch.setattr("scrobble_recs.cli.record_feedback_event", fake_record)
    monkeypatch.setattr("scrobble_recs.cli.rerank_recommendation_run", fake_rerank)

    result = runner.invoke(
        app,
        [
            "feedback",
            "--config",
            str(config_path),
            "--run-id",
            "00000000-0000-0000-0000-000000000001",
            "--artist-id",
            "42",
            "--action",
            "more_like_this",
            "--tag",
            "post-punk",
            "--tag",
            "coldwave",
            "--note",
            "good lead",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "00000000-0000-0000-0000-000000000001",
            42,
            "more_like_this",
            {"tags": ["post-punk", "coldwave"]},
        )
    ]
    assert reranks == ["00000000-0000-0000-0000-000000000001"]
    assert "Feedback recorded" in result.output
    assert "reranked recommendations: 4" in result.output
    assert "hidden recommendations: 1" in result.output


def test_feedback_command_can_skip_rerank(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    reranks: list[str] = []

    class FakeEvent:
        feedback_id = "00000000-0000-0000-0000-000000000702"
        action = "note"

    def fake_record(conn, *, run_id, artist_id, action, value=1.0, context=None, note=None):
        return FakeEvent()

    def fake_rerank(conn, run_id):
        reranks.append(run_id)
        raise AssertionError("rerank should not be called")

    monkeypatch.setattr("scrobble_recs.cli.record_feedback_event", fake_record)
    monkeypatch.setattr("scrobble_recs.cli.rerank_recommendation_run", fake_rerank)

    result = runner.invoke(
        app,
        [
            "feedback",
            "--config",
            str(config_path),
            "--run-id",
            "00000000-0000-0000-0000-000000000001",
            "--artist-id",
            "42",
            "--action",
            "note",
            "--no-rerank",
        ],
    )

    assert result.exit_code == 0
    assert reranks == []
    assert "Feedback recorded" in result.output
    assert "reranked recommendations" not in result.output
```

- [ ] **Step 2: Run CLI tests to verify they fail**

Run:

```bash
python -m pytest tests/test_cli.py::test_feedback_command_records_event_and_reranks tests/test_cli.py::test_feedback_command_can_skip_rerank -v
```

Expected:

```text
No such command 'feedback'
```

- [ ] **Step 3: Add feedback command**

Modify imports in `src/scrobble_recs/cli.py`:

```python
from scrobble_recs.models.feedback import record_feedback_event, rerank_recommendation_run
```

Add command:

```python
@app.command("feedback")
def feedback_command(
    config: Annotated[Path, typer.Option("--config", "-c", help="Path to JSON config file.")],
    run_id: Annotated[str, typer.Option("--run-id", help="Recommendation run UUID.")],
    artist_id: Annotated[int, typer.Option("--artist-id", help="Artist ID to annotate.")],
    action: Annotated[str, typer.Option("--action", help="Feedback action.")],
    value: Annotated[float, typer.Option("--value", help="Feedback strength multiplier.")] = 1.0,
    tag: Annotated[
        list[str] | None,
        typer.Option("--tag", help="Tag affected by this feedback. Can be repeated."),
    ] = None,
    note: Annotated[str | None, typer.Option("--note", help="Optional note.")] = None,
    rerank: Annotated[
        bool,
        typer.Option("--rerank/--no-rerank", help="Rerank the target run after recording."),
    ] = True,
) -> None:
    resolved_config = load_config(config)
    conn = initialize_database(resolved_config)
    try:
        try:
            event = record_feedback_event(
                conn,
                run_id=run_id,
                artist_id=artist_id,
                action=action,
                value=value,
                context={"tags": tag or []},
                note=note,
            )
            console.print("Feedback recorded")
            console.print(f"feedback id: {event.feedback_id}")
            console.print(f"action: {event.action}")
            if rerank:
                stats = rerank_recommendation_run(conn, run_id)
                console.print(f"reranked recommendations: {stats.recommendations}")
                console.print(f"hidden recommendations: {stats.hidden}")
        except ValueError as exc:
            console.print(f"Error: {exc}")
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()
```

- [ ] **Step 4: Run CLI tests to verify they pass**

Run:

```bash
python -m pytest tests/test_cli.py::test_feedback_command_records_event_and_reranks tests/test_cli.py::test_feedback_command_can_skip_rerank -v
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Run CLI test file**

Run:

```bash
python -m pytest tests/test_cli.py -v
```

Expected:

```text
all CLI tests pass
```

- [ ] **Step 6: Commit feedback CLI**

Run:

```bash
git add src/scrobble_recs/cli.py tests/test_cli.py docs/superpowers/plans/2026-06-04-feedback-loop.md
git commit -m "feat: add feedback command"
```

---

### Task 6: Docs And Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/implementation-log.md`

- [ ] **Step 1: Run focused feedback-loop tests**

Run:

```bash
python -m pytest tests/test_feedback.py tests/test_recommend.py tests/test_cli.py -v
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

Append this section after the recommender usage section in `README.md`:

```markdown
## Feedback Loop

Feedback is stored locally in DuckDB and can rerank an existing recommendation
run without calling live APIs:

```bash
scrobble-recs feedback \
  --config config.example.json \
  --run-id 00000000-0000-0000-0000-000000000001 \
  --artist-id 42 \
  --action more_like_this \
  --tag post-punk
```

Supported actions are `save`, `reject`, `already_know`, `too_obvious`,
`wrong_genre`, `more_like_this`, `less_like_this`, `hide_artist`, `hide_tag`,
`boost_tag`, `rediscovery_good`, `rediscovery_bad`, `opened_link`,
`played_externally`, and `note`.

By default the command records the event and reranks that persisted run. Use
`--no-rerank` to record feedback only. Future `recommend` runs also consume
feedback as artist boosts/suppression, tag boosts/suppression, and lightweight
similarity-neighborhood adjustments.
```

- [ ] **Step 7: Update implementation log**

Append this entry to `docs/implementation-log.md`:

```markdown
## 2026-06-04: Feedback loop

Changed:

- Added the local `feedback_event` table and indexes.
- Added feedback event validation and persistence for the spec-defined action set.
- Added derived feedback profiles for artist suppression/boosts, tag suppression/boosts, known-artist suppression, and feedback seed boosts.
- Added feedback-aware recommendation scoring, including persisted `feedback_score` values and explanation feedback metadata.
- Added lightweight current-run reranking after feedback without live API calls or full model retraining.
- Added `scrobble-recs feedback` to record an event and rerank the target run by default.

Verified:

- `python -m pytest tests/test_feedback.py tests/test_recommend.py tests/test_cli.py -v`
- `python -m pytest`
- `python -m ruff check .`
- `python -m ruff format --check .`
- `python -m mypy src`
```

- [ ] **Step 8: Commit docs and verification**

Run:

```bash
git add README.md docs/implementation-log.md docs/superpowers/plans/2026-06-04-feedback-loop.md
git commit -m "docs: record feedback loop"
```

---

## Self Review

Spec coverage:

- Section 8.8 is covered by Task 1 and Task 2 through the `feedback_event` table, allowed action validation, JSON context, notes, and immutable event writes.
- Section 13 feedback scoring is covered by Task 3 through direct artist adjustments, tag adjustments, known-artist suppression, hidden artists, feedback seeds, and `feedback_score` persistence.
- Section 17 is covered by Task 3 and Task 4: feedback affects artist-level suppression/boost, tag-level suppression/boost, similarity-neighborhood scoring, and immediate lightweight reranking without retraining.

Design consistency:

- The feedback table is a migration file loaded by the existing migration runner.
- `recommend` remains offline-first because it only reads local DuckDB tables and does not call API clients.
- The current-run rerank mutates persisted recommendation rows, which matches the TUI requirement to operate on persisted runs rather than live calls.
- Feedback explanations remain inspectable JSON instead of only formatted strings.

Testing quality:

- Tests use temporary DuckDB files and deterministic rows.
- Tests assert behavior: persisted events, validation, derived profile values, ranking changes, hidden artists, future seed behavior, current-run rerank, and CLI dispatch.
- No test uses network access or real API responses.

Execution notes:

- The worktree currently has user-owned changes in `.gitignore`, `artifacts/recenttracks-tallain-1780422643.csv`, `src/scrobble_recs/models/recommend.py`, `tests/test_recommend.py`, and `docs/superpowers/plans/2026-06-04-recommender-quality-pass.md`; do not revert or stage unrelated changes while executing this plan.
- The suggested migration filename assumes the existing SQL files are `001...`, `002...`, and `003...`; if `src/scrobble_recs/sql` already contains a later migration when executing, use the next sortable migration number while keeping the same SQL body.
