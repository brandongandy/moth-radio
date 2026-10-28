from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Any, Literal

from duckdb import DuckDBPyConnection

from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.evaluation_metrics import (
    _first_listened_artist_ids,
    _identity_coverage,
    _ndcg_at_k,
    _novelty_rate,
    _recall_at_k,
    _source_memberships,
    _tag_diversity_at_k,
    run_guardrails,
    run_quality_metrics,
)
from scrobble_recs.models.experiments import LABELS, JudgmentLabel, _effective, _experiment
from scrobble_recs.models.profile_window import normalize_timestamp, resolve_listening_window
from scrobble_recs.models.recommend import generate_recommendations
from scrobble_recs.models.recommendation_query import RecommendationRequest


def graded_artist_relevance(
    *,
    future_play_count: int,
    future_active_days: int,
    future_active_months: int,
    explicit_labels: Sequence[JudgmentLabel],
) -> float:
    """Bounded discovery relevance v1; explicit negatives override all positives."""
    for count in (future_play_count, future_active_days, future_active_months):
        if type(count) is not int or count < 0:
            raise ValueError("future counts must be nonnegative integers")
    if (
        not isinstance(explicit_labels, Sequence)
        or isinstance(explicit_labels, str | bytes)
        or any(label not in LABELS for label in explicit_labels)
    ):
        raise ValueError("explicit_labels must be a sequence of judgment labels")
    if {"reject", "wrong_direction"}.intersection(explicit_labels):
        return 0.0
    implicit = (
        3
        if future_active_days >= 2 or future_active_months >= 2
        else 2
        if future_play_count >= 2
        else 1
        if any((future_play_count, future_active_days, future_active_months))
        else 0
    )
    grades = {"want_to_try": 1, "strong_fit": 2, "surprising_good": 3}
    return float(max([implicit, *(grades.get(label, 0) for label in explicit_labels)]))


@dataclass(frozen=True)
class EvaluationWindow:
    train_until: datetime
    test_from: datetime
    test_until: datetime


@dataclass(frozen=True)
class EvaluationResult:
    train_until: str
    test_from: str
    test_until: str
    scoring_engine: str
    recommendations_generated: int
    precision_at_50: float
    recall_at_50: float
    recall_at_100: float
    ndcg_at_50: float
    knownness_median: float
    obviousness_median: float
    tag_diversity_at_20: float
    rediscovery_hit_rate: float
    external_candidate_share: float
    eligible_candidate_coverage: float
    novelty_rate: float
    cooldown_or_repeat_rate: float
    candidate_count_by_source: dict[str, int] = field(default_factory=dict)
    future_hit_count_by_source: dict[str, int] = field(default_factory=dict)
    unique_candidate_count_by_source: dict[str, int] = field(default_factory=dict)
    unique_future_hit_count_by_source: dict[str, int] = field(default_factory=dict)
    candidate_count_by_family: dict[str, int] = field(default_factory=dict)
    evidence_snapshot_id: str = ""
    ablated_source: str | None = None
    identity_resolved_rate: float = 0.0
    identity_ambiguous_rate: float = 0.0

    pool_candidate_count: int = 0
    eligible_candidate_count: int = 0
    pool_recall: float = 0.0
    eligible_recall: float = 0.0
    pool_coverage: float = 0.0
    eligible_coverage: float = 0.0
    provider_concentration: float = 0.0
    multi_family_share: float = 0.0
    popularity_distribution: dict[str, float] = field(default_factory=dict)
    runtime_seconds: float = 0.0
    selected_slate_recall_at_50: float = 0.0
    selected_slate_ndcg_at_50: float = 0.0
    selected_slate_tag_diversity_at_20: float = 0.0
    graded_ndcg_at_10: float = 0.0
    graded_ndcg_at_25: float = 0.0
    graded_ndcg_at_50: float = 0.0
    repeated_listen_recall_at_50: float = 0.0
    multi_day_adoption_rate_at_50: float = 0.0
    one_great_find_rate_at_50: float = 0.0
    graded_metrics: dict[str, Any] = field(default_factory=dict)
    guardrails: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""

    @property
    def evaluation_task(self) -> str:
        return "general"


