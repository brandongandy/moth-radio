from datetime import datetime
from pathlib import Path

import httpx
import pytest

from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.apis.lastfm import LastFmClient
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import LastFmApiConfig, load_config
from scrobble_recs.db import initialize_database


def _config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        f"""
        {{
          "app": {{
            "name": "scrobble-recs",
            "db_path": "{(tmp_path / "db.duckdb").as_posix()}",
            "cache_dir": "{(tmp_path / "cache").as_posix()}",
            "log_path": "{(tmp_path / "app.log").as_posix()}"
          }},
          "input": {{
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
              "track_mbid"
            ]
          }}
        }}
        """,
        encoding="utf-8",
    )
    return path


def test_get_similar_artists_uses_http_once_then_fresh_cache(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "similarartists": {
                    "artist": [
                        {
                            "name": "Siouxsie and the Banshees",
                            "mbid": "mbid-siouxsie",
                            "match": "0.91",
                        },
                        {"name": "Bauhaus", "mbid": "", "match": "0.72"},
                    ]
                }
            },
        )

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key"),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    first = client.get_similar_artists("The Cure", limit=2)
    second = client.get_similar_artists("The Cure", limit=2)

    assert [artist.name for artist in first] == ["Siouxsie and the Banshees", "Bauhaus"]
    assert [artist.name for artist in second] == ["Siouxsie and the Banshees", "Bauhaus"]
    assert len(calls) == 1
    assert calls[0].url.params["api_key"] == "secret-key"
    cached = conn.execute("SELECT request_params, request_url FROM api_cache").fetchone()
    assert cached is not None
    assert "secret-key" not in str(cached[0])
    assert "secret-key" not in str(cached[1])
    conn.close()


def test_cached_similar_artists_skip_rate_limiter_and_http(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("HTTP should not be called for a fresh cache hit")

    class FakeLimiter:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self) -> None:
            self.calls += 1

    conn = initialize_database(load_config(_config_path(tmp_path)))
    cache = ApiCache(conn)
    now = datetime(2026, 6, 3, 12, 0, 0)
    params = {"artist": "The Cure", "limit": 2}
    cache.write_response(
        source="lastfm",
        endpoint="artist.getSimilar",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params={"method": "artist.getSimilar", **params},
        response_status=200,
        response_json={
            "similarartists": {"artist": [{"name": "Bauhaus", "mbid": "", "match": "0.72"}]}
        },
        fetched_at=now,
        expires_at=now.replace(year=2027),
        error_message=None,
    )
    limiter = FakeLimiter()
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key"),
        cache=cache,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=limiter,
        now=lambda: now,
    )

    artists = client.get_similar_artists("The Cure", limit=2)

    assert [artist.name for artist in artists] == ["Bauhaus"]
    assert limiter.calls == 0
    assert calls == []
    conn.close()


