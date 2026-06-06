from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx

from scrobble_recs.apis.cache import ApiCache, JsonObject
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import LastFmApiConfig

_SOURCE = "lastfm"


@dataclass(frozen=True)
class LastFmSimilarArtist:
    name: str
    mbid: str | None
    match: float


@dataclass(frozen=True)
class LastFmArtistTag:
    name: str
    count: float


@dataclass(frozen=True)
class LastFmTagTopArtist:
    rank: int
    name: str
    mbid: str | None
    count: float


class LastFmClient:
    def __init__(
        self,
        *,
        config: LastFmApiConfig,
        cache: ApiCache,
        http_client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        retry_sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.Client(timeout=30.0)
        self._rate_limiter = rate_limiter or RateLimiter(config.rate_limit_per_second)
        self._retry_sleep = retry_sleep or time.sleep
        self._now = now or datetime.utcnow

    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        payload = self._request(
            "artist.getSimilar",
            {"artist": artist, "limit": limit},
        )
        artists = _as_list(payload.get("similarartists", {}).get("artist"))
        result: list[LastFmSimilarArtist] = []
        for item in artists:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            result.append(
                LastFmSimilarArtist(
                    name=name,
                    mbid=_optional_string(item.get("mbid")),
                    match=_float(item.get("match")),
                )
            )
        return result

    def get_artist_tags(self, artist: str) -> list[LastFmArtistTag]:
        payload = self._request("artist.getTopTags", {"artist": artist})
        tags = _as_list(payload.get("toptags", {}).get("tag"))
        result: list[LastFmArtistTag] = []
        for item in tags:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            result.append(
                LastFmArtistTag(
                    name=name,
                    count=_float(item.get("count")),
                )
            )
        return result

    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        payload = self._request("tag.getTopArtists", {"tag": tag, "limit": limit})
        artists = _as_list(payload.get("topartists", {}).get("artist"))
        result: list[LastFmTagTopArtist] = []
        for index, item in enumerate(artists, start=1):
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            result.append(
                LastFmTagTopArtist(
                    rank=index,
                    name=name,
                    mbid=_optional_string(item.get("mbid")),
                    count=_float(item.get("count")),
                )
            )
        return result

    def _request(self, method: str, params: JsonObject) -> JsonObject:
        self._ensure_enabled()
        self._ensure_api_key()

        cache_params = {"method": method, **params}
        now = self._now()
        cached = self._cache.get_fresh(_SOURCE, method, cache_params, now)
        if cached is not None:
            return cached

        request_params = {
            "method": method,
            "api_key": self._config.api_key,
            "format": "json",
            **params,
        }
        request_url = self._config.base_url
        attempts = self._config.max_retries + 1
        last_status: int | None = None
        last_payload: JsonObject | None = None
        last_transport_error: str | None = None

        for attempt in range(attempts):
            self._rate_limiter.wait()
            try:
                response = self._http_client.get(request_url, params=request_params)
            except httpx.TransportError as exc:
                last_transport_error = str(exc).strip() or (
                    "Last.fm request failed before receiving a response"
                )
                if attempt < attempts - 1:
                    self._retry_sleep(2.0**attempt)
                    continue
                break

            if 200 <= response.status_code <= 299:
                response_json = _response_json(response)
                fetched_at = self._now()
                expires_at = fetched_at + timedelta(days=self._config.cache_ttl_days)
                self._cache.write_response(
                    source=_SOURCE,
                    endpoint=method,
                    request_url=request_url,
                    request_params=cache_params,
                    response_status=response.status_code,
                    response_json=response_json,
                    fetched_at=fetched_at,
                    expires_at=expires_at,
                    error_message=None,
                )
                return response_json

            response_json = _error_response_json(response)
            last_status = response.status_code
            last_payload = response_json

            if not _is_transient_status(response.status_code):
                self._write_error_response(
                    method=method,
                    request_url=request_url,
                    cache_params=cache_params,
                    response_status=response.status_code,
                    response_json=response_json,
                )
                raise RuntimeError(_error_message(response.status_code, response_json))

            if attempt < attempts - 1:
                if response.status_code == 429:
                    self._retry_sleep(2.0**attempt)
                continue

        stale = self._cache.get_stale_success(_SOURCE, method, cache_params)
        if stale is not None:
            return stale

        if last_status is None or last_payload is None:
            raise RuntimeError(
                last_transport_error or "Last.fm request failed before receiving a response"
            )

        self._write_error_response(
            method=method,
            request_url=request_url,
            cache_params=cache_params,
            response_status=last_status,
            response_json=last_payload,
        )
        raise RuntimeError(_error_message(last_status, last_payload))

    def _write_error_response(
        self,
        *,
        method: str,
        request_url: str,
        cache_params: JsonObject,
        response_status: int,
        response_json: JsonObject,
    ) -> None:
        fetched_at = self._now()
        expires_at = fetched_at + timedelta(days=self._config.cache_ttl_days)
        self._cache.write_response(
            source=_SOURCE,
            endpoint=method,
            request_url=request_url,
            request_params=cache_params,
            response_status=response_status,
            response_json=response_json,
            fetched_at=fetched_at,
            expires_at=expires_at,
            error_message=(
                _extract_error_message(response_json) if response_status >= 400 else None
            ),
        )

    def _ensure_enabled(self) -> None:
        if not self._config.enabled:
            raise RuntimeError("Last.fm enrichment is disabled")

    def _ensure_api_key(self) -> None:
        if self._config.api_key is None:
            raise RuntimeError("Last.fm API key is required for enrichment")

    def close(self) -> None:
        if self._owns_http_client and not self._http_client.is_closed:
            self._http_client.close()

    def __enter__(self) -> LastFmClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> Literal[False]:
        self.close()
        return False


def _response_json(response: httpx.Response) -> JsonObject:
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Last.fm API returned a non-object JSON payload")
    return payload


def _error_response_json(response: httpx.Response) -> JsonObject:
    try:
        payload = response.json()
    except ValueError:
        return {"message": _status_error_message(response.status_code)}
    if isinstance(payload, dict):
        return payload
    return {"message": _status_error_message(response.status_code)}


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _extract_error_message(payload: JsonObject) -> str | None:
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return None


def _error_message(status_code: int, payload: JsonObject) -> str:
    return _extract_error_message(payload) or _status_error_message(status_code)


def _is_transient_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _status_error_message(status_code: int) -> str:
    return f"Last.fm request failed with status {status_code}"
