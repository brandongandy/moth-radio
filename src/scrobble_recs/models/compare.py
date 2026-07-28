"""Run multiple scoring engines over one temporal split and report deltas.

This is the whole point of the A/B exercise: identical split, identical DB, one
knob (the scoring engine) changed, so the metrics are directly comparable.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from duckdb import DuckDBPyConnection

from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.evaluate import _first_listened_artist_ids, _recall_at_k
from scrobble_recs.models.profile_window import normalize_timestamp, resolve_listening_window
from scrobble_recs.models.recommend import generate_recommendations


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


def compare_scorers(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    train_until: datetime,
    test_from: datetime,
    test_until: datetime,
    scorers: list[str],
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

    comparisons: list[ScorerComparison] = []
    for scorer in scorers:
        scoped = _with_engine(config, scorer)
        stats = generate_recommendations(
            conn,
            scoped,
            limit=100,
            as_of=train_until,
            history_window=history_window,
            local_history_end=train_until,
            historical_cutoff=train_until,
        )
        comparisons.append(_metrics_for_run(conn, scorer, stats.run_id, test_artists, catalog_size))
    return comparisons


def _with_engine(config: ScrobbleRecsConfig, scorer: str) -> ScrobbleRecsConfig:
    if scorer not in ("legacy", "walk"):
        raise ValueError(f"unknown scorer: {scorer}")
    scoring = config.recommendation.scoring.model_copy(update={"engine": scorer})
    recommendation = config.recommendation.model_copy(update={"scoring": scoring})
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
