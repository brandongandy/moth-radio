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
        value = 1.0 if value_raw is None else float(value_raw)
        context = _parse_context(context_raw)
        _apply_artist_feedback(profile, artist_id=artist_id, action=action, value=value)
        for tag in _context_tags(context):
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
    return sorted({str(tag).strip().casefold() for tag in raw_tags if str(tag).strip()})
