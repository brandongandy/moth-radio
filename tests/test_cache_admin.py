from datetime import datetime, timedelta

from duckdb import DuckDBPyConnection

from scrobble_recs.admin.cache_admin import (
    expire_cache,
    inspect_cache,
    stats_cache,
)


def _make_cache_db() -> DuckDBPyConnection:
    import duckdb

    conn = duckdb.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE api_cache (
            cache_key VARCHAR PRIMARY KEY,
            source VARCHAR,
            endpoint VARCHAR,
            request_url VARCHAR,
            request_params JSON,
            response_status INTEGER,
            response_json JSON,
            fetched_at TIMESTAMP,
            expires_at TIMESTAMP,
            error_message VARCHAR
        )
        """
    )
    return conn


def test_cache_stats_counts_by_source_and_status():
    conn = _make_cache_db()
    now = datetime(2026, 6, 9)
    fresh = now + timedelta(days=30)
    stale = now - timedelta(days=1)

    conn.execute(
        """
        INSERT INTO api_cache (
            cache_key, source, endpoint, request_url, request_params,
            response_status, response_json, fetched_at, expires_at
        )
        VALUES
            ('k1', 'lastfm', 'artist.getSimilar', 'http://x', '{}', 200, '{}', ?, ?),
            ('k2', 'lastfm', 'artist.getTopTags', 'http://x', '{}', 200, '{}', ?, ?),
            ('k3', 'lastfm', 'tag.getTopArtists', 'http://x', '{}', 429, NULL, ?, ?),
            ('k4', 'lastfm', 'artist.getSimilar', 'http://x', '{}', 200, '{}', ?, ?)
        """,
        [now, fresh, now, fresh, now, stale, now - timedelta(days=30), stale],
    )

    stats = stats_cache(conn, now=now)
    assert stats.total_entries == 4
    assert stats.fresh_entries >= 1
    assert stats.stale_entries >= 1
    assert stats.by_source["lastfm"] == 4
    assert stats.by_endpoint["artist.getSimilar"] == 2
    assert stats.by_status["200"] == 3
    assert stats.by_status["429"] == 1


def test_cache_inspect_returns_entries_for_source():
    conn = _make_cache_db()
    now = datetime(2026, 6, 9)
    fresh = now + timedelta(days=30)

    conn.execute(
        """
        INSERT INTO api_cache (
            cache_key, source, endpoint, request_url, request_params,
            response_status, response_json, fetched_at, expires_at
        )
        VALUES
            ('k1', 'lastfm', 'artist.getSimilar', 'http://x', '{}', 200, '{"ok":true}', ?, ?),
            ('k2', 'lastfm', 'artist.getTopTags', 'http://x', '{}', 200, '{"ok":true}', ?, ?)
        """,
        [now, fresh, now, fresh],
    )

    entries = inspect_cache(conn, source="lastfm", now=now)
    assert len(entries) == 2
    assert entries[0].cache_key in ("k1", "k2")
    assert entries[0].source == "lastfm"
    assert entries[0].is_fresh is True


def test_cache_expire_removes_stale_entries_older_than_days():
    conn = _make_cache_db()
    now = datetime(2026, 6, 9)
    fresh = now + timedelta(days=30)
    very_old = now - timedelta(days=200)

    conn.execute(
        """
        INSERT INTO api_cache (
            cache_key, source, endpoint, request_url, request_params,
            response_status, response_json, fetched_at, expires_at
        )
        VALUES
            ('keep', 'lastfm', 'artist.getSimilar', 'http://x', '{}', 200, '{}', ?, ?),
            ('expire', 'lastfm', 'artist.getSimilar', 'http://x', '{}', 200, '{}', ?, ?)
        """,
        [now, fresh, very_old, very_old],
    )

    removed = expire_cache(conn, older_than_days=180, now=now)
    assert removed == 1

    rows = conn.execute("SELECT cache_key FROM api_cache").fetchall()
    keys = {row[0] for row in rows}
    assert keys == {"keep"}


def test_cache_expire_keeps_error_entries_by_default():
    conn = _make_cache_db()
    now = datetime(2026, 6, 9)
    old = now - timedelta(days=200)

    conn.execute(
        """
        INSERT INTO api_cache (
            cache_key, source, endpoint, request_url, request_params,
            response_status, response_json, fetched_at, expires_at,
            error_message
        )
        VALUES
            ('err', 'lastfm', 'artist.getSimilar', 'http://x', '{}',
             500, NULL, ?, ?, 'server error')
        """,
        [old, old],
    )

    removed = expire_cache(conn, older_than_days=180, now=now)
    assert removed == 0


def test_cache_stats_empty_cache():
    conn = _make_cache_db()
    stats = stats_cache(conn)
    assert stats.total_entries == 0
    assert stats.fresh_entries == 0
    assert stats.stale_entries == 0
