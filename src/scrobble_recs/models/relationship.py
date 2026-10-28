"""Local exact target resolution and inspectable, cutoff-safe relationship math."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta

from duckdb import DuckDBPyConnection

from scrobble_recs.config import ArtistRelationshipConfig, ScrobbleRecsConfig
from scrobble_recs.models.feedback import (
    DIRECT_ARTIST_ADJUSTMENTS,
    _feedback_artist_ids,
    _parse_context,
)
from scrobble_recs.models.knownness import recency_decay
from scrobble_recs.models.modes import RecommendationModeSettings
from scrobble_recs.models.profile_window import ListeningWindow, normalize_timestamp
from scrobble_recs.models.recommendation_query import (
    ArtistQuerySettings,
    RelationshipState,
    TargetRelationshipSnapshot,
)
from scrobble_recs.normalize.strings import normalize_key


def resolve_target_artist(conn: DuckDBPyConnection, reference: str) -> int:
    """Resolve MBID, positive ID, then normalized canonical name; never choose ties."""
    reference = reference.strip()
    rows = (
        conn.execute(
            """
                SELECT artist_id, canonical_name FROM artist WHERE musicbrainz_artist_mbid = ?
                ORDER BY artist_id
            """,
            [reference],
        ).fetchall()
        if reference
        else []
    )
    if not rows and reference.isascii() and reference.isdecimal() and int(reference) > 0:
        # Compare text to avoid overflow on arbitrary numeric references.
        rows = conn.execute(
            """
                SELECT artist_id, canonical_name FROM artist WHERE cast(artist_id AS VARCHAR) =
                ? ORDER BY artist_id
            """,
            [str(int(reference))],
        ).fetchall()
    if not rows and reference:
        rows = [
            row
            for row in conn.execute(
                "SELECT artist_id, canonical_name FROM artist ORDER BY artist_id"
            ).fetchall()
            if normalize_key(str(row[1])) == normalize_key(reference)
        ]
    if not rows:
        raise ValueError(f"target artist not found: {reference}")
    if len(rows) > 1:
        raise ValueError(
            f"ambiguous target artist {reference}: "
            + ", ".join(f"{artist}: {name}" for artist, name in rows)
        )
    return int(rows[0][0])


def build_target_relationship_snapshot(
    conn: DuckDBPyConnection,
    *,
    artist_id: int,
    as_of: datetime,
    history_window: ListeningWindow,
    feedback_cutoff: datetime,
    thresholds: ArtistRelationshipConfig,
) -> TargetRelationshipSnapshot:
    as_of = normalize_timestamp(as_of)
    cutoff = min(as_of, normalize_timestamp(feedback_cutoff))
    end = min(history_window.end, cutoff)
    rows = conn.execute(
        """
        SELECT s.played_at, s.track_key, s.album_key FROM scrobble s
        JOIN artist a ON a.artist_key = s.artist_key
        WHERE a.artist_id = ? AND NOT s.is_duplicate
          AND s.played_at >= ? AND s.played_at < ? ORDER BY s.played_at, s.scrobble_id
    """,
        [artist_id, history_window.start, end],
    ).fetchall()
    dates = [normalize_timestamp(row[0]) for row in rows]
    first, last = (dates[0], dates[-1]) if dates else (None, None)
    count = len(rows)
    months = len({(date.year, date.month) for date in dates})
    established = (
        count >= thresholds.established_min_plays
        and months >= thresholds.established_min_active_months
    )
    state: RelationshipState = (
        "unheard"
        if not count
        else "dormant_favorite"
        if established and last is not None and (as_of - last).days >= thresholds.dormant_days
        else "established_favorite"
        if established
        else "sampled"
    )
    recent_start = max(history_window.start, end - timedelta(days=90))
    recent_days = max(0.0, (end - recent_start).total_seconds() / 86400)
    earlier_days = max(0.0, (recent_start - history_window.start).total_seconds() / 86400)
    recent_count = sum(date >= recent_start for date in dates)
    recent_rate = recent_count / recent_days if recent_days else 0.0
    earlier_rate = (count - recent_count) / earlier_days if earlier_days else 0.0
    trend = recent_rate / earlier_rate if earlier_rate else 1.0 if recent_rate else 0.0
    # Unknown tracks contribute individual singleton groups, never one artificial hit.
    groups = Counter(str(track) for _, track, _ in rows if track)
    largest_group = max(max(groups.values(), default=0), int(bool(count)))
    positive, negative = 0.0, 0.0
    for feedback_artist, action, value, context in conn.execute(
        """
            SELECT artist_id, action, value, context FROM feedback_event WHERE created_at < ?
            ORDER BY created_at, feedback_id
        """,
        [cutoff],
    ).fetchall():
        if artist_id in _feedback_artist_ids(conn, int(feedback_artist), _parse_context(context)):
            adjustment = DIRECT_ARTIST_ADJUSTMENTS.get(str(action), 0.0) * (
                1.0 if value is None else float(value)
            )
            positive += max(0.0, adjustment)
            negative += max(0.0, -adjustment)
    exposure = conn.execute(
        """
            SELECT count(*) FROM recommendation r JOIN recommendation_run run USING (run_id)
            WHERE r.artist_id = ? AND run.created_at < ?
        """,
        [artist_id, cutoff],
    ).fetchone()
    assert exposure is not None
    exposure_count, exposure_source = int(exposure[0]), "run_appearance"
    table_exists = conn.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name = 'recommendation_exposure' AND table_schema = 'main'"
    ).fetchone()
    if table_exists and table_exists[0]:
        recorded = conn.execute(
            "SELECT shown_items FROM recommendation_exposure WHERE created_at < ?", [cutoff]
        ).fetchall()
        if recorded:
            exposure_source = "recorded_exposure"
            exposure_count = sum(
                artist_id in {item["artist_id"] for item in json.loads(row[0])} for row in recorded
            )
    return TargetRelationshipSnapshot(
        state=state,
        play_count=count,
        recency_weighted_play_count=sum(recency_decay((as_of - date).days) for date in dates),
        unique_tracks=len({track for _, track, _ in rows if track}),
        unique_releases=len({album for _, _, album in rows if album}),
        active_days=len({date.date() for date in dates}),
        active_months=months,
        first_listen=first,
        last_listen=last,
        listening_span_days=(last - first).days if last and first else 0,
        recent_trend=trend,
        replay_concentration=largest_group / count if count else 0.0,
        positive_feedback=positive,
        negative_feedback=negative,
        prior_recommendation_exposures=exposure_count,
        exposure_source=exposure_source,
        thresholds=thresholds.model_dump(),
        low_depth_sample=0 < count <= thresholds.sampled_max_plays,
    )


def artist_query_settings(
    snapshot: TargetRelationshipSnapshot,
    *,
    config: ScrobbleRecsConfig,
    mode: RecommendationModeSettings,
) -> ArtistQuerySettings:
    """Default to existing scorer controls; state variations require explicit opt-in."""
    challenger = config.recommendation.artist_challenger
    if not challenger.enabled:
        return ArtistQuerySettings(
            profile="control",
            version="artist-control-v1",
            max_hops=config.recommendation.walk.max_hops,
            popularity_preference=0.0,
            obviousness_penalty_weight=mode.obviousness_penalty_weight,
            known_neighbor_penalty=0.0,
            target_weight=0.0,
            personal_weight=0.0,
            existing_weight=1.0,
            structural_membership_prior=0.0,
            hop_proximity_scale=0.0,
            scoring_engine=config.recommendation.scoring.engine,
            component_weights=config.recommendation.weights.model_dump(),
        )
    state = getattr(challenger, snapshot.state)
    return ArtistQuerySettings(
        profile="challenger",
        version=challenger.version,
        max_hops=min(state.max_hops, config.recommendation.walk.max_hops),
        popularity_preference=state.popularity_preference,
        obviousness_penalty_weight=state.obviousness_penalty_weight,
        known_neighbor_penalty=challenger.known_neighbor_penalty,
        target_weight=challenger.target_weight,
        personal_weight=challenger.personal_weight,
        existing_weight=challenger.existing_weight,
        structural_membership_prior=challenger.structural_membership_prior,
        hop_proximity_scale=challenger.hop_proximity_scale,
        scoring_engine=config.recommendation.scoring.engine,
        component_weights=config.recommendation.weights.model_dump(),
    )
