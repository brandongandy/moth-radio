"""Local target identity and strict-window relationship contracts."""

from datetime import UTC, datetime, timedelta, timezone
from math import exp

import pytest
from pydantic import ValidationError
from test_retrieval import stage_database as stage_database

from scrobble_recs.config import ArtistRelationshipConfig
from scrobble_recs.models.profile_window import ListeningWindow
from scrobble_recs.models.relationship import (
    build_target_relationship_snapshot,
    resolve_target_artist,
)


def test_target_resolution_order_normalization_and_ambiguity(stage_database):
    conn, _, _ = stage_database
    conn.execute("UPDATE artist SET musicbrainz_artist_mbid = 'mbid-seed' WHERE artist_id = 1")
    assert resolve_target_artist(conn, "mbid-seed") == 1
    assert resolve_target_artist(conn, "2") == 2
    assert resolve_target_artist(conn, " ＳＥＥＤ ") == 1
    conn.execute(
        """
            INSERT INTO artist (artist_id, canonical_name, artist_key) VALUES (5, 'SEED',
            'other'), (6, '2', 'numeric')
        """
    )
    assert resolve_target_artist(conn, "2") == 2
    with pytest.raises(ValueError, match="ambiguous.*1.*Seed.*5.*SEED"):
        resolve_target_artist(conn, "seed")
    conn.execute("UPDATE artist SET musicbrainz_artist_mbid = 'mbid-seed' WHERE artist_id = 5")
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_target_artist(conn, "mbid-seed")
    with pytest.raises(ValueError, match="target artist not found: missing"):
        resolve_target_artist(conn, "missing")


def snapshot(conn, *, end=datetime(2025, 1, 1), start=datetime(2020, 1, 1), cutoff=None):
    return build_target_relationship_snapshot(
        conn,
        artist_id=1,
        as_of=end,
        history_window=ListeningWindow(start, end),
        feedback_cutoff=cutoff or end,
        thresholds=ArtistRelationshipConfig(),
    )


def add_plays(conn, dates, *, tracks=None, albums=None):
    for i, date in enumerate(dates, 1):
        conn.execute(
            """
                INSERT INTO scrobble (scrobble_id, artist_key, track_key, album_key, played_at,
                played_date, is_duplicate) VALUES (?, 'seed', ?, ?, ?, ?, false)
            """,
            [
                i,
                tracks[i - 1] if tracks else "track",
                albums[i - 1] if albums else None,
                date,
                date.date(),
            ],
        )


@pytest.mark.parametrize(
    ("plays", "months", "old", "state"),
    [
        (0, 0, False, "unheard"),
        (5, 1, False, "sampled"),
        (6, 1, False, "sampled"),
        (19, 3, False, "sampled"),
        (20, 1, False, "sampled"),
        (20, 3, False, "established_favorite"),
        (20, 3, True, "dormant_favorite"),
        (1, 1, True, "sampled"),
    ],
)
def test_relationship_state_is_exhaustive(stage_database, plays, months, old, state):
    conn, _, _ = stage_database
    dates = [datetime(2022 if old else 2024, 1 + i % max(1, months), 1) for i in range(plays)]
    add_plays(conn, dates)
    result = snapshot(conn)
    assert result.state == state
    assert result.play_count == plays
    assert result.exposure_source == "run_appearance"
    assert result.relationship_version == "artist-relationship-v1"
    assert result.thresholds["sampled_max_plays"] == 5
    assert result.low_depth_sample is (0 < plays <= 5)


def test_relationship_metrics_exclude_future_duplicates_and_missing_keys(stage_database):
    conn, _, _ = stage_database
    end = datetime(2025, 1, 1)
    dates = [end - timedelta(days=100), end - timedelta(days=2), end - timedelta(days=1), end]
    add_plays(conn, dates, tracks=["a", "a", "", "future"], albums=["release", None, "", "future"])
    conn.execute(
        """
            INSERT INTO scrobble (scrobble_id, artist_key, track_key, played_at, played_date,
            is_duplicate) VALUES (5, 'seed', 'a', '2024-12-31', '2024-12-31', true)
        """
    )
    result = snapshot(conn, start=end - timedelta(days=180))
    assert result.play_count == 3
    assert (result.unique_tracks, result.unique_releases) == (1, 1)
    assert (result.active_days, result.active_months, result.listening_span_days) == (3, 2, 99)
    assert result.replay_concentration == pytest.approx(2 / 3)
    assert result.recent_trend == pytest.approx(2.0)
    assert result.recency_weighted_play_count == pytest.approx(
        sum(exp(-d / 400) for d in [100, 2, 1])
    )
    aware = snapshot(
        conn,
        end=end.replace(tzinfo=UTC),
        cutoff=(end + timedelta(hours=2)).replace(tzinfo=timezone(timedelta(hours=2))),
        start=end - timedelta(days=180),
    )
    assert aware == result


def test_short_window_and_feedback_exposures_use_strict_cutoff(stage_database):
    from scrobble_recs.models.feedback import record_feedback_event

    conn, _, _ = stage_database
    end = datetime(2025, 1, 1)
    add_plays(conn, [end - timedelta(days=1)])
    conn.execute(
        """
            INSERT INTO recommendation_run (run_id, created_at, mode, config_hash, seed_summary,
            scoring_weights) VALUES ('00000000-0000-0000-0000-000000000001', '2024-12-31',
            'balanced', 'x', '{}', '{}'), ('00000000-0000-0000-0000-000000000002', '2025-01-01',
            'balanced', 'x', '{}', '{}')
        """
    )
    conn.execute(
        """
            INSERT INTO recommendation (run_id, artist_id, rank) VALUES
            ('00000000-0000-0000-0000-000000000001', 1, 1),
            ('00000000-0000-0000-0000-000000000002', 1, 1)
        """
    )
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000001",
        artist_id=1,
        action="save",
        created_at=end - timedelta(days=1),
    )
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000001",
        artist_id=1,
        action="reject",
        created_at=end,
    )
    result = snapshot(conn, start=end - timedelta(days=30))
    assert result.recent_trend == 1.0
    assert result.prior_recommendation_exposures == 1
    assert result.positive_feedback == 0.2
    assert result.negative_feedback == 0.0
    assert snapshot(conn, cutoff=end - timedelta(days=1)).play_count == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(sampled_max_plays=-1),
        dict(established_min_plays=5),
        dict(established_min_active_months=-1),
        dict(dormant_days=-1),
    ],
)
def test_relationship_config_rejects_invalid_thresholds(kwargs):
    with pytest.raises(ValidationError):
        ArtistRelationshipConfig(**kwargs)


def test_dormancy_threshold_and_missing_track_singletons(stage_database):
    conn, _, _ = stage_database
    end = datetime(2025, 1, 1)
    dates = [end - timedelta(days=730 + i % 3 * 31) for i in range(20)]
    add_plays(conn, dates, tracks=[""] * 20)
    result = snapshot(conn)
    assert result.state == "dormant_favorite"
    assert result.unique_tracks == result.unique_releases == 0
    assert result.replay_concentration == 0.05
    assert snapshot(conn, end=end - timedelta(days=1)).state == "established_favorite"
