"""Resolve recommendation inputs and orchestrate the persisted-data pipeline."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from duckdb import DuckDBPyConnection

from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.feedback import apply_feedback_to_tag_profile, load_feedback_profile
from scrobble_recs.models.listening import refresh_artist_listening_scores
from scrobble_recs.models.modes import resolve_recommendation_mode
from scrobble_recs.models.policy import (
    _classify_score_band as _classify_score_band,
)
from scrobble_recs.models.policy import (
    _compute_trial_penalty as _compute_trial_penalty,
)
from scrobble_recs.models.policy import (
    _is_rediscovery_eligible as _is_rediscovery_eligible,
)
from scrobble_recs.models.policy import (
    apply_slate_policy,
)
from scrobble_recs.models.profile import select_seed_artists
from scrobble_recs.models.profile_window import (
    ListeningWindow,
    normalize_timestamp,
    resolve_listening_window,
)
from scrobble_recs.models.ranking import build_policy_explanations, rank_candidates
from scrobble_recs.models.recommendation_query import (
    NoveltyIntent,
    QueryContext,
    RecommendationRequest,
    RecommendationVersions,
    TasteRegionSeed,
    capture_evidence_snapshot,
)
from scrobble_recs.models.recommendation_store import (
    RecommendationRunPayload,
    persist_recommendation_run,
)
from scrobble_recs.models.recommendation_store import RecommendationStats as RecommendationStats
from scrobble_recs.models.relationship import (
    artist_query_settings,
    build_target_relationship_snapshot,
    resolve_target_artist,
)
from scrobble_recs.models.retrieval import (
    LegacyRetrievalInputs,
    _add_feedback_seed_artists,
    _apply_tag_focus,
    _load_seed_popularity,
    _load_user_tag_profile,
    _report_progress,
    retrieve_with_backoff,
)
from scrobble_recs.models.retrieval import (
    RecommendationProgress as RecommendationProgress,
)
from scrobble_recs.models.retrieval import (
    RecommendationProgressCallback as RecommendationProgressCallback,
)
from scrobble_recs.models.retrieval import (
    RecommendationProgressPhase as RecommendationProgressPhase,
)
from scrobble_recs.models.retrieval import (
    RecommendationProgressStatus as RecommendationProgressStatus,
)
from scrobble_recs.models.retrieval import (
    compute_identity_confidence as compute_identity_confidence,
)
from scrobble_recs.models.seed_weighting import compute_seed_idf_weights


def generate_recommendations(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    limit: int,
    mode: str | None = None,
    as_of: datetime | None = None,
    history_window: ListeningWindow | None = None,
    local_history_end: datetime | None = None,
    historical_cutoff: datetime | None = None,
    request: RecommendationRequest | None = None,
    progress_callback: RecommendationProgressCallback | None = None,
) -> RecommendationStats:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    resolved_request = request or RecommendationRequest()

    mode_settings = resolve_recommendation_mode(mode, config)
    resolved_mode = mode_settings.name
    resolved_as_of = normalize_timestamp(as_of or datetime.now(tz=UTC))
    if local_history_end is not None:
        local_history_end = normalize_timestamp(local_history_end)
    if historical_cutoff is not None:
        historical_cutoff = normalize_timestamp(historical_cutoff)
    if history_window is None:
        resolved_history_window = resolve_listening_window(
            as_of=resolved_as_of,
            default_months=config.recommendation.profile_window_months,
            since=None,
            until=None,
        )
        history_window_source = "default"
    else:
        resolved_history_window = history_window
        history_window_source = "explicit"
    relationship = None
    settings = None
    if resolved_request.query_kind == "artist":
        assert resolved_request.target_artist_id is not None
        resolve_target_artist(conn, str(resolved_request.target_artist_id))
        historical_cutoff = min(resolved_as_of, historical_cutoff or resolved_as_of)
        local_history_end = min(
            resolved_history_window.end, historical_cutoff, local_history_end or resolved_as_of
        )
        resolved_history_window = ListeningWindow(resolved_history_window.start, local_history_end)
        relationship = build_target_relationship_snapshot(
            conn,
            artist_id=resolved_request.target_artist_id,
            as_of=resolved_as_of,
            history_window=resolved_history_window,
            feedback_cutoff=historical_cutoff,
            thresholds=config.recommendation.artist_relationship,
        )
        settings = artist_query_settings(relationship, config=config, mode=mode_settings)
        mode_settings = replace(
            mode_settings, obviousness_penalty_weight=settings.obviousness_penalty_weight
        )
    _report_progress(
        progress_callback,
        phase="refresh_listening",
        current=0,
        total=1,
        subject="artist listening scores",
        status="processing",
    )
    if local_history_end is None:
        refresh_artist_listening_scores(conn, as_of=resolved_as_of)
    _report_progress(
        progress_callback,
        phase="refresh_listening",
        current=1,
        total=1,
        subject="artist listening scores",
        status="completed",
    )
    _report_progress(
        progress_callback,
        phase="select_seeds",
        current=0,
        total=1,
        subject=resolved_mode,
        status="processing",
    )
    feedback_profile = load_feedback_profile(conn, as_of=historical_cutoff)
    seeds = select_seed_artists(
        conn,
        limit=config.recommendation.seed_artist_limit,
        min_scrobbles=config.recommendation.min_seed_scrobbles,
        regions=mode_settings.seed_regions,
        window=resolved_history_window,
    )
    seeds = _add_feedback_seed_artists(
        conn, seeds, feedback_profile, evidence_cutoff=historical_cutoff
    )
    product_regions = {
        "lifetime": "core",
        "recent": "current",
        "old_obsession": "dormant",
        "underexplored": "curiosity",
    }
    novelty_intent: NoveltyIntent = (
        "mixed"
        if resolved_mode == "balanced"
        else "rediscover"
        if resolved_mode in ("rediscovery", "old")
        else "discover"
    )
    evidence_snapshot = capture_evidence_snapshot(conn, historical_cutoff=historical_cutoff)
    query_context = QueryContext(
        query_kind=resolved_request.query_kind,
        target_artist_id=resolved_request.target_artist_id,
        target_relationship=relationship,
        artist_settings=settings,
        history_window=resolved_history_window,
        history_cutoff=local_history_end,
        feedback_cutoff=historical_cutoff,
        mode=resolved_mode,
        novelty_intent=novelty_intent,
        taste_region_seeds=tuple(
            TasteRegionSeed(
                seed.artist_id,
                seed.seed_weight,
                tuple(product_regions.get(region, region) for region in seed.regions),
            )
            for seed in seeds
        ),
        evidence_snapshot_id=evidence_snapshot.snapshot_id,
        evidence_freshness=evidence_snapshot.freshness,
        versions=RecommendationVersions(
            ranker=f"{config.recommendation.scoring.engine}-v1",
            **(
                {"feature_schema": "artist-features-v1", "slate_policy": settings.version}
                if settings
                else {}
            ),
        ),
        config_hash=_config_hash(config),
    )
    seed_ids = {seed.artist_id for seed in seeds}
    idf_weights = compute_seed_idf_weights(
        {seed.artist_id: seed.seed_weight for seed in seeds},
        _load_seed_popularity(conn, seed_ids, evidence_cutoff=historical_cutoff),
        alpha=config.recommendation.scoring.seed_idf_alpha,
    )
    _report_progress(
        progress_callback,
        phase="select_seeds",
        current=1,
        total=1,
        subject=f"{len(seeds)} seed artists",
        status="completed",
    )
    _report_progress(
        progress_callback,
        phase="build_profiles",
        current=0,
        total=1,
        subject="tags and feedback",
        status="processing",
    )
    tag_profile = _load_user_tag_profile(
        conn,
        seeds,
        weak_tags=config.recommendation.tag_profile.weak_tags,
        weak_tag_penalty=config.recommendation.tag_profile.weak_tag_penalty,
        evidence_cutoff=historical_cutoff,
    )
    effective_tag_profile = apply_feedback_to_tag_profile(
        _apply_tag_focus(tag_profile, mode_settings),
        feedback_profile,
    )
    _report_progress(
        progress_callback,
        phase="build_profiles",
        current=1,
        total=1,
        subject=f"{len(effective_tag_profile)} tag weights",
        status="completed",
    )
    _report_progress(
        progress_callback,
        phase="load_candidates",
        current=0,
        total=1,
        subject="local graph",
        status="processing",
    )
    retrieval = retrieve_with_backoff(
        conn,
        context=query_context,
        config=config,
        limit=limit,
        inputs=LegacyRetrievalInputs(
            tuple(seeds),
            feedback_profile,
            idf_weights,
            effective_tag_profile,
            resolved_as_of,
            mode_settings,
        ),
    )
    _report_progress(
        progress_callback,
        phase="load_candidates",
        current=1,
        total=1,
        subject=f"{len(retrieval.candidates)} candidates",
        status="completed",
    )
    ranked = rank_candidates(
        conn,
        context=query_context,
        retrieval=retrieval,
        config=config,
        progress_callback=progress_callback,
    )
    _report_progress(
        progress_callback,
        phase="rerank_diversity",
        current=0,
        total=limit,
        subject="ranked candidates",
        status="processing",
    )
    decisions = apply_slate_policy(
        conn,
        context=query_context,
        ranked=ranked,
        limit=limit,
        config=config,
        progress_callback=progress_callback,
        mode_settings=mode_settings,
    )
    selected_count = sum(decision.included for decision in decisions)
    _report_progress(
        progress_callback,
        phase="rerank_diversity",
        current=selected_count,
        total=limit,
        subject=f"{selected_count} selected",
        status="completed",
    )
    _report_progress(
        progress_callback,
        phase="build_explanations",
        current=0,
        total=selected_count,
        subject="recommendations",
        status="processing",
    )
    decisions = build_policy_explanations(decisions, retrieval)
    _report_progress(
        progress_callback,
        phase="build_explanations",
        current=selected_count,
        total=selected_count,
        subject="recommendations",
        status="completed",
    )

    _report_progress(
        progress_callback,
        phase="persist_run",
        current=0,
        total=selected_count,
        subject="recommendation rows",
        status="processing",
    )
    stats = persist_recommendation_run(
        conn,
        payload=RecommendationRunPayload(
            run_id=str(uuid4()),
            context=query_context,
            config=config,
            retrieval=retrieval,
            ranked=ranked,
            decisions=decisions,
            scorer_diagnostics=tuple(
                item.legacy.diagnostic
                for item in sorted(
                    ranked, key=lambda item: item.legacy.diagnostic_order if item.legacy else 0
                )
                if item.legacy is not None
            ),
            history_window_source=history_window_source,
        ),
        progress_callback=progress_callback,
    )
    _report_progress(
        progress_callback,
        phase="persist_run",
        current=selected_count,
        total=selected_count,
        subject="recommendation rows",
        status="completed",
    )
    return stats


def _config_hash(config: ScrobbleRecsConfig) -> str:
    payload = config.recommendation.model_dump_json(by_alias=True)
    return sha256(payload.encode("utf-8")).hexdigest()