def test_get_artist_tags_and_tag_top_artists_parse_lastfm_payloads(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.params["method"]
        if method == "artist.getTopTags":
            return httpx.Response(
                200,
                json={
                    "toptags": {
                        "tag": [
                            {"name": "post-punk", "count": 100},
                            {"name": "gothic rock", "count": "75"},
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "topartists": {
                    "artist": [
                        {"name": "The Cure", "mbid": "mbid-cure", "count": "1000"},
                        {"name": "Joy Division", "mbid": "", "count": 800},
                    ]
                }
            },
        )

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key"),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    tags = client.get_artist_tags("The Cure")
    top_artists = client.get_tag_top_artists("post-punk", limit=2)

    assert [(tag.name, tag.count) for tag in tags] == [("post-punk", 100.0), ("gothic rock", 75.0)]
    assert [(artist.rank, artist.name, artist.count) for artist in top_artists] == [
        (1, "The Cure", 1000.0),
        (2, "Joy Division", 800.0),
    ]
    conn.close()


def test_lastfm_client_normalizes_singleton_payloads_and_missing_values(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.params["method"]
        if method == "artist.getSimilar":
            return httpx.Response(
                200,
                json={
                    "similarartists": {
                        "artist": {
                            "name": "The Sisters of Mercy",
                            "mbid": None,
                            "match": None,
                        }
                    }
                },
            )
        if method == "artist.getTopTags":
            return httpx.Response(
                200,
                json={"toptags": {"tag": "goth"}},
            )
        return httpx.Response(
            200,
            json={
                "topartists": {
                    "artist": {
                        "name": "Siouxsie and the Banshees",
                        "mbid": None,
                        "count": None,
                    }
                }
            },
        )

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key"),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    similar = client.get_similar_artists("The Cure", limit=1)
    tags = client.get_artist_tags("The Cure")
    top_artists = client.get_tag_top_artists("goth", limit=1)

    assert [(artist.name, artist.mbid, artist.match) for artist in similar] == [
        ("The Sisters of Mercy", None, 0.0)
    ]
    assert tags == []
    assert [(artist.rank, artist.name, artist.mbid, artist.count) for artist in top_artists] == [
        (1, "Siouxsie and the Banshees", None, 0.0)
    ]
    conn.close()


def test_lastfm_client_returns_stale_cache_after_transient_5xx(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"message": "temporary failure"})

    conn = initialize_database(load_config(_config_path(tmp_path)))
    cache = ApiCache(conn)
    now = datetime(2026, 6, 3, 12, 0, 0)
    cache.write_response(
        source="lastfm",
        endpoint="artist.getSimilar",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params={"method": "artist.getSimilar", "artist": "The Cure", "limit": 1},
        response_status=200,
        response_json={
            "similarartists": {"artist": [{"name": "Bauhaus", "mbid": "", "match": "0.72"}]}
        },
        fetched_at=now.replace(year=2025),
        expires_at=now.replace(year=2025, month=12),
        error_message=None,
    )
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key", max_retries=1),
        cache=cache,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        now=lambda: now,
    )

    artists = client.get_similar_artists("The Cure", limit=1)

    assert [artist.name for artist in artists] == ["Bauhaus"]
    assert len(calls) == 2
    conn.close()


def test_lastfm_client_returns_stale_cache_after_transient_non_json_503(
    tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            503,
            content=b"<html><body>temporarily unavailable</body></html>",
            headers={"content-type": "text/html"},
        )

    conn = initialize_database(load_config(_config_path(tmp_path)))
    cache = ApiCache(conn)
    now = datetime(2026, 6, 3, 12, 0, 0)
    cache.write_response(
        source="lastfm",
        endpoint="artist.getSimilar",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params={"method": "artist.getSimilar", "artist": "The Cure", "limit": 1},
        response_status=200,
        response_json={
            "similarartists": {"artist": [{"name": "Bauhaus", "mbid": "", "match": "0.72"}]}
        },
        fetched_at=now.replace(year=2025),
        expires_at=now.replace(year=2025, month=12),
        error_message=None,
    )
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key", max_retries=1),
        cache=cache,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        retry_sleep=lambda seconds: None,
        now=lambda: now,
    )

    artists = client.get_similar_artists("The Cure", limit=1)

    assert [artist.name for artist in artists] == ["Bauhaus"]
    assert len(calls) == 2
    conn.close()


def test_lastfm_client_returns_stale_cache_after_transport_error(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.TransportError("connection reset", request=request)

    conn = initialize_database(load_config(_config_path(tmp_path)))
    cache = ApiCache(conn)
    now = datetime(2026, 6, 3, 12, 0, 0)
    cache.write_response(
        source="lastfm",
        endpoint="artist.getSimilar",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params={"method": "artist.getSimilar", "artist": "The Cure", "limit": 1},
        response_status=200,
        response_json={
            "similarartists": {"artist": [{"name": "Bauhaus", "mbid": "", "match": "0.72"}]}
        },
        fetched_at=now.replace(year=2025),
        expires_at=now.replace(year=2025, month=12),
        error_message=None,
    )
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key", max_retries=1),
        cache=cache,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        retry_sleep=lambda seconds: None,
        now=lambda: now,
    )

    artists = client.get_similar_artists("The Cure", limit=1)

    assert [artist.name for artist in artists] == ["Bauhaus"]
    assert len(calls) == 2
    conn.close()


def test_lastfm_client_backs_off_between_429_retries(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    sleeps: list[float] = []
    responses = [
        httpx.Response(429, json={"message": "rate limited"}),
        httpx.Response(429, json={"message": "rate limited again"}),
        httpx.Response(
            200,
            json={
                "similarartists": {
                    "artist": [{"name": "Bauhaus", "mbid": "", "match": "0.72"}]
                }
            },
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses.pop(0)

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key", max_retries=2),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        retry_sleep=sleeps.append,
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    artists = client.get_similar_artists("The Cure", limit=1)

    assert [artist.name for artist in artists] == ["Bauhaus"]
    assert sleeps == [1.0, 2.0]
    assert len(calls) == 3
    conn.close()


def test_lastfm_client_does_not_retry_4xx_errors(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403, json={"message": "bad key"})

    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key", max_retries=2),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    with pytest.raises(RuntimeError, match="bad key"):
        client.get_similar_artists("The Cure", limit=1)

    assert len(calls) == 1
    conn.close()


def test_lastfm_client_uses_a_30_second_default_http_timeout(tmp_path: Path) -> None:
    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key"),
        cache=ApiCache(conn),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    timeout = client._http_client.timeout

    assert timeout.connect == 30.0
    assert timeout.read == 30.0
    assert timeout.write == 30.0
    assert timeout.pool == 30.0
    conn.close()


def test_lastfm_client_close_does_not_close_injected_http_client(tmp_path: Path) -> None:
    conn = initialize_database(load_config(_config_path(tmp_path)))
    injected_client = httpx.Client(timeout=5.0)
    client = LastFmClient(
        config=LastFmApiConfig(api_key="secret-key"),
        cache=ApiCache(conn),
        http_client=injected_client,
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    client.close()

    assert not injected_client.is_closed
    injected_client.close()
    conn.close()


def test_lastfm_client_context_manager_closes_owned_http_client(tmp_path: Path) -> None:
    conn = initialize_database(load_config(_config_path(tmp_path)))
    with LastFmClient(
        config=LastFmApiConfig(api_key="secret-key"),
        cache=ApiCache(conn),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    ) as client:
        owned_client = client._http_client
        assert not owned_client.is_closed

    assert owned_client.is_closed
    conn.close()


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            LastFmApiConfig(enabled=False, api_key="secret-key"),
            "Last.fm enrichment is disabled",
        ),
        (
            LastFmApiConfig(api_key=None),
            "Last.fm API key is required for enrichment",
        ),
    ],
)
def test_lastfm_client_raises_clear_configuration_errors(
    tmp_path: Path,
    config: LastFmApiConfig,
    message: str,
) -> None:
    conn = initialize_database(load_config(_config_path(tmp_path)))
    client = LastFmClient(
        config=config,
        cache=ApiCache(conn),
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: pytest.fail("HTTP should not be called"))
        ),
        rate_limiter=RateLimiter(1000.0, sleep=lambda seconds: None),
        now=lambda: datetime(2026, 6, 3, 12, 0, 0),
    )

    with pytest.raises(RuntimeError, match=message):
        client.get_similar_artists("The Cure", limit=1)

    conn.close()
