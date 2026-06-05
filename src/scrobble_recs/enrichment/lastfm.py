from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, TypeAlias

from duckdb import DuckDBPyConnection

from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.apis.lastfm import (
    LastFmArtistTag,
    LastFmClient,
    LastFmSimilarArtist,
    LastFmTagTopArtist,
)
from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.profile import SeedArtist, select_seed_artists
from scrobble_recs.normalize.strings import normalize_key

_SOURCE = "lastfm"
_SIMILAR_ENDPOINT = "artist.getSimilar"
_ARTIST_TAGS_ENDPOINT = "artist.getTopTags"
_TAG_TOP_ARTISTS_ENDPOINT = "tag.getTopArtists"
_NO_REQUEST_LIMIT = 0

LastFmEnrichmentPhase: TypeAlias = Literal[
    "seed_similarity",
    "seed_tags",
    "tag_top_artists",
]
LastFmEnrichmentProgressStatus: TypeAlias = Literal["processing", "completed", "skipped"]
LastFmEnrichmentProgressCallback: TypeAlias = Callable[["LastFmEnrichmentProgress"], None]


class LastFmClientProtocol(Protocol):
    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        raise NotImplementedError

    def get_artist_tags(self, artist: str) -> list[LastFmArtistTag]:
        raise NotImplementedError

    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        raise NotImplementedError


@dataclass(frozen=True)
class LastFmEnrichmentStats:
    seed_artists: int
    similar_artist_requests: int
    artist_tag_requests: int
    tag_top_artist_requests: int
    artists_created: int
    similarity_edges: int
    artist_tags: int
    tag_top_artists: int


@dataclass(frozen=True)
class LastFmEnrichmentProgress:
    phase: LastFmEnrichmentPhase
    current: int
    total: int
    subject: str
    status: LastFmEnrichmentProgressStatus


def run_lastfm_enrichment(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    client: LastFmClientProtocol | None = None,
    progress_callback: LastFmEnrichmentProgressCallback | None = None,
) -> LastFmEnrichmentStats:
    owns_client = client is None
    if owns_client and not config.apis.lastfm.enabled:
        raise RuntimeError("Last.fm enrichment is disabled")
    if owns_client and config.apis.lastfm.api_key is None:
        raise RuntimeError("Last.fm API key is required for enrichment")

    resolved_client: LastFmClientProtocol = client or LastFmClient(
        config=config.apis.lastfm,
        cache=ApiCache(conn),
    )
    try:
        seeds = select_seed_artists(
            conn,
            limit=config.recommendation.seed_artist_limit,
            min_scrobbles=config.recommendation.min_seed_scrobbles,
        )
        stats = _MutableStats(seed_artists=len(seeds))

        for index, seed in enumerate(seeds):
            _enrich_seed_similarity(
                conn,
                resolved_client,
                seed,
                config,
                stats,
                progress_callback=progress_callback,
                current=index,
                total=len(seeds),
            )
            _enrich_seed_tags(
                conn,
                resolved_client,
                seed,
                stats,
                progress_callback=progress_callback,
                current=index,
                total=len(seeds),
            )

        _enrich_tag_top_artists(conn, resolved_client, config, stats, progress_callback)

        return stats.snapshot()
    finally:
        if owns_client:
            close = getattr(resolved_client, "close", None)
            if callable(close):
                close()


@dataclass
class _MutableStats:
    seed_artists: int = 0
    similar_artist_requests: int = 0
    artist_tag_requests: int = 0
    tag_top_artist_requests: int = 0
    artists_created: int = 0
    similarity_edges: int = 0
    artist_tags: int = 0
    tag_top_artists: int = 0

    def snapshot(self) -> LastFmEnrichmentStats:
        return LastFmEnrichmentStats(
            seed_artists=self.seed_artists,
            similar_artist_requests=self.similar_artist_requests,
            artist_tag_requests=self.artist_tag_requests,
            tag_top_artist_requests=self.tag_top_artist_requests,
            artists_created=self.artists_created,
            similarity_edges=self.similarity_edges,
            artist_tags=self.artist_tags,
            tag_top_artists=self.tag_top_artists,
        )


