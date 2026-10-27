"""Run multiple scoring engines over one temporal split and report deltas.

This is the whole point of the A/B exercise: identical split, identical DB, one
knob (the scoring engine) changed, so the metrics are directly comparable.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

from duckdb import DuckDBPyConnection, connect

from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.evaluate import (
    _first_listened_artist_ids,
    _identity_coverage,
    _ndcg_at_k,
    _novelty_rate,
    _recall_at_k,
    _source_memberships,
    _tag_diversity_at_k,
)
from scrobble_recs.models.profile_window import normalize_timestamp, resolve_listening_window
from scrobble_recs.models.recommend import generate_recommendations
from scrobble_recs.models.recommendation_query import (
    RecommendationRequest,
    capture_evidence_snapshot,
)
from scrobble_recs.models.retrieval import _load_seed_popularity


@dataclass(frozen=True)
class ScorerComparison:
    scorer: str
    recommendations: int
    hit_rate_at_10: float
    hit_rate_at_25: float
    hit_rate_at_100: float
    catalog_coverage: float
    knownness_mean: float
    knownness_median: float
    tag_entropy: float
    mean_hop_distance: float | None
    evidence_snapshot_id: str = ""
    stable_state_id: str = ""
    query_kind: str = "general"
    target_artist_id: int | None = None
    pool_candidate_count: int = 0
    eligible_candidate_count: int = 0
    pool_recall: float = 0.0
    eligible_recall: float = 0.0
    pool_coverage: float = 0.0
    eligible_coverage: float = 0.0
    slate_recall_at_50: float = 0.0
    ndcg_at_50: float = 0.0
    identity_resolved_rate: float = 0.0
    identity_ambiguous_rate: float = 0.0
    novelty_rate: float = 0.0
    popularity_distribution: dict[str, float] = field(default_factory=dict)
    tag_diversity_at_20: float = 0.0
    provider_concentration: float = 0.0
    multi_family_share: float = 0.0
    runtime_seconds: float = 0.0
    selected_slate_recall_at_50: float = 0.0
    selected_slate_ndcg_at_50: float = 0.0
    selected_slate_tag_diversity_at_20: float = 0.0

    candidate_count_by_source: dict[str, int] = field(default_factory=dict)
    candidate_added_by_source: dict[str, int] = field(default_factory=dict)
    candidate_removed_by_source: dict[str, int] = field(default_factory=dict)
    promotion_status: str = "not_promoted"
    missing_promotion_evidence: tuple[str, ...] = (
        "missing_blinded_evidence",
        "incomplete_guardrails",
    )


def compare_scorers(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    train_until: datetime,
    test_from: datetime,
    test_until: datetime,
    scorers: list[str],
    ablate_sources: list[str] | None = None,
    request: RecommendationRequest | None = None,
) -> list[ScorerComparison]:
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
    test_artists = _first_listened_artist_ids(
        conn,
        start=test_from,
        end=test_until,
        exclude_before=train_until,
    )
    catalog_size = _catalog_size(conn)

    request = request or RecommendationRequest()
    variants = [(scorer, _with_engine(config, scorer)) for scorer in scorers]
    if ablate_sources and "fusion" not in scorers:
        variants.append(("fusion", _with_engine(config, "fusion")))
    for source in ablate_sources or ():
        scoped = _with_engine(config, "fusion")
        # Pydantic validation rejects unsupported names rather than silently doing nothing.
        scoped.recommendation.fusion = type(scoped.recommendation.fusion).model_validate(
            {**scoped.recommendation.fusion.model_dump(), "excluded_sources": [source]}
        )
        variants.append((f"fusion-without-{source}", scoped))
    comparisons: list[ScorerComparison] = []
    pools: dict[str, set[int]] = {}
    stable_state = _comparison_state_id(conn)
    snapshot = capture_evidence_snapshot(conn, historical_cutoff=train_until).snapshot_id
    with TemporaryDirectory(prefix="scrobble-compare-") as directory:
        export = str(Path(directory) / "snapshot").replace("'", "''")
        conn.execute("BEGIN TRANSACTION")
        try:
            if _comparison_state_id(conn) != stable_state:
                raise ValueError("comparison state changed before capture")
            conn.execute(f"EXPORT DATABASE '{export}' (FORMAT PARQUET)")
        finally:
            conn.execute("ROLLBACK")
        for index, (scorer, scoped) in enumerate(variants):
            isolated = connect(str(Path(directory) / f"variant-{index}.duckdb"))
            try:
                isolated.execute(f"IMPORT DATABASE '{export}'")
                if _comparison_state_id(isolated) != stable_state:
                    raise ValueError("comparison state changed between variants")
                started = perf_counter()
                stats = generate_recommendations(
                    isolated,
                    scoped,
                    limit=100,
                    as_of=train_until,
                    history_window=history_window,
                    local_history_end=train_until,
                    historical_cutoff=train_until,
                    request=request,
                )
                actual = isolated.execute(
                    "SELECT evidence_snapshot_id FROM recommendation_run WHERE run_id = ?",
                    [stats.run_id],
                ).fetchone()
                if actual is None or actual[0] != snapshot:
                    raise ValueError("comparison evidence snapshot changed")
                measured = _metrics_for_run(
                    isolated, scorer, stats.run_id, test_artists, catalog_size
                )
                measured = replace(
                    measured,
                    **run_quality_metrics(
                        isolated,
                        stats.run_id,
                        test_artists,
                        cutoff=train_until,
                    ),
                    stable_state_id=stable_state,
                    evidence_snapshot_id=snapshot,
                    query_kind=request.query_kind,
                    target_artist_id=request.target_artist_id,
                    runtime_seconds=perf_counter() - started,
                )
                pools[scorer] = {
                    int(row[0])
                    for row in isolated.execute(
                        "SELECT artist_id FROM recommendation_candidate WHERE run_id = ?",
                        [stats.run_id],
                    ).fetchall()
                }
                comparisons.append(measured)
            finally:
                isolated.close()
        if _comparison_state_id(conn) != stable_state:
            raise ValueError("comparison state changed during comparison")
    base = pools.get("fusion", set())
    for index, row in enumerate(comparisons):
        if row.scorer.startswith("fusion-without-"):
            source = row.scorer.removeprefix("fusion-without-")
            comparisons[index] = replace(
                row,
                candidate_added_by_source={source: len(base - pools[row.scorer])},
                candidate_removed_by_source={source: len(pools[row.scorer] - base)},
            )
    return comparisons


def _comparison_state_id(conn: DuckDBPyConnection) -> str:
    """Content check, including legacy values and prior runs; an ID is not a DB copy."""
    digest = sha256()
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_name"
        ).fetchall()
    ]
    for table in tables:
        digest.update(table.encode())
        cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY ALL')
        while rows := cursor.fetchmany(1000):
            for row in rows:
                digest.update(repr(row).encode())
                digest.update(b"\n")
    return digest.hexdigest()


def run_quality_metrics(
    conn: DuckDBPyConnection,
    run_id: str,
    test_artists: set[int],
    *,
    cutoff: datetime,
) -> dict[str, Any]:
    """Additional pool/eligible/slate metrics; older metric definitions stay intact."""
    # These new coverage fields include tag/catalog/radio identities beyond the
    # legacy graph denominator retained by catalog_coverage.
    universe_row = conn.execute("SELECT count(*) FROM artist").fetchone()
    assert universe_row is not None
    universe_size = int(universe_row[0])
    candidates = conn.execute(
        "SELECT artist_id, eligibility_state, feature_record, evidence "
        "FROM recommendation_candidate WHERE run_id = ?",
        [run_id],
    ).fetchall()
    pool = {int(row[0]) for row in candidates}
    eligible = {int(row[0]) for row in candidates if row[1] == "eligible"}
    slate = [
        int(row[0])
        for row in conn.execute(
            "SELECT artist_id FROM recommendation WHERE run_id = ? "
            "AND score_band != 'candidate_only' ORDER BY rank",
            [run_id],
        ).fetchall()
    ]
    selected = set(slate)
    memberships = _source_memberships(conn, run_id)
    providers: dict[str, set[int]] = {}
    for source, ids in memberships.items():
        if source == "local_feedback":
            continue
        provider = (
            "lastfm"
            if source in ("lastfm", "lastfm_tag", "lastfm_similarity", "lastfm_walk", "semantic")
            else source
        )
        providers.setdefault(provider, set()).update(ids)
    provider_counts = [len(ids & selected) for ids in providers.values()]
    denominator = sum(provider_counts)
    families = []
    for artist, _, features, evidence in candidates:
        if artist in selected:
            values = json.loads(features or "{}").get("values", {})
            families.append(
                values.get(
                    "fusion_independent_family_count",
                    len(
                        {
                            row["family"]
                            for row in json.loads(evidence or "{}").get("observations", [])
                        }
                    ),
                )
            )
    # Reuse the tag-chart rank-affinity popularity proxy at the same cutoff.
    popularity = list(_load_seed_popularity(conn, selected, evidence_cutoff=cutoff).values())
    resolved, ambiguous = _identity_coverage(conn, run_id)
    slate_diversity = _tag_diversity_at_k(conn, run_id, 20, selected_only=True)
    return dict(
        pool_candidate_count=len(pool),
        eligible_candidate_count=len(eligible),
        pool_recall=len(pool & test_artists) / len(test_artists) if test_artists else 0.0,
        eligible_recall=len(eligible & test_artists) / len(test_artists) if test_artists else 0.0,
        pool_coverage=len(pool) / universe_size if universe_size else 0.0,
        eligible_coverage=len(eligible) / universe_size if universe_size else 0.0,
        slate_recall_at_50=_recall_at_k(test_artists, slate, 50),
        ndcg_at_50=_ndcg_at_k(test_artists, slate, 50),
        identity_resolved_rate=resolved,
        identity_ambiguous_rate=ambiguous,
        novelty_rate=_novelty_rate(conn, slate, cutoff),
        tag_diversity_at_20=slate_diversity,
        selected_slate_recall_at_50=_recall_at_k(test_artists, slate, 50),
        selected_slate_ndcg_at_50=_ndcg_at_k(test_artists, slate, 50),
        selected_slate_tag_diversity_at_20=_tag_diversity_at_k(
            conn, run_id, 20, selected_only=True
        ),
        popularity_distribution={
            "mean": sum(popularity) / len(popularity) if popularity else 0.0,
            "median": float(median(popularity)) if popularity else 0.0,
            "missing_share": 1 - len(popularity) / len(slate) if slate else 0.0,
        },
        provider_concentration=max(provider_counts, default=0) / denominator
        if denominator
        else 0.0,
        multi_family_share=sum(n >= 2 for n in families) / len(families) if families else 0.0,
        candidate_count_by_source={source: len(ids) for source, ids in memberships.items()},
    )


def _with_engine(config: ScrobbleRecsConfig, scorer: str) -> ScrobbleRecsConfig:
    if scorer not in ("legacy", "walk", "fusion"):
        raise ValueError(f"unknown scorer: {scorer}")
    scoring = config.recommendation.scoring.model_copy(update={"engine": scorer})
    fusion = config.recommendation.fusion.model_copy(update={"enabled": scorer == "fusion"})
    recommendation = config.recommendation.model_copy(update={"scoring": scoring, "fusion": fusion})
    return config.model_copy(update={"recommendation": recommendation})


def _catalog_size(conn: DuckDBPyConnection) -> int:
    row = conn.execute(
        """
        SELECT count(*) FROM (
            SELECT source_artist_id AS artist_id
            FROM artist_similarity_edge WHERE source = 'lastfm'
            UNION
            SELECT target_artist_id FROM artist_similarity_edge WHERE source = 'lastfm'
        )
        """
    ).fetchone()
    return int(row[0]) if row else 0


def _metrics_for_run(
    conn: DuckDBPyConnection,
    scorer: str,
    run_id: str,
    test_artists: set[int],
    catalog_size: int,
) -> ScorerComparison:
    rows = conn.execute(
        """
        SELECT artist_id, knownness, hop_distance, explanation
        FROM recommendation
        WHERE run_id = ? AND score_band != 'candidate_only'
        ORDER BY rank
        """,
        [run_id],
    ).fetchall()
    artist_ids = [int(row[0]) for row in rows]
    knownness_values = [float(row[1]) for row in rows]
    hop_values = [int(row[2]) for row in rows if row[2] is not None]
    primary_tags = [_primary_tag(row[3]) for row in rows]

    return ScorerComparison(
        scorer=scorer,
        recommendations=len(artist_ids),
        hit_rate_at_10=_recall_at_k(test_artists, artist_ids, 10),
        hit_rate_at_25=_recall_at_k(test_artists, artist_ids, 25),
        hit_rate_at_100=_recall_at_k(test_artists, artist_ids, 100),
        catalog_coverage=(len(set(artist_ids)) / catalog_size) if catalog_size else 0.0,
        knownness_mean=(sum(knownness_values) / len(knownness_values)) if knownness_values else 0.0,
        knownness_median=float(median(knownness_values)) if knownness_values else 0.0,
        tag_entropy=_tag_entropy([tag for tag in primary_tags if tag is not None]),
        mean_hop_distance=(sum(hop_values) / len(hop_values)) if hop_values else None,
    )


def _primary_tag(explanation_json: object) -> str | None:
    if not isinstance(explanation_json, str | bytes | bytearray):
        return None
    try:
        explanation = json.loads(explanation_json)
    except (json.JSONDecodeError, TypeError):
        return None
    tags = explanation.get("tags", []) if isinstance(explanation, dict) else []
    if tags and isinstance(tags[0], dict):
        tag = tags[0].get("tag")
        if isinstance(tag, str) and tag:
            return tag
    return None


def _tag_entropy(tags: list[str]) -> float:
    if not tags:
        return 0.0
    counts: dict[str, int] = {}
    for tag in tags:
        counts[tag] = counts.get(tag, 0) + 1
    total = len(tags)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())
