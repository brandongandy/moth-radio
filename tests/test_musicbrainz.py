import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.apis.musicbrainz import MusicBrainzClient
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import MusicBrainzApiConfig, load_config
from scrobble_recs.db import initialize_database


def _recording_payload():
    return json.loads(Path("tests/fixtures/musicbrainz_recording.json").read_text())


def test_recording_lookup_caches_canonical_identity_and_exact_request(tmp_path):
    payload = _recording_payload()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    conn = initialize_database(load_config(_config_path(tmp_path)))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = MusicBrainzClient(
            config=MusicBrainzApiConfig(enabled=True),
            cache=ApiCache(conn),
            http_client=http,
            rate_limiter=RateLimiter(999, sleep=lambda _: None),
            now=lambda: datetime(2026, 1, 1),
        )
        assert client.lookup_recording("invalid") is None
        result = client.lookup_recording(payload["id"].upper())
        assert result.title == 'A, "literal" [recording]\r\n雪'
        assert result.artist_mbids == (
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
        assert result.release_title == "First"
        assert client.lookup_recording(payload["id"]) == result
        assert len(calls) == 1
        assert calls[0].url.path.endswith("/recording/" + payload["id"])
        assert dict(calls[0].url.params) == {"inc": "artist-credits+releases", "fmt": "json"}
        assert conn.execute("SELECT fetched_at FROM api_cache").fetchone()[0] == datetime(
            2026, 1, 1
        )
        client.close()
        assert not http.is_closed
    conn.close()


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {},
        {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
        {"title": " "},
        {"artist-credit": []},
        {"artist-credit": [" & "]},
        {"artist-credit": [{"artist": {"id": "bad"}}]},
        {"artist-credit": [{"artist": {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}}, {}]},
        {"releases": {}},
    ],
)
def test_recording_rejects_malformed_contract(tmp_path, bad):
    conn = initialize_database(load_config(_config_path(tmp_path)))
    payload = _recording_payload()
    data = dict(payload, **bad) if isinstance(bad, dict) and bad else bad
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))
    ) as http:
        client = MusicBrainzClient(
            config=MusicBrainzApiConfig(enabled=True),
            cache=ApiCache(conn),
            http_client=http,
            rate_limiter=RateLimiter(999, sleep=lambda _: None),
        )
        assert client.lookup_recording(payload["id"]) is None
        assert conn.execute("SELECT count(*) FROM api_cache").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(429),
        httpx.Response(503),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={}),
    ],
)
def test_recording_failed_refresh_preserves_stale_success(tmp_path, response):
    from scrobble_recs.apis.musicbrainz import recording_params

    conn = initialize_database(load_config(_config_path(tmp_path)))
    payload, cache = _recording_payload(), ApiCache(conn)
    cache.write_response(
        source="musicbrainz",
        endpoint=f"recording/{payload['id']}",
        request_url="https://example.test",
        request_params=recording_params(),
        response_status=200,
        response_json=payload,
        fetched_at=datetime(2020, 1, 1),
        expires_at=datetime(2020, 2, 1),
        error_message=None,
    )
    before = conn.execute("SELECT * FROM api_cache").fetchall()
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as http:
        client = MusicBrainzClient(
            config=MusicBrainzApiConfig(enabled=True),
            cache=cache,
            http_client=http,
            rate_limiter=RateLimiter(999, sleep=lambda _: None),
            now=lambda: datetime(2026, 1, 1),
        )
        assert client.lookup_recording(payload["id"]).title == payload["title"]
    assert conn.execute("SELECT * FROM api_cache").fetchall() == before
    conn.close()


def test_recording_parser_deterministic_releases_and_missing_release():
    from scrobble_recs.apis.musicbrainz import parse_recording

    payload = _recording_payload()
    releases = payload["releases"]
    payload["releases"] = releases + [dict(releases[1], title="Contradiction"), {}, releases[0]]
    result = parse_recording(payload, payload["id"])
    assert result.release_title == "Later"
    payload["releases"].reverse()
    assert parse_recording(payload, payload["id"]) == result
    del payload["releases"]
    assert parse_recording(payload, payload["id"]).release_mbid is None


