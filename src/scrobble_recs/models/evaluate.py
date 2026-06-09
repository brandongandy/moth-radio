from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime

from duckdb import DuckDBPyConnection

from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.profile_builder import build_profile
from scrobble_recs.models.recommend import generate_recommendations


@dataclass(frozen=True)
class EvaluationResult:
    train_until: str
    test_from: str
    recommendations_generated: int
    recall_at_50: float
    recall_at_100: float
    ndcg_at_50: float
    knownness_median: float
    obviousness_median: float
    tag_diversity_at_20: float
    rediscovery_hit_rate: float
    external_candidate_share: float


def evaluate_recommendations(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    train_until: datetime,
    test_from: datetime,
) -> EvaluationResult:
    build_profile(conn, config, as_of=train_until)
    stats = generate_recommendations(
        conn,
        config,
        limit=config.recommendation.similar_artists_per_seed,
        as_of=train_until,
    )
    run_id = stats.run_id

    test_rows = conn.execute(
        """
        SELECT DISTINCT a.artist_id
        FROM scrobble s
        JOIN artist a ON a.artist_key = s.artist_key
        WHERE s.played_at >= ?
          AND NOT s.is_duplicate
        """,
        [test_from],
    ).fetchall()
    test_artists: set[int] = {int(row[0]) for row in test_rows}

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

    return EvaluationResult(
        train_until=train_until.isoformat(),
        test_from=test_from.isoformat(),
        recommendations_generated=stats.recommendations,
        recall_at_50=recall_50,
        recall_at_100=recall_100,
        ndcg_at_50=ndcg_50,
        knownness_median=knownness_median,
        obviousness_median=obviousness_median,
        tag_diversity_at_20=tag_diversity,
        rediscovery_hit_rate=rediscovery_hit_rate,
        external_candidate_share=external_share,
    )


def _recall_at_k(test_artists: set[int], recommendations: list[int], k: int) -> float:
    if not test_artists:
        return 0.0
    top_k = set(recommendations[:k])
    hits = len(test_artists & top_k)
    return hits / len(test_artists)


def _ndcg_at_k(test_artists: set[int], recommendations: list[int], k: int) -> float:
    if not test_artists:
        return 0.0
    dcg = 0.0
    for i, artist_id in enumerate(recommendations[:k]):
        if artist_id in test_artists:
            dcg += 1.0 / math.log2(i + 2)
    ideal_hits = min(len(test_artists), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def _scalar_or_zero(
    conn: DuckDBPyConnection,
    query: str,
    params: list[object] | None = None,
) -> float:
    row = conn.execute(query, params or []).fetchone()
    if row is None or row[0] is None:
        return 0.0
    return float(row[0])


def _tag_diversity_at_k(
    conn: DuckDBPyConnection,
    run_id: str,
    k: int,
) -> float:
    rows = conn.execute(
        """
        SELECT explanation
        FROM recommendation
        WHERE run_id = ?
        ORDER BY rank
        LIMIT ?
        """,
        [run_id, k],
    ).fetchall()
    unique_tags: set[str] = set()
    total_tags = 0
    for (explanation_json,) in rows:
        try:
            explanation = json.loads(explanation_json)
        except (json.JSONDecodeError, TypeError):
            continue
        tags = explanation.get("tags", [])
        for tag_entry in tags:
            if isinstance(tag_entry, dict):
                tag_name = tag_entry.get("tag")
                if isinstance(tag_name, str) and tag_name:
                    unique_tags.add(tag_name)
                    total_tags += 1
    if total_tags == 0:
        return 0.0
    return len(unique_tags) / total_tags


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
