from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from scrobble_recs.apis.cache import ApiCache, JsonObject
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import MusicBrainzApiConfig

_SOURCE = "musicbrainz"


@dataclass(frozen=True)
class MusicBrainzArtistMatch:
    mbid: str
    name: str
    disambiguation: str | None
    country: str | None
    type: str | None
    score: float


@dataclass(frozen=True)
class MusicBrainzArtistResult:
    mbid: str
    name: str
    disambiguation: str
    country: str
    type: str
    life_span_begin: str
    life_span_end: str


class MusicBrainzClient:
    def __init__(
        self,
        *,
        config: MusicBrainzApiConfig,
        cache: ApiCache,
        http_client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.Client(
            timeout=30.0,
            headers={"User-Agent": config.user_agent},
        )
        self._rate_limiter = rate_limiter or RateLimiter(config.rate_limit_per_second)
        self._now = now or datetime.utcnow

    def search_artist(self, query: str, *, limit: int = 5) -> list[MusicBrainzArtistMatch]:
        params: JsonObject = {"query": f'artist:"{query}"', "limit": str(limit), "fmt": "json"}
        data = self._request("artist", params)
        if data is None:
            return []
        artists = data.get("artists", [])
        if not isinstance(artists, list):
            return []
        results: list[MusicBrainzArtistMatch] = []
        for artist in artists:
            if not isinstance(artist, dict):
                continue
            mbid = str(artist.get("id", ""))
            if not mbid:
                continue
            results.append(
                MusicBrainzArtistMatch(
                    mbid=mbid,
                    name=str(artist.get("name", "")),
                    disambiguation=_optional_string(artist.get("disambiguation")),
                    country=_optional_string(artist.get("country")),
                    type=_optional_string(artist.get("type")),
                    score=_float(artist.get("score")),
                )
            )
        return results

    def lookup_artist(self, mbid: str) -> MusicBrainzArtistResult | None:
        params: JsonObject = {"fmt": "json"}
        data = self._request(f"artist/{mbid}", params)
        if data is None:
            return None
        life_span = data.get("life-span", {}) or {}
        if not isinstance(life_span, dict):
            life_span = {}
        return MusicBrainzArtistResult(
            mbid=str(data.get("id", mbid)),
            name=str(data.get("name", "")),
            disambiguation=str(data.get("disambiguation", "")),
            country=str(data.get("country", "")),
            type=str(data.get("type", "")),
            life_span_begin=str(life_span.get("begin", "")),
            life_span_end=str(life_span.get("end", "")),
        )

    def _request(self, endpoint: str, params: JsonObject) -> JsonObject | None:
        now = self._now()
        cached = self._cache.get_fresh(_SOURCE, endpoint, params, now=now)
        if cached is not None:
            return cached

        stale = self._cache.get_stale_success(_SOURCE, endpoint, params)

        self._rate_limiter.wait()
        url = f"{self._config.base_url.rstrip('/')}/{endpoint}"
        try:
            response = self._http_client.get(url, params=params)
            status = response.status_code
            data: JsonObject = {}
            error_msg: str | None = None
            if 200 <= status < 300:
                data = response.json()
                if not isinstance(data, dict):
                    data = {}
            else:
                error_msg = response.text[:500]
            expires_at = now + timedelta(days=self._config.cache_ttl_days)
            self._cache.write_response(
                source=_SOURCE,
                endpoint=endpoint,
                request_url=str(response.url),
                request_params=params,
                response_status=status,
                response_json=data,
                fetched_at=now,
                expires_at=expires_at,
                error_message=error_msg,
            )
            if status >= 400:
                if stale is not None:
                    return stale
                return None
            return data
        except Exception:
            if stale is not None:
                return stale
            return None

    def close(self) -> None:
        if self._owns_http_client and not self._http_client.is_closed:
            self._http_client.close()


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
