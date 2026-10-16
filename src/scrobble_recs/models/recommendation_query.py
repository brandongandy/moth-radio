"""Immutable, locally persisted inputs identifying a recommendation query."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal

from duckdb import DuckDBPyConnection

from scrobble_recs.models.profile_window import ListeningWindow, normalize_timestamp

QueryKind = Literal["general", "artist"]
NoveltyIntent = Literal["discover", "mixed", "rediscover"]
EvidenceFreshness = tuple[tuple[str, int, datetime | None], ...]


@dataclass(frozen=True)
class RecommendationRequest:
    query_kind: QueryKind = "general"
    target_artist_id: int | None = None

    def __post_init__(self) -> None:
        if self.query_kind not in ("general", "artist"):
            raise ValueError("query_kind must be 'general' or 'artist'")
        if self.query_kind == "general" and self.target_artist_id is not None:
            raise ValueError("general queries cannot have a target artist")
        if self.query_kind == "artist" and (
            not isinstance(self.target_artist_id, int)
            or isinstance(self.target_artist_id, bool)
            or self.target_artist_id < 1
        ):
            raise ValueError("artist queries require a positive target_artist_id")


@dataclass(frozen=True)
class TasteRegionSeed:
    artist_id: int
    weight: float
    regions: tuple[str, ...]


@dataclass(frozen=True)
class TargetRelationshipSnapshot:
    state: Literal["unheard", "sampled", "established_favorite", "dormant_favorite"]
    play_count: int
    recency_weighted_play_count: float
    unique_tracks: int
    unique_releases: int
    active_days: int
    active_months: int
    first_listen: datetime | None
    last_listen: datetime | None
    listening_span_days: int
    recent_trend: float
    replay_concentration: float
    positive_feedback: float
    negative_feedback: float
    prior_recommendation_exposures: int


@dataclass(frozen=True)
class RecommendationVersions:
    query_schema: str = "query-v1"
    feature_schema: str = "legacy-features-v1"
    fusion: str = "legacy-direct-v1"
    ranker: str = "legacy-v1"
    slate_policy: str = "legacy-mmr-v1"


@dataclass(frozen=True, kw_only=True)
class QueryContext:
    query_kind: QueryKind
    history_window: ListeningWindow
    mode: str
    novelty_intent: NoveltyIntent
    taste_region_seeds: tuple[TasteRegionSeed, ...]
    evidence_snapshot_id: str
    config_hash: str
    history_cutoff: datetime | None = None
    feedback_cutoff: datetime | None = None
    evidence_freshness: EvidenceFreshness = ()
    versions: RecommendationVersions = RecommendationVersions()
    code_version: str | None = None
    target_artist_id: int | None = None
    target_relationship: TargetRelationshipSnapshot | None = None

    def __post_init__(self) -> None:
        RecommendationRequest(self.query_kind, self.target_artist_id)

    def to_json_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["taste_region_seeds"] = [
            {"artist_id": seed.artist_id, "weight": seed.weight, "regions": sorted(seed.regions)}
            for seed in sorted(self.taste_region_seeds, key=lambda seed: seed.artist_id)
        ]
        payload["evidence_freshness"] = _freshness_json(self.evidence_freshness)
        if self.query_kind == "general":
            payload.pop("target_artist_id")
            payload.pop("target_relationship")
        return {key: _json_value(value) for key, value in payload.items()}


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return normalize_timestamp(value).isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def load_evidence_freshness(
    conn: DuckDBPyConnection, *, historical_cutoff: datetime | None
) -> EvidenceFreshness:
    """Summarize persisted evidence, including missing sources as zero/null."""
    sources = (
        ("artist_similarity_edge", "fetched_at"),
        ("artist_tag", "fetched_at"),
        ("tag_top_artist", "fetched_at"),
        ("artist_identity_resolution", "resolved_at"),
    )
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    summary: list[tuple[str, int, datetime | None]] = []
    for table, timestamp in sources:
        count, latest = 0, None
        if table in tables:
            clause = f"WHERE {timestamp} <= ?" if historical_cutoff is not None else ""
            params = [normalize_timestamp(historical_cutoff)] if historical_cutoff else []
            row = conn.execute(
                f"SELECT count(*), max({timestamp}) FROM {table} {clause}", params
            ).fetchone()
            if row is not None:
                count, latest = int(row[0]), row[1]
        summary.append((table, count, latest))
    return tuple(summary)


def _freshness_json(freshness: EvidenceFreshness) -> dict[str, object]:
    return {
        table: {"row_count": count, "latest_at": _json_value(latest)}
        for table, count, latest in freshness
    }


def build_evidence_snapshot_id(
    conn: DuckDBPyConnection, *, historical_cutoff: datetime | None
) -> str:
    """Compatibility wrapper; run construction should reuse one captured snapshot."""
    return capture_evidence_snapshot(conn, historical_cutoff=historical_cutoff).snapshot_id


@dataclass(frozen=True)
class EvidenceSnapshot:
    snapshot_id: str
    freshness: EvidenceFreshness


def capture_evidence_snapshot(
    conn: DuckDBPyConnection,
    *,
    historical_cutoff: datetime | None,
) -> EvidenceSnapshot:
    """Capture source summaries once, including canonical normalized/feedback identities."""
    freshness = load_evidence_freshness(conn, historical_cutoff=historical_cutoff)
    payload = _freshness_json(freshness)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    cutoff = normalize_timestamp(historical_cutoff) if historical_cutoff else None
    additions: list[tuple[str, int, datetime | None]] = []
    for table, timestamp, fields, operator in (
        (
            "artist_evidence_observation",
            "fetched_at",
            "observation_id::VARCHAR, source, family, relationship_kind, observation_key, "
            "source_artist_id, target_artist_id, raw_score, source_rank, provider_mode, "
            "cache_key, payload_hash, identity_status, identity_confidence, evidence",
            "<=",
        ),
        (
            "feedback_event",
            "created_at",
            "feedback_id::VARCHAR, artist_id, action, value, context",
            "<",
        ),
    ):
        if table not in tables:
            continue
        rows = conn.execute(
            f"SELECT {timestamp}, {fields} FROM {table} "
            f"WHERE (? IS NULL OR {timestamp} {operator} ?)",
            [cutoff, cutoff],
        ).fetchall()
        latest = max((row[0] for row in rows if row[0] is not None), default=None)
        canonical = sorted(
            json.dumps(
                [
                    _json_value(row[0]),
                    *row[1:-1],
                    json.loads(row[-1]) if row[-1] is not None else None,
                ],
                sort_keys=True,
                separators=(",", ":"),
                default=str,
                allow_nan=False,
            )
            for row in rows
        )
        digest = sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()
        payload[table] = {
            "row_count": len(rows),
            "latest_at": _json_value(latest),
            "payload_hash": digest,
        }
        additions.append((table, len(rows), latest))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return EvidenceSnapshot(sha256(encoded.encode()).hexdigest(), freshness + tuple(additions))
