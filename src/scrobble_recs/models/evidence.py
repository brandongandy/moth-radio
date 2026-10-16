"""Provider-local evidence vocabulary, durable identity, and deterministic read adapters."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from math import isfinite
from typing import Literal, get_args
from uuid import NAMESPACE_URL, uuid5

from duckdb import DuckDBPyConnection

from scrobble_recs.models.profile_window import normalize_timestamp
from scrobble_recs.models.recommendation_query import QueryContext

EvidenceFamily = Literal["collaborative", "semantic", "catalog", "personal"]
RelationshipKind = Literal[
    "similar_artist",
    "artist_radio_membership",
    "artist_tag",
    "tag_membership",
    "catalog_relationship",
    "personal_feedback",
]
_RESERVED_LEGACY_KEYS = frozenset({"legacy", "confidence_projection", "stored_identity_confidence"})


@dataclass(frozen=True)
class EvidenceObservation:
    source: str
    family: EvidenceFamily
    relationship_kind: RelationshipKind
    source_artist_id: int | None
    target_artist_id: int
    raw_score: float | None
    source_rank: int | None
    provider_mode: str | None
    fetched_at: datetime | None
    identity_confidence: float
    evidence: dict[str, object]

    def __post_init__(self) -> None:
        if self.family not in get_args(EvidenceFamily):
            raise ValueError("unknown evidence family")
        if self.relationship_kind not in get_args(RelationshipKind):
            raise ValueError("unknown relationship kind")
        if self.raw_score is not None and not isfinite(self.raw_score):
            raise ValueError("raw_score must be finite")
        if self.source_rank is not None and (
            isinstance(self.source_rank, bool)
            or not isinstance(self.source_rank, int)
            or self.source_rank < 1
        ):
            raise ValueError("source_rank must be a positive integer or None")
        if not 0 <= self.identity_confidence <= 1:
            raise ValueError("identity_confidence must be between 0 and 1")
        if "strength" in self.evidence and self.evidence["strength"] not in (
            "strong",
            "moderate",
            "weak",
        ):
            raise ValueError("unknown provider-local strength")
        object.__setattr__(self, "evidence", deepcopy(self.evidence))
        if self.fetched_at is not None:
            object.__setattr__(self, "fetched_at", normalize_timestamp(self.fetched_at))


class _TranslatedLegacyObservation(EvidenceObservation):
    """Trusted in-memory origin, created only by the legacy loader (never stored JSON)."""


class _LocalFeedbackObservation(EvidenceObservation):
    """Local feedback may accompany legacy admission without asserting provider origin."""


def is_translated_legacy(observation: EvidenceObservation) -> bool:
    return isinstance(observation, _TranslatedLegacyObservation)


def is_legacy_compatible(observation: EvidenceObservation) -> bool:
    return isinstance(observation, (_TranslatedLegacyObservation, _LocalFeedbackObservation))


@dataclass(frozen=True)
class CandidateEvidence:
    artist_id: int
    observations: tuple[EvidenceObservation, ...]

    def __post_init__(self) -> None:
        if any(row.target_artist_id != self.artist_id for row in self.observations):
            raise ValueError("observation target must match candidate")
        object.__setattr__(self, "observations", deepcopy(tuple(self.observations)))


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    stage: int
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.stage not in range(5):
            raise ValueError("stage must be between 0 and 4")
        object.__setattr__(self, "reasons", tuple(self.reasons))


def observation_key(**relationship: object) -> str:
    """Canonical provider-specific identity; include mode/type/direction/event as applicable."""
    return json.dumps(relationship, sort_keys=True, separators=(",", ":"), allow_nan=False)


def observation_id(source: str, relationship_kind: str, key: str) -> str:
    natural_key = observation_key(source=source, relationship_kind=relationship_kind, key=key)
    return str(uuid5(NAMESPACE_URL, "scrobble-recs:evidence:" + natural_key))


def persist_evidence_observation(
    conn: DuckDBPyConnection,
    observation: EvidenceObservation,
    *,
    observation_key: str,
    cache_key: str | None = None,
    payload_hash: str | None = None,
    identity_status: str | None = None,
) -> str:
    if not observation_key:
        raise ValueError("observation_key must not be empty")
    if _RESERVED_LEGACY_KEYS.intersection(observation.evidence):
        raise ValueError("normalized evidence contains reserved translated-legacy provenance keys")
    identifier = observation_id(observation.source, observation.relationship_kind, observation_key)
    conn.execute(
        """INSERT INTO artist_evidence_observation VALUES
        (?::UUID, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?::JSON)
        ON CONFLICT (source, relationship_kind, observation_key) DO NOTHING""",
        [
            identifier,
            observation.source,
            observation.family,
            observation.relationship_kind,
            observation_key,
            observation.source_artist_id,
            observation.target_artist_id,
            observation.raw_score,
            observation.source_rank,
            observation.provider_mode,
            observation.fetched_at,
            cache_key,
            payload_hash,
            identity_status,
            observation.identity_confidence,
            json.dumps(observation.evidence, sort_keys=True, allow_nan=False),
        ],
    )
    return identifier


def observation_json(observation: EvidenceObservation) -> dict[str, object]:
    result = asdict(observation)
    result["fetched_at"] = observation.fetched_at.isoformat() if observation.fetched_at else None
    return result


def candidate_evidence_json(evidence: CandidateEvidence) -> dict[str, object]:
    return {
        "schema_version": "candidate-evidence-v1",
        "observations": [
            observation_json(row) for row in sorted(evidence.observations, key=_canonical_order)
        ],
    }


def _canonical_order(row: EvidenceObservation) -> tuple[str, str, int, int, str]:
    return (
        row.source,
        row.relationship_kind,
        row.source_artist_id or 0,
        row.target_artist_id,
        json.dumps(observation_json(row), sort_keys=True, separators=(",", ":"), allow_nan=False),
    )


@dataclass(frozen=True)
class LegacyEvidenceRows:
    """One captured provider view, retaining invalid historical numbers for diagnostics."""

    similarities: tuple[tuple[str, int, int, float | None, float, datetime | None, str | None], ...]
    artist_tags: tuple[tuple[str, int, str, float | None, float, datetime | None], ...]
    tag_memberships: tuple[tuple[str, int, str, int, float | None, datetime | None], ...]


def load_legacy_evidence_rows(
    conn: DuckDBPyConnection,
    *,
    cutoff: datetime | None,
) -> LegacyEvidenceRows:
    cutoff = normalize_timestamp(cutoff) if cutoff else None
    clause = "WHERE source = 'lastfm' AND (? IS NULL OR fetched_at <= ?)"
    return LegacyEvidenceRows(
        tuple(
            conn.execute(
                "SELECT source, source_artist_id, target_artist_id, source_score, "
                "normalized_score, fetched_at, evidence FROM artist_similarity_edge " + clause,
                [cutoff, cutoff],
            ).fetchall()
        ),
        tuple(
            conn.execute(
                "SELECT source, artist_id, tag, tag_count, normalized_weight, fetched_at "
                "FROM artist_tag " + clause,
                [cutoff, cutoff],
            ).fetchall()
        ),
        tuple(
            conn.execute(
                "SELECT source, artist_id, tag, tag_rank, tag_count, fetched_at "
                "FROM tag_top_artist " + clause,
                [cutoff, cutoff],
            ).fetchall()
        ),
    )


def load_candidate_evidence(
    conn: DuckDBPyConnection,
    *,
    context: QueryContext,
    legacy_rows: LegacyEvidenceRows | None = None,
) -> dict[int, CandidateEvidence]:
    cutoff = normalize_timestamp(context.feedback_cutoff) if context.feedback_cutoff else None
    legacy_rows = legacy_rows or load_legacy_evidence_rows(conn, cutoff=cutoff)
    identity = {
        int(row[0]): float(row[1])
        for row in conn.execute(
            "SELECT artist_id, coalesce(identity_confidence, .5) FROM artist"
        ).fetchall()
    }
    observations: list[EvidenceObservation] = []

    def legacy(
        source: str,
        family: EvidenceFamily,
        kind: RelationshipKind,
        source_id: int | None,
        target: int,
        score: float | None,
        rank: int | None,
        fetched: datetime | None,
        payload: dict[str, object],
    ) -> None:
        # Invalid historical provider numbers remain inspectable in legacy diagnostics,
        # but cannot become a valid normalized observation.
        if score is not None and not isfinite(score):
            return
        confidence = identity.get(target, 0.5)
        if source == "lastfm" and payload.get("legacy") is True and confidence == 0:
            payload = {
                **payload,
                "stored_identity_confidence": confidence,
                "confidence_projection": "legacy_zero_as_unknown",
            }
            confidence = 0.5
        observation_type = (
            _TranslatedLegacyObservation if source == "lastfm" else _LocalFeedbackObservation
        )
        observations.append(
            observation_type(
                source,
                family,
                kind,
                source_id,
                target,
                score,
                rank,
                None,
                fetched,
                confidence,
                payload,
            )
        )

    for source, source_id, target, raw, normalized, fetched, payload in legacy_rows.similarities:
        if not isfinite(normalized):
            continue
        legacy(
            source,
            "collaborative",
            "similar_artist",
            source_id,
            target,
            raw,
            None,
            fetched,
            {
                "legacy": True,
                "normalized_score": normalized,
                "provider_evidence": json.loads(payload) if payload else None,
            },
        )
    for source, target, tag, count, weight, fetched in legacy_rows.artist_tags:
        legacy(
            source,
            "semantic",
            "artist_tag",
            None,
            target,
            count,
            None,
            fetched,
            {"legacy": True, "tag": tag, "tag_count": count, "normalized_weight": weight},
        )
    for source, target, tag, rank, count, fetched in legacy_rows.tag_memberships:
        legacy(
            source,
            "semantic",
            "tag_membership",
            None,
            target,
            count,
            rank,
            fetched,
            {"legacy": True, "tag": tag, "tag_count": count},
        )
    for stored in conn.execute(
        "SELECT source, family, relationship_kind, source_artist_id, target_artist_id, raw_score, "
        "source_rank, provider_mode, fetched_at, identity_confidence, evidence "
        "FROM artist_evidence_observation WHERE (? IS NULL OR fetched_at <= ?)",
        [cutoff, cutoff],
    ).fetchall():
        observations.append(
            EvidenceObservation(
                source=stored[0],
                family=stored[1],
                relationship_kind=stored[2],
                source_artist_id=stored[3],
                target_artist_id=stored[4],
                raw_score=stored[5],
                source_rank=stored[6],
                provider_mode=stored[7],
                fetched_at=stored[8],
                identity_confidence=stored[9],
                evidence={
                    key: value
                    for key, value in json.loads(stored[10]).items()
                    if key not in _RESERVED_LEGACY_KEYS
                },
            )
        )
    for event_id, created, target, action, value, payload in conn.execute(
        "SELECT feedback_id::VARCHAR, created_at, artist_id, action, value, context "
        "FROM feedback_event WHERE artist_id IS NOT NULL AND (? IS NULL OR created_at < ?)",
        [cutoff, cutoff],
    ).fetchall():
        legacy(
            "local_feedback",
            "personal",
            "personal_feedback",
            None,
            target,
            value,
            None,
            created,
            {
                "feedback_id": event_id,
                "action": action,
                "context": json.loads(payload) if payload else {},
            },
        )
    grouped: dict[int, list[EvidenceObservation]] = {}
    for row in sorted(observations, key=_canonical_order):
        grouped.setdefault(row.target_artist_id, []).append(row)
    return {
        target: CandidateEvidence(target, tuple(rows)) for target, rows in sorted(grouped.items())
    }