@dataclass(frozen=True, kw_only=True)
class ArtistEvaluationResult(EvaluationResult):
    target_artist_id: int
    run_id: str
    graded_adoption_ndcg_at_50: float
    evaluation_task: Literal["artist"] = field(default="artist", init=False)


@dataclass(frozen=True)
class RollingEvaluationReport:
    windows: tuple[EvaluationResult, ...]
    window_count: int
    mean_recall_at_50: float
    mean_recall_at_100: float
    mean_ndcg_at_50: float
    mean_identity_resolved_rate: float
    mean_selected_slate_recall_at_50: float = 0.0
    mean_selected_slate_ndcg_at_50: float = 0.0
    mean_selected_slate_tag_diversity_at_20: float = 0.0
    mean_graded_ndcg_at_10: float = 0.0
    mean_graded_ndcg_at_25: float = 0.0
    mean_graded_ndcg_at_50: float = 0.0
    mean_repeated_listen_recall_at_50: float = 0.0
    mean_multi_day_adoption_rate_at_50: float = 0.0
    mean_one_great_find_rate_at_50: float = 0.0
    promotion: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "unevaluated",
            "reason": "single_config_replay; paired evidence required",
        }
    )

    @property
    def evaluation_task(self) -> str:
        return "general"


@dataclass(frozen=True, kw_only=True)
class ArtistRollingEvaluationReport(RollingEvaluationReport):
    target_count: int
    run_count: int
    mean_graded_adoption_ndcg_at_50: float
    evaluation_task: Literal["artist"] = field(default="artist", init=False)


def evaluate_rolling_recommendations(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    windows: Sequence[EvaluationWindow],
    evaluation_task: Literal["general", "artist"] = "general",
) -> RollingEvaluationReport:
    """Replay each historical window in input order using the single-window evaluator."""
    if not windows:
        raise ValueError("at least one evaluation window is required")
    if evaluation_task not in ("general", "artist"):
        raise ValueError("evaluation_task must be general or artist")
    if evaluation_task == "artist":
        return _evaluate_artist_windows(conn, config, windows)
    results = tuple(
        evaluate_recommendations(
            conn,
            config,
            train_until=window.train_until,
            test_from=window.test_from,
            test_until=window.test_until,
        )
        for window in windows
    )
    count = len(results)
    return RollingEvaluationReport(
        windows=results,
        window_count=count,
        mean_recall_at_50=sum(item.recall_at_50 for item in results) / count,
        mean_recall_at_100=sum(item.recall_at_100 for item in results) / count,
        mean_ndcg_at_50=sum(item.ndcg_at_50 for item in results) / count,
        mean_identity_resolved_rate=sum(item.identity_resolved_rate for item in results) / count,
        mean_selected_slate_recall_at_50=sum(item.selected_slate_recall_at_50 for item in results)
        / count,
        **_graded_means(results),
        mean_selected_slate_ndcg_at_50=sum(item.selected_slate_ndcg_at_50 for item in results)
        / count,
        mean_selected_slate_tag_diversity_at_20=sum(
            item.selected_slate_tag_diversity_at_20 for item in results
        )
        / count,
    )


