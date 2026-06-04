from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from math import sqrt
from typing import cast
from uuid import uuid4

from duckdb import DuckDBPyConnection

from scrobble_recs.config import RecommendationWeightsConfig, ScrobbleRecsConfig
from scrobble_recs.models.knownness import ArtistListeningInput, compute_artist_listening_scores
from scrobble_recs.models.modes import RecommendationModeSettings, resolve_recommendation_mode
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
    diversity_penalty: float
    primary_tag: str | None
    explanation: dict[str, object]


@dataclass(frozen=True)
class _ScoringData:
    artist_names: dict[int, str]
    candidates: set[int]
    similarity_by_target: dict[int, list[tuple[int, float]]]
    direct_tags_by_artist: dict[int, dict[str, float]]
    tag_ranks_by_artist: dict[int, list[tuple[str, int]]]
    listening_by_artist: dict[int, tuple[float, float]]


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
    seed_ids = {seed.artist_id for seed in seeds}
    tag_profile = _load_user_tag_profile(conn, seeds)
    scoring_data = _load_scoring_data(conn, seed_ids)
    scores = _score_candidates(config, mode_settings, seeds, seed_ids, tag_profile, scoring_data)
    ranked_without_explanations = sorted(
        scores,
        key=lambda item: (-item.final_score, item.artist_name.casefold()),
    )[:limit]
    ranked = [
        replace(
            score,
            explanation=_explanation(
                score,
                scoring_data,
                seed_weight_by_id={seed.artist_id: seed.seed_weight for seed in seeds},
                tag_profile=tag_profile,
            ),
        )
        for score in ranked_without_explanations
    ]

    run_id = str(uuid4())
    _persist_run(conn, run_id, resolved_mode, config, mode_settings, seeds, ranked)
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
                first_played_at=cast(datetime, row[4]),
                last_played_at=cast(datetime, row[5]),
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


def _load_scoring_data(conn: DuckDBPyConnection, seed_ids: set[int]) -> _ScoringData:
    similarity_by_target: dict[int, list[tuple[int, float]]] = {}
    candidates = set[int]()
    for source_artist_id, target_artist_id, normalized_score in conn.execute(
        """
        SELECT source_artist_id, target_artist_id, normalized_score
        FROM artist_similarity_edge
        WHERE source = 'lastfm'
        """
    ).fetchall():
        target_id = int(target_artist_id)
        candidates.add(target_id)
        similarity_by_target.setdefault(target_id, []).append(
            (int(source_artist_id), float(normalized_score))
        )

    direct_tags_by_artist: dict[int, dict[str, float]] = {}
    for artist_id, tag, normalized_weight in conn.execute(
        """
        SELECT artist_id, tag, normalized_weight
        FROM artist_tag
        WHERE source = 'lastfm'
        """
    ).fetchall():
        direct_tags_by_artist.setdefault(int(artist_id), {})[str(tag)] = float(normalized_weight)

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
        tag_ranks_by_artist.setdefault(artist, []).append((str(tag), int(tag_rank)))

    listening_by_artist = {
        int(artist_id): (float(knownness), float(rediscovery_score))
        for artist_id, knownness, rediscovery_score in conn.execute(
            """
            SELECT
                artist_id,
                coalesce(knownness, 0.0),
                coalesce(rediscovery_score, 0.0)
            FROM artist_listening_fact
            """
        ).fetchall()
    }

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
        direct_tags_by_artist=direct_tags_by_artist,
        tag_ranks_by_artist=tag_ranks_by_artist,
        listening_by_artist=listening_by_artist,
    )


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
        knownness, rediscovery = scoring_data.listening_by_artist.get(artist_id, (0.0, 0.0))
        lastfm_score = _lastfm_similarity_score(
            artist_id,
            seed_weight_by_id,
            total_seed_weight,
            scoring_data.similarity_by_target,
        )
        tag_score = _tag_similarity_score(artist_id, focused_tag_profile, scoring_data)
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
        if (
            rank <= mode.hard_hide_top_tag_rank
            and tag_profile.get(tag, 0.0) >= mode.high_affinity_tag_weight
        ):
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
    return sorted(
        ranked_tags,
        key=lambda row: (-tag_profile.get(row[0], 0.0), row[1], row[0]),
    )[0][0]


