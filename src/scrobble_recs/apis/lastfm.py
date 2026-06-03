from __future__ import annotations

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
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.Client(timeout=30.0)
        self._rate_limiter = rate_limiter or RateLimiter(config.rate_limit_per_second)
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

        self._rate_limiter.wait()
        request_params = {
            "method": method,
            "api_key": self._config.api_key,
            "format": "json",
            **params,
        }
        request_url = self._config.base_url
        response = self._http_client.get(request_url, params=request_params)
        response_json = _response_json(response)
        expires_at = now + timedelta(days=self._config.cache_ttl_days)
        self._cache.write_response(
            source=_SOURCE,
            endpoint=method,
            request_url=request_url,
            request_params=cache_params,
            response_status=response.status_code,
            response_json=response_json,
            fetched_at=now,
            expires_at=expires_at,
            error_message=(
                _extract_error_message(response_json) if response.status_code >= 400 else None
            ),
        )
        if response.status_code < 200 or response.status_code > 299:
            message = _extract_error_message(response_json) or (
                f"Last.fm request failed with status {response.status_code}"
            )
            raise RuntimeError(message)
        return response_json

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
