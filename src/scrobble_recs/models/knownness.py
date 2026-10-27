from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import exp, log1p

# Constants are calibrated to the approved Task 2 behavioral examples.
_TRACK_DIVERSITY_WEIGHT = 0.15
_ACTIVE_MONTH_LIFETIME_WEIGHT = 0.188
_RECENT_DECAY_DAYS = 400
_KNOWNNESS_ACTIVE_MONTHS = 12
_KNOWNNESS_DECAY_DAYS = 1600
_REDISCOVERY_DORMANCY_DAYS = 365 * 9.4


@dataclass(frozen=True)
class ArtistListeningInput:
    play_count: int
    track_count: int
    active_months: int
    first_played_at: datetime
    last_played_at: datetime


@dataclass(frozen=True)
class ArtistListeningScores:
    lifetime_weight: float
    recent_weight: float
    knownness: float
    rediscovery_score: float


def compute_artist_listening_scores(
    artist: ArtistListeningInput,
    *,
    as_of: datetime,
) -> ArtistListeningScores:
    if artist.play_count < 0:
        raise ValueError("play_count must be non-negative")
    if artist.track_count < 0:
        raise ValueError("track_count must be non-negative")
    if artist.active_months < 0:
        raise ValueError("active_months must be non-negative")
    _validate_datetimes(artist, as_of)

    days_since_last_play = max((as_of - artist.last_played_at).days, 0)
    lifetime_weight = (
        log1p(artist.play_count)
        * (1.0 + _TRACK_DIVERSITY_WEIGHT * log1p(artist.track_count))
        * (1.0 + _ACTIVE_MONTH_LIFETIME_WEIGHT * log1p(artist.active_months))
    )
    recency_weight = recency_decay(days_since_last_play)
    recent_weight = lifetime_weight * (0.35 + 0.65 * recency_weight)
    knownness = (
        min(1.0, log1p(artist.play_count) / log1p(50))
        * min(1.0, artist.active_months / _KNOWNNESS_ACTIVE_MONTHS)
        * (0.40 + 0.60 * exp(-days_since_last_play / _KNOWNNESS_DECAY_DAYS))
    )
    rediscovery_score = _rediscovery_score(
        play_count=artist.play_count,
        days_since_last_play=days_since_last_play,
        knownness=knownness,
    )
    return ArtistListeningScores(
        lifetime_weight=lifetime_weight,
        recent_weight=recent_weight,
        knownness=knownness,
        rediscovery_score=rediscovery_score,
    )


def _validate_datetimes(artist: ArtistListeningInput, as_of: datetime) -> None:
    if not _has_same_timezone_awareness(
        artist.first_played_at,
        artist.last_played_at,
        as_of,
    ):
        raise ValueError("datetimes must all be naive or all be timezone-aware")
    if artist.first_played_at > artist.last_played_at:
        raise ValueError("first_played_at must be before or equal to last_played_at")


def _has_same_timezone_awareness(*values: datetime) -> bool:
    modes = {_is_timezone_aware(value) for value in values}
    return len(modes) == 1


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _rediscovery_score(*, play_count: int, days_since_last_play: int, knownness: float) -> float:
    if play_count < 1 or play_count > 20 or days_since_last_play < 730:
        return 0.0

    play_band = 1.0 - abs(play_count - 8) / 20
    dormancy = min(1.0, days_since_last_play / _REDISCOVERY_DORMANCY_DAYS)
    novelty = 1.0 - knownness
    return max(0.0, min(1.0, play_band * dormancy * novelty))


def recency_decay(days_since_play: int) -> float:
    """The existing 400-day profile decay, shared with target snapshots."""
    return exp(-max(days_since_play, 0) / _RECENT_DECAY_DAYS)