def evaluate_recommendations(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    train_until: datetime,
    test_from: datetime,
    test_until: datetime,
    ablate_source: str | None = None,
    evaluation_task: Literal["general", "artist"] = "general",
    target_artist_id: int | None = None,
    judgment_experiment_ids: Sequence[str] = (),
) -> EvaluationResult:
    started = perf_counter()
    if evaluation_task not in ("general", "artist"):
        raise ValueError("evaluation_task must be general or artist")
    request = RecommendationRequest(evaluation_task, target_artist_id)
    allowed_sources = (
        "listenbrainz",
        "musicbrainz",
        "lastfm_similarity",
        "lastfm_walk",
        "semantic",
    )
    if ablate_source is not None and ablate_source not in allowed_sources:
        raise ValueError(
            "ablate_source must be listenbrainz, musicbrainz, lastfm_similarity, "
            "lastfm_walk, or semantic"
        )
    if ablate_source:
        config = config.model_copy(deep=True)
        if config.recommendation.scoring.engine == "fusion":
            config.recommendation.fusion = type(config.recommendation.fusion).model_validate(
                {**config.recommendation.fusion.model_dump(), "excluded_sources": [ablate_source]}
            )
        elif ablate_source == "listenbrainz":
            config.apis.listenbrainz.enabled = False
        else:
            raise ValueError(
                "ranked/catalog/personal source ablations require scoring.engine=fusion"
            )
    train_until = normalize_timestamp(train_until)
    test_from = normalize_timestamp(test_from)
    test_until = normalize_timestamp(test_until)
    if test_from < train_until:
        raise ValueError("test_from must not be before train_until")
    if test_until <= test_from:
        raise ValueError("test_until must be after test_from")

    history_window = resolve_listening_window(
        as_of=train_until,
        default_months=config.recommendation.profile_window_months,
        since=None,
        until=train_until,
    )
    stats = generate_recommendations(
        conn,
        config,
        limit=100,
        as_of=train_until,
        history_window=history_window,
        local_history_end=train_until,
        historical_cutoff=train_until,
        request=request,
    )
    run_id = stats.run_id

    test_artists = _first_listened_artist_ids(
        conn,
        start=test_from,
        end=test_until,
        exclude_before=train_until,
    )

    rec_rows = conn.execute(
        """
        SELECT artist_id
        FROM recommendation
        WHERE run_id = ?
        ORDER BY rank
        """,
        [run_id],
    ).fetchall()
    rec_artist_ids: list[int] = [int(row[0]) for row in rec_rows]

    precision_50 = _precision_at_k(test_artists, rec_artist_ids, 50)
    recall_50 = _recall_at_k(test_artists, rec_artist_ids, 50)
    recall_100 = _recall_at_k(test_artists, rec_artist_ids, 100)
    ndcg_50 = _ndcg_at_k(test_artists, rec_artist_ids, 50)

    knownness_median = _scalar_or_zero(
        conn,
        "SELECT median(knownness) FROM recommendation WHERE run_id = ?",
        [run_id],
    )
    obviousness_median = _scalar_or_zero(
        conn,
        "SELECT median(obviousness) FROM recommendation WHERE run_id = ?",
        [run_id],
    )

    tag_diversity = _tag_diversity_at_k(conn, run_id, 20)
    rediscovery_hit_rate = _rediscovery_hit_rate(conn, run_id, test_artists)
    external_share = _external_candidate_share(conn, run_id)
    eligible_coverage = _eligible_candidate_coverage(conn, run_id, test_artists)
    novelty_rate = _novelty_rate(conn, rec_artist_ids, train_until)
    cooldown_or_repeat_rate = _cooldown_or_repeat_rate(conn, run_id)
    memberships = _source_memberships(conn, run_id)
    if config.apis.listenbrainz.enabled and config.recommendation.scoring.engine != "fusion":
        _add_radio_memberships(conn, run_id, train_until, memberships)
    if ablate_source:
        memberships.pop(ablate_source, None)
    candidate_counts = {source: len(ids) for source, ids in memberships.items()}
    future_hit_counts = {source: len(ids & test_artists) for source, ids in memberships.items()}
    unique = {
        source: ids - set().union(*(other for name, other in memberships.items() if name != source))
        for source, ids in memberships.items()
    }
    snapshot = conn.execute(
        "SELECT evidence_snapshot_id FROM recommendation_run WHERE run_id = ?", [run_id]
    ).fetchone()
    assert snapshot is not None
    families = {
        "collaborative": len(
            memberships.get("lastfm", set())
            | memberships.get("listenbrainz", set())
            | memberships.get("lastfm_similarity", set())
            | memberships.get("lastfm_walk", set())
        ),
        "semantic": len(memberships.get("lastfm_tag", set()) | memberships.get("semantic", set())),
        "catalog": len(memberships.get("musicbrainz", set())),
        "personal": len(memberships.get("local_feedback", set())),
    }
    identity_resolved_rate, identity_ambiguous_rate = _identity_coverage(conn, run_id)

    quality = run_quality_metrics(conn, run_id, test_artists, cutoff=train_until)
    graded = _graded_run_metrics(
        conn,
        run_id,
        train_until=train_until,
        test_from=test_from,
        test_until=test_until,
        evaluation_task=evaluation_task,
        target_artist_id=target_artist_id,
        judgment_experiment_ids=judgment_experiment_ids,
    )
    result = EvaluationResult(
        run_id=run_id,
        train_until=train_until.isoformat(),
        test_from=test_from.isoformat(),
        test_until=test_until.isoformat(),
        scoring_engine=config.recommendation.scoring.engine,
        recommendations_generated=stats.recommendations,
        precision_at_50=precision_50,
        recall_at_50=recall_50,
        recall_at_100=recall_100,
        ndcg_at_50=ndcg_50,
        knownness_median=knownness_median,
        obviousness_median=obviousness_median,
        tag_diversity_at_20=tag_diversity,
        rediscovery_hit_rate=rediscovery_hit_rate,
        external_candidate_share=external_share,
        eligible_candidate_coverage=eligible_coverage,
        novelty_rate=novelty_rate,
        cooldown_or_repeat_rate=cooldown_or_repeat_rate,
        candidate_count_by_source=candidate_counts,
        future_hit_count_by_source=future_hit_counts,
        unique_candidate_count_by_source={source: len(ids) for source, ids in unique.items()},
        unique_future_hit_count_by_source={
            source: len(ids & test_artists) for source, ids in unique.items()
        },
        candidate_count_by_family=families,
        evidence_snapshot_id=str(snapshot[0]),
        ablated_source=ablate_source,
        identity_resolved_rate=identity_resolved_rate,
        identity_ambiguous_rate=identity_ambiguous_rate,
        **{
            key: quality[key]
            for key in (
                "pool_candidate_count",
                "eligible_candidate_count",
                "pool_recall",
                "eligible_recall",
                "pool_coverage",
                "eligible_coverage",
                "provider_concentration",
                "multi_family_share",
                "popularity_distribution",
                "selected_slate_recall_at_50",
                "selected_slate_ndcg_at_50",
                "selected_slate_tag_diversity_at_20",
            )
        },
        runtime_seconds=perf_counter() - started,
        guardrails=run_guardrails(conn, run_id, cutoff=train_until),
        **graded,
    )

    if evaluation_task == "artist":
        assert target_artist_id is not None
        gains = _artist_adoption_gains(
            conn, start=test_from, end=test_until, exclude_before=train_until
        )
        gains.pop(target_artist_id, None)
        return ArtistEvaluationResult(
            **asdict(result),
            target_artist_id=target_artist_id,
            graded_adoption_ndcg_at_50=_adoption_ndcg_at_k(gains, rec_artist_ids, 50),
        )
    return result


