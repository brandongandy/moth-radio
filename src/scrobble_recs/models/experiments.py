"""Local blinded evaluation. Public display artifacts never carry variant provenance."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import isfinite
from random import Random
from typing import Any, Literal
from uuid import UUID, uuid4

from duckdb import DuckDBPyConnection

from scrobble_recs.models.profile_window import ListeningWindow, normalize_timestamp
from scrobble_recs.models.recommendation_query import (
    RecommendationRequest,
    TargetRelationshipSnapshot,
)

JudgmentLabel = Literal[
    "want_to_try", "strong_fit", "surprising_good", "already_known", "wrong_direction", "reject"
]
SideChoice = Literal["left", "right", "tie", "none"]
LABELS: tuple[JudgmentLabel, ...] = (
    "want_to_try",
    "strong_fit",
    "surprising_good",
    "already_known",
    "wrong_direction",
    "reject",
)


@dataclass(frozen=True)
class BlindedItem:
    artist_id: int
    artist: str
    position: int


@dataclass(frozen=True)
class EffectiveJudgment:
    preferred_side: SideChoice | None = None
    list_choice: SideChoice | None = None
    best_discovery_artist_id: int | None = None
    labels: dict[int, tuple[JudgmentLabel, ...]] = field(default_factory=dict)
    confidence: float | None = None
    note: str | None = None


@dataclass(frozen=True)
class BlindedExposure:
    exposure_id: str
    experiment_id: str
    task: str
    target_artist_id: int | None
    left: tuple[BlindedItem, ...]
    right: tuple[BlindedItem, ...]
    judgment: EffectiveJudgment | None
    completed: bool
    resumed: bool


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("invalid local identifier") from exc


def _recorded_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("recorded timestamps must be ISO strings or null")
    return normalize_timestamp(datetime.fromisoformat(value))


def _validate_relationship(value: dict[str, Any]) -> None:
    snapshot = TargetRelationshipSnapshot(**value)
    if snapshot.state not in ("unheard", "sampled", "established_favorite", "dormant_favorite"):
        raise ValueError("invalid target relationship state")
    for name in (
        "play_count",
        "unique_tracks",
        "unique_releases",
        "active_days",
        "active_months",
        "listening_span_days",
        "prior_recommendation_exposures",
    ):
        count = getattr(snapshot, name)
        if type(count) is not int or count < 0:
            raise ValueError("relationship counts must be nonnegative integers")
    for name in (
        "recency_weighted_play_count",
        "recent_trend",
        "replay_concentration",
        "positive_feedback",
        "negative_feedback",
    ):
        number = getattr(snapshot, name)
        if type(number) not in (int, float) or number < 0 or not isfinite(number):
            raise ValueError("relationship measurements must be finite and nonnegative")
    if snapshot.replay_concentration > 1:
        raise ValueError("replay concentration must not exceed one")
    first, last = (_recorded_timestamp(value.get(name)) for name in ("first_listen", "last_listen"))
    if first is not None and last is not None and first > last:
        raise ValueError("first listen must not follow last listen")
    if type(snapshot.low_depth_sample) is not bool:
        raise ValueError("low_depth_sample must be boolean")
    if snapshot.exposure_source not in ("run_appearance", "recorded_exposure"):
        raise ValueError("invalid relationship exposure source")
    if (
        not isinstance(snapshot.relationship_version, str)
        or not snapshot.relationship_version.strip()
    ):
        raise ValueError("relationship version must be a nonempty string")
    if not isinstance(snapshot.thresholds, dict) or any(
        not isinstance(key, str) or not key or type(threshold) is not int or threshold < 0
        for key, threshold in snapshot.thresholds.items()
    ):
        raise ValueError("relationship thresholds must map names to nonnegative integers")


def _canonical(value: Any, key: str = "") -> Any:
    if key in (
        "start",
        "end",
        "history_cutoff",
        "feedback_cutoff",
        "latest_at",
        "first_listen",
        "last_listen",
    ):
        timestamp = _recorded_timestamp(value)
        return timestamp.isoformat() if timestamp is not None else None
    if isinstance(value, dict):
        return {k: _canonical(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        values = [_canonical(v) for v in value]
        return sorted(values, key=_json) if key in ("taste_region_seeds", "regions") else values
    return value


def _run(conn: DuckDBPyConnection, run_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT query_kind, target_artist_id, mode, evidence_snapshot_id, query_context "
        "FROM recommendation_run WHERE run_id = ?",
        [_id(run_id)],
    ).fetchone()
    if row is None:
        raise ValueError("persisted run not found")
    try:
        context = json.loads(row[4])
        if not isinstance(context, dict):
            raise ValueError("missing recorded context")
        RecommendationRequest(context["query_kind"], context.get("target_artist_id"))
        for key, top in zip(
            ("query_kind", "target_artist_id", "mode", "evidence_snapshot_id"), row[:4], strict=True
        ):
            if context.get(key) != top:
                raise ValueError("contradictory recorded provenance")
        if not isinstance(row[3], str) or not row[3].strip():
            raise ValueError("missing evidence snapshot")
        required = (
            "history_window",
            "history_cutoff",
            "feedback_cutoff",
            "mode",
            "novelty_intent",
            "taste_region_seeds",
            "evidence_freshness",
        )
        semantic = {key: context[key] for key in required}
        if context["query_kind"] == "artist":
            if not isinstance(context.get("target_relationship"), dict):
                raise ValueError("missing target relationship")
            _validate_relationship(context["target_relationship"])
            semantic["target_relationship"] = context["target_relationship"]
        elif context.get("target_relationship") is not None:
            raise ValueError("general context cannot carry a target relationship")
        window = context["history_window"]
        ListeningWindow(
            datetime.fromisoformat(window["start"]), datetime.fromisoformat(window["end"])
        )
        for name in ("history_cutoff", "feedback_cutoff"):
            _recorded_timestamp(context[name])
        if context["novelty_intent"] not in ("discover", "mixed", "rediscover"):
            raise ValueError("invalid novelty intent")
        if not isinstance(context["mode"], str) or not context["mode"]:
            raise ValueError("missing query mode")
        seeds = context["taste_region_seeds"]
        if not isinstance(seeds, list):
            raise ValueError("invalid taste seeds")
        for seed in seeds:
            if (
                type(seed["artist_id"]) is not int
                or seed["artist_id"] < 1
                or type(seed["weight"]) not in (int, float)
                or not isfinite(seed["weight"])
                or seed["weight"] < 0
                or not isinstance(seed["regions"], list)
                or any(not isinstance(region, str) for region in seed["regions"])
            ):
                raise ValueError("invalid taste seed")
        if len({seed["artist_id"] for seed in seeds}) != len(seeds):
            raise ValueError("duplicate taste seeds")
        freshness = context["evidence_freshness"]
        if not isinstance(freshness, dict):
            raise ValueError("invalid evidence freshness")
        for value in freshness.values():
            if (
                not isinstance(value, dict)
                or type(value.get("row_count")) is not int
                or value["row_count"] < 0
                or "latest_at" not in value
            ):
                raise ValueError("invalid evidence freshness")
            _recorded_timestamp(value["latest_at"])
        canonical = _canonical(semantic)
        _json(context)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise ValueError("invalid or missing recorded query provenance") from exc
    return {
        "context": context,
        "semantic": canonical,
        "query_kind": row[0],
        "target_artist_id": row[1],
        "snapshot": row[3],
    }


def create_blinded_experiment(
    conn: DuckDBPyConnection,
    *,
    control_run_id: str,
    challenger_run_id: str,
    randomization_seed: int,
) -> str:
    """Freeze compatible persisted slates before any presentation or feedback mutation."""
    control_run_id, challenger_run_id = _id(control_run_id), _id(challenger_run_id)
    if control_run_id == challenger_run_id:
        raise ValueError("experiments require two distinct runs")
    if type(randomization_seed) is not int or not -(2**63) <= randomization_seed < 2**63:
        raise ValueError("seed must be a signed 64-bit integer")
    control, challenger = _run(conn, control_run_id), _run(conn, challenger_run_id)
    if any(
        control[key] != challenger[key]
        for key in ("semantic", "query_kind", "target_artist_id", "snapshot")
    ):
        raise ValueError("runs do not have compatible recorded query inputs")
    mapping = ["control", "challenger"]
    Random(randomization_seed).shuffle(mapping)
    side_variants = dict(zip(("left", "right"), mapping, strict=True))
    artists: dict[str, Any] = {}
    guards: dict[str, Any] = {}
    for variant, run in (("control", control_run_id), ("challenger", challenger_run_id)):
        rows = conn.execute(
            "SELECT r.artist_id, r.rank, a.canonical_name, r.knownness, r.identity_confidence, "
            "r.explanation FROM recommendation r LEFT JOIN artist a USING (artist_id) "
            "WHERE r.run_id = ? AND coalesce(r.score_band, '') != 'candidate_only' "
            "ORDER BY r.rank, r.artist_id",
            [run],
        ).fetchall()
        for artist_id, rank, name, *_ in rows:
            item = artists.setdefault(
                str(artist_id),
                {
                    "artist_id": int(artist_id),
                    "artist": str(name or f"Artist {artist_id}"),
                    "memberships": [],
                },
            )
            membership = next((m for m in item["memberships"] if m["variant"] == variant), None)
            if membership is None:
                item["memberships"].append(
                    {"variant": variant, "run_id": run, "ranks": [int(rank)]}
                )
            else:
                membership["ranks"].append(int(rank))
        guards[variant] = _guardrails(conn, run, rows)
    queues: dict[str, list[int]] = {"left": [], "right": []}
    for artist in artists.values():
        variants = sorted({m["variant"] for m in artist["memberships"]})
        digest = sha256(f"blinded-v1:{randomization_seed}:{artist['artist_id']}".encode()).digest()
        owner = variants[int.from_bytes(digest[:8], "big") % len(variants)]
        side = next(side for side, variant in side_variants.items() if variant == owner)
        artist["display_side"] = side
        queues[side].append(artist["artist_id"])
    for side, queue in queues.items():
        variant = side_variants[side]
        queue.sort(
            key=lambda aid: (
                min(
                    rank
                    for m in artists[str(aid)]["memberships"]
                    if m["variant"] == variant
                    for rank in m["ranks"]
                ),
                aid,
            )
        )
    definition = {
        "version": "blinded-v1",
        "semantic_context": control["semantic"],
        "side_variants": side_variants,
        "artists": artists,
        "queues": queues,
        "guardrails": guards,
        "attribution_policy": "first-exposure; shared-equal-fraction",
        "source_contexts": {"control": control["context"], "challenger": challenger["context"]},
        "source_run_ids": {"control": control_run_id, "challenger": challenger_run_id},
        "source_artifacts": {
            variant: _run_artifact(conn, run)
            for variant, run in (("control", control_run_id), ("challenger", challenger_run_id))
        },
        "target_artist_id": control["target_artist_id"],
    }
    experiment_id = str(uuid4())
    conn.execute(
        "INSERT INTO recommendation_experiment (experiment_id, created_at, task, control_run_id, "
        "challenger_run_id, evidence_snapshot_id, randomization_seed, definition, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            experiment_id,
            _now(),
            control["query_kind"],
            control_run_id,
            challenger_run_id,
            control["snapshot"],
            randomization_seed,
            _json(definition),
            "active" if artists else "completed",
        ],
    )
    return experiment_id


def _metric(values: list[float], total: int, source: str) -> dict[str, Any]:
    return {
        "value": sum(values) / len(values) if values else None,
        "denominator": len(values),
        "total": total,
        "available": bool(values),
        "coverage": len(values) / total if total else None,
        "source": source,
        "reason": None if values else "historical facts unavailable",
    }


def _run_artifact(conn: DuckDBPyConnection, run_id: str) -> Any:
    row = conn.execute(
        "SELECT seed_summary FROM recommendation_run WHERE run_id = ?", [run_id]
    ).fetchone()
    assert row is not None
    return json.loads(row[0] or "{}").get("evaluation_artifact")


def _guardrails(
    conn: DuckDBPyConnection, run_id: str, rows: list[tuple[Any, ...]]
) -> dict[str, Any]:
    # Only facts persisted with selected recommendations; never mutable catalog estimates.
    from scrobble_recs.models.evaluation_metrics import (
        _primary_tag,
        _source_memberships,
        _tag_entropy,
        provider_name,
    )

    def values(index: int) -> list[float]:
        return [
            float(row[index])
            for row in rows
            if isinstance(row[index], (int, float)) and isfinite(row[index])
        ]

    tags = [tag for row in rows if (tag := _primary_tag(row[5])) is not None]
    diversity = _metric([], len(rows), "persisted primary-tag entropy, bits")
    if tags:
        diversity.update(
            value=_tag_entropy(tags),
            denominator=len(tags),
            available=True,
            coverage=len(tags) / len(rows),
            reason=None,
        )
    selected = {int(row[0]) for row in rows}
    providers: dict[str, set[int]] = {}
    for source, artists in _source_memberships(conn, run_id).items():
        if provider := provider_name(source):
            providers.setdefault(provider, set()).update(artists & selected)
    denominator = sum(len(artists) for artists in providers.values())
    concentration = _metric([], len(rows), "persisted candidate provider memberships, max share")
    if denominator:
        covered = len(set().union(*providers.values()))
        concentration.update(
            value=max(map(len, providers.values())) / denominator,
            denominator=denominator,
            available=True,
            coverage=covered / len(rows),
            reason=None,
            artist_coverage=covered,
        )
    return {
        "novelty": _metric(
            [1 - value for value in values(3)], len(rows), "persisted slate 1-knownness"
        ),
        "identity": _metric(values(4), len(rows), "persisted slate identity confidence"),
        "popularity": _metric([], len(rows), "no historical popularity in selected artifact"),
        "diversity": diversity,
        "provider_concentration": concentration,
    }


def _experiment(conn: DuckDBPyConnection, experiment_id: str) -> tuple[str, dict[str, Any]]:
    row = conn.execute(
        "SELECT task, definition FROM recommendation_experiment WHERE experiment_id = ?",
        [_id(experiment_id)],
    ).fetchone()
    if row is None:
        raise ValueError("experiment not found")
    return str(row[0]), json.loads(row[1])


def _effective(conn: DuckDBPyConnection, exposure_id: str) -> EffectiveJudgment | None:
    row = conn.execute(
        "SELECT preferred_side, list_choice, best_discovery_artist_id, labels, confidence, note "
        "FROM recommendation_judgment WHERE exposure_id = ? ORDER BY created_at DESC, "
        "judgment_id DESC LIMIT 1",
        [exposure_id],
    ).fetchone()
    if row is None:
        return None
    return EffectiveJudgment(
        row[0],
        row[1],
        row[2],
        {int(k): tuple(v) for k, v in json.loads(row[3]).items()},
        row[4],
        row[5],
    )


def _public(conn: DuckDBPyConnection, row: tuple[Any, ...], *, resumed: bool) -> BlindedExposure:
    identity, experiment, context_json, shown_json, completed = row
    context, shown = json.loads(context_json), json.loads(shown_json)
    return BlindedExposure(
        str(identity),
        str(experiment),
        context["task"],
        context["target_artist_id"],
        _items(shown, "left"),
        _items(shown, "right"),
        _effective(conn, str(identity)),
        completed is not None,
        resumed,
    )


def _items(shown: list[dict[str, Any]], side: str) -> tuple[BlindedItem, ...]:
    return tuple(
        BlindedItem(item["artist_id"], item["artist"], item["position"])
        for item in shown
        if item["side"] == side
    )


def next_blinded_exposure(
    conn: DuckDBPyConnection, *, experiment_id: str, slate_size: int = 10
) -> BlindedExposure | None:
    """Persist before display; pending pages always replay their stored order."""
    if type(slate_size) is not int or slate_size < 1:
        raise ValueError("slate_size must be a positive integer")
    experiment_id = _id(experiment_id)
    task, definition = _experiment(conn, experiment_id)
    pending = conn.execute(
        "SELECT exposure_id, experiment_id, context, shown_items, completed_at "
        "FROM recommendation_exposure WHERE experiment_id = ? AND completed_at IS NULL "
        "ORDER BY created_at, exposure_id LIMIT 1",
        [experiment_id],
    ).fetchone()
    if pending is not None:
        return _public(conn, pending, resumed=True)
    seen = {
        item["artist_id"]
        for row in conn.execute(
            "SELECT shown_items FROM recommendation_exposure WHERE experiment_id = ?",
            [experiment_id],
        ).fetchall()
        for item in json.loads(row[0])
    }
    shown = []
    for side in ("left", "right"):
        page = [aid for aid in definition["queues"][side] if aid not in seen][:slate_size]
        for position, aid in enumerate(page, 1):
            artist = definition["artists"][str(aid)]
            shown.append(
                {"artist_id": aid, "artist": artist["artist"], "side": side, "position": position}
            )
    if not shown:
        return None
    context = _json({"task": task, "target_artist_id": definition["target_artist_id"]})
    attribution = {
        str(item["artist_id"]): definition["artists"][str(item["artist_id"])]["memberships"]
        for item in shown
    }
    identity, timestamp, encoded = str(uuid4()), _now(), _json(shown)
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "INSERT INTO recommendation_exposure (exposure_id, experiment_id, created_at, "
            "context, shown_items, attribution) VALUES (?, ?, ?, ?, ?, ?)",
            [identity, experiment_id, timestamp, context, encoded, _json(attribution)],
        )
        conn.execute(
            "UPDATE recommendation_experiment SET status = 'active' WHERE experiment_id = ?",
            [experiment_id],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _public(conn, (identity, experiment_id, context, encoded, None), resumed=False)


def record_judgment(
    conn: DuckDBPyConnection,
    *,
    exposure_id: str,
    preferred_side: SideChoice | None = None,
    list_choice: SideChoice | None = None,
    best_discovery_artist_id: int | None = None,
    labels: Mapping[int, Sequence[JudgmentLabel]] | None = None,
    confidence: float | None = None,
    note: str | None = None,
) -> str:
    """Append a complete replacement snapshot; nulls/omissions clear previous answers."""
    exposure_id = _id(exposure_id)
    row = conn.execute(
        "SELECT shown_items, completed_at, experiment_id FROM recommendation_exposure "
        "WHERE exposure_id = ?",
        [exposure_id],
    ).fetchone()
    if row is None:
        raise ValueError("exposure not found")
    shown = {item["artist_id"] for item in json.loads(row[0])}
    for side in (preferred_side, list_choice):
        if side is not None and side not in ("left", "right", "tie", "none"):
            raise ValueError("invalid side choice")

    def validate_artist(aid: int) -> None:
        if type(aid) is not int or aid < 1 or aid not in shown:
            raise ValueError("artist must be a displayed positive integer identity")

    if best_discovery_artist_id is not None:
        validate_artist(best_discovery_artist_id)
    normalized: dict[str, list[str]] = {}
    if labels is not None and not isinstance(labels, Mapping):
        raise ValueError("labels must map displayed artists to label sequences")
    for aid, artist_labels in (labels or {}).items():
        validate_artist(aid)
        if (
            not isinstance(artist_labels, Sequence)
            or isinstance(artist_labels, (str, bytes))
            or any(not isinstance(label, str) or label not in LABELS for label in artist_labels)
        ):
            raise ValueError("invalid judgment labels")
        normalized[str(aid)] = sorted(set(artist_labels))
    if confidence is not None and (
        type(confidence) not in (float, int) or not 0 <= confidence <= 1 or not isfinite(confidence)
    ):
        raise ValueError("confidence must be finite and between zero and one")
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be text")
    latest = conn.execute(
        "SELECT max(created_at) FROM recommendation_judgment WHERE exposure_id = ?", [exposure_id]
    ).fetchone()
    timestamp = _now()
    if latest is not None and latest[0] is not None:
        timestamp = max(timestamp, latest[0] + timedelta(microseconds=1))
    completed_at = (row[1] or timestamp) if list_choice is not None else None
    judgment_id = str(uuid4())
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "INSERT INTO recommendation_judgment VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                judgment_id,
                exposure_id,
                timestamp,
                preferred_side,
                list_choice,
                best_discovery_artist_id,
                _json(normalized),
                confidence,
                note,
            ],
        )
        conn.execute(
            "UPDATE recommendation_exposure SET completed_at = ? WHERE exposure_id = ?",
            [completed_at, exposure_id],
        )
        _refresh_status(conn, str(row[2]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return judgment_id


def _refresh_status(conn: DuckDBPyConnection, experiment_id: str) -> None:
    _, definition = _experiment(conn, experiment_id)
    rows = conn.execute(
        "SELECT shown_items, completed_at FROM recommendation_exposure WHERE experiment_id = ?",
        [experiment_id],
    ).fetchall()
    seen = {item["artist_id"] for row in rows for item in json.loads(row[0])}
    complete = len(seen) == len(definition["artists"]) and all(row[1] is not None for row in rows)
    conn.execute(
        "UPDATE recommendation_experiment SET status = ? WHERE experiment_id = ?",
        ["completed" if complete else "active", experiment_id],
    )


@dataclass(frozen=True)
class ExperimentReport:
    experiment_id: str
    task: str
    evidence_snapshot_id: str
    outcome_cutoff: str
    definition: dict[str, Any]
    exposure_counts: dict[str, int]
    completion_rate: dict[str, Any]
    list_choices: dict[str, int]
    preferred_sides: dict[str, int]
    best_discovery: dict[str, float]
    labels: dict[str, dict[str, float]]
    raw_shared_facts: dict[str, Any]
    downstream: dict[str, Any]
    guardrail_deltas: dict[str, Any]
    assessment: str = "incomplete_guardrails; not_promoted"
    judgment_basis: str = "current effective snapshots; outcome_cutoff bounds listening only"
    promotion_evidence: dict[str, Any] = field(default_factory=dict)


def _shares(memberships: list[dict[str, Any]]) -> dict[str, float]:
    variants = {membership["variant"] for membership in memberships}
    return {variant: 1 / len(variants) for variant in sorted(variants)}


def _choice_counts() -> dict[str, int]:
    return dict(control=0, challenger=0, tie=0, abstentions=0, unanswered=0, denominator=0)


def _count_choice(
    counts: dict[str, int], choice: SideChoice | None, side_variants: dict[str, str]
) -> None:
    if choice is None:
        counts["unanswered"] += 1
    elif choice == "none":
        counts["abstentions"] += 1
    else:
        counts["denominator"] += 1
        counts["tie" if choice == "tie" else side_variants[choice]] += 1


def _replace_outcomes(
    conn: DuckDBPyConnection, experiment_id: str, outcomes: list[list[Any]]
) -> None:
    conn.execute(
        "DELETE FROM recommendation_downstream_outcome WHERE exposure_id IN "
        "(SELECT exposure_id FROM recommendation_exposure WHERE experiment_id = ?)",
        [experiment_id],
    )
    if outcomes:
        conn.executemany(
            "INSERT INTO recommendation_downstream_outcome VALUES (?, ?, ?, ?, ?, ?)", outcomes
        )


def experiment_report(
    conn: DuckDBPyConnection, experiment_id: str, *, outcome_cutoff: datetime
) -> ExperimentReport:
    """Reveal frozen attribution and recompute local post-exposure listening atomically."""
    if not isinstance(outcome_cutoff, datetime):
        raise ValueError("outcome_cutoff must be a timestamp")
    cutoff = normalize_timestamp(outcome_cutoff)
    experiment_id = _id(experiment_id)
    task, definition = _experiment(conn, experiment_id)
    snapshot = conn.execute(
        "SELECT evidence_snapshot_id FROM recommendation_experiment WHERE experiment_id = ?",
        [experiment_id],
    ).fetchone()
    assert snapshot is not None
    rows = conn.execute(
        "SELECT exposure_id, created_at, shown_items, attribution, completed_at "
        "FROM recommendation_exposure WHERE experiment_id = ? "
        "ORDER BY created_at, exposure_id",
        [experiment_id],
    ).fetchall()
    counts = dict(total=len(rows), completed=0, partial=0, unjudged=0)
    choices, preferred = _choice_counts(), _choice_counts()
    best = dict(control=0.0, challenger=0.0, denominator=0.0)
    labels: dict[str, dict[str, float]] = {
        label: dict(control=0.0, challenger=0.0) for label in LABELS
    }
    raw_shared: dict[str, Any] = {"best_discovery": 0, "labels": dict.fromkeys(LABELS, 0)}
    downstream: dict[str, Any] = {
        variant: dict(
            denominator=0.0, adopted=0.0, repeated=0.0, play_count=0.0, artist_active_days=0.0
        )
        for variant in ("control", "challenger")
    }
    owners: set[int] = set()
    unique_shared = 0
    outcomes: list[list[Any]] = []
    promotion_exposures = []
    for exposure_id, timestamp, shown_json, attribution_json, completed_at in rows:
        identity = str(exposure_id)
        attribution = json.loads(attribution_json)
        judgment = _effective(conn, identity)
        counts[
            "completed"
            if completed_at is not None
            and judgment is not None
            and judgment.list_choice is not None
            else "partial"
            if judgment is not None
            else "unjudged"
        ] += 1
        _count_choice(
            choices, judgment.list_choice if judgment else None, definition["side_variants"]
        )
        _count_choice(
            preferred, judgment.preferred_side if judgment else None, definition["side_variants"]
        )
        if judgment is not None:
            aid = judgment.best_discovery_artist_id
            if aid is not None:
                shares = _shares(attribution[str(aid)])
                best["denominator"] += 1
                raw_shared["best_discovery"] += int(len(shares) == 2)
                for variant, share in shares.items():
                    best[variant] += share
            for aid, artist_labels in judgment.labels.items():
                shares = _shares(attribution[str(aid)])
                for label in set(artist_labels):
                    raw_shared["labels"][label] += int(len(shares) == 2)
                    for variant, share in shares.items():
                        labels[label][variant] += share
        # Shown rows are counted once, even if imported/corrupted artifacts repeat positions.
        shown = {int(item["artist_id"]) for item in json.loads(shown_json)}
        effective_record = conn.execute(
            "SELECT judgment_id, created_at FROM recommendation_judgment WHERE exposure_id = ? "
            "ORDER BY created_at DESC, judgment_id DESC LIMIT 1",
            [exposure_id],
        ).fetchone()
        promotion_exposures.append(
            {
                "exposure_id": identity,
                "shown_artist_ids": sorted(shown),
                "effective_list_choice": judgment.list_choice if judgment else None,
                "preferred_side": judgment.preferred_side if judgment else None,
                "judgment_id": str(effective_record[0]) if effective_record else None,
                "judged_at": effective_record[1].isoformat() if effective_record else None,
                "completed_at": completed_at.isoformat() if completed_at is not None else None,
            }
        )
        for aid in sorted(shown):
            first, plays, days = None, 0, 0
            shares = _shares(attribution[str(aid)])
            if aid not in owners:
                owners.add(aid)
                unique_shared += int(len(shares) == 2)
                if timestamp < cutoff:
                    result = conn.execute(
                        "SELECT min(s.played_at), count(*), "
                        "count(DISTINCT cast(s.played_at AS DATE)) "
                        "FROM scrobble s WHERE coalesce(s.is_duplicate, false) = false "
                        "AND s.played_at > ? AND s.played_at < ? AND s.artist_key IN "
                        "(SELECT artist_key FROM artist WHERE artist_id = ?)",
                        [timestamp, cutoff, aid],
                    ).fetchone()
                    assert result is not None
                    first, plays, days = result[0], int(result[1]), int(result[2])
                    for variant, share in shares.items():
                        values = downstream[variant]
                        values["denominator"] += share
                        values["adopted"] += share * int(plays > 0)
                        values["repeated"] += share * int(plays >= 2)
                        values["play_count"] += share * plays
                        values["artist_active_days"] += share * days
            outcomes.append([identity, aid, first, plays, days, cutoff])
    for variant in ("control", "challenger"):
        values = downstream[variant]
        denominator = values["denominator"]
        values["adoption_rate"] = values["adopted"] / denominator if denominator else None
        values["repeat_rate"] = values["repeated"] / denominator if denominator else None
    downstream["unique_exposed_artists"] = len(owners)
    downstream["shared_artists"] = unique_shared
    downstream["basis"] = (
        "post-exposure listening; first-exposure attribution, not causal discovery"
    )
    deltas = {}
    for name, control in definition["guardrails"]["control"].items():
        challenger = definition["guardrails"]["challenger"][name]
        available = control["available"] and challenger["available"]
        deltas[name] = {
            "value": challenger["value"] - control["value"] if available else None,
            "available": available,
            "control": control,
            "challenger": challenger,
            "source": "frozen persisted run artifacts",
            "reason": None if available else "historical facts unavailable",
        }
    report = ExperimentReport(
        experiment_id,
        task,
        str(snapshot[0]),
        cutoff.isoformat(),
        definition,
        counts,
        {"value": counts["completed"] / len(rows) if rows else None, "denominator": len(rows)},
        choices,
        preferred,
        best,
        labels,
        raw_shared,
        downstream,
        deltas,
        promotion_evidence={"exposures": promotion_exposures, "exposed_artist_ids": sorted(owners)},
    )
    conn.execute("BEGIN TRANSACTION")
    try:
        _replace_outcomes(conn, experiment_id, outcomes)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return report
