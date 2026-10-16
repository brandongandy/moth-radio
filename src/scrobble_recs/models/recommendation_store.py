"""One transaction for run provenance, the complete candidate pool and selected results."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import quote

from duckdb import DuckDBPyConnection

from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.modes import RecommendationModeSettings
from scrobble_recs.models.policy import PolicyDecision, _CandidateDiagnostic, _CandidateScore
from scrobble_recs.models.profile import SeedArtist
from scrobble_recs.models.ranking import RankedCandidate
from scrobble_recs.models.recommendation_query import QueryContext
from scrobble_recs.models.retrieval import (
    RecommendationProgressCallback,
    RetrievalResult,
    _report_progress,
    _ScoringData,
)
from scrobble_recs.models.walk import WalkResult


@dataclass(frozen=True)
class RecommendationStats:
    run_id: str
    mode: str
    recommendations: int


@dataclass(frozen=True)
class RecommendationRunPayload:
    run_id: str
    context: QueryContext
    config: ScrobbleRecsConfig
    retrieval: RetrievalResult
    ranked: tuple[RankedCandidate, ...]
    decisions: tuple[PolicyDecision, ...]
    scorer_diagnostics: tuple[_CandidateDiagnostic, ...]
    history_window_source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "retrieval", deepcopy(self.retrieval))
        object.__setattr__(self, "ranked", deepcopy(tuple(self.ranked)))
        object.__setattr__(self, "decisions", deepcopy(tuple(self.decisions)))
        object.__setattr__(self, "scorer_diagnostics", deepcopy(tuple(self.scorer_diagnostics)))
        object.__setattr__(self, "config", self.config.model_copy(deep=True))


def persist_recommendation_run(
    conn: DuckDBPyConnection,
    *,
    payload: RecommendationRunPayload,
    progress_callback: RecommendationProgressCallback | None = None,
) -> RecommendationStats:
    """Commit the complete persisted artifact, or roll back all three tables on any failure."""
    if payload.retrieval.legacy is None:
        raise ValueError("legacy persistence requires retrieval details")
    details = payload.retrieval.legacy
    selected = [
        decision.legacy_score
        for decision in payload.decisions
        if decision.included and decision.legacy_score is not None
    ]
    conn.execute("BEGIN TRANSACTION")
    try:
        _insert_run(conn, payload)
        _persist_candidates(
            conn,
            payload.run_id,
            details.scoring_data,
            list(payload.scorer_diagnostics),
            progress_callback=progress_callback,
        )
        _persist_recommendations(conn, payload.run_id, selected)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return RecommendationStats(payload.run_id, payload.context.mode, len(selected))


def _seed_region_counts(
    seeds: list[SeedArtist],
    mode_settings: RecommendationModeSettings,
) -> dict[str, int]:
    configured_regions = {region for region, _limit, _multiplier in mode_settings.seed_regions}
    counts: dict[str, int] = {
        region: sum(1 for seed in seeds if region in seed.regions)
        for region, _limit, _multiplier in mode_settings.seed_regions
    }
    for seed in seeds:
        for region in seed.regions:
            if region not in configured_regions:
                counts[region] = counts.get(region, 0) + 1
    return counts


def _walk_run_summary(walk_result: WalkResult | None) -> dict[str, object] | None:
    if walk_result is None:
        return None
    diagnostics = walk_result.diagnostics
    return {
        "node_count": diagnostics.node_count,
        "edge_count": diagnostics.edge_count,
        "iterations": diagnostics.iterations,
        "final_delta": diagnostics.final_delta,
        "node_cap_bound": diagnostics.node_cap_bound,
        "iteration_cap_bound": diagnostics.iteration_cap_bound,
    }


def _persist_candidates(
    conn: DuckDBPyConnection,
    run_id: str,
    scoring_data: _ScoringData,
    diagnostics: list[_CandidateDiagnostic],
    *,
    progress_callback: RecommendationProgressCallback | None,
) -> None:
    """Persist candidate artists for the current recommendation run."""
    total = len(diagnostics)
    _report_progress(
        progress_callback,
        phase="persist_candidates",
        current=0,
        total=total,
        subject="candidate rows",
        status="processing",
    )
    batch_size = 1_000
    for start in range(0, total, batch_size):
        batch = diagnostics[start : start + batch_size]
        rows: list[list[object]] = []
        for diagnostic in batch:
            artist_id = diagnostic.artist_id
            sources = scoring_data.similarity_by_target.get(artist_id, [])
            tags = scoring_data.tag_ranks_by_artist.get(artist_id, [])
            rows.append(
                [
                    run_id,
                    artist_id,
                    json.dumps([int(source[0]) for source in sources]),
                    float(max((source[1] for source in sources), default=0.0)),
                    len(sources),
                    len(tags),
                    diagnostic.state,
                    json.dumps(list(diagnostic.reasons)),
                    json.dumps(diagnostic.quality_gate, sort_keys=True),
                ]
            )
        conn.executemany(
            """
            INSERT INTO recommendation_candidate
                (run_id, artist_id, candidate_sources, max_source_score,
                 appeared_from_seed_count, appeared_from_tag_count, eligibility_state,
                 eligibility_reasons, quality_gate)
            VALUES (CAST(? AS UUID), ?, ?::JSON, ?, ?, ?, ?, ?::JSON, ?::JSON)
            ON CONFLICT (run_id, artist_id) DO UPDATE SET
                candidate_sources = excluded.candidate_sources,
                max_source_score = excluded.max_source_score,
                appeared_from_seed_count = excluded.appeared_from_seed_count,
                appeared_from_tag_count = excluded.appeared_from_tag_count,
                eligibility_state = excluded.eligibility_state,
                eligibility_reasons = excluded.eligibility_reasons,
                quality_gate = excluded.quality_gate
            """,
            rows,
        )
        persisted = start + len(batch)
        _report_progress(
            progress_callback,
            phase="persist_candidates",
            current=persisted,
            total=total,
            subject="candidate rows",
            status="completed" if persisted == total else "processing",
        )
    if total == 0:
        _report_progress(
            progress_callback,
            phase="persist_candidates",
            current=0,
            total=0,
            subject="candidate rows",
            status="completed",
        )


def _insert_run(conn: DuckDBPyConnection, payload: RecommendationRunPayload) -> None:
    assert payload.retrieval.legacy is not None
    details = payload.retrieval.legacy
    inputs = details.inputs
    run_id = payload.run_id
    config = payload.config
    mode = payload.context.mode
    mode_settings = inputs.mode
    seeds = list(inputs.seeds)
    idf_weights = inputs.idf_weights
    walk_result = details.walk_result
    history_window = payload.context.history_window
    history_window_source = payload.history_window_source
    query_context = payload.context
    conn.execute(
        """
        INSERT INTO recommendation_run (
            run_id,
            config_hash,
            mode,
            scoring_engine,
            seed_summary,
            scoring_weights,
            notes,
            query_kind, target_artist_id, query_context, evidence_snapshot_id,
            feature_schema_version, fusion_version, ranker_version, slate_policy_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run_id,
            query_context.config_hash,
            mode,
            config.recommendation.scoring.engine,
            json.dumps(
                {
                    "seed_count": len(seeds),
                    "history_window": {
                        "start": history_window.start.isoformat(),
                        "end": history_window.end.isoformat(),
                        "source": history_window_source,
                    },
                    "regions": _seed_region_counts(seeds, mode_settings),
                    "idf": {
                        "alpha": idf_weights.alpha,
                        "missing_seed_count": len(idf_weights.missing_seed_ids),
                        "missing_fraction": idf_weights.missing_fraction,
                    },
                    "walk": _walk_run_summary(walk_result),
                    "top_seeds": [
                        {
                            "artist": seed.name,
                            "seed_weight": seed.seed_weight,
                            "idf_weight": idf_weights.idf_by_seed.get(seed.artist_id, 1.0),
                            "regions": list(seed.regions),
                        }
                        for seed in seeds[:10]
                    ],
                }
            ),
            json.dumps(
                {
                    "base": config.recommendation.weights.model_dump(mode="json"),
                    "serendipity": config.recommendation.serendipity.model_dump(mode="json"),
                    "mode": {
                        "name": mode_settings.name,
                        "diversity_lambda": mode_settings.diversity_lambda,
                        "max_knownness": mode_settings.max_knownness,
                        "obviousness_penalty_weight": (mode_settings.obviousness_penalty_weight),
                        "hard_hide_top_tag_rank": mode_settings.hard_hide_top_tag_rank,
                        "same_primary_tag_cap": mode_settings.same_primary_tag_cap,
                        "focus_tags": list(mode_settings.focus_tags),
                        "lastfm_similarity_multiplier": (
                            mode_settings.lastfm_similarity_multiplier
                        ),
                        "tag_similarity_multiplier": (mode_settings.tag_similarity_multiplier),
                        "rediscovery_multiplier": mode_settings.rediscovery_multiplier,
                        "tag_focus_boost": mode_settings.tag_focus_boost,
                        "high_affinity_tag_weight": mode_settings.high_affinity_tag_weight,
                        "obviousness_enabled": config.recommendation.obviousness.enabled,
                    },
                },
                sort_keys=True,
            ),
            "offline lastfm/tag first recommender",
            query_context.query_kind,
            query_context.target_artist_id,
            json.dumps(query_context.to_json_dict(), sort_keys=True, allow_nan=False),
            query_context.evidence_snapshot_id,
            query_context.versions.feature_schema,
            query_context.versions.fusion,
            query_context.versions.ranker,
            query_context.versions.slate_policy,
        ],
    )


