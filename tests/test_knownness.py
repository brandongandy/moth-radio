from datetime import UTC, datetime

import pytest

from scrobble_recs.models.knownness import ArtistListeningInput, compute_artist_listening_scores


def test_compute_scores_suppresses_recent_well_known_artist() -> None:
    scores = compute_artist_listening_scores(
        ArtistListeningInput(
            play_count=100,
            track_count=20,
            active_months=12,
            first_played_at=datetime(2024, 1, 1),
            last_played_at=datetime(2026, 5, 1),
        ),
        as_of=datetime(2026, 6, 4),
    )

    assert scores.lifetime_weight == pytest.approx(9.9634, rel=0.001)
    assert scores.recent_weight == pytest.approx(9.4365, rel=0.001)
    assert scores.knownness == pytest.approx(0.9873, rel=0.001)
    assert scores.rediscovery_score == 0.0


def test_compute_scores_marks_old_light_artist_as_rediscovery_candidate() -> None:
    scores = compute_artist_listening_scores(
        ArtistListeningInput(
            play_count=5,
            track_count=3,
            active_months=2,
            first_played_at=datetime(2018, 1, 1),
            last_played_at=datetime(2019, 1, 1),
        ),
        as_of=datetime(2026, 6, 4),
    )

    assert scores.knownness < 0.05
    assert scores.rediscovery_score == pytest.approx(0.6454, rel=0.001)


def test_compute_scores_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match="play_count must be non-negative"):
        compute_artist_listening_scores(
            ArtistListeningInput(
                play_count=-1,
                track_count=1,
                active_months=1,
                first_played_at=datetime(2024, 1, 1),
                last_played_at=datetime(2024, 1, 1),
            ),
            as_of=datetime(2026, 6, 4),
        )


def test_compute_scores_rejects_negative_track_count() -> None:
    with pytest.raises(ValueError, match="track_count must be non-negative"):
        compute_artist_listening_scores(
            ArtistListeningInput(
                play_count=1,
                track_count=-1,
                active_months=1,
                first_played_at=datetime(2024, 1, 1),
                last_played_at=datetime(2024, 1, 1),
            ),
            as_of=datetime(2026, 6, 4),
        )


def test_compute_scores_rejects_negative_active_months() -> None:
    with pytest.raises(ValueError, match="active_months must be non-negative"):
        compute_artist_listening_scores(
            ArtistListeningInput(
                play_count=1,
                track_count=1,
                active_months=-1,
                first_played_at=datetime(2024, 1, 1),
                last_played_at=datetime(2024, 1, 1),
            ),
            as_of=datetime(2026, 6, 4),
        )


def test_compute_scores_rejects_mixed_timezone_awareness() -> None:
    with pytest.raises(ValueError, match="datetimes must all be naive or all be timezone-aware"):
        compute_artist_listening_scores(
            ArtistListeningInput(
                play_count=1,
                track_count=1,
                active_months=1,
                first_played_at=datetime(2024, 1, 1),
                last_played_at=datetime(2024, 1, 1),
            ),
            as_of=datetime(2026, 6, 4, tzinfo=UTC),
        )


def test_compute_scores_rejects_first_play_after_last_play() -> None:
    with pytest.raises(
        ValueError,
        match="first_played_at must be before or equal to last_played_at",
    ):
        compute_artist_listening_scores(
            ArtistListeningInput(
                play_count=1,
                track_count=1,
                active_months=1,
                first_played_at=datetime(2024, 2, 1),
                last_played_at=datetime(2024, 1, 1),
            ),
            as_of=datetime(2026, 6, 4),
        )
