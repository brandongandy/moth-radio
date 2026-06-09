from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from duckdb import DuckDBPyConnection

from scrobble_recs.models.profile import select_seed_artists

_SOURCE = "lastfm"
_SIMILAR_ENDPOINT = "artist.getSimilar"
_ARTIST_TAGS_ENDPOINT = "artist.getTopTags"
_TAG_TOP_ARTISTS_ENDPOINT = "tag.getTopArtists"


@dataclass(frozen=True)
class EnrichmentQueuePlan:
    source: str
    seed_artists: int
    similar_artist_queued: int
    similar_artist_cached_fresh: int
    artist_tag_queued: int
    artist_tag_cached_fresh: int
    tag_top_artist_queued: int
    tag_top_artist_cached_fresh: int
    total_queued: int


@dataclass(frozen=True)
class EnrichmentQueueStatus:
    source: str
    total: int
    queued: int
    running: int
    complete: int
    failed: int
    skipped: int


def plan_enrichment(
    conn: DuckDBPyConnection,
    *,
    source: str = _SOURCE,
    seed_artist_limit: int = 250,
    similar_artists_per_seed: int = 100,
    top_tags_per_artist: int = 20,
    tag_top_artists_limit: int = 100,
) -> EnrichmentQueuePlan:
    if source != _SOURCE:
        raise ValueError(f"unsupported source: {source}")

    seeds = select_seed_artists(
        conn,
        limit=seed_artist_limit,
        min_scrobbles=3,
    )

    similar_queued = 0
    similar_cached = 0
    tag_queued = 0
    tag_cached = 0
    tag_top_queued = 0
    tag_top_cached = 0

    for seed in seeds:
        subject_key = str(seed.artist_id)

        # artist.getSimilar
        if _enrichment_work_exists(
            conn, source, _SIMILAR_ENDPOINT, subject_key, similar_artists_per_seed
        ):
            similar_cached += 1
        elif not _queue_entry_exists(conn, source, _SIMILAR_ENDPOINT, "artist", seed.artist_id):
            _insert_queue_entry(
                conn, source, _SIMILAR_ENDPOINT, "artist", seed.artist_id, priority=1.0
            )
            similar_queued += 1

        # artist.getTopTags
        if _enrichment_work_exists(
            conn, source, _ARTIST_TAGS_ENDPOINT, subject_key, top_tags_per_artist
        ):
            tag_cached += 1
        elif not _queue_entry_exists(conn, source, _ARTIST_TAGS_ENDPOINT, "artist", seed.artist_id):
            _insert_queue_entry(
                conn, source, _ARTIST_TAGS_ENDPOINT, "artist", seed.artist_id, priority=0.80
            )
            tag_queued += 1

    # tag.getTopArtists — queue high-affinity tags from tag_profile
    tag_rows = conn.execute(
        """
        SELECT tag FROM tag_profile
        WHERE source = ? AND user_weight > 0
        ORDER BY user_weight DESC
        LIMIT 50
        """,
        [source],
    ).fetchall()

    for (tag_name,) in tag_rows:
        subject_key = str(tag_name)
        if _enrichment_work_exists(
            conn, source, _TAG_TOP_ARTISTS_ENDPOINT, subject_key, tag_top_artists_limit
        ):
            tag_top_cached += 1
        elif not _queue_entry_exists_by_tag(conn, source, _TAG_TOP_ARTISTS_ENDPOINT, tag_name):
            _insert_queue_entry_by_tag(
                conn, source, _TAG_TOP_ARTISTS_ENDPOINT, tag_name, priority=0.60
            )
            tag_top_queued += 1

    return EnrichmentQueuePlan(
        source=source,
        seed_artists=len(seeds),
        similar_artist_queued=similar_queued,
        similar_artist_cached_fresh=similar_cached,
        artist_tag_queued=tag_queued,
        artist_tag_cached_fresh=tag_cached,
        tag_top_artist_queued=tag_top_queued,
        tag_top_artist_cached_fresh=tag_top_cached,
        total_queued=similar_queued + tag_queued + tag_top_queued,
    )


def enrichment_queue_status(
    conn: DuckDBPyConnection,
    *,
    source: str = _SOURCE,
) -> EnrichmentQueueStatus:
    total_row = conn.execute(
        "SELECT count(*) FROM enrichment_queue WHERE source = ?",
        [source],
    ).fetchone()
    total = int(total_row[0]) if total_row else 0

    def _count(status: str) -> int:
        row = conn.execute(
            "SELECT count(*) FROM enrichment_queue WHERE source = ? AND status = ?",
            [source, status],
        ).fetchone()
        return int(row[0]) if row else 0

    return EnrichmentQueueStatus(
        source=source,
        total=total,
        queued=_count("queued"),
        running=_count("running"),
        complete=_count("complete"),
        failed=_count("failed"),
        skipped=_count("skipped"),
    )


def retry_failed_enrichment(
    conn: DuckDBPyConnection,
    *,
    source: str = _SOURCE,
    max_attempts: int = 3,
) -> int:
    before_row = conn.execute(
        """SELECT count(*) FROM enrichment_queue
           WHERE source = ? AND status = 'failed' AND attempts < ?""",
        [source, max_attempts],
    ).fetchone()
    before = int(before_row[0]) if before_row else 0
    conn.execute(
        """
        UPDATE enrichment_queue
        SET status = 'queued', attempts = 0, error_message = NULL
        WHERE source = ? AND status = 'failed' AND attempts < ?
        """,
        [source, max_attempts],
    )
    return before


def _enrichment_work_exists(
    conn: DuckDBPyConnection,
    source: str,
    endpoint: str,
    subject_key: str,
    request_limit: int,
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM enrichment_work
        WHERE source = ? AND endpoint = ? AND subject_key = ? AND request_limit = ?
        """,
        [source, endpoint, subject_key, request_limit],
    ).fetchone()
    return row is not None


def _queue_entry_exists(
    conn: DuckDBPyConnection,
    source: str,
    endpoint: str,
    entity_type: str,
    entity_id: int,
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM enrichment_queue
        WHERE source = ? AND endpoint = ? AND entity_type = ? AND entity_id = ?
        """,
        [source, endpoint, entity_type, entity_id],
    ).fetchone()
    return row is not None


def _queue_entry_exists_by_tag(
    conn: DuckDBPyConnection,
    source: str,
    endpoint: str,
    tag_name: str,
) -> bool:
    # Use hash of tag name as entity_id for tag entries
    tag_hash = abs(hash(tag_name)) % (2**63)
    return _queue_entry_exists(conn, source, endpoint, "tag", tag_hash)


def _insert_queue_entry(
    conn: DuckDBPyConnection,
    source: str,
    endpoint: str,
    entity_type: str,
    entity_id: int,
    *,
    priority: float = 0.0,
) -> None:
    conn.execute(
        """
        INSERT INTO enrichment_queue
            (queue_id, source, endpoint, entity_type, entity_id, priority, status)
        VALUES (?, ?, ?, ?, ?, ?, 'queued')
        """,
        [str(uuid4()), source, endpoint, entity_type, entity_id, priority],
    )


def _insert_queue_entry_by_tag(
    conn: DuckDBPyConnection,
    source: str,
    endpoint: str,
    tag_name: str,
    *,
    priority: float = 0.0,
) -> None:
    tag_hash = abs(hash(tag_name)) % (2**63)
    _insert_queue_entry(conn, source, endpoint, "tag", tag_hash, priority=priority)
