"""Cache-first access to public aggregate artist radio; no account or history upload."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Literal
from uuid import UUID

import httpx

from scrobble_recs.apis.cache import ApiCache, JsonObject
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import ListenBrainzApiConfig

SOURCE = "listenbrainz"
ENDPOINT = "artist-radio"


@dataclass(frozen=True)
class ListenBrainzArtistRadioRecording:
    recording_mbid: str
    similar_artist_mbid: str
    similar_artist_name: str
    total_listen_count: int


@dataclass(frozen=True)
class ListenBrainzArtistRadioGroup:
    artist_mbid: str
    artist_name: str
    recordings: tuple[ListenBrainzArtistRadioRecording, ...]
    response_position: int


def valid_mbid(value: str | None) -> str | None:
    try:
        return str(UUID(value)) if value else None
    except (ValueError, AttributeError):
        return None


def artist_radio_params(config: ListenBrainzApiConfig, seed_artist_mbid: str) -> JsonObject:
    return {
        "seed_artist_mbid": seed_artist_mbid,
        "mode": config.mode,
        "max_similar_artists": config.max_similar_artists,
        "max_recordings_per_artist": config.max_recordings_per_artist,
        "pop_begin": config.pop_begin,
        "pop_end": config.pop_end,
    }


class ListenBrainzClient:
    def __init__(
        self,
        *,
        config: ListenBrainzApiConfig,
        cache: ApiCache,
        http_client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        retry_sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.Client(timeout=config.timeout_seconds)
        self._rate_limiter = rate_limiter or RateLimiter(config.rate_limit_per_second)
        self._retry_sleep = retry_sleep or time.sleep
        self._now = now or (lambda: datetime.now(UTC).replace(tzinfo=None))

    def get_artist_radio(self, seed_artist_mbid: str) -> tuple[ListenBrainzArtistRadioGroup, ...]:
        if not self._config.enabled:
            raise RuntimeError("ListenBrainz enrichment is disabled")
        mbid = valid_mbid(seed_artist_mbid)
        if mbid is None:
            raise ValueError("seed_artist_mbid must be a valid MBID")
        params = artist_radio_params(self._config, mbid)
        cached = self._cache.get_fresh(SOURCE, ENDPOINT, params, self._now())
        if cached is not None:
            return _cached_groups(cached)
        url = self._config.base_url.rstrip("/") + "/lb-radio/artist/" + mbid
        query = {key: value for key, value in params.items() if key != "seed_artist_mbid"}
        error = "ListenBrainz request failed"
        for attempt in range(self._config.max_retries + 1):
            self._rate_limiter.wait()
            try:
                response = self._http_client.get(
                    url, params=query, timeout=self._config.timeout_seconds
                )
            except httpx.TransportError as exc:
                error = f"ListenBrainz transport error: {exc}"
            else:
                if 200 <= response.status_code < 300:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise RuntimeError("ListenBrainz malformed JSON payload") from exc
                    groups = _parse_groups(payload)
                    fetched = self._now()
                    # ApiCache sorts object keys. Store ordered pairs separately so response
                    # positions on a cache hit are identical to the original provider order.
                    self._cache.write_response(
                        source=SOURCE,
                        endpoint=ENDPOINT,
                        request_url=url,
                        request_params=params,
                        response_status=response.status_code,
                        response_json={
                            "artist_groups": list(payload.items()),
                            "payload_hash": hashlib.sha256(response.content).hexdigest(),
                        },
                        fetched_at=fetched,
                        expires_at=fetched + timedelta(days=self._config.cache_ttl_days),
                        error_message=None,
                    )
                    return groups
                error = f"ListenBrainz request failed with status {response.status_code}"
                if response.status_code != 429 and response.status_code < 500:
                    raise RuntimeError(error)
            if attempt < self._config.max_retries:
                self._retry_sleep(2.0**attempt)
        if self._config.stale_cache_on_error:
            stale = self._cache.get_stale_success(SOURCE, ENDPOINT, params)
            if stale is not None:
                return _cached_groups(stale)
        raise RuntimeError(error)

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def __enter__(self) -> ListenBrainzClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        self.close()
        return False


def _cached_groups(payload: JsonObject) -> tuple[ListenBrainzArtistRadioGroup, ...]:
    return _parse_groups(dict(payload["artist_groups"]))


def _parse_groups(payload: object) -> tuple[ListenBrainzArtistRadioGroup, ...]:
    def malformed() -> RuntimeError:
        return RuntimeError("ListenBrainz malformed artist-radio payload")

    if not isinstance(payload, dict):
        raise malformed()
    result = []
    for position, (mbid, items) in enumerate(payload.items()):
        if not isinstance(mbid, str) or not isinstance(items, list):
            raise malformed()
        recordings = []
        for item in items:
            if not isinstance(item, dict):
                raise malformed()
            for key in ("recording_mbid", "similar_artist_mbid", "similar_artist_name"):
                if not isinstance(item.get(key), str) or not item[key].strip():
                    raise malformed()
            count = item.get("total_listen_count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise malformed()
            if item["similar_artist_mbid"] != mbid:
                raise malformed()
            recordings.append(
                ListenBrainzArtistRadioRecording(
                    item["recording_mbid"], mbid, item["similar_artist_name"], count
                )
            )
        if recordings:
            result.append(
                ListenBrainzArtistRadioGroup(
                    mbid, recordings[0].similar_artist_name, tuple(recordings), position
                )
            )
    return tuple(result)
