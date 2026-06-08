from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from duckdb import DuckDBPyConnection

from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.listening import refresh_artist_listening_scores
from scrobble_recs.models.profile import SeedArtist, select_seed_artists

_PROFILE_SOURCE = "lastfm"
_MAX_TOP_SEED_ARTISTS = 10


@dataclass(frozen=True)
class BuildProfileStats:
    artists_updated: int
    tag_weights: int
    genre_clusters: int


@dataclass(frozen=True)
class _TagContribution:
    artist_id: int
    artist: str
    seed_weight: float
    tag_weight: float
    contribution: float
    regions: tuple[str, ...]


@dataclass(frozen=True)
class _TagProfileEntry:
    tag: str
    source: str
    user_weight: float
    contributions: tuple[_TagContribution, ...]

    @property
    def artist_count(self) -> int:
        return len({contribution.artist_id for contribution in self.contributions})


def build_profile(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    as_of: datetime | None = None,
) -> BuildProfileStats:
    resolved_as_of = as_of or datetime.now(tz=UTC).replace(tzinfo=None)
    conn.execute("BEGIN TRANSACTION")
    try:
        listening_stats = refresh_artist_listening_scores(conn, as_of=resolved_as_of)
        seeds = select_seed_artists(
            conn,
            limit=config.recommendation.seed_artist_limit,
            min_scrobbles=config.recommendation.min_seed_scrobbles,
        )
        tag_profile = build_user_tag_profile(
            conn,
            seeds,
            weak_tags=config.recommendation.tag_profile.weak_tags,
            weak_tag_penalty=config.recommendation.tag_profile.weak_tag_penalty,
        )
        _persist_tag_profile(conn, tag_profile)
        clusters = _build_genre_clusters(tag_profile)
        _persist_genre_clusters(conn, clusters)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return BuildProfileStats(
        artists_updated=listening_stats.artists_updated,
        tag_weights=len(tag_profile),
        genre_clusters=len(clusters),
    )


def build_user_tag_profile(
    conn: DuckDBPyConnection,
    seeds: list[SeedArtist],
    *,
    weak_tags: list[str] | None = None,
    weak_tag_penalty: float = 1.0,
) -> list[_TagProfileEntry]:
    seed_by_id = {seed.artist_id: seed for seed in seeds}
    if not seed_by_id:
        return []

    normalized_weak_tags = {
        _normalize_tag_key(tag)
        for tag in (weak_tags or [])
        if _normalize_tag_key(tag) != ""
    }

    raw_weights: dict[str, float] = {}
    contributions: dict[str, list[_TagContribution]] = {}
    rows = conn.execute(
        """
        SELECT artist_id, tag, normalized_weight
        FROM artist_tag
        WHERE source = ?
          AND artist_id IN (SELECT unnest(?))
        """,
        [_PROFILE_SOURCE, sorted(seed_by_id)],
    ).fetchall()

    artists_by_tag: dict[str, set[int]] = {}
    for artist_id, tag, _normalized_weight in rows:
        normalized_tag = _normalize_tag_key(tag)
        if normalized_tag:
            artists_by_tag.setdefault(normalized_tag, set()).add(int(artist_id))

    for artist_id, tag, normalized_weight in rows:
        seed = seed_by_id.get(int(artist_id))
        if seed is None:
            continue
        normalized_tag = _normalize_tag_key(tag)
        if normalized_tag == "":
            continue
        tag_weight = float(normalized_weight)
        specificity = _tag_specificity(len(artists_by_tag.get(normalized_tag, set())))
        weak_multiplier = (
            weak_tag_penalty if normalized_tag in normalized_weak_tags else 1.0
        )
        contribution = seed.seed_weight * tag_weight * specificity * weak_multiplier
        if contribution <= 0.0:
            continue
        raw_weights[normalized_tag] = raw_weights.get(normalized_tag, 0.0) + contribution
        contributions.setdefault(normalized_tag, []).append(
            _TagContribution(
                artist_id=seed.artist_id,
                artist=seed.name,
                seed_weight=seed.seed_weight,
                tag_weight=tag_weight,
                contribution=contribution,
                regions=seed.regions,
            )
        )

    max_weight = max(raw_weights.values(), default=0.0)
    if max_weight <= 0.0:
        return []
    return [
        _TagProfileEntry(
            tag=tag,
            source=_PROFILE_SOURCE,
            user_weight=weight / max_weight,
            contributions=tuple(_sort_contributions(contributions[tag])),
        )
        for tag, weight in sorted(raw_weights.items(), key=lambda item: (-item[1], item[0]))
    ]


def _persist_tag_profile(
    conn: DuckDBPyConnection,
    tag_profile: list[_TagProfileEntry],
) -> None:
    conn.execute("DELETE FROM tag_profile WHERE source = ?", [_PROFILE_SOURCE])
    for entry in tag_profile:
        conn.execute(
            """
            INSERT INTO tag_profile (
                tag,
                source,
                user_weight,
                artist_count,
                top_seed_artists,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, current_timestamp)
            """,
            [
                entry.tag,
                entry.source,
                entry.user_weight,
                entry.artist_count,
                json.dumps(_top_seed_artist_payload(entry.contributions), sort_keys=True),
            ],
        )


def _build_genre_clusters(
    tag_profile: list[_TagProfileEntry],
) -> list[dict[str, object]]:
    return [
        {
            "cluster_key": _cluster_key(entry),
            "cluster_name": entry.tag,
            "source": entry.source,
            "tag_weights": {entry.tag: entry.user_weight},
            "seed_artists": _top_seed_artist_payload(entry.contributions),
            "artist_count": entry.artist_count,
        }
        for entry in tag_profile
    ]


def _persist_genre_clusters(
    conn: DuckDBPyConnection,
    clusters: list[dict[str, object]],
) -> None:
    conn.execute("DELETE FROM profile_cluster WHERE source = ?", [_PROFILE_SOURCE])
    for cluster in clusters:
        conn.execute(
            """
            INSERT INTO profile_cluster (
                cluster_key,
                cluster_name,
                source,
                tag_weights,
                seed_artists,
                artist_count,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, current_timestamp)
            """,
            [
                cluster["cluster_key"],
                cluster["cluster_name"],
                cluster["source"],
                json.dumps(cluster["tag_weights"], sort_keys=True),
                json.dumps(cluster["seed_artists"], sort_keys=True),
                cluster["artist_count"],
            ],
        )


def _top_seed_artist_payload(
    contributions: tuple[_TagContribution, ...],
) -> list[dict[str, object]]:
    return [
        {
            "artist_id": contribution.artist_id,
            "artist": contribution.artist,
            "seed_weight": contribution.seed_weight,
            "tag_weight": contribution.tag_weight,
            "contribution": contribution.contribution,
            "regions": list(contribution.regions),
        }
        for contribution in contributions[:_MAX_TOP_SEED_ARTISTS]
    ]


def _sort_contributions(
    contributions: list[_TagContribution],
) -> list[_TagContribution]:
    return sorted(
        contributions,
        key=lambda item: (-item.contribution, item.artist.casefold(), item.artist_id),
    )


def _normalize_tag_key(tag: object) -> str:
    return str(tag).strip().casefold()


def _tag_specificity(seed_artist_count: int) -> float:
    if seed_artist_count < 1:
        return 0.0
    return 1.0 / math.log(2 + seed_artist_count)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "untagged"


def _cluster_key(entry: _TagProfileEntry) -> str:
    tag_hash = sha256(entry.tag.encode("utf-8")).hexdigest()[:12]
    return f"{entry.source}:{_slugify(entry.tag)}:{tag_hash}"