def _add_radio_memberships(
    conn: DuckDBPyConnection, run_id: str, cutoff: datetime, memberships: dict[str, set[int]]
) -> None:
    # Measure the unranked set even while candidate generation is gated off. Restrict
    # it to this run's seeds and historical snapshot; repeated modes/seeds add no credit.
    row = conn.execute(
        "SELECT query_context FROM recommendation_run WHERE run_id = ?", [run_id]
    ).fetchone()
    assert row is not None
    context = json.loads(row[0])
    seeds = (
        [context["target_artist_id"]]
        if context["query_kind"] == "artist"
        else [seed["artist_id"] for seed in context["taste_region_seeds"]]
    )
    targets = conn.execute(
        "SELECT DISTINCT target_artist_id FROM artist_evidence_observation "
        "WHERE source = 'listenbrainz' AND relationship_kind = 'artist_radio_membership' "
        "AND fetched_at <= ? AND source_artist_id IN (SELECT unnest(?::UBIGINT[]))",
        [cutoff, seeds],
    ).fetchall()
    if targets:
        memberships.setdefault("listenbrainz", set()).update(
            int(target) for (target,) in targets if int(target) not in seeds
        )


def _precision_at_k(test_artists: set[int], recommendations: list[int], k: int) -> float:
    top_k = recommendations[:k]
    if not top_k:
        return 0.0
    return len(test_artists & set(top_k)) / len(top_k)


def _scalar_or_zero(
    conn: DuckDBPyConnection,
    query: str,
    params: list[object] | None = None,
) -> float:
    row = conn.execute(query, params or []).fetchone()
    if row is None or row[0] is None:
        return 0.0
    return float(row[0])


