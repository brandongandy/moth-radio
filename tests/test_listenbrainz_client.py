import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.apis.listenbrainz import ListenBrainzClient
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import ListenBrainzApiConfig, load_config
from scrobble_recs.db import initialize_database

SEED = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
NOW = datetime(2026, 1, 1)
PAYLOAD = json.loads(Path("tests/fixtures/listenbrainz_artist_radio.json").read_text())


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "app": {
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {"lastfm_csv": "tests/fixtures/tiny_lastfm_export.csv"},
            }
        )
    )
    connection = initialize_database(load_config(path))
    yield connection
    connection.close()


def make_client(conn, handler, **settings):
    return ListenBrainzClient(
        config=ListenBrainzApiConfig(enabled=True, **settings),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000, sleep=lambda _: None),
        now=lambda: NOW,
        retry_sleep=lambda _: None,
    )


def test_request_and_fresh_cache_preserve_payload_order(conn):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=PAYLOAD)

    client = make_client(conn, handler)
    first = client.get_artist_radio(SEED)
    assert first == client.get_artist_radio(SEED)
    assert [row.artist_mbid for row in first] == list(PAYLOAD)
    assert [row.response_position for row in first] == [0, 1]
    assert first[0].recordings[0].total_listen_count == 42
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == f"/1/lb-radio/artist/{SEED}"
    assert dict(request.url.params) == {
        "mode": "easy",
        "max_similar_artists": "100",
        "max_recordings_per_artist": "3",
        "pop_begin": "0",
        "pop_end": "100",
    }
    assert "authorization" not in request.headers
    assert request.extensions["timeout"]["read"] == 20.0
    params = json.loads(conn.execute("SELECT request_params FROM api_cache").fetchone()[0])
    assert params == {
        "seed_artist_mbid": SEED,
        **{k: (v if k == "mode" else int(v)) for k, v in request.url.params.items()},
    }


@pytest.mark.parametrize("failure", [503, 429, "transport"])
@pytest.mark.parametrize("stale", [False, True])
def test_transient_retry_and_stale_policy(conn, failure, stale):
    prime = make_client(conn, lambda _: httpx.Response(200, json=PAYLOAD))
    expected = prime.get_artist_radio(SEED)
    conn.execute("UPDATE api_cache SET expires_at = ?", [NOW - timedelta(days=1)])
    calls = []
    sleeps = []

    def handler(request):
        calls.append(request)
        if failure == "transport":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(failure)

    client = ListenBrainzClient(
        config=ListenBrainzApiConfig(enabled=True, stale_cache_on_error=stale),
        cache=ApiCache(conn),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(1000, sleep=lambda _: None),
        now=lambda: NOW,
        retry_sleep=sleeps.append,
    )
    if stale:
        assert client.get_artist_radio(SEED) == expected
    else:
        with pytest.raises(RuntimeError):
            client.get_artist_radio(SEED)
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]
    # A failed refresh must not destroy the last usable payload.
    assert conn.execute("SELECT response_status FROM api_cache").fetchone()[0] == 200


@pytest.mark.parametrize("status", [400, 401, 404])
def test_4xx_is_not_retried(conn, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    with pytest.raises(RuntimeError, match=str(status)):
        make_client(conn, handler).get_artist_radio(SEED)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {SEED: {}},
        {SEED: [None]},
        {SEED: [{}]},
        {
            SEED: [
                {
                    "recording_mbid": "x",
                    "similar_artist_mbid": SEED,
                    "similar_artist_name": "Name",
                    "total_listen_count": -1,
                }
            ]
        },
    ],
)
def test_malformed_success_is_not_cached(conn, payload):
    with pytest.raises(RuntimeError, match="payload"):
        make_client(conn, lambda _: httpx.Response(200, json=payload)).get_artist_radio(SEED)
    assert conn.execute("SELECT count(*) FROM api_cache").fetchone()[0] == 0


def test_client_close_ownership_and_disabled(conn):
    injected = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    with ListenBrainzClient(
        config=ListenBrainzApiConfig(), cache=ApiCache(conn), http_client=injected
    ) as client:
        with pytest.raises(RuntimeError, match="disabled"):
            client.get_artist_radio(SEED)
    assert not injected.is_closed
    injected.close()
    owned = ListenBrainzClient(config=ListenBrainzApiConfig(), cache=ApiCache(conn))
    owned.close()
    owned.close()
    assert owned._http_client.is_closed