def _persist_recommendations(
    conn: DuckDBPyConnection,
    run_id: str,
    ranked: list[_CandidateScore],
) -> None:
    candidate_only_offset = 999900

    artist_identity = {
        int(aid): (
            str(canonical_name or ""),
            str(mbid or ""),
            float(conf or 0.50),
            str(warnings or "[]"),
        )
        for aid, canonical_name, mbid, conf, warnings in conn.execute(
            """
            SELECT artist_id, canonical_name,
                   coalesce(musicbrainz_artist_mbid, ''),
                   coalesce(identity_confidence, 0.50),
                   coalesce(identity_warnings, '[]')
            FROM artist
            """
        ).fetchall()
    }

    for rank, score in enumerate(ranked, start=1):
        if getattr(score, "score_band", "") == "candidate_only":
            effective_rank = candidate_only_offset
            candidate_only_offset += 1
        else:
            effective_rank = rank
        identity = artist_identity.get(score.artist_id, (score.artist_name, "", 0.50, "[]"))
        mbid_val = identity[1]
        lastfm_url_val = f"https://www.last.fm/music/{quote(score.artist_name, safe='')}"
        musicbrainz_url_val = f"https://musicbrainz.org/artist/{mbid_val}" if mbid_val else ""
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
                explanation,
                score_band,
                evidence_source_count,
                local_scrobble_count,
                listened_day_count,
                first_listened_at,
                last_listened_at,
                years_since_last_listen,
                rediscovery_eligible,
                trial_penalty,
                candidate_artist_name,
                canonical_artist_name,
                musicbrainz_artist_mbid,
                lastfm_url,
                musicbrainz_url,
                identity_confidence,
                identity_warnings,
                hop_distance
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?
            )
            """,
            [
                run_id,
                effective_rank,
                score.artist_id,
                score.final_score,
                score.raw_score,
                score.lastfm_similarity_score,
                score.tag_similarity_score,
                0.0,
                score.rediscovery_score,
                0.0,
                score.feedback_score,
                score.knownness,
                score.obviousness,
                score.diversity_penalty,
                json.dumps(score.explanation),
                score.score_band,
                score.evidence_source_count,
                score.local_scrobble_count,
                score.listened_day_count,
                score.first_listened_at,
                score.last_listened_at,
                score.years_since_last_listen,
                score.rediscovery_eligible,
                score.trial_penalty,
                score.artist_name,
                identity[0],
                identity[1],
                lastfm_url_val,
                musicbrainz_url_val,
                identity[2],
                identity[3],
                score.hop_distance,
            ],
        )
