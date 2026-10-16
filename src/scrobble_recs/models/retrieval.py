"""Persisted Last.fm/tag facts and lossless legacy retrieval contracts."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from math import log
from typing import Literal, TypeAlias

from duckdb import DuckDBPyConnection

from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.feedback import (
    FeedbackProfile,
    apply_feedback_to_tag_profile,
    load_feedback_profile,
)
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "seeds", tuple(self.seeds))
        object.__setattr__(self, "feedback_profile", deepcopy(self.feedback_profile))
        object.__setattr__(self, "idf_weights", deepcopy(self.idf_weights))
        object.__setattr__(self, "tag_profile", dict(self.tag_profile))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", deepcopy(tuple(self.candidates)))
        object.__setattr__(self, "diagnostics", deepcopy(self.diagnostics))
        object.__setattr__(self, "legacy", deepcopy(self.legacy))


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
) -> list[SeedArtist]:
    existing_seed_ids = {seed.artist_id for seed in seeds}
    current_candidate_ids = _candidate_targets_from_seeds(conn, existing_seed_ids)
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
              AND source_artist_id IN (SELECT unnest(?))
        )
        ORDER BY a.canonical_name
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
            seed_weight=max(0.25, feedback_profile.artist_adjustments.get(int(row[0]), 0.0)),
            regions=("feedback",),
        )
        for row in rows
    ]
    return seeds + feedback_seeds


def _candidate_targets_from_seeds(
    conn: DuckDBPyConnection,
    seed_ids: set[int],
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
              AND source_artist_id IN (SELECT unnest(?))
            """,
            [sorted(seed_ids)],
        ).fetchall()
    }


def _load_user_tag_profile(
    conn: DuckDBPyConnection,
    seeds: list[SeedArtist],
    *,
    weak_tags: list[str] | None = None,
    weak_tag_penalty: float = 1.0,
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
          AND artist_id IN (SELECT unnest(?))
        """,
        [sorted(seed_ids)],
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
) -> _ScoringData:
    similarity_by_target: dict[int, list[tuple[int, float]]] = {}
    seed_graph: dict[int, dict[int, float]] = {seed_id: {} for seed_id in seed_ids}
    candidates = set[int]()
    for source_artist_id, target_artist_id, normalized_score in conn.execute(
        """
        SELECT source_artist_id, target_artist_id, normalized_score
        FROM artist_similarity_edge
        WHERE source = 'lastfm'
        """
    ).fetchall():
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
        for source_artist_id, target_artist_id, normalized_score in conn.execute(
            """
            SELECT source_artist_id, target_artist_id, normalized_score
            FROM artist_similarity_edge
            WHERE source = 'lastfm'
              AND source_artist_id IN (SELECT unnest(?))
            """,
            [sorted(feedback_neighborhood_source_ids)],
        ).fetchall():
            source_id = int(source_artist_id)
            target_id = int(target_artist_id)
            candidates.add(target_id)
            feedback_similarity_by_target.setdefault(target_id, []).append(
                (source_id, float(normalized_score))
            )

    direct_tags_by_artist: dict[int, dict[str, float]] = {}
    for artist_id, tag, normalized_weight in conn.execute(
        """
        SELECT artist_id, tag, normalized_weight
        FROM artist_tag
        WHERE source = 'lastfm'
        """
    ).fetchall():
        direct_tags_by_artist.setdefault(int(artist_id), {})[_normalize_tag_key(tag)] = float(
            normalized_weight
        )

    tag_ranks_by_artist: dict[int, list[tuple[str, int]]] = {}
    for artist_id, tag, tag_rank in conn.execute(
        """
        SELECT artist_id, tag, tag_rank
        FROM tag_top_artist
        WHERE source = 'lastfm'
        """
    ).fetchall():
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
    inputs = inputs or _inputs_from_context(conn, context, config)
    seed_ids = {seed.artist_id for seed in inputs.seeds}
    data = _load_scoring_data(
        conn, seed_ids, inputs.feedback_profile, local_history_end=context.history_cutoff
    )
    walk = (
        build_walk_result(
            data.similarity_by_target,
            seed_ids=seed_ids,
            seed_weights=inputs.idf_weights.effective_weight_by_seed,
            config=config.recommendation.walk,
        )
        if config.recommendation.scoring.engine == "walk"
        else None
    )
    candidates = tuple(
        _retrieval_candidate(artist_id, data) for artist_id in sorted(data.candidates)
    )
    return RetrievalResult(
        candidates, {"candidate_count": len(candidates)}, LegacyRetrievalDetails(inputs, data, walk)
    )


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


def _inputs_from_context(
    conn: DuckDBPyConnection, context: QueryContext, config: ScrobbleRecsConfig
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
        _load_seed_popularity(conn, {seed.artist_id for seed in seeds}),
        alpha=config.recommendation.scoring.seed_idf_alpha,
    )
    profile = _load_user_tag_profile(
        conn,
        seeds,
        weak_tags=config.recommendation.tag_profile.weak_tags,
        weak_tag_penalty=config.recommendation.tag_profile.weak_tag_penalty,
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
