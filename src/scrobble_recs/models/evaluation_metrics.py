"""Shared evaluation math and present-state run metrics, independent of orchestration."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import datetime
from statistics import median
from typing import Any

from duckdb import DuckDBPyConnection

from scrobble_recs.models.retrieval import _load_seed_popularity


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
    return _weighted_entropy([float(count) for count in counts.values()])


def _weighted_entropy(weights: Sequence[float]) -> float:
    total = sum(weights)
    return (
        -sum((weight / total) * math.log2(weight / total) for weight in weights) if total else 0.0
    )


def provider_name(source: str) -> str | None:
    if source == "local_feedback":
        return None
    if source in ("lastfm", "lastfm_tag", "lastfm_similarity", "lastfm_walk", "semantic"):
        return "lastfm"
    return source


def quantile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def metric_envelope(
    value: float | None,
    *,
    numerator: int | float | None = None,
    denominator: int = 0,
    total: int = 0,
    source: str,
    snapshot: str,
    cutoff: datetime,
    reason: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    available = value is not None and denominator > 0
    return dict(
        value=value,
        available=available,
        availability="available" if available else "unavailable",
        numerator=numerator,
        denominator=denominator,
        total_eligible_population=total,
        coverage=min(1.0, denominator / total) if total else 0.0,
        source=source,
        basis="captured present local state; retrospective screening, noncausal",
        version="promotion-guardrails-v1",
        snapshot_id=snapshot,
        cutoff=cutoff.isoformat(),
        reason=None if available else reason or "no_supported_population",
        **details,
    )


def run_guardrails(
    conn: DuckDBPyConnection,
    run_id: str,
    *,
    cutoff: datetime,
) -> dict[str, Any]:
    """Freeze honest present replay facts; never reconstruct historical experiment facts."""
    run = conn.execute(
        "SELECT evidence_snapshot_id, seed_summary FROM recommendation_run WHERE run_id = ?",
        [run_id],
    ).fetchone()
    assert run is not None
    snapshot = str(run[0])
    selected = conn.execute(
        "SELECT artist_id, explanation, knownness, start_here FROM recommendation "
        "WHERE run_id = ? AND coalesce(score_band, '') != 'candidate_only' "
        "ORDER BY rank, artist_id",
        [run_id],
    ).fetchall()
    ids = {int(row[0]) for row in selected}
    candidates = conn.execute(
        "SELECT c.artist_id, a.resolution_status, c.evidence FROM recommendation_candidate c "
        "LEFT JOIN artist a USING (artist_id) WHERE c.run_id = ? ORDER BY c.artist_id",
        [run_id],
    ).fetchall()

    def metric(
        value: float | None, denominator: int, total: int, source: str, **extra: Any
    ) -> dict[str, Any]:
        return metric_envelope(
            value,
            denominator=denominator,
            total=total,
            source=source,
            snapshot=snapshot,
            cutoff=cutoff,
            **extra,
        )

    result: dict[str, Any] = {}
    for prefix, rows in (
        ("", candidates),
        ("selected_", [row for row in candidates if row[0] in ids]),
    ):
        total = len(rows)
        supported = [row for row in rows if row[1] is not None]
        for name, statuses in (
            ("identity_resolved_rate", {"resolved_musicbrainz"}),
            ("identity_ambiguous_rate", {"ambiguous_musicbrainz", "provisional_mbid_conflict"}),
        ):
            hits = sum(row[1] in statuses for row in supported)
            result[prefix + name] = metric(
                hits / len(supported) if supported else None,
                len(supported),
                total,
                "captured replay identity-map resolution_status",
                numerator=hits,
            )
    result["novelty"] = metric(
        _novelty_rate(conn, sorted(ids), cutoff) if ids else None,
        len(ids),
        len(ids),
        "selected unique artists without pre-cutoff nonduplicate listening",
    )
    knownness = [float(row[2]) for row in selected if row[2] is not None]
    result["mean_one_minus_knownness"] = metric(
        sum(1 - value for value in knownness) / len(knownness) if knownness else None,
        len(knownness),
        len(ids),
        "persisted selected 1-knownness",
    )
    popularity = list(_load_seed_popularity(conn, ids, evidence_cutoff=cutoff).values())
    for name, p in (("q25", 0.25), ("q50", 0.5), ("q75", 0.75), ("q90", 0.9)):
        result[f"popularity_{name}"] = metric(
            quantile(popularity, p),
            len(popularity),
            len(ids),
            "cutoff-scoped Last.fm tag-chart rank-affinity proxy",
            missing_share=1 - len(popularity) / len(ids) if ids else None,
        )
    result["popularity_mean"] = metric(
        sum(popularity) / len(popularity) if popularity else None,
        len(popularity),
        len(ids),
        "cutoff-scoped Last.fm tag-chart rank-affinity proxy",
    )
    memberships = _source_memberships(conn, run_id)
    providers: dict[str, set[int]] = {}
    families: dict[int, set[str]] = {}
    source_families = {
        "lastfm": "collaborative",
        "lastfm_similarity": "collaborative",
        "lastfm_walk": "collaborative",
        "listenbrainz": "collaborative",
        "lastfm_tag": "semantic",
        "semantic": "semantic",
        "musicbrainz": "catalog",
        "local_feedback": "personal",
    }
    for source, artists in memberships.items():
        provider = provider_name(source)
        if provider:
            providers.setdefault(provider, set()).update(artists & ids)
        for artist in artists & ids:
            if source in source_families:
                families.setdefault(artist, set()).add(source_families[source])
    provider_counts = {provider: len(artists) for provider, artists in sorted(providers.items())}
    denominator = sum(provider_counts.values())
    covered = len(set().union(*providers.values())) if providers else 0
    result["provider_concentration"] = metric(
        max(provider_counts.values(), default=0) / denominator if denominator else None,
        covered,
        len(ids),
        "deduplicated external provider/artist memberships",
        provider_counts=provider_counts,
        membership_denominator=denominator,
    )
    corroborated = sum(len(values) >= 2 for values in families.values())
    result["family_agreement"] = metric(
        corroborated / len(families) if families else None,
        len(families),
        len(ids),
        "persisted source families; collaborative, semantic, catalog, personal",
        numerator=corroborated,
        family_counts={
            family: sum(family in values for values in families.values())
            for family in ("collaborative", "semantic", "catalog", "personal")
        },
    )
    explanations = [json.loads(row[1] or "{}") for row in selected]
    primary_tags = [
        tags[0]["tag"]
        for explanation in explanations[:20]
        if (tags := explanation.get("tags", []))
        and isinstance(tags[0], dict)
        and isinstance(tags[0].get("tag"), str)
        and tags[0]["tag"]
    ]
    tag_counts = {tag: primary_tags.count(tag) for tag in sorted(set(primary_tags))}
    result["tag_diversity"] = metric(
        _tag_entropy(primary_tags) if primary_tags else None,
        len(primary_tags),
        min(20, len(ids)),
        "persisted top-20 selected primary-tag Shannon entropy, bits",
        token_counts=tag_counts,
    )
    descriptors: dict[int, set[str]] = {}
    for artist, _, payload in candidates:
        if artist not in ids:
            continue
        for observation in json.loads(payload or "{}").get("observations", []):
            if observation.get("family") == "catalog":
                token = observation.get("evidence", {}).get("relationship_type")
                if isinstance(token, str) and token:
                    descriptors.setdefault(int(artist), set()).add(token)
    token_weights: dict[str, float] = {}
    for tokens in descriptors.values():
        for token in tokens:
            token_weights[token] = token_weights.get(token, 0) + 1 / len(tokens)
    result["catalog_diversity"] = metric(
        _weighted_entropy(list(token_weights.values())) if token_weights else None,
        len(descriptors),
        len(ids),
        "persisted canonical catalog relationship types, entropy bits",
        namespace="relationship_type",
        token_counts=token_weights,
    )
    fatigue = [
        explanation["fatigue"]
        for explanation in explanations
        if isinstance(explanation.get("fatigue"), dict)
    ]
    tired = sum(
        value.get("prior_run_appearances", 0) > 0 or value.get("penalty", 0) > 0
        for value in fatigue
    )
    result["fatigue"] = metric(
        tired / len(fatigue) if fatigue else None,
        len(fatigue),
        len(ids),
        "persisted selected fatigue decisions relative to pre-run state",
        numerator=tired,
    )
    sources = json.loads(run[1] or "{}").get("retrieval_diagnostics", {}).get("fusion_sources", {})
    external = [value for source, value in sources.items() if provider_name(source)]
    active = [value for value in external if value.get("status") != "disabled"]
    assessed = [
        value
        for value in active
        if value.get("status") in ("stale", "present_ranked", "present_unranked")
    ]
    stale = sum(value.get("status") == "stale" for value in assessed)
    result["stale_source_rate"] = metric(
        stale / len(assessed) if assessed else None,
        len(assessed),
        len(active),
        "persisted active external source-status assessments",
        numerator=stale,
        disabled_count=sum(value.get("status") == "disabled" for value in external),
        missing_count=sum(value.get("status") == "missing" for value in active),
        unavailable_count=sum(value.get("status") == "unavailable" for value in active),
    )
    result["provider_failure_rate"] = metric(
        None,
        0,
        0,
        "no immutable bounded acquisition-attempt history recorded",
        reason="historical_attempts_unavailable; mutable queue is insufficient",
    )
    attachments = [json.loads(row[3]) for row in selected if row[3]]
    result["start_here_attachment_coverage"] = metric(
        len(attachments) / len(ids) if ids else None,
        len(ids),
        len(ids),
        "persisted recording attachments; not recording conversion",
        numerator=len(attachments),
        attachments=attachments,
    )
    return result


def _source_memberships(conn: DuckDBPyConnection, run_id: str) -> dict[str, set[int]]:
    memberships: dict[str, set[int]] = {}
    rows = conn.execute(
        "SELECT artist_id, appeared_from_seed_count, appeared_from_tag_count, evidence "
        "FROM recommendation_candidate WHERE run_id = ?",
        [run_id],
    ).fetchall()
    for artist_id, seed_count, tag_count, payload in rows:
        evidence = json.loads(payload or "{}")
        fusion = evidence.get("fusion")
        if isinstance(fusion, dict):
            sources = (
                set(fusion.get("source_ranks", {}))
                | set(fusion.get("source_contributions", {}))
                | set(fusion.get("unranked_sources", []))
            )
            for source in sources:
                memberships.setdefault(source, set()).add(int(artist_id))
            continue
        for source, count in (("lastfm", seed_count), ("lastfm_tag", tag_count)):
            if count is not None and count > 0:
                memberships.setdefault(source, set()).add(int(artist_id))
        for observation in evidence.get("observations", []):
            source = observation["source"]
            if source not in ("lastfm", "local_feedback"):
                memberships.setdefault(source, set()).add(int(artist_id))
    if not rows or not any("fusion" in json.loads(row[3] or "{}") for row in rows):
        memberships.setdefault("lastfm", set())
        memberships.setdefault("lastfm_tag", set())
    return memberships


def _identity_coverage(conn: DuckDBPyConnection, run_id: str) -> tuple[float, float]:
    row = conn.execute(
        """
        SELECT count(DISTINCT c.artist_id),
               count(DISTINCT CASE WHEN a.resolution_status = 'resolved_musicbrainz'
                     THEN c.artist_id END),
               count(DISTINCT CASE WHEN a.resolution_status IN (
                     'ambiguous_musicbrainz', 'provisional_mbid_conflict'
               ) THEN c.artist_id END)
        FROM recommendation_candidate c
        LEFT JOIN artist a ON a.artist_id = c.artist_id
        WHERE c.run_id = ?
        """,
        [run_id],
    ).fetchone()
    if row is None or row[0] == 0:
        return 0.0, 0.0
    return int(row[1]) / int(row[0]), int(row[2]) / int(row[0])


def _first_listened_artist_ids(
    conn: DuckDBPyConnection,
    *,
    start: datetime,
    end: datetime,
    exclude_before: datetime,
) -> set[int]:
    """Return artists first heard in the evaluation horizon after training."""
    rows = conn.execute(
        """
        SELECT a.artist_id
        FROM scrobble AS s
        JOIN artist AS a ON a.artist_key = s.artist_key
        WHERE NOT s.is_duplicate
        GROUP BY a.artist_id
        HAVING min(s.played_at) >= ?
           AND min(s.played_at) < ?
           AND min(s.played_at) >= ?
        """,
        [start, end, exclude_before],
    ).fetchall()
    return {int(row[0]) for row in rows}


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


def _tag_diversity_at_k(
    conn: DuckDBPyConnection,
    run_id: str,
    k: int,
    *,
    selected_only: bool = False,
) -> float:
    rows = conn.execute(
        """
        SELECT explanation
        FROM recommendation
        WHERE run_id = ? AND (NOT ? OR score_band != 'candidate_only')
        ORDER BY rank
        LIMIT ?
        """,
        [run_id, selected_only, k],
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


def _novelty_rate(
    conn: DuckDBPyConnection,
    recommendation_artist_ids: list[int],
    train_until: datetime,
) -> float:
    if not recommendation_artist_ids:
        return 0.0
    rows = conn.execute(
        """
        SELECT DISTINCT a.artist_id
        FROM scrobble AS s
        JOIN artist AS a ON a.artist_key = s.artist_key
        WHERE NOT s.is_duplicate
          AND s.played_at < ?
          AND a.artist_id IN (SELECT unnest(?))
        """,
        [train_until, recommendation_artist_ids],
    ).fetchall()
    known_artist_ids = {int(row[0]) for row in rows}
    return (len(recommendation_artist_ids) - len(known_artist_ids)) / len(recommendation_artist_ids)


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
        if provider := provider_name(source):
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