def _enrich_seed_similarity(
    conn: DuckDBPyConnection,
    client: LastFmClientProtocol,
    seed: SeedArtist,
    config: ScrobbleRecsConfig,
    stats: _MutableStats,
    *,
    progress_callback: LastFmEnrichmentProgressCallback | None,
    current: int,
    total: int,
) -> None:
    limit = config.recommendation.similar_artists_per_seed
    _report_progress(
        progress_callback,
        phase="seed_similarity",
        current=current,
        total=total,
        subject=seed.lastfm_name,
        status="processing",
    )
    if _work_completed(conn, _SIMILAR_ENDPOINT, seed.artist_key, limit):
        _report_progress(
            progress_callback,
            phase="seed_similarity",
            current=current + 1,
            total=total,
            subject=seed.lastfm_name,
            status="skipped",
        )
        return
    replacement_needed = _work_completed_for_subject(conn, _SIMILAR_ENDPOINT, seed.artist_key)

    similar_artists = client.get_similar_artists(seed.lastfm_name, limit=limit)
    stats.similar_artist_requests += 1
    if replacement_needed:
        created, inserted = _replace_similarity_edges(
            conn,
            seed,
            limit,
            similar_artists,
        )
        stats.artists_created += created
        stats.similarity_edges += inserted
        _report_progress(
            progress_callback,
            phase="seed_similarity",
            current=current + 1,
            total=total,
            subject=seed.lastfm_name,
            status="completed",
        )
        return

    created, inserted = _persist_similarity_edges(
        conn,
        seed,
        limit,
        similar_artists,
    )
    stats.artists_created += created
    stats.similarity_edges += inserted
    _report_progress(
        progress_callback,
        phase="seed_similarity",
        current=current + 1,
        total=total,
        subject=seed.lastfm_name,
        status="completed",
    )


def _enrich_seed_tags(
    conn: DuckDBPyConnection,
    client: LastFmClientProtocol,
    seed: SeedArtist,
    stats: _MutableStats,
    *,
    progress_callback: LastFmEnrichmentProgressCallback | None,
    current: int,
    total: int,
) -> None:
    _report_progress(
        progress_callback,
        phase="seed_tags",
        current=current,
        total=total,
        subject=seed.lastfm_name,
        status="processing",
    )
    if _work_completed(conn, _ARTIST_TAGS_ENDPOINT, seed.artist_key, _NO_REQUEST_LIMIT):
        _report_progress(
            progress_callback,
            phase="seed_tags",
            current=current + 1,
            total=total,
            subject=seed.lastfm_name,
            status="skipped",
        )
        return

    tags = client.get_artist_tags(seed.lastfm_name)
    stats.artist_tag_requests += 1
    max_count = max((tag.count for tag in tags), default=0.0)
    stats.artist_tags += _persist_artist_tags(conn, seed, tags, max_count)
    _report_progress(
        progress_callback,
        phase="seed_tags",
        current=current + 1,
        total=total,
        subject=seed.lastfm_name,
        status="completed",
    )


def _enrich_tag_top_artists(
    conn: DuckDBPyConnection,
    client: LastFmClientProtocol,
    config: ScrobbleRecsConfig,
    stats: _MutableStats,
    progress_callback: LastFmEnrichmentProgressCallback | None,
) -> None:
    limit = config.recommendation.obviousness.tag_top_artists_limit
    tags = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT tag
            FROM artist_tag
            WHERE source = 'lastfm'
            GROUP BY tag
            ORDER BY sum(normalized_weight) DESC, tag ASC
            """
        ).fetchall()
    ]
    for index, tag in enumerate(tags):
        _report_progress(
            progress_callback,
            phase="tag_top_artists",
            current=index,
            total=len(tags),
            subject=tag,
            status="processing",
        )
        if _work_completed(conn, _TAG_TOP_ARTISTS_ENDPOINT, tag, limit):
            _report_progress(
                progress_callback,
                phase="tag_top_artists",
                current=index + 1,
                total=len(tags),
                subject=tag,
                status="skipped",
            )
            continue
        replacement_needed = _work_completed_for_subject(
            conn,
            _TAG_TOP_ARTISTS_ENDPOINT,
            tag,
        )

        top_artists = client.get_tag_top_artists(tag, limit=limit)
        stats.tag_top_artist_requests += 1
        if replacement_needed:
            created, inserted = _replace_tag_top_artists(
                conn,
                tag,
                limit,
                top_artists,
            )
            stats.artists_created += created
            stats.tag_top_artists += inserted
            _report_progress(
                progress_callback,
                phase="tag_top_artists",
                current=index + 1,
                total=len(tags),
                subject=tag,
                status="completed",
            )
            continue

        created, inserted = _persist_tag_top_artists(
            conn,
            tag,
            limit,
            top_artists,
        )
        stats.artists_created += created
        stats.tag_top_artists += inserted
        _report_progress(
            progress_callback,
            phase="tag_top_artists",
            current=index + 1,
            total=len(tags),
            subject=tag,
            status="completed",
        )


def _report_progress(
    progress_callback: LastFmEnrichmentProgressCallback | None,
    *,
    phase: LastFmEnrichmentPhase,
    current: int,
    total: int,
    subject: str,
    status: LastFmEnrichmentProgressStatus,
) -> None:
    if progress_callback is None:
        return
    progress_callback(
        LastFmEnrichmentProgress(
            phase=phase,
            current=current,
            total=total,
            subject=subject,
            status=status,
        )
    )


def _ensure_artist(conn: DuckDBPyConnection, name: str, mbid: str | None) -> tuple[int, bool]:
    artist_key = normalize_key(name)
    existing = conn.execute(
        "SELECT artist_id FROM artist WHERE artist_key = ?",
        [artist_key],
    ).fetchone()
    if existing is not None:
        return int(existing[0]), False

    artist_id = _next_id(conn, "artist", "artist_id")
    conn.execute(
        """
        INSERT INTO artist (
            artist_id,
            canonical_name,
            artist_key,
            musicbrainz_artist_mbid,
            lastfm_name,
            resolution_status,
            resolution_confidence
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            artist_id,
            name,
            artist_key,
            mbid,
            name,
            "lastfm_enriched",
            0.5 if mbid else 0.25,
        ],
    )
    return artist_id, True