def _rediscovery_hit_rate(
    conn: DuckDBPyConnection,
    run_id: str,
    test_artists: set[int],
) -> float:
    if not test_artists:
        return 0.0
    rows = conn.execute(
        """
        SELECT artist_id
        FROM recommendation
        WHERE run_id = ?
          AND rediscovery_score > 0
        """,
        [run_id],
    ).fetchall()
    rediscovery_artist_ids = {int(row[0]) for row in rows}
    if not rediscovery_artist_ids:
        return 0.0
    hits = len(rediscovery_artist_ids & test_artists)
    return hits / len(rediscovery_artist_ids)


def _external_candidate_share(
    conn: DuckDBPyConnection,
    run_id: str,
) -> float:
    total = conn.execute(
        "SELECT count(*) FROM recommendation WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if total is None or total[0] == 0:
        return 0.0
    total_count = int(total[0])
    external_count = conn.execute(
        """
        SELECT count(*)
        FROM recommendation r
        JOIN artist a ON a.artist_id = r.artist_id
        WHERE r.run_id = ?
          AND a.resolution_status LIKE 'external_%'
        """,
        [run_id],
    ).fetchone()
    if external_count is None:
        return 0.0
    return int(external_count[0]) / total_count


def _eligible_candidate_coverage(
    conn: DuckDBPyConnection,
    run_id: str,
    test_artists: set[int],
) -> float:
    if not test_artists:
        return 0.0
    rows = conn.execute(
        """
        SELECT artist_id
        FROM recommendation_candidate
        WHERE run_id = ?
          AND eligibility_state = 'eligible'
        """,
        [run_id],
    ).fetchall()
    eligible_artist_ids = {int(row[0]) for row in rows}
    return len(test_artists & eligible_artist_ids) / len(test_artists)


def _cooldown_or_repeat_rate(conn: DuckDBPyConnection, run_id: str) -> float:
    rows = conn.execute(
        """
        SELECT eligibility_reasons
        FROM recommendation_candidate
        WHERE run_id = ?
        """,
        [run_id],
    ).fetchall()
    if not rows:
        return 0.0
    cooldown_or_hidden = 0
    for (reasons_json,) in rows:
        try:
            reasons = json.loads(reasons_json)
        except (json.JSONDecodeError, TypeError):
            continue
        excluded_reasons = {"feedback_cooldown", "hidden_artist"}
        if isinstance(reasons, list) and excluded_reasons.intersection(reasons):
            cooldown_or_hidden += 1
    return cooldown_or_hidden / len(rows)


def _evaluate_artist_windows(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    windows: Sequence[EvaluationWindow],
) -> ArtistRollingEvaluationReport:
    results: list[ArtistEvaluationResult] = []
    target_ids: set[int] = set()
    for window in windows:
        cutoff = normalize_timestamp(window.train_until)
        start, end = normalize_timestamp(window.test_from), normalize_timestamp(window.test_until)
        if start < cutoff:
            raise ValueError("test_from must not be before train_until")
        if end <= start:
            raise ValueError("test_until must be after test_from")
        history = resolve_listening_window(
            as_of=cutoff,
            default_months=config.recommendation.profile_window_months,
            since=None,
            until=cutoff,
        )
        eligible = conn.execute(
            """
            SELECT a.artist_id FROM artist a JOIN scrobble s USING (artist_key)
            WHERE NOT s.is_duplicate AND s.played_at >= ? AND s.played_at < ?
            GROUP BY a.artist_id HAVING count(*) >= ? ORDER BY a.artist_id
        """,
            [history.start, cutoff, config.recommendation.min_seed_scrobbles],
        ).fetchall()
        for (artist,) in eligible:
            target_ids.add(int(artist))
            result = evaluate_recommendations(
                conn,
                config,
                train_until=cutoff,
                test_from=start,
                test_until=end,
                evaluation_task="artist",
                target_artist_id=int(artist),
            )
            assert isinstance(result, ArtistEvaluationResult)
            results.append(result)
    denominator = len(results) or 1
    return ArtistRollingEvaluationReport(
        windows=tuple(results),
        window_count=len(windows),
        mean_recall_at_50=sum(row.recall_at_50 for row in results) / denominator,
        mean_recall_at_100=sum(row.recall_at_100 for row in results) / denominator,
        mean_ndcg_at_50=sum(row.ndcg_at_50 for row in results) / denominator,
        mean_identity_resolved_rate=sum(row.identity_resolved_rate for row in results)
        / denominator,
        **_graded_means(results),
        mean_selected_slate_recall_at_50=sum(row.selected_slate_recall_at_50 for row in results)
        / denominator,
        mean_selected_slate_ndcg_at_50=sum(row.selected_slate_ndcg_at_50 for row in results)
        / denominator,
        mean_selected_slate_tag_diversity_at_20=sum(
            row.selected_slate_tag_diversity_at_20 for row in results
        )
        / denominator,
        target_count=len(target_ids),
        run_count=len(results),
        mean_graded_adoption_ndcg_at_50=sum(row.graded_adoption_ndcg_at_50 for row in results)
        / denominator,
    )


def _artist_adoption_gains(
    conn: DuckDBPyConnection,
    *,
    start: datetime,
    end: datetime,
    exclude_before: datetime,
) -> dict[int, float]:
    """Local artist replay v1: first listen=1, repeat=2, multi-day adoption=3.

    Only artists first heard in the horizon receive credit. Future plays outside
    that horizon and duplicate imports never increase the grade.
    """
    first_heard = _first_listened_artist_ids(
        conn, start=start, end=end, exclude_before=exclude_before
    )
    rows = conn.execute(
        """
        SELECT a.artist_id, count(*), count(DISTINCT cast(s.played_at AS DATE))
        FROM scrobble s JOIN artist a USING (artist_key)
        WHERE NOT s.is_duplicate AND s.played_at >= ? AND s.played_at < ?
        GROUP BY a.artist_id ORDER BY a.artist_id
    """,
        [normalize_timestamp(start), normalize_timestamp(end)],
    ).fetchall()
    return {
        int(artist): 3.0 if days >= 2 else 2.0 if count >= 2 else 1.0
        for artist, count, days in rows
        if int(artist) in first_heard
    }


def _adoption_ndcg_at_k(gains: dict[int, float], recommendations: list[int], k: int) -> float:
    dcg = sum(
        (2 ** gains.get(artist, 0.0) - 1) / math.log2(i + 2)
        for i, artist in enumerate(recommendations[:k])
    )
    ideal = sum(
        (2**gain - 1) / math.log2(i + 2)
        for i, gain in enumerate(sorted(gains.values(), reverse=True)[:k])
    )
    return dcg / ideal if ideal else 0.0


def _graded_means(results: Sequence[EvaluationResult]) -> dict[str, Any]:
    names = (
        "graded_ndcg_at_10",
        "graded_ndcg_at_25",
        "graded_ndcg_at_50",
        "repeated_listen_recall_at_50",
        "multi_day_adoption_rate_at_50",
        "one_great_find_rate_at_50",
    )
    return {
        f"mean_{name}": sum(getattr(row, name) for row in results) / len(results)
        if results
        else 0.0
        for name in names
    }


def _graded_run_metrics(
    conn: DuckDBPyConnection,
    run_id: str,
    *,
    train_until: datetime,
    test_from: datetime,
    test_until: datetime,
    evaluation_task: str,
    target_artist_id: int | None,
    judgment_experiment_ids: Sequence[str] = (),
) -> dict[str, Any]:
    universe = _first_listened_artist_ids(
        conn, start=test_from, end=test_until, exclude_before=train_until
    )
    counts = {
        int(artist): (int(plays), int(days), int(months))
        for artist, plays, days, months in conn.execute(
            "SELECT a.artist_id, count(*), count(DISTINCT cast(s.played_at AS DATE)), "
            "count(DISTINCT date_trunc('month', s.played_at)) "
            "FROM scrobble s JOIN artist a USING (artist_key) "
            "WHERE NOT s.is_duplicate AND s.played_at >= ? AND s.played_at < ? "
            "GROUP BY a.artist_id ORDER BY a.artist_id",
            [test_from, test_until],
        ).fetchall()
    }
    labels: dict[int, list[JudgmentLabel]] = {}
    associations: list[dict[str, Any]] = []
    for experiment_id in sorted(set(judgment_experiment_ids)):
        task, definition = _experiment(conn, experiment_id)
        if task != evaluation_task or definition.get("target_artist_id") != target_artist_id:
            raise ValueError("judgment experiment task/target is incompatible")
        for (exposure,) in conn.execute(
            "SELECT exposure_id FROM recommendation_exposure WHERE experiment_id = ? "
            "ORDER BY created_at, exposure_id",
            [experiment_id],
        ).fetchall():
            effective = _effective(conn, str(exposure))
            if effective is None:
                continue
            identity = conn.execute(
                "SELECT judgment_id, created_at FROM recommendation_judgment "
                "WHERE exposure_id = ? ORDER BY created_at DESC, judgment_id DESC LIMIT 1",
                [exposure],
            ).fetchone()
            assert identity is not None
            associations.append(
                {
                    "experiment_id": experiment_id,
                    "exposure_id": str(exposure),
                    "judgment_id": str(identity[0]),
                    "judged_at": identity[1],
                }
            )
            for artist, values in effective.labels.items():
                if str(artist) not in definition["artists"]:
                    raise ValueError("judgment artist missing frozen association")
                labels.setdefault(artist, []).extend(values)
    for artist in labels:
        known = conn.execute(
            "SELECT count(*) FROM scrobble s JOIN artist a USING (artist_key) "
            "WHERE a.artist_id = ? AND NOT s.is_duplicate AND s.played_at < ?",
            [artist, train_until],
        ).fetchone()
        if known is not None and known[0] == 0:
            universe.add(artist)
    if target_artist_id is not None:
        universe.discard(target_artist_id)
    relevance: dict[str, Any] = {}
    gains: dict[int, float] = {}
    for artist in sorted(universe):
        plays, days, months = counts.get(artist, (0, 0, 0))
        grade = graded_artist_relevance(
            future_play_count=plays,
            future_active_days=days,
            future_active_months=months,
            explicit_labels=labels.get(artist, []),
        )
        gains[artist] = grade
        relevance[str(artist)] = dict(
            future_play_count=plays,
            future_active_days=days,
            future_active_months=months,
            explicit_labels=sorted(set(labels.get(artist, []))),
            grade=grade,
        )
    slate = list(
        dict.fromkeys(
            int(row[0])
            for row in conn.execute(
                "SELECT artist_id FROM recommendation WHERE run_id = ? "
                "AND coalesce(score_band, '') != 'candidate_only' ORDER BY rank, artist_id",
                [run_id],
            ).fetchall()
        )
    )
    top = set(slate[:50])
    repeated = {artist for artist in universe if counts.get(artist, (0, 0, 0))[0] >= 2}
    selected_discovery = top & universe

    def rate(numerator: int, denominator: int, population: str) -> dict[str, Any]:
        return dict(
            value=numerator / denominator if denominator else 0.0,
            numerator=numerator,
            denominator=denominator,
            k=50,
            population=population,
            available=denominator > 0,
        )

    rates = {
        "repeated_listen_recall_at_50": rate(
            len(top & repeated), len(repeated), "horizon-new artists with >=2 future plays"
        ),
        "multi_day_adoption_rate_at_50": rate(
            sum(counts.get(artist, (0, 0, 0))[1] >= 2 for artist in selected_discovery),
            len(selected_discovery),
            "selected top-50 new-discovery artists",
        ),
        "one_great_find_rate_at_50": rate(
            int(any(gains[artist] == 3 for artist in selected_discovery)),
            1,
            "evaluated query slates",
        ),
    }
    return {
        **{f"graded_ndcg_at_{k}": _adoption_ndcg_at_k(gains, slate, k) for k in (10, 25, 50)},
        **{name: metric["value"] for name, metric in rates.items()},
        "graded_metrics": {
            **rates,
            "relevance": relevance,
            "positive_relevance_count": sum(grade > 0 for grade in gains.values()),
            "ndcg_available": any(gains.values()),
            "judgment_basis": "current effective snapshots; outcome horizon bounds listening only"
            if associations
            else "implicit-only; no supplied matching judgment evidence",
            "judgments": associations,
            "already_known_count": sum("already_known" in values for values in labels.values()),
            "population": (
                "horizon-new plus compatible explicitly judged artists without prior listening"
            ),
        },
    }
