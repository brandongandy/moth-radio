import json
from datetime import datetime
from pathlib import Path

import httpx

from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.apis.musicbrainz import MusicBrainzClient
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import MusicBrainzApiConfig, load_config
from scrobble_recs.db import initialize_database


def _config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": "tests/fixtures/tiny_lastfm_export.csv",
                    "timezone": "UTC",
                    "timestamp_column": "uts",
                    "csv_columns": [
                        "uts",
                        "utc_time",
                        "artist",
                        "artist_mbid",
                        "album",
                        "album_mbid",
                        "track",
                        "track_mbid",
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_search_artist_returns_matches(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "artists": [
                    {"id": "abc123", "name": "Radiohead", "score": "100"},
                ]
            },
        )

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = MusicBrainzClient(
        config=MusicBrainzApiConfig(enabled=True),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(999.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    results = client.search_artist("Radiohead", limit=5)

    assert len(results) == 1
    assert results[0].mbid == "abc123"
    assert results[0].name == "Radiohead"
    assert results[0].score == 100.0
    assert len(calls) == 1
    conn.close()


def test_search_artist_caches_responses(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "artists": [
                    {"id": "abc", "name": "Test", "score": "90"},
                ]
            },
        )

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = MusicBrainzClient(
        config=MusicBrainzApiConfig(enabled=True),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(999.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    client.search_artist("Test", limit=5)
    assert len(calls) == 1
    client.search_artist("Test", limit=5)
    assert len(calls) == 1  # cached
    conn.close()


def test_search_artist_includes_disambiguation_and_type(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "artists": [
                    {
                        "id": "mbid-1",
                        "name": "Nirvana",
                        "disambiguation": "US grunge band",
                        "country": "US",
                        "type": "Group",
                        "score": "100",
                    },
                ]
            },
        )

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = MusicBrainzClient(
        config=MusicBrainzApiConfig(enabled=True),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(999.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    results = client.search_artist("Nirvana", limit=5)

    assert len(results) == 1
    assert results[0].disambiguation == "US grunge band"
    assert results[0].country == "US"
    assert results[0].type == "Group"
    conn.close()


def test_lookup_artist_returns_result(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "abc123",
                "name": "Test Artist",
                "disambiguation": "",
                "country": "US",
                "type": "Person",
                "life-span": {"begin": "1980", "end": ""},
            },
        )

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = MusicBrainzClient(
        config=MusicBrainzApiConfig(enabled=True),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(999.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    result = client.lookup_artist("abc123")

    assert result is not None
    assert result.mbid == "abc123"
    assert result.name == "Test Artist"
    assert result.country == "US"
    assert result.type == "Person"
    assert result.life_span_begin == "1980"
    assert result.life_span_end == ""
    conn.close()


def test_lookup_artist_returns_none_on_404(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = MusicBrainzClient(
        config=MusicBrainzApiConfig(enabled=True),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(999.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    assert client.lookup_artist("nonexistent") is None
    conn.close()


def test_search_artist_returns_stale_cache_on_error(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, json={"error": "temporary failure"})

    conn = initialize_database(load_config(_config_path(tmp_path)))
    cache = ApiCache(conn)
    now = datetime(2026, 6, 3, 12, 0, 0)
    cache.write_response(
        source="musicbrainz",
        endpoint="artist",
        request_url="https://musicbrainz.org/ws/2/artist",
        request_params={"query": 'artist:"Radiohead"', "limit": "5", "fmt": "json"},
        response_status=200,
        response_json={"artists": [{"id": "abc123", "name": "Radiohead", "score": "100"}]},
        fetched_at=now.replace(year=2025),
        expires_at=now.replace(year=2025, month=12),
        error_message=None,
    )
    client = MusicBrainzClient(
        config=MusicBrainzApiConfig(enabled=True),
        cache=cache,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(999.0, sleep=lambda seconds: None),
        now=lambda: now,
    )

    results = client.search_artist("Radiohead")

    assert len(results) == 1
    assert results[0].mbid == "abc123"
    assert len(calls) == 1
    conn.close()


def test_search_artist_returns_empty_list_when_no_matches(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"artists": []})

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = MusicBrainzClient(
        config=MusicBrainzApiConfig(enabled=True),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(999.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    results = client.search_artist("asdfqwerzxcv")

    assert results == []
    conn.close()


def test_search_artist_skips_entries_without_mbid(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "artists": [
                    {"name": "No ID", "score": "50"},
                    {"id": "mbid-ok", "name": "Has ID", "score": "60"},
                ]
            },
        )

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = MusicBrainzClient(
        config=MusicBrainzApiConfig(enabled=True),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(999.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    results = client.search_artist("Test")

    assert len(results) == 1
    assert results[0].mbid == "mbid-ok"
    conn.close()


def test_musicbrainz_client_close_does_not_close_injected_client(tmp_path: Path) -> None:
    conn = initialize_database(load_config(_config_path(tmp_path)))
    injected_client = httpx.Client(timeout=5.0)
    client = MusicBrainzClient(
        config=MusicBrainzApiConfig(enabled=True),
        cache=ApiCache(conn),
        http_client=injected_client,
        rate_limiter=RateLimiter(999.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    client.close()

    assert not injected_client.is_closed
    injected_client.close()
    conn.close()