def _lastfm_similarity_score(
    artist_id: int,
    seed_weight_by_id: dict[int, float],
    total_seed_weight: float,
    similarity_by_target: dict[int, list[tuple[int, float]]],
) -> float:
    return (
        sum(
            seed_weight_by_id.get(source_artist_id, 0.0) * normalized_score
            for source_artist_id, normalized_score in similarity_by_target.get(artist_id, [])
        )
        / total_seed_weight
    )


def _tag_similarity_score(
    artist_id: int,
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
) -> float:
    if not tag_profile:
        return 0.0
    direct_tags = scoring_data.direct_tags_by_artist.get(artist_id, {})
    ranked_tags = {
        tag: _rank_affinity(rank)
        for tag, rank in scoring_data.tag_ranks_by_artist.get(artist_id, [])
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


def _obviousness(
    artist_id: int,
    tag_profile: dict[str, float],
    scoring_data: _ScoringData,
) -> float:
    return max(
        (
            tag_profile.get(tag, 0.0) * _rank_penalty(rank)
            for tag, rank in scoring_data.tag_ranks_by_artist.get(artist_id, [])
        ),
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
    score: _CandidateScore,
    scoring_data: _ScoringData,
    *,
    seed_weight_by_id: dict[int, float],
    tag_profile: dict[str, float],
) -> dict[str, object]:
    seed_rows = [
        (
            source_artist_id,
            scoring_data.artist_names.get(source_artist_id, ""),
            similarity,
            seed_weight_by_id[source_artist_id] * similarity,
        )
        for source_artist_id, similarity in scoring_data.similarity_by_target.get(
            score.artist_id, []
        )
        if source_artist_id in seed_weight_by_id
    ]
    seed_rows.sort(key=lambda row: (-row[3], row[1]))
    tag_rows = scoring_data.tag_ranks_by_artist.get(score.artist_id, [])
    ordered_tag_rows = sorted(
        tag_rows,
        key=lambda row: (-tag_profile.get(row[0], 0.0), row[1], row[0]),
    )
    return {
        "summary": (
            f"{score.artist_name} is similar to high-weight seed artists"
            " and fits tags from the local listening profile."
        ),
        "seed_artists": [
            {
                "artist": seed_name,
                "seed_weight": seed_weight_by_id[source_artist_id],
                "source": "lastfm_artist_similarity",
                "similarity": similarity,
            }
            for source_artist_id, seed_name, similarity, _contribution in seed_rows[:5]
        ],
        "tags": [
            {
                "tag": str(tag),
                "user_weight": tag_profile.get(str(tag), 0.0),
                "candidate_weight": _rank_affinity(int(rank)),
                "candidate_rank_for_tag": int(rank),
            }
            for tag, rank in ordered_tag_rows[:5]
        ],
        "penalties": {
            "knownness": score.knownness_penalty,
            "obviousness": score.obviousness_penalty,
            "diversity": score.diversity_penalty,
        },
        "classification": {
            "is_new": score.knownness == 0.0,
            "is_rediscovery": score.rediscovery_score > 0.0,
            "is_obvious": score.obviousness >= 0.75,
        },
    }


def _persist_run(
    conn: DuckDBPyConnection,
    run_id: str,
    mode: str,
    config: ScrobbleRecsConfig,
    mode_settings: RecommendationModeSettings,
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
                json.dumps(
                    {
                        "base": config.recommendation.weights.model_dump(mode="json"),
                        "mode": {
                            "name": mode_settings.name,
                            "diversity_lambda": mode_settings.diversity_lambda,
                            "max_knownness": mode_settings.max_knownness,
                            "obviousness_penalty_weight": (
                                mode_settings.obviousness_penalty_weight
                            ),
                            "hard_hide_top_tag_rank": mode_settings.hard_hide_top_tag_rank,
                            "same_primary_tag_cap": mode_settings.same_primary_tag_cap,
                            "focus_tags": list(mode_settings.focus_tags),
                        },
                    },
                    sort_keys=True,
                ),
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
                    score.diversity_penalty,
                    json.dumps(score.explanation),
                ],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


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
