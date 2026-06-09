from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from duckdb import DuckDBPyConnection


@dataclass(frozen=True)
class CacheStats:
    total_entries: int
    fresh_entries: int
    stale_entries: int
    error_entries: int
    by_source: dict[str, int]
    by_endpoint: dict[str, int]
    by_status: dict[str, int]
    oldest_fetched_at: str | None
    newest_fetched_at: str | None


@dataclass(frozen=True)
class CacheEntry:
    cache_key: str
    source: str
    endpoint: str
    response_status: int | None
    is_fresh: bool
    fetched_at: str
    expires_at: str
    error_message: str | None


def stats_cache(
    conn: DuckDBPyConnection,
    *,
    now: datetime | None = None,
) -> CacheStats:
    resolved_now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)

    total_row = conn.execute("SELECT count(*) FROM api_cache").fetchone()
    total = int(total_row[0]) if total_row else 0

    fresh_row = conn.execute(
        "SELECT count(*) FROM api_cache WHERE expires_at > ? AND response_status BETWEEN 200 AND 299",
        [resolved_now],
    ).fetchone()
    fresh = int(fresh_row[0]) if fresh_row else 0

    stale_row = conn.execute(
        "SELECT count(*) FROM api_cache WHERE expires_at <= ?",
        [resolved_now],
    ).fetchone()
    stale = int(stale_row[0]) if stale_row else 0

    error_row = conn.execute(
        "SELECT count(*) FROM api_cache WHERE response_status IS NULL OR response_status >= 400",
    ).fetchone()
    error = int(error_row[0]) if error_row else 0

    by_source = _count_by_column(conn, "source")
    by_endpoint = _count_by_column(conn, "endpoint")
    by_status = _count_by_column(conn, "CAST(response_status AS VARCHAR)")

    oldest_row = conn.execute(
        "SELECT CAST(MIN(fetched_at) AS VARCHAR) FROM api_cache"
    ).fetchone()
    newest_row = conn.execute(
        "SELECT CAST(MAX(fetched_at) AS VARCHAR) FROM api_cache"
    ).fetchone()

    return CacheStats(
        total_entries=total,
        fresh_entries=fresh,
        stale_entries=stale,
        error_entries=error,
        by_source=by_source,
        by_endpoint=by_endpoint,
        by_status=by_status,
        oldest_fetched_at=str(oldest_row[0]) if oldest_row and oldest_row[0] else None,
        newest_fetched_at=str(newest_row[0]) if newest_row and newest_row[0] else None,
    )


def inspect_cache(
    conn: DuckDBPyConnection,
    *,
    source: str | None = None,
    limit: int = 1000,
    now: datetime | None = None,
) -> list[CacheEntry]:
    resolved_now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)

    if source:
        rows = conn.execute(
            """
            SELECT cache_key, source, endpoint, response_status,
                   fetched_at, expires_at, error_message
            FROM api_cache
            WHERE source = ?
            ORDER BY fetched_at DESC
            LIMIT ?
            """,
            [source, limit],
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT cache_key, source, endpoint, response_status,
                   fetched_at, expires_at, error_message
            FROM api_cache
            ORDER BY fetched_at DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()

    return [
        CacheEntry(
            cache_key=str(row[0]),
            source=str(row[1]),
            endpoint=str(row[2]),
            response_status=int(row[3]) if row[3] is not None else None,
            is_fresh=(
                row[5] is not None
                and isinstance(row[5], datetime)
                and row[5] > resolved_now
                and row[3] is not None
                and 200 <= int(row[3]) < 300
            ),
            fetched_at=str(row[4]) if row[4] else "",
            expires_at=str(row[5]) if row[5] else "",
            error_message=str(row[6]) if row[6] else None,
        )
        for row in rows
    ]


def expire_cache(
    conn: DuckDBPyConnection,
    *,
    source: str | None = None,
    older_than_days: int = 180,
    now: datetime | None = None,
) -> int:
    resolved_now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    cutoff = resolved_now - timedelta(days=older_than_days)

    if source:
        count_row = conn.execute(
            """
            SELECT count(*)
            FROM api_cache
            WHERE source = ?
              AND expires_at < ?
              AND response_status BETWEEN 200 AND 299
            """,
            [source, cutoff],
        ).fetchone()
        conn.execute(
            """
            DELETE FROM api_cache
            WHERE source = ?
              AND expires_at < ?
              AND response_status BETWEEN 200 AND 299
            """,
            [source, cutoff],
        )
    else:
        count_row = conn.execute(
            """
            SELECT count(*)
            FROM api_cache
            WHERE expires_at < ?
              AND response_status BETWEEN 200 AND 299
            """,
            [cutoff],
        ).fetchone()
        conn.execute(
            """
            DELETE FROM api_cache
            WHERE expires_at < ?
              AND response_status BETWEEN 200 AND 299
            """,
            [cutoff],
        )
    return int(count_row[0]) if count_row else 0


def _count_by_column(conn: DuckDBPyConnection, column: str) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT {column}, count(*) FROM api_cache GROUP BY {column} ORDER BY count(*) DESC"
    ).fetchall()
    return {str(row[0]) if row[0] is not None else "null": int(row[1]) for row in rows}