def _insert_similarity_edge(
    conn: DuckDBPyConnection,
    source_artist_id: int,
    target_artist_id: int,
    similar: LastFmSimilarArtist,
) -> bool:
    row = conn.execute(
        """
        INSERT INTO artist_similarity_edge (
            source_artist_id,
            target_artist_id,
            source,
            source_score,
            normalized_score,
            evidence,
            fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        RETURNING source_artist_id
        """,
        [
            source_artist_id,
            target_artist_id,
            _SOURCE,
            similar.match,
            similar.match,
            json.dumps({"method": _SIMILAR_ENDPOINT, "target_name": similar.name}),
            _now(),
        ],
    ).fetchone()
    return row is not None


def _insert_artist_tag(
    conn: DuckDBPyConnection,
    artist_id: int,
    tag: LastFmArtistTag,
    normalized_weight: float,
) -> bool:
    row = conn.execute(
        """
        INSERT INTO artist_tag (
            artist_id,
            tag,
            source,
            tag_count,
            normalized_weight,
            fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        RETURNING artist_id
        """,
        [artist_id, tag.name, _SOURCE, tag.count, normalized_weight, _now()],
    ).fetchone()
    return row is not None


def _insert_tag_top_artist(
    conn: DuckDBPyConnection,
    tag: str,
    artist_id: int,
    top_artist: LastFmTagTopArtist,
) -> bool:
    row = conn.execute(
        """
        INSERT INTO tag_top_artist (
            tag,
            artist_id,
            source,
            tag_rank,
            tag_count,
            fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        RETURNING tag
        """,
        [tag, artist_id, _SOURCE, top_artist.rank, top_artist.count, _now()],
    ).fetchone()
    return row is not None


def _work_completed(
    conn: DuckDBPyConnection,
    endpoint: str,
    subject_key: str,
    request_limit: int,
) -> bool:
    return (
        _count_rows(
            conn,
            """
            SELECT count(*)
            FROM enrichment_work
            WHERE source = ?
              AND endpoint = ?
              AND subject_key = ?
              AND request_limit = ?
            """,
            [_SOURCE, endpoint, subject_key, request_limit],
        )
        > 0
    )