@pytest.mark.parametrize("payload", [None, [], {}, {"title": "Malformed"}])
def test_recording_malformed_fresh_success_never_requests_http(tmp_path, payload):
    from scrobble_recs.apis.musicbrainz import recording_params

    conn = initialize_database(load_config(_config_path(tmp_path)))
    identifier = _recording_payload()["id"]
    cache = ApiCache(conn)
    cache.write_response(
        source="musicbrainz",
        endpoint=f"recording/{identifier}",
        request_url="https://example.test",
        request_params=recording_params(),
        response_status=200,
        response_json=payload,
        fetched_at=datetime(2026, 1, 1),
        expires_at=datetime(2026, 2, 1),
        error_message=None,
    )

    def forbidden(request):
        raise AssertionError("malformed fresh success triggered HTTP")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as http:
        client = MusicBrainzClient(
            config=MusicBrainzApiConfig(enabled=True),
            cache=cache,
            http_client=http,
            rate_limiter=RateLimiter(999, sleep=lambda _: None),
            now=lambda: datetime(2026, 1, 2),
        )
        assert client.lookup_recording(identifier) is None
    conn.close()


def test_recording_valid_refresh_replaces_stale_but_wrong_stale_identity_is_not_fallback(tmp_path):
    from scrobble_recs.apis.musicbrainz import recording_params

    conn = initialize_database(load_config(_config_path(tmp_path)))
    payload, cache = _recording_payload(), ApiCache(conn)
    cache.write_response(
        source="musicbrainz",
        endpoint=f"recording/{payload['id']}",
        request_url="https://example.test",
        request_params=recording_params(),
        response_status=200,
        response_json=dict(payload, id="20000000-0000-4000-8000-000000000002"),
        fetched_at=datetime(2020, 1, 1),
        expires_at=datetime(2020, 2, 1),
        error_message=None,
    )
    responses = [httpx.Response(404), httpx.Response(200, json=dict(payload, title="Refreshed"))]
    with httpx.Client(transport=httpx.MockTransport(lambda _: responses.pop(0))) as http:
        client = MusicBrainzClient(
            config=MusicBrainzApiConfig(enabled=True),
            cache=cache,
            http_client=http,
            rate_limiter=RateLimiter(999, sleep=lambda _: None),
            now=lambda: datetime(2026, 1, 1),
        )
        assert client.lookup_recording(payload["id"]) is None
        assert client.lookup_recording(payload["id"]).title == "Refreshed"
    assert conn.execute("SELECT fetched_at FROM api_cache").fetchone()[0] == datetime(2026, 1, 1)
    conn.close()


def test_recording_cache_write_failure_propagates_and_keyboard_interrupt_is_not_swallowed(
    tmp_path, monkeypatch
):
    import duckdb

    conn = initialize_database(load_config(_config_path(tmp_path)))
    cache, payload = ApiCache(conn), _recording_payload()

    def fail_write(**kwargs):
        raise duckdb.IOException("cache unavailable")

    monkeypatch.setattr(cache, "write_response", fail_write)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        client = MusicBrainzClient(
            config=MusicBrainzApiConfig(enabled=True),
            cache=cache,
            http_client=http,
            rate_limiter=RateLimiter(999, sleep=lambda _: None),
        )
        with pytest.raises(duckdb.IOException, match="cache unavailable"):
            client.lookup_recording(payload["id"])
    assert conn.execute("SELECT count(*) FROM api_cache").fetchone()[0] == 0

    def interrupted(request):
        raise KeyboardInterrupt

    with httpx.Client(transport=httpx.MockTransport(interrupted)) as http:
        client = MusicBrainzClient(
            config=MusicBrainzApiConfig(enabled=True),
            cache=cache,
            http_client=http,
            rate_limiter=RateLimiter(999, sleep=lambda _: None),
        )
        with pytest.raises(KeyboardInterrupt):
            client.lookup_recording(payload["id"])
    conn.close()


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


