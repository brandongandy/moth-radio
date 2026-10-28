"""Persisted Last.fm/tag facts and lossless legacy retrieval contracts."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from math import isfinite, log, sqrt
from typing import Any, Literal, TypeAlias

from duckdb import DuckDBPyConnection

from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.evidence import (
    AdmissionDecision,
    CandidateEvidence,
    EvidenceFamily,
    EvidenceObservation,
    LegacyEvidenceRows,
    is_legacy_compatible,
    is_translated_legacy,
    load_candidate_evidence,
    load_legacy_evidence_rows,
)
from scrobble_recs.models.feedback import (
    FeedbackProfile,
    apply_feedback_to_tag_profile,
    load_feedback_profile,
)
from scrobble_recs.models.fusion import FusedCandidate, SourceCandidate, fuse_source_results
from scrobble_recs.models.knownness import ArtistListeningInput, compute_artist_listening_scores
from scrobble_recs.models.modes import RecommendationModeSettings, resolve_recommendation_mode
from scrobble_recs.models.profile import SeedArtist
from scrobble_recs.models.recommendation_query import QueryContext
from scrobble_recs.models.seed_weighting import SeedIdfWeights, compute_seed_idf_weights
from scrobble_recs.models.walk import WalkResult, build_walk_result

RecommendationProgressPhase: TypeAlias = Literal[
    "refresh_listening",
    "select_seeds",
    "build_profiles",
    "load_candidates",
    "score_candidates",
    "rerank_diversity",
    "build_explanations",
    "persist_candidates",
    "persist_run",
]
RecommendationProgressStatus: TypeAlias = Literal["processing", "completed"]
RecommendationProgressCallback: TypeAlias = Callable[["RecommendationProgress"], None]


@dataclass(frozen=True)
class RecommendationProgress:
    phase: RecommendationProgressPhase
    current: int
    total: int
    subject: str
    status: RecommendationProgressStatus


@dataclass(frozen=True)
class _ScoringData:
    artist_names: dict[int, str]
    candidates: set[int]
    similarity_by_target: dict[int, list[tuple[int, float]]]
    feedback_similarity_by_target: dict[int, list[tuple[int, float]]]
    direct_tags_by_artist: dict[int, dict[str, float]]
    tag_ranks_by_artist: dict[int, list[tuple[str, int]]]
    listening_by_artist: dict[int, tuple[float, float, int, int, datetime | None, datetime | None]]
    identity_confidence_by_artist: dict[int, float]
    identity_warnings_by_artist: dict[int, list[str]]
    seed_graph: dict[int, dict[int, float]]
    target_features: dict[int, dict[str, float | int | bool | str | None]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class RetrievalCandidate:
    artist_id: int
    artist_name: str
    provenance: tuple[dict[str, object], ...]
    source_values: dict[str, float | int | str | None]

    def __post_init__(self) -> None:
        object.__setattr__(self, "provenance", deepcopy(tuple(self.provenance)))
        object.__setattr__(self, "source_values", dict(self.source_values))


@dataclass(frozen=True)
class LegacyRetrievalInputs:
    """Runtime values absent from the persisted query schema; never inferred by orchestration."""

    seeds: tuple[SeedArtist, ...]
    feedback_profile: FeedbackProfile
    idf_weights: SeedIdfWeights
    tag_profile: dict[str, float]
    as_of: datetime
    mode: RecommendationModeSettings
    legacy_rows: LegacyEvidenceRows | None = None
    invalid_legacy_rows: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "seeds", tuple(self.seeds))
        object.__setattr__(self, "feedback_profile", deepcopy(self.feedback_profile))
        object.__setattr__(self, "idf_weights", deepcopy(self.idf_weights))
        object.__setattr__(self, "tag_profile", dict(self.tag_profile))
        object.__setattr__(self, "legacy_rows", deepcopy(self.legacy_rows))
        object.__setattr__(self, "invalid_legacy_rows", deepcopy(self.invalid_legacy_rows))


@dataclass(frozen=True)
class LegacyRetrievalDetails:
    """Retain graph, listening, identity and walk facts without flattening their types."""

    inputs: LegacyRetrievalInputs
    scoring_data: _ScoringData
    walk_result: WalkResult | None


@dataclass(frozen=True)
class RetrievalResult:
    candidates: tuple[RetrievalCandidate, ...]
    diagnostics: dict[str, object]
    legacy: LegacyRetrievalDetails | None = field(default=None, repr=False, compare=True)
    evidence: dict[int, CandidateEvidence] = field(default_factory=dict)
    admissions: dict[int, AdmissionDecision] = field(default_factory=dict)
    source_candidates: tuple[SourceCandidate, ...] = ()
    fused_candidates: tuple[FusedCandidate, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", deepcopy(tuple(self.candidates)))
        object.__setattr__(self, "diagnostics", deepcopy(self.diagnostics))
        object.__setattr__(self, "legacy", deepcopy(self.legacy))
        object.__setattr__(self, "evidence", deepcopy(self.evidence))
        object.__setattr__(self, "admissions", deepcopy(self.admissions))


def _report_progress(
    progress_callback: RecommendationProgressCallback | None,
    *,
    phase: RecommendationProgressPhase,
    current: int,
    total: int,
    subject: str,
    status: RecommendationProgressStatus,
) -> None:
    if progress_callback is None:
        return
    progress_callback(
        RecommendationProgress(
            phase=phase,
            current=current,
            total=total,
            subject=subject,
            status=status,
        )
    )


def _add_feedback_seed_artists(
    conn: DuckDBPyConnection,
    seeds: list[SeedArtist],
    feedback_profile: FeedbackProfile,
    *,
    evidence_cutoff: datetime | None = None,
) -> list[SeedArtist]:
    existing_seed_ids = {seed.artist_id for seed in seeds}
    current_candidate_ids = _candidate_targets_from_seeds(
        conn, existing_seed_ids, evidence_cutoff=evidence_cutoff
    )
    feedback_seed_ids = sorted(
        feedback_profile.boosted_seed_artist_ids
        - feedback_profile.hidden_artist_ids
        - feedback_profile.suppressed_seed_artist_ids
        - existing_seed_ids
        - current_candidate_ids
    )
    if not feedback_seed_ids:
        return seeds
    rows = conn.execute(
        """
        SELECT
            a.artist_id,
            a.canonical_name,
            a.artist_key,
            coalesce(nullif(a.lastfm_name, ''), a.canonical_name) AS lastfm_name
        FROM artist a
        WHERE a.artist_id IN (
            SELECT source_artist_id
            FROM artist_similarity_edge
            WHERE source = 'lastfm'
              AND (? IS NULL OR fetched_at <= ?)
              AND source_artist_id IN (SELECT unnest(?))
        )
        ORDER BY a.canonical_name
        """,
        [evidence_cutoff, evidence_cutoff, feedback_seed_ids],
    ).fetchall()
    feedback_seeds = [
        SeedArtist(
            artist_id=int(row[0]),
            name=str(row[1]),
            artist_key=str(row[2]),
            lastfm_name=str(row[3]),
            play_count=0,
            seed_weight=max(0.25, feedback_profile.artist_adjustments.get(int(row[0]), 0.0)),
            regions=("feedback",),
        )
        for row in rows
    ]
    return seeds + feedback_seeds


def _candidate_targets_from_seeds(
    conn: DuckDBPyConnection,
    seed_ids: set[int],
    *,
    evidence_cutoff: datetime | None = None,
) -> set[int]:
    if not seed_ids:
        return set()
    return {
        int(target_artist_id)
        for (target_artist_id,) in conn.execute(
            """
            SELECT DISTINCT target_artist_id
            FROM artist_similarity_edge
            WHERE source = 'lastfm'
              AND (? IS NULL OR fetched_at <= ?)
              AND source_artist_id IN (SELECT unnest(?))
            """,
            [evidence_cutoff, evidence_cutoff, sorted(seed_ids)],
        ).fetchall()
    }


def _load_user_tag_profile(
    conn: DuckDBPyConnection,
    seeds: list[SeedArtist],
    *,
    weak_tags: list[str] | None = None,
    weak_tag_penalty: float = 1.0,
    evidence_cutoff: datetime | None = None,
    legacy_rows: LegacyEvidenceRows | None = None,
) -> dict[str, float]:
    profile: dict[str, float] = {}
    seed_weight_by_id = {seed.artist_id: seed.seed_weight for seed in seeds}
    if not seed_weight_by_id:
        return profile

    if legacy_rows is not None:
        rows = [(artist, tag, weight) for _, artist, tag, _, weight, _ in legacy_rows.artist_tags]
    else:
        rows = conn.execute(
            """
            SELECT artist_id, tag, normalized_weight
            FROM artist_tag
            WHERE source = 'lastfm'
              AND (? IS NULL OR fetched_at <= ?)
            """,
            [evidence_cutoff, evidence_cutoff],
        ).fetchall()

    normalized_weak_tags = {
        _normalize_tag_key(tag) for tag in (weak_tags or []) if _normalize_tag_key(tag) != ""
    }
    artists_by_tag: dict[str, set[int]] = {}
    for artist_id, tag, _normalized_weight in rows:
        seed_weight = seed_weight_by_id.get(int(artist_id))
        if seed_weight is None:
            continue
        normalized_tag = _normalize_tag_key(tag)
        if normalized_tag:
            artists_by_tag.setdefault(normalized_tag, set()).add(int(artist_id))

    for artist_id, tag, normalized_weight in rows:
        seed_weight = seed_weight_by_id.get(int(artist_id))
        if seed_weight is None:
            continue
        normalized_tag = _normalize_tag_key(tag)
        specificity = _tag_specificity(len(artists_by_tag.get(normalized_tag, set())))
        weak_multiplier = weak_tag_penalty if normalized_tag in normalized_weak_tags else 1.0
        profile[normalized_tag] = profile.get(normalized_tag, 0.0) + (
            seed_weight * float(normalized_weight) * specificity * weak_multiplier
        )

    max_weight = max(profile.values(), default=0.0)
    if max_weight <= 0:
        return {}
    return {tag: weight / max_weight for tag, weight in profile.items()}


def _load_seed_popularity(
    conn: DuckDBPyConnection,
    seed_ids: set[int],
    *,
    evidence_cutoff: datetime | None = None,
) -> dict[int, float]:
    """World-popularity proxy for seeds from ``tag_top_artist`` ranks.

    An artist that ranks highly across many tags is globally popular. We sum
    rank affinity across a seed's ``tag.getTopArtists`` placements; seeds with no
    placements are absent from the result and treated as neutral (median idf)
    downstream. This is the same signal family the obviousness penalty reads and
    requires no additional API call.
    """
    if not seed_ids:
        return {}
    popularity: dict[int, float] = {}
    for artist_id, tag_rank in conn.execute(
        """
        SELECT artist_id, tag_rank
        FROM tag_top_artist
        WHERE source = 'lastfm'
          AND (? IS NULL OR fetched_at <= ?)
          AND artist_id IN (SELECT unnest(?))
        """,
        [evidence_cutoff, evidence_cutoff, sorted(seed_ids)],
    ).fetchall():
        popularity[int(artist_id)] = popularity.get(int(artist_id), 0.0) + _rank_affinity(
            int(tag_rank)
        )
    return popularity


def _load_scoring_data(
    conn: DuckDBPyConnection,
    seed_ids: set[int],
    feedback_profile: FeedbackProfile,
    *,
    local_history_end: datetime | None = None,
    evidence_cutoff: datetime | None = None,
    legacy_rows: LegacyEvidenceRows | None = None,
) -> _ScoringData:
    legacy_rows = legacy_rows or load_legacy_evidence_rows(conn, cutoff=evidence_cutoff)
    similarity_by_target: dict[int, list[tuple[int, float]]] = {}
    seed_graph: dict[int, dict[int, float]] = {seed_id: {} for seed_id in seed_ids}
    candidates = set[int]()
    for (
        _source,
        source_artist_id,
        target_artist_id,
        _raw,
        normalized_score,
        _fetched,
        _payload,
    ) in legacy_rows.similarities:
        source_id = int(source_artist_id)
        target_id = int(target_artist_id)
        edge_weight = float(normalized_score)
        candidates.add(target_id)
        similarity_by_target.setdefault(target_id, []).append((source_id, edge_weight))
        if source_id in seed_ids and target_id in seed_ids:
            retained_weight = max(
                edge_weight,
                seed_graph[source_id].get(target_id, 0.0),
                seed_graph[target_id].get(source_id, 0.0),
            )
            seed_graph[source_id][target_id] = retained_weight
            seed_graph[target_id][source_id] = retained_weight

    feedback_similarity_by_target: dict[int, list[tuple[int, float]]] = {}
    feedback_neighborhood_source_ids = (
        feedback_profile.boosted_seed_artist_ids | feedback_profile.suppressed_seed_artist_ids
    ) - feedback_profile.hidden_artist_ids
    if feedback_neighborhood_source_ids:
        for (
            _source,
            source_artist_id,
            target_artist_id,
            _raw,
            normalized_score,
            _fetched,
            _payload,
        ) in legacy_rows.similarities:
            if source_artist_id not in feedback_neighborhood_source_ids:
                continue
            source_id = int(source_artist_id)
            target_id = int(target_artist_id)
            candidates.add(target_id)
            feedback_similarity_by_target.setdefault(target_id, []).append(
                (source_id, float(normalized_score))
            )

    direct_tags_by_artist: dict[int, dict[str, float]] = {}
    for _source, artist_id, tag, _count, normalized_weight, _fetched in legacy_rows.artist_tags:
        direct_tags_by_artist.setdefault(int(artist_id), {})[_normalize_tag_key(tag)] = float(
            normalized_weight
        )

    tag_ranks_by_artist: dict[int, list[tuple[str, int]]] = {}
    for _source, artist_id, tag, tag_rank, _count, _fetched in legacy_rows.tag_memberships:
        artist = int(artist_id)
        candidates.add(artist)
        tag_ranks_by_artist.setdefault(artist, []).append((_normalize_tag_key(tag), int(tag_rank)))

    if local_history_end is not None:
        listening_by_artist = _load_local_history_before(conn, end=local_history_end)
    else:
        listening_rows = conn.execute(
            """
            SELECT
                artist_id,
                coalesce(knownness, 0.0),
                coalesce(rediscovery_score, 0.0),
                coalesce(play_count, 0),
                coalesce(active_days, 0),
                first_played_at,
                last_played_at
            FROM artist_listening_fact
            """
        ).fetchall()
        listening_by_artist = {
            int(row[0]): (
                float(row[1]),
                float(row[2]),
                int(row[3] or 0),
                int(row[4] or 0),
                row[5],
                row[6],
            )
            for row in listening_rows
        }

    identity_by_artist = {
        int(artist_id): float(conf or 0.50)
        for artist_id, conf in conn.execute(
            "SELECT artist_id, coalesce(identity_confidence, 0.50) FROM artist"
        ).fetchall()
    }

    identity_warnings_by_artist: dict[int, list[str]] = {}
    for artist_id, warnings_json in conn.execute(
        "SELECT artist_id, coalesce(identity_warnings, '[]') FROM artist"
    ).fetchall():
        try:
            parsed = json.loads(str(warnings_json))
            identity_warnings_by_artist[int(artist_id)] = (
                list(parsed) if isinstance(parsed, list) else []
            )
        except (json.JSONDecodeError, TypeError):
            identity_warnings_by_artist[int(artist_id)] = []

    artist_names = {
        int(artist_id): str(canonical_name)
        for artist_id, canonical_name in conn.execute(
            """
            SELECT artist_id, canonical_name
            FROM artist
            """
        ).fetchall()
    }

    return _ScoringData(
        artist_names=artist_names,
        candidates=candidates - seed_ids,
        similarity_by_target=similarity_by_target,
        feedback_similarity_by_target=feedback_similarity_by_target,
        direct_tags_by_artist=direct_tags_by_artist,
        tag_ranks_by_artist=tag_ranks_by_artist,
        listening_by_artist=listening_by_artist,
        identity_confidence_by_artist=identity_by_artist,
        identity_warnings_by_artist=identity_warnings_by_artist,
        seed_graph=seed_graph,
    )


def _load_local_history_before(
    conn: DuckDBPyConnection,
    *,
    end: datetime,
) -> dict[int, tuple[float, float, int, int, datetime | None, datetime | None]]:
    rows = conn.execute(
        """
        SELECT
            a.artist_id,
            count(*) AS play_count,
            count(DISTINCT s.track_key) AS track_count,
            count(DISTINCT date_trunc('month', s.played_at)) AS active_months,
            count(DISTINCT s.played_date) AS active_days,
            min(s.played_at) AS first_played_at,
            max(s.played_at) AS last_played_at
        FROM scrobble AS s
        JOIN artist AS a ON a.artist_key = s.artist_key
        WHERE NOT s.is_duplicate
          AND s.played_at < ?
        GROUP BY a.artist_id
        """,
        [end],
    ).fetchall()
    scoped_listening = {
        int(artist_id): (
            compute_artist_listening_scores(
                ArtistListeningInput(
                    play_count=int(play_count),
                    track_count=int(track_count),
                    active_months=int(active_months),
                    first_played_at=first_played_at,
                    last_played_at=last_played_at,
                ),
                as_of=end,
            ),
            int(play_count),
            int(active_days),
            first_played_at,
            last_played_at,
        )
        for (
            artist_id,
            play_count,
            track_count,
            active_months,
            active_days,
            first_played_at,
            last_played_at,
        ) in rows
    }
    return {
        artist_id: (
            scores.knownness,
            scores.rediscovery_score,
            play_count,
            active_days,
            first_played_at,
            last_played_at,
        )
        for artist_id, (
            scores,
            play_count,
            active_days,
            first_played_at,
            last_played_at,
        ) in scoped_listening.items()
    }


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


def _tag_similarity_score(
    artist_id: int,
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
) -> float:
    """Shared legacy affinity for admission and subsequent relevance features."""
    if not tag_profile:
        return 0.0
    candidate = _candidate_tag_vector(artist_id, scoring_data)
    if not candidate:
        return 0.0
    dot = sum(tag_profile.get(tag, 0.0) * weight for tag, weight in candidate.items())
    profile_norm = sqrt(sum(weight * weight for weight in tag_profile.values()))
    candidate_norm = sqrt(sum(weight * weight for weight in candidate.values()))
    if profile_norm == 0.0 or candidate_norm == 0.0:
        return 0.0
    return dot / (profile_norm * candidate_norm)


def _normalize_tag_key(tag: object) -> str:
    return str(tag).strip().casefold()


def _tag_specificity(seed_artist_count: int) -> float:
    if seed_artist_count < 1:
        return 0.0
    return 1.0 / log(2 + seed_artist_count)


def _apply_tag_focus(
    tag_profile: dict[str, float],
    mode: RecommendationModeSettings,
) -> dict[str, float]:
    if not mode.focus_tags:
        return tag_profile
    focused = dict(tag_profile)
    for tag in mode.focus_tags:
        normalized_tag = _normalize_tag_key(tag)
        focused[normalized_tag] = max(focused.get(normalized_tag, 0.0), 1.0) * mode.tag_focus_boost
    max_weight = max(focused.values(), default=0.0)
    if max_weight <= 0.0:
        return {}
    return {tag: weight / max_weight for tag, weight in focused.items()}


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
    return sorted(
        ranked_tags,
        key=lambda row: (-tag_profile.get(row[0], 0.0), row[1], row[0]),
    )[0][0]


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


def retrieve_legacy_candidates(
    conn: DuckDBPyConnection,
    *,
    context: QueryContext,
    config: ScrobbleRecsConfig,
    inputs: LegacyRetrievalInputs | None = None,
) -> RetrievalResult:
    """Read persisted legacy facts. Bare calls use the history-window end as as_of."""
    supplied_view = inputs is not None and inputs.legacy_rows is not None
    legacy_rows = (
        inputs.legacy_rows
        if supplied_view and inputs is not None
        else load_legacy_evidence_rows(conn, cutoff=context.feedback_cutoff)
    )
    assert legacy_rows is not None
    invalid_legacy_rows = list(inputs.invalid_legacy_rows) if inputs is not None else []
    if config.recommendation.scoring.engine == "fusion" and not supplied_view:
        legacy_rows = _fusion_legacy_rows(legacy_rows, invalid_legacy_rows)
    inputs = inputs or _inputs_from_context(
        conn,
        context,
        config,
        legacy_rows=legacy_rows if config.recommendation.scoring.engine == "fusion" else None,
    )
    seed_ids = {seed.artist_id for seed in inputs.seeds}
    data = _load_scoring_data(
        conn,
        seed_ids,
        inputs.feedback_profile,
        local_history_end=context.history_cutoff,
        evidence_cutoff=context.feedback_cutoff,
        legacy_rows=legacy_rows,
    )
    walk = (
        build_walk_result(
            data.similarity_by_target,
            seed_ids=seed_ids,
            seed_weights=inputs.idf_weights.effective_weight_by_seed,
            config=config.recommendation.walk,
        )
        if config.recommendation.scoring.engine in ("walk", "fusion")
        and context.query_kind == "general"
        and seed_ids
        and data.similarity_by_target
        else None
    )
    invalid_observations: list[dict[str, object]] = []
    candidates = tuple(
        _retrieval_candidate(artist_id, data) for artist_id in sorted(data.candidates)
    )
    result = RetrievalResult(
        candidates,
        {"candidate_count": len(candidates), "invalid_observations": invalid_observations},
        LegacyRetrievalDetails(inputs, data, walk),
        load_candidate_evidence(
            conn,
            context=context,
            legacy_rows=legacy_rows,
            invalid_observations=invalid_observations,
            allowed_catalog_relationship_types=frozenset(
                config.recommendation.catalog_evidence.allowed_relationship_types
                if config.apis.musicbrainz.enabled
                and config.recommendation.catalog_evidence.enabled
                else ()
            ),
            excluded_sources=(
                frozenset()
                if (
                    config.apis.listenbrainz.enabled
                    and config.apis.listenbrainz.candidate_generation_enabled
                )
                else frozenset({"listenbrainz"})
            ),
        ),
    )
    if invalid_legacy_rows:
        result = replace(
            result, diagnostics={**result.diagnostics, "invalid_legacy_rows": invalid_legacy_rows}
        )
    if not invalid_observations:
        diagnostics = dict(result.diagnostics)
        diagnostics.pop("invalid_observations", None)
        result = replace(result, diagnostics=diagnostics)
    return result


def _retrieval_candidate(artist_id: int, data: _ScoringData) -> RetrievalCandidate:
    edges = data.similarity_by_target.get(artist_id, [])
    tags = data.tag_ranks_by_artist.get(artist_id, [])
    provenance: tuple[dict[str, object], ...] = tuple(
        [{"source": "lastfm", "seed_artist_id": source, "score": score} for source, score in edges]
        + [{"source": "lastfm_tag", "tag": tag, "rank": rank} for tag, rank in tags]
    )
    return RetrievalCandidate(
        artist_id,
        data.artist_names.get(artist_id, ""),
        provenance,
        {
            "lastfm_similarity": max((score for _, score in edges), default=None),
            "tag_count": len(tags),
        },
    )


def retrieve_with_backoff(
    conn: DuckDBPyConnection,
    *,
    context: QueryContext,
    config: ScrobbleRecsConfig,
    limit: int,
    inputs: LegacyRetrievalInputs | None = None,
    allow_low_confidence: bool = False,
) -> RetrievalResult:
    """Cumulative admission over the exhaustive persisted pool; never acquire provider data.

    Retain every considered candidate for diagnostics. Admissions record the first
    successful stage, even when later stages are needed to fill the requested pool.
    """
    from scrobble_recs.models.feedback import load_cooldown_artist_ids
    from scrobble_recs.models.policy import (
        _quality_gate_decision,
        _walk_gate_decision,
        candidate_eligibility,
    )
    from scrobble_recs.models.recommendation_quality import decide_candidate_admission

    if limit < 1:
        raise ValueError("limit must be positive")
    result = retrieve_legacy_candidates(conn, context=context, config=config, inputs=inputs)
    assert result.legacy is not None
    details = result.legacy
    evidence = result.evidence
    seeds = {seed.artist_id for seed in details.inputs.seeds}
    if context.query_kind == "artist":
        result = _condition_on_artist(conn, result, context=context, config=config)
        assert result.legacy is not None
        details = result.legacy
        evidence = result.evidence
        seeds = {seed.artist_id for seed in details.inputs.seeds}
    if (
        config.recommendation.scoring.engine == "fusion"
        and config.recommendation.fusion.excluded_sources
    ):
        result = _ablate_retrieval(
            result, config.recommendation.fusion.excluded_sources, context=context
        )
        assert result.legacy is not None
        details, evidence = result.legacy, result.evidence
    candidate_ids = ({row.artist_id for row in result.candidates} | set(evidence)) - seeds
    # Artist tags alone do not introduce new legacy candidates.
    legacy_ids = {row.artist_id for row in result.candidates}
    candidate_ids = {
        artist
        for artist in candidate_ids
        if artist in legacy_ids
        or any(not is_translated_legacy(row) for row in evidence[artist].observations)
    }
    data = replace(details.scoring_data, candidates=candidate_ids)
    literal_confidences = {
        int(artist): float(confidence)
        for artist, confidence in conn.execute(
            "SELECT artist_id, coalesce(identity_confidence, .5) FROM artist"
        ).fetchall()
    }
    cooldown = load_cooldown_artist_ids(
        conn,
        as_of=details.inputs.as_of,
        days=config.recommendation.feedback_cooldown_days,
        until=context.feedback_cutoff,
    )
    admissions: dict[int, AdmissionDecision] = {}
    usable_ids = {
        artist
        for artist in candidate_ids
        if candidate_eligibility(
            artist,
            seed_ids=seeds,
            feedback_profile=details.inputs.feedback_profile,
            cooldown_artist_ids=cooldown,
            scoring_data=data,
            mode=details.inputs.mode,
            config=config,
            tag_profile=details.inputs.tag_profile,
            walk_result=details.walk_result,
            context=context,
        ).eligible
    }
    stages: list[dict[str, object]] = []
    for stage in range(5 if allow_low_confidence else 4):
        for artist in sorted(candidate_ids):
            if artist in admissions and admissions[artist].admitted:
                continue
            facts = evidence.get(artist, CandidateEvidence(artist, ()))
            legacy_only = all(is_legacy_compatible(row) for row in facts.observations)
            tag_score = _tag_similarity_score(artist, details.inputs.tag_profile, data)
            node = details.walk_result.nodes.get(artist) if details.walk_result else None
            walk_supported = bool(
                node is not None
                and 0 <= node.hop_distance <= config.recommendation.walk.max_hops
                and _walk_gate_decision(node, config.recommendation.walk).passed
            )
            legacy_gate = _quality_gate_decision(
                config,
                details.inputs.mode,
                edges=tuple(
                    (source, weight)
                    for source, weight in data.similarity_by_target.get(artist, ())
                    if source in seeds
                ),
                tag_score=tag_score,
                rediscovery_score=data.listening_by_artist.get(artist, (0, 0, 0, 0, None, None))[1],
            )
            legacy_policy_passed = (
                artist in legacy_ids
                and legacy_only
                and (
                    (legacy_gate.passed or walk_supported)
                    if config.recommendation.scoring.engine == "fusion"
                    else (walk_supported if details.walk_result is not None else legacy_gate.passed)
                )
            )
            confidence = (
                data.identity_confidence_by_artist.get(artist, 0.5)
                if legacy_policy_passed and context.query_kind == "general"
                else literal_confidences.get(artist, 0.5)
            )
            admissions[artist] = decide_candidate_admission(
                facts,
                stage=stage,
                hidden=artist in details.inputs.feedback_profile.hidden_artist_ids,
                strongly_rejected=artist in cooldown
                or (
                    context.query_kind == "artist"
                    and artist in details.inputs.feedback_profile.suppressed_seed_artist_ids
                ),
                identity_confidence=confidence,
                config=config.recommendation.discovery_evidence,
                context=context,
                tag_score=tag_score,
                seed_ids=seeds,
                walk_supported=walk_supported,
                legacy_policy_passed=legacy_policy_passed,
                target_neighborhood_member=(artist in data.target_features),
            )
        admitted_count = sum(
            row.admitted for artist, row in admissions.items() if artist in usable_ids
        )
        stages.append(
            {
                "stage": stage,
                "admitted_count": admitted_count,
                "depth_expansion": "legacy_exhaustive_noop" if stage == 1 else None,
            }
        )
        if admitted_count >= limit:
            break
    result = RetrievalResult(
        tuple(_retrieval_candidate(artist, data) for artist in sorted(candidate_ids)),
        {
            **result.diagnostics,
            "candidate_count": len(candidate_ids),
            "backoff_stages": stages,
            "requested_limit": limit,
            "admitted_count": admitted_count,
            "exhausted": admitted_count < limit,
        },
        replace(details, scoring_data=data),
        evidence,
        admissions,
    )
    return (
        _with_source_lists(result, config=config, context=context)
        if config.recommendation.scoring.engine == "fusion"
        else result
    )


def _inputs_from_context(
    conn: DuckDBPyConnection,
    context: QueryContext,
    config: ScrobbleRecsConfig,
    *,
    legacy_rows: LegacyEvidenceRows | None = None,
) -> LegacyRetrievalInputs:
    rows = conn.execute(
        "SELECT artist_id, canonical_name, artist_key, "
        "coalesce(lastfm_name, canonical_name) FROM artist"
    ).fetchall()
    identities = {int(row[0]): row for row in rows}
    seeds = [
        SeedArtist(
            seed.artist_id,
            str(identities[seed.artist_id][1]),
            str(identities[seed.artist_id][2]),
            str(identities[seed.artist_id][3]),
            0,
            seed.weight,
            seed.regions,
        )
        for seed in context.taste_region_seeds
        if seed.artist_id in identities
    ]
    feedback = load_feedback_profile(conn, as_of=context.feedback_cutoff)
    mode = resolve_recommendation_mode(context.mode, config)
    weights = compute_seed_idf_weights(
        {seed.artist_id: seed.seed_weight for seed in seeds},
        _load_seed_popularity(
            conn, {seed.artist_id for seed in seeds}, evidence_cutoff=context.feedback_cutoff
        ),
        alpha=config.recommendation.scoring.seed_idf_alpha,
    )
    profile = _load_user_tag_profile(
        conn,
        seeds,
        weak_tags=config.recommendation.tag_profile.weak_tags,
        weak_tag_penalty=config.recommendation.tag_profile.weak_tag_penalty,
        evidence_cutoff=context.feedback_cutoff,
        legacy_rows=legacy_rows,
    )
    profile = apply_feedback_to_tag_profile(_apply_tag_focus(profile, mode), feedback)
    return LegacyRetrievalInputs(
        tuple(seeds), feedback, weights, profile, context.history_window.end, mode
    )


def compute_identity_confidence(
    *,
    artist_name: str,
    mbid: str | None,
    resolution_status: str,
    lastfm_enrichment_mbid: str | None = None,
    lastfm_url_from_enrichment: str | None = None,
    canonical_name_match: bool = True,
) -> tuple[float, list[str]]:
    confidence = 0.50
    warnings: list[str] = []

    if mbid and resolution_status == "provisional_mbid":
        confidence += 0.30
    if lastfm_enrichment_mbid or lastfm_url_from_enrichment:
        confidence += 0.15
    if canonical_name_match:
        confidence += 0.10

    name_parts = artist_name.strip().split()
    if len(name_parts) <= 1 and not mbid:
        confidence -= 0.30
        warnings.append("generic_short_name_no_mbid")
    if resolution_status == "provisional_mbid_conflict":
        confidence -= 0.20
        warnings.append("multiple_possible_local_matches")
    if not mbid:
        warnings.append("missing_musicbrainz_mbid")

    return (max(0.0, min(1.0, confidence)), sorted(warnings))


def _condition_on_artist(
    conn: DuckDBPyConnection,
    result: RetrievalResult,
    *,
    context: QueryContext,
    config: ScrobbleRecsConfig,
) -> RetrievalResult:
    """Bound the shared pool before admission; taste and feedback cannot add origins."""
    assert result.legacy is not None and context.target_artist_id is not None
    assert context.artist_settings is not None and context.target_relationship is not None
    details = result.legacy
    data = details.scoring_data
    target = context.target_artist_id
    identity = conn.execute(
        """
            SELECT canonical_name, artist_key, coalesce(lastfm_name, canonical_name) FROM artist
            WHERE artist_id = ?
        """,
        [target],
    ).fetchone()
    if identity is None:
        raise ValueError(f"target artist not found: {target}")
    seed = SeedArtist(
        target,
        str(identity[0]),
        str(identity[1]),
        str(identity[2]),
        context.target_relationship.play_count,
        1.0,
        ("target",),
    )
    target_tags = _candidate_tag_vector(target, data)
    invalid_rows = [
        {
            "source_artist_id": source,
            "target_artist_id": artist,
            "reason": "nonfinite_similarity",
            "value": "nan" if weight != weight else "infinity" if weight > 0 else "-infinity",
        }
        for artist, edges in sorted(data.similarity_by_target.items())
        for source, weight in sorted(edges)
        if not isfinite(weight)
    ]
    data = replace(
        data,
        similarity_by_target={
            artist: [(source, weight) for source, weight in edges if isfinite(weight)]
            for artist, edges in data.similarity_by_target.items()
        },
        feedback_similarity_by_target={
            artist: [(source, weight) for source, weight in edges if isfinite(weight)]
            for artist, edges in data.feedback_similarity_by_target.items()
        },
    )
    # Only finite, positive Last.fm edges can create target-origin reachability.
    graph = {
        artist: [(source, weight) for source, weight in edges if isfinite(weight) and weight > 0]
        for artist, edges in data.similarity_by_target.items()
    }
    walk = build_walk_result(
        graph,
        seed_ids={target},
        seed_weights={target: 1.0},
        config=config.recommendation.walk.model_copy(
            update={"max_hops": context.artist_settings.max_hops, "symmetrize": False}
        ),
    )
    candidates = (
        set(data.direct_tags_by_artist)
        | set(data.tag_ranks_by_artist)
        | set(walk.nodes)
        | set(result.evidence)
    )
    features: dict[int, dict[str, float | int | bool | str | None]] = {}
    evidence: dict[int, CandidateEvidence] = {}
    personal_weights = {seed.artist_id: seed.weight for seed in context.taste_region_seeds}
    total_personal = sum(personal_weights.values()) or 1.0
    for artist in sorted(candidates - {target}):
        node = walk.nodes.get(artist)
        hop = (
            node.hop_distance
            if node and 0 < node.hop_distance <= context.artist_settings.max_hops
            else None
        )
        semantic = _tag_similarity_score(artist, target_tags, data)
        observations = _target_observations(
            result.evidence.get(artist, CandidateEvidence(artist, ())),
            target=target,
            semantic_overlap=semantic,
            identity_threshold=config.recommendation.discovery_evidence.identity_safety_threshold,
            catalog_enabled=config.recommendation.discovery_evidence.catalog_relationships_enabled,
        )
        radio = any(
            row.source == "listenbrainz"
            and row.source_artist_id == target
            and row.relationship_kind == "artist_radio_membership"
            for row in observations
        )
        catalog = any(
            row.family == "catalog"
            and row.source_artist_id == target
            and row.relationship_kind == "catalog_relationship"
            for row in observations
        )
        direct = max(
            (weight for source, weight in graph.get(artist, ()) if source == target), default=0.0
        )
        if hop is None and semantic <= 0 and not radio and not catalog:
            continue
        known = (
            data.listening_by_artist.get(artist, (0, 0, 0, 0, None, None))[2] > 0
            or artist in details.inputs.feedback_profile.known_artist_ids
        )
        personal = (
            sum(
                personal_weights.get(source, 0.0) * weight
                for source, weight in graph.get(artist, ())
            )
            / total_personal
        )
        personal_collaborative = personal
        personal_semantic = _tag_similarity_score(artist, details.inputs.tag_profile, data)
        personal = max(personal, personal_semantic)
        features[artist] = {
            "target_direct_strength": direct,
            "target_hop_distance": hop,
            "target_semantic_overlap": semantic,
            "target_radio_membership": radio,
            "target_catalog_relationship": catalog,
            "target_relationship_state": context.target_relationship.state,
            "known_status": "known" if known else "new",
            "target_personal_agreement": personal,
            "target_personal_collaborative_agreement": personal_collaborative,
            "target_personal_semantic_agreement": personal_semantic,
            "target_relevance": max(
                direct,
                context.artist_settings.hop_proximity_scale / (hop + 1) if hop else 0.0,
                semantic,
                context.artist_settings.structural_membership_prior if radio or catalog else 0.0,
            ),
            "artist_profile": context.artist_settings.profile,
            "artist_max_hops": context.artist_settings.max_hops,
            "artist_popularity_preference": context.artist_settings.popularity_preference,
            "artist_obviousness_penalty_weight": context.artist_settings.obviousness_penalty_weight,
        }
        evidence[artist] = CandidateEvidence(artist, observations)
    inputs = replace(
        details.inputs,
        seeds=(seed,),
        idf_weights=compute_seed_idf_weights({target: 1.0}, {}, alpha=0.0),
        tag_profile=target_tags,
    )
    data = replace(
        data,
        candidates=set(features),
        target_features=features,
        identity_confidence_by_artist={
            int(a): float(c)
            for a, c in conn.execute(
                "SELECT artist_id, coalesce(identity_confidence, .5) FROM artist"
            ).fetchall()
        },
    )
    return RetrievalResult(
        tuple(_retrieval_candidate(artist, data) for artist in sorted(features)),
        {
            **result.diagnostics,
            "candidate_count": len(features),
            **_artist_quarantine_diagnostics(result.diagnostics, target, set(features)),
            "target_artist_id": target,
            "target_neighborhood": sorted(features),
            "invalid_similarity_rows": invalid_rows,
        },
        LegacyRetrievalDetails(inputs, data, walk),
        evidence,
    )


def _target_observations(
    facts: CandidateEvidence,
    *,
    target: int,
    semantic_overlap: float,
    identity_threshold: float,
    catalog_enabled: bool,
) -> tuple[EvidenceObservation, ...]:
    observations = []
    for row in facts.observations:
        if row.family == "catalog" and not catalog_enabled:
            continue
        if (
            not is_translated_legacy(row)
            and row.family != "personal"
            and row.identity_confidence < identity_threshold
        ):
            continue
        if (
            row.family == "personal"
            or (row.family == "semantic" and semantic_overlap > 0)
            or (
                row.source_artist_id == target
                and row.relationship_kind
                in ("similar_artist", "artist_radio_membership", "catalog_relationship")
            )
        ):
            observations.append(row)
    return tuple(observations)


def _with_source_lists(
    result: RetrievalResult, *, config: ScrobbleRecsConfig, context: QueryContext
) -> RetrievalResult:
    """Rank each retriever only inside the final query pool (never provider raw values)."""
    from scrobble_recs.models.ranking import _lastfm_similarity_score

    assert result.legacy is not None
    details = result.legacy
    data, inputs = details.scoring_data, details.inputs
    ids = sorted(row.artist_id for row in result.candidates)
    weights = inputs.idf_weights.effective_weight_by_seed
    definitions: dict[str, tuple[EvidenceFamily, str]] = {
        "lastfm_similarity": ("collaborative", "seed-weighted-normalized-v1"),
        "lastfm_walk": ("collaborative", "walk-lift-v1"),
        "semantic": ("semantic", "query-tag-overlap-v1"),
    }
    values = {
        "lastfm_similarity": {
            artist: _lastfm_similarity_score(
                artist, weights, sum(weights.values()) or 1.0, data.similarity_by_target
            )
            for artist in ids
        },
        "lastfm_walk": {
            artist: node.lift
            for artist in ids
            if details.walk_result is not None
            and (node := details.walk_result.nodes.get(artist)) is not None
            and node.hop_distance > 0
        },
        "semantic": {
            artist: _tag_similarity_score(artist, inputs.tag_profile, data) for artist in ids
        },
    }
    excluded = set(config.recommendation.fusion.excluded_sources)
    rows: list[SourceCandidate] = []
    diagnostics: dict[str, object] = {}
    for source, (family, version) in definitions.items():
        ordered = sorted(
            (
                (artist, value)
                for artist, value in values[source].items()
                if isfinite(value) and (value > 0 or source == "lastfm_walk")
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
        if source not in excluded:
            rows.extend(
                SourceCandidate(artist, source, family, rank, True, 1.0)
                for rank, (artist, _) in enumerate(ordered, 1)
            )
        facts = [
            fact
            for artist in ids
            for fact in result.evidence.get(artist, CandidateEvidence(artist, ())).observations
            if fact.family == family
            and fact.source == "lastfm"
            and (
                family == "semantic" or source == "lastfm_walk" or fact.source_artist_id in weights
            )
        ]
        latest = max(
            (fact.fetched_at for fact in facts if fact.fetched_at is not None), default=None
        )
        stale = bool(
            ordered
            and latest is not None
            and (inputs.as_of - latest).total_seconds() > config.apis.lastfm.cache_ttl_days * 86400
        )
        diagnostics[source] = {
            "status": "disabled"
            if source in excluded
            else "stale"
            if stale
            else "present_ranked"
            if ordered
            else "missing",
            "calibration": version,
            "ranked": True,
            "stale": stale if source not in excluded else False,
            "latest_at": latest.isoformat() if latest is not None else None,
        }
    for source, enabled, ttl in (
        (
            "listenbrainz",
            config.apis.listenbrainz.enabled
            and config.apis.listenbrainz.candidate_generation_enabled,
            config.apis.listenbrainz.cache_ttl_days,
        ),
        (
            "musicbrainz",
            config.apis.musicbrainz.enabled and config.recommendation.catalog_evidence.enabled,
            config.apis.musicbrainz.cache_ttl_days,
        ),
        ("local_feedback", True, None),
    ):
        facts = [
            fact
            for artist in ids
            for fact in result.evidence.get(artist, CandidateEvidence(artist, ())).observations
            if fact.source == source
            and (fact.source_artist_id in weights or fact.family == "personal")
        ]
        stale = bool(
            facts
            and ttl is not None
            and all(
                fact.fetched_at is not None
                and (inputs.as_of - fact.fetched_at).total_seconds() > ttl * 86400
                for fact in facts
            )
        )
        active = enabled and source not in excluded
        if active:
            rows.extend(
                SourceCandidate(
                    fact.target_artist_id,
                    source,
                    fact.family,
                    None,
                    False,
                    fact.identity_confidence,
                )
                for fact in facts
            )
        diagnostics[source] = {
            "status": "disabled"
            if not active
            else "stale"
            if stale
            else "present_unranked"
            if facts
            else "missing",
            "ranked": False,
            "stale": stale if active else False,
        }
    if details.walk_result is None and "lastfm_walk" not in excluded:
        diagnostics["lastfm_walk"] = {
            "status": "unavailable",
            "ranked": True,
            "calibration": "walk-lift-v1",
        }
    return replace(
        result,
        source_candidates=tuple(rows),
        fused_candidates=fuse_source_results(
            rows,
            rrf_constant=config.recommendation.fusion.rrf_constant,
            source_weights=config.recommendation.fusion.source_weights,
            family_caps=config.recommendation.fusion.family_caps,
        ),
        diagnostics={
            **result.diagnostics,
            "fusion_sources": diagnostics,
            "fusion_config": config.recommendation.fusion.model_dump(mode="json"),
        },
    )


def _ablate_retrieval(
    result: RetrievalResult, excluded: Sequence[str], *, context: QueryContext
) -> RetrievalResult:
    """Remove derived source views without changing persisted provider evidence."""
    assert result.legacy is not None
    details, evidence = result.legacy, {}
    for artist, facts in result.evidence.items():
        kept = tuple(
            row
            for row in facts.observations
            if row.source not in excluded
            and not (row.family == "semantic" and "semantic" in excluded)
            and not (
                row.source == "lastfm"
                and row.relationship_kind == "similar_artist"
                and "lastfm_similarity" in excluded
            )
        )
        if kept:
            evidence[artist] = CandidateEvidence(artist, kept)
    data = details.scoring_data
    if "lastfm_similarity" in excluded:
        data = replace(
            data, similarity_by_target={}, feedback_similarity_by_target={}, seed_graph={}
        )
    if "semantic" in excluded:
        data = replace(data, direct_tags_by_artist={}, tag_ranks_by_artist={})
        details = replace(details, inputs=replace(details.inputs, tag_profile={}))
    walk = None if "lastfm_walk" in excluded else details.walk_result
    if context.query_kind == "artist":
        assert context.artist_settings is not None
        features = deepcopy(data.target_features)
        for values in features.values():
            if "lastfm_similarity" in excluded:
                values["target_direct_strength"] = 0.0
                values["target_personal_collaborative_agreement"] = 0.0
            if "semantic" in excluded:
                values["target_semantic_overlap"] = 0.0
                values["target_personal_semantic_agreement"] = 0.0
            if "lastfm_walk" in excluded:
                values["target_hop_distance"] = None
            if "listenbrainz" in excluded:
                values["target_radio_membership"] = False
            if "musicbrainz" in excluded:
                values["target_catalog_relationship"] = False
            hop = values.get("target_hop_distance")
            values["target_personal_agreement"] = max(
                float(values.get("target_personal_collaborative_agreement") or 0),
                float(values.get("target_personal_semantic_agreement") or 0),
            )
            values["target_relevance"] = max(
                float(values.get("target_direct_strength") or 0),
                float(values.get("target_semantic_overlap") or 0),
                context.artist_settings.hop_proximity_scale / (float(hop) + 1) if hop else 0,
                context.artist_settings.structural_membership_prior
                if values.get("target_radio_membership")
                or values.get("target_catalog_relationship")
                else 0,
            )
        data = replace(data, target_features=features)
    ids = {
        artist
        for artist in data.candidates
        if artist in evidence
        or (walk is not None and artist in walk.nodes and walk.nodes[artist].hop_distance > 0)
    }
    return replace(
        result,
        candidates=tuple(row for row in result.candidates if row.artist_id in ids),
        evidence=evidence,
        legacy=replace(details, scoring_data=replace(data, candidates=ids), walk_result=walk),
    )


def _fusion_legacy_rows(
    rows: LegacyEvidenceRows, invalid: list[dict[str, object]]
) -> LegacyEvidenceRows:
    """Quarantine malformed rows before they poison a candidate's aggregates."""

    def keep(row: tuple[Any, ...], fields: tuple[int, ...], target: int, kind: str) -> bool:
        valid = all(row[index] is None or isfinite(row[index]) for index in fields)
        if kind == "similarity":
            valid = valid and row[4] is not None
            try:
                payload = json.loads(row[6]) if row[6] is not None else None
                json.dumps(payload, allow_nan=False)
            except (ValueError, TypeError):
                valid = False
        elif kind == "tag":
            valid = valid and row[4] is not None
        else:
            valid = valid and type(row[3]) is int and row[3] > 0
        if not valid:
            invalid.append(
                {
                    "source": row[0],
                    "source_artist_id": row[1] if kind == "similarity" else None,
                    "target_artist_id": row[target],
                    "kind": kind,
                }
            )
        return valid

    return LegacyEvidenceRows(
        tuple(
            sorted(
                (row for row in rows.similarities if keep(row, (3, 4), 2, "similarity")), key=repr
            )
        ),
        tuple(sorted((row for row in rows.artist_tags if keep(row, (3, 4), 1, "tag")), key=repr)),
        tuple(
            sorted(
                (row for row in rows.tag_memberships if keep(row, (4,), 1, "membership")), key=repr
            )
        ),
    )


def _artist_quarantine_diagnostics(
    diagnostics: dict[str, object], target: int, neighbors: set[int]
) -> dict[str, object]:
    scoped: dict[str, object] = {}
    for key in ("invalid_observations", "invalid_legacy_rows"):
        records = diagnostics.get(key)
        if isinstance(records, list):
            scoped[key] = [
                row
                for row in records
                if row.get("source_artist_id") == target
                or (
                    row.get("source_artist_id") is None
                    and row.get("target_artist_id") in neighbors | {target}
                )
            ]
    return scoped