def _persist_similarity_edges(
    conn: DuckDBPyConnection,
    seed: SeedArtist,
    request_limit: int,
    similar_artists: list[LastFmSimilarArtist],
) -> tuple[int, int]:
    conn.execute("BEGIN TRANSACTION")
    try:
        artists_created = 0
        edges_inserted = 0
        for similar in similar_artists:
            target_id, created = _ensure_artist(conn, similar.name, similar.mbid)
            artists_created += int(created)
            inserted = _insert_similarity_edge(conn, seed.artist_id, target_id, similar)
            edges_inserted += int(inserted)
        _mark_work_completed(
            conn,
            _SIMILAR_ENDPOINT,
            seed.artist_key,
            request_limit,
            {"artist": seed.lastfm_name, "returned_count": len(similar_artists)},
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return artists_created, edges_inserted


def _persist_artist_tags(
    conn: DuckDBPyConnection,
    seed: SeedArtist,
    tags: list[LastFmArtistTag],
    max_count: float,
) -> int:
    conn.execute("BEGIN TRANSACTION")
    try:
        tags_inserted = 0
        for tag in tags:
            normalized_weight = 0.0 if max_count <= 0 else tag.count / max_count
            inserted = _insert_artist_tag(conn, seed.artist_id, tag, normalized_weight)
            tags_inserted += int(inserted)
        _mark_work_completed(
            conn,
            _ARTIST_TAGS_ENDPOINT,
            seed.artist_key,
            _NO_REQUEST_LIMIT,
            {"artist": seed.lastfm_name, "returned_count": len(tags)},
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return tags_inserted


def _persist_tag_top_artists(
    conn: DuckDBPyConnection,
    tag: str,
    request_limit: int,
    top_artists: list[LastFmTagTopArtist],
) -> tuple[int, int]:
    conn.execute("BEGIN TRANSACTION")
    try:
        artists_created = 0
        top_artists_inserted = 0
        for top_artist in top_artists:
            artist_id, created = _ensure_artist(conn, top_artist.name, top_artist.mbid)
            artists_created += int(created)
            inserted = _insert_tag_top_artist(conn, tag, artist_id, top_artist)
            top_artists_inserted += int(inserted)
        _mark_work_completed(
            conn,
            _TAG_TOP_ARTISTS_ENDPOINT,
            tag,
            request_limit,
            {"tag": tag, "returned_count": len(top_artists)},
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return artists_created, top_artists_inserted


def _replace_similarity_edges(
    conn: DuckDBPyConnection,
    seed: SeedArtist,
    request_limit: int,
    similar_artists: list[LastFmSimilarArtist],
) -> tuple[int, int]:
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            """
            DELETE FROM artist_similarity_edge
            WHERE source = ? AND source_artist_id = ?
            """,
            [_SOURCE, seed.artist_id],
        )
        _delete_work_markers(conn, _SIMILAR_ENDPOINT, seed.artist_key)
        artists_created = 0
        edges_inserted = 0
        for similar in similar_artists:
            target_id, created = _ensure_artist(conn, similar.name, similar.mbid)
            artists_created += int(created)
            inserted = _insert_similarity_edge(conn, seed.artist_id, target_id, similar)
            edges_inserted += int(inserted)
        _mark_work_completed(
            conn,
            _SIMILAR_ENDPOINT,
            seed.artist_key,
            request_limit,
            {"artist": seed.lastfm_name, "returned_count": len(similar_artists)},
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return artists_created, edges_inserted


def _replace_tag_top_artists(
    conn: DuckDBPyConnection,
    tag: str,
    request_limit: int,
    top_artists: list[LastFmTagTopArtist],
) -> tuple[int, int]:
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            """
            DELETE FROM tag_top_artist
            WHERE source = ? AND tag = ?
            """,
            [_SOURCE, tag],
        )
        _delete_work_markers(conn, _TAG_TOP_ARTISTS_ENDPOINT, tag)
        artists_created = 0
        top_artists_inserted = 0
        for top_artist in top_artists:
            artist_id, created = _ensure_artist(conn, top_artist.name, top_artist.mbid)
            artists_created += int(created)
            inserted = _insert_tag_top_artist(conn, tag, artist_id, top_artist)
            top_artists_inserted += int(inserted)
        _mark_work_completed(
            conn,
            _TAG_TOP_ARTISTS_ENDPOINT,
            tag,
            request_limit,
            {"tag": tag, "returned_count": len(top_artists)},
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return artists_created, top_artists_inserted


def _work_completed_for_subject(
    conn: DuckDBPyConnection,
    endpoint: str,
    subject_key: str,
) -> bool:
    return (
        _count_rows(
            conn,
            """
            SELECT count(*)
            FROM enrichment_work
            WHERE source = ?
              AND endpoint = ?
              AND subject_key = ?
            """,
            [_SOURCE, endpoint, subject_key],
        )
        > 0
    )


def _delete_work_markers(
    conn: DuckDBPyConnection,
    endpoint: str,
    subject_key: str,
) -> None:
    conn.execute(
        """
        DELETE FROM enrichment_work
        WHERE source = ?
          AND endpoint = ?
          AND subject_key = ?
        """,
        [_SOURCE, endpoint, subject_key],
    )


def _mark_work_completed(
    conn: DuckDBPyConnection,
    endpoint: str,
    subject_key: str,
    request_limit: int,
    evidence: dict[str, object],
) -> None:
    conn.execute(
        """
        INSERT INTO enrichment_work (
            source,
            endpoint,
            subject_key,
            request_limit,
            completed_at,
            evidence
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        [
            _SOURCE,
            endpoint,
            subject_key,
            request_limit,
            _now(),
            json.dumps(evidence),
        ],
    )


def _next_id(conn: DuckDBPyConnection, table: str, column: str) -> int:
    return _count_rows(conn, f"SELECT coalesce(max({column}), 0) + 1 FROM {table}", [])


def _count_rows(conn: DuckDBPyConnection, sql: str, params: list[object]) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


def _now() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)