def test_catalog_contract_parses_aliases_and_artist_relationships(tmp_path):
    payload = json.loads(Path("tests/fixtures/musicbrainz_artist_relationships.json").read_text())
    calls = []

    def handler(request):
        calls.append(request)
        assert dict(request.url.params) == {"inc": "aliases+artist-rels", "fmt": "json"}
        return httpx.Response(200, json=payload)

    conn = initialize_database(load_config(_config_path(tmp_path)))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = MusicBrainzClient(
            config=MusicBrainzApiConfig(enabled=True),
            cache=ApiCache(conn),
            http_client=http,
            rate_limiter=RateLimiter(999, sleep=lambda _: None),
            now=lambda: datetime(2026, 1, 1),
        )
        catalog = client.lookup_artist_catalog(payload["id"])
        assert catalog.artist.mbid == payload["id"]
        assert catalog.artist.life_span_begin == "1970"
        assert len(catalog.aliases) == 2
        assert catalog.aliases[0].name == "Shared"
        assert catalog.aliases[0].locale is None
        assert catalog.aliases[0].primary_for_locale is True
        assert catalog.aliases[0].begin == "1990"
        rows = catalog.artist_relationships
        assert len(rows) == 3
        assert (
            rows[0].relationship_type,
            rows[0].direction,
            rows[0].begin,
            rows[0].end,
            rows[0].ended,
        ) == ("member of band", "forward", "1990", "2001-02", True)
        assert rows[1].direction == "backward" and rows[1].begin is None
        assert rows[2].relationship_type == "teacher"
        assert client.lookup_artist_catalog(payload["id"]) == catalog
        assert len(calls) == 1
        client.close()
        assert not http.is_closed
    conn.close()


def test_catalog_stale_fallback_preserves_success_and_provenance(tmp_path):
    payload = json.loads(Path("tests/fixtures/musicbrainz_artist_relationships.json").read_text())
    conn = initialize_database(load_config(_config_path(tmp_path)))
    cache = ApiCache(conn)
    params = {"inc": "aliases+artist-rels", "fmt": "json"}
    cache.write_response(
        source="musicbrainz",
        endpoint=f"artist/{payload['id']}",
        request_url="https://example.test",
        request_params=params,
        response_status=200,
        response_json=payload,
        fetched_at=datetime(2020, 1, 1),
        expires_at=datetime(2020, 2, 1),
        error_message=None,
    )
    before = conn.execute("SELECT * FROM api_cache").fetchall()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as http:
        client = MusicBrainzClient(
            config=MusicBrainzApiConfig(enabled=True),
            cache=cache,
            http_client=http,
            rate_limiter=RateLimiter(999, sleep=lambda _: None),
            now=lambda: datetime(2026, 1, 1),
        )
        assert client.lookup_artist_catalog(payload["id"]).artist.name == "Seed 1"
        assert client.lookup_artist_catalog(payload["id"]).artist.name == "Seed 1"
    assert conn.execute("SELECT * FROM api_cache").fetchall() == before
    conn.close()


def test_catalog_rejects_404_malformed_and_mismatched_identity(tmp_path):
    conn = initialize_database(load_config(_config_path(tmp_path)))
    mbid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    responses = [
        httpx.Response(404),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "name": "Wrong"}),
    ]
    with httpx.Client(transport=httpx.MockTransport(lambda _: responses.pop(0))) as http:
        client = MusicBrainzClient(
            config=MusicBrainzApiConfig(enabled=True),
            cache=ApiCache(conn),
            http_client=http,
            rate_limiter=RateLimiter(999, sleep=lambda _: None),
        )
        for _ in range(4):
            assert client.lookup_artist_catalog(mbid) is None
        assert not responses
        assert client.lookup_artist_catalog("invalid") is None
    conn.close()


def test_catalog_malformed_collection_is_not_a_successful_empty_refresh(tmp_path):
    conn = initialize_database(load_config(_config_path(tmp_path)))
    payload = json.loads(Path("tests/fixtures/musicbrainz_artist_relationships.json").read_text())
    responses = [dict(payload, aliases={}), dict(payload, relations="malformed")]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=responses.pop(0)))
    ) as http:
        client = MusicBrainzClient(
            config=MusicBrainzApiConfig(enabled=True),
            cache=ApiCache(conn),
            http_client=http,
            rate_limiter=RateLimiter(999, sleep=lambda _: None),
            now=lambda: datetime(2026, 1, 1),
        )
        for _ in range(2):
            assert client.lookup_artist_catalog(payload["id"]) is None
        assert conn.execute("SELECT count(*) FROM api_cache").fetchone()[0] == 0
    conn.close()
