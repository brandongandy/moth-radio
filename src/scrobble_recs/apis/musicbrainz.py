from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import httpx

from scrobble_recs.apis.cache import ApiCache, JsonObject
from scrobble_recs.apis.rate_limit import RateLimiter
from scrobble_recs.config import MusicBrainzApiConfig

_SOURCE = "musicbrainz"


@dataclass(frozen=True)
class MusicBrainzRecording:
    mbid: str
    title: str
    artist_mbids: tuple[str, ...]
    release_mbid: str | None
    release_title: str | None


def recording_params() -> JsonObject:
    return {"inc": "artist-credits+releases", "fmt": "json"}


def parse_recording(data: object, mbid: str) -> MusicBrainzRecording | None:
    """Validate identity and every credit; preserve literal provider titles."""
    identifier = valid_mbid(mbid)
    if not identifier or not isinstance(data, dict) or valid_mbid(data.get("id")) != identifier:
        return None
    title, credits = data.get("title"), data.get("artist-credit")
    if not isinstance(title, str) or not title.strip() or not isinstance(credits, list):
        return None
    artists = set()
    for credit in credits:
        if isinstance(credit, str):
            continue
        if not isinstance(credit, dict) or not isinstance(credit.get("artist"), dict):
            return None
        artist = valid_mbid(credit["artist"].get("id"))
        if not artist:
            return None
        artists.add(artist)
    if not artists:
        return None
    releases = data.get("releases", [])
    if not isinstance(releases, list):
        return None
    titles: dict[str, set[str]] = {}
    for release in releases:
        if not isinstance(release, dict):
            continue
        release_id, release_title = valid_mbid(release.get("id")), release.get("title")
        if release_id and isinstance(release_title, str) and release_title.strip():
            titles.setdefault(release_id, set()).add(release_title)
    pairs = sorted((key, next(iter(values))) for key, values in titles.items() if len(values) == 1)
    release_id, release_title = pairs[0] if pairs else (None, None)
    return MusicBrainzRecording(
        identifier, title, tuple(sorted(artists)), release_id, release_title
    )


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


@dataclass(frozen=True)
class MusicBrainzArtistAlias:
    name: str
    locale: str | None = None
    primary_for_locale: bool = False
    begin: str | None = None
    end: str | None = None
    type: str | None = None


@dataclass(frozen=True)
class MusicBrainzArtistRelationship:
    target_mbid: str
    target_name: str
    relationship_type: str
    direction: Literal["forward", "backward"]
    begin: str | None
    end: str | None
    ended: bool


@dataclass(frozen=True)
class MusicBrainzArtistCatalog:
    artist: MusicBrainzArtistResult
    aliases: tuple[MusicBrainzArtistAlias, ...]
    artist_relationships: tuple[MusicBrainzArtistRelationship, ...]


def valid_mbid(value: Any) -> str | None:
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except (ValueError, AttributeError):
        return None


def catalog_params() -> JsonObject:
    return {"inc": "aliases+artist-rels", "fmt": "json"}


def _parse_catalog(data: Any, mbid: str) -> MusicBrainzArtistCatalog | None:
    if not isinstance(data, dict) or valid_mbid(data.get("id")) != mbid:
        return None
    if any(
        field in data and not isinstance(data[field], list) for field in ("aliases", "relations")
    ):
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    life = data.get("life-span")
    life = life if isinstance(life, dict) else {}
    artist = MusicBrainzArtistResult(
        mbid,
        name,
        _optional_string(data.get("disambiguation")) or "",
        _optional_string(data.get("country")) or "",
        _optional_string(data.get("type")) or "",
        _optional_string(life.get("begin")) or "",
        _optional_string(life.get("end")) or "",
    )
    aliases = []
    seen = set()
    for row in data.get("aliases", []) if isinstance(data.get("aliases"), list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        alias = row["name"].strip()
        locale = _optional_string(row.get("locale"))
        if not alias or (alias, locale) in seen:
            continue
        seen.add((alias, locale))
        aliases.append(
            MusicBrainzArtistAlias(
                alias,
                locale,
                row.get("primary") is True,
                _optional_string(row.get("begin-date")),
                _optional_string(row.get("end-date")),
                _optional_string(row.get("type")),
            )
        )
    relations = []
    for row in data.get("relations", []) if isinstance(data.get("relations"), list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("artist"), dict):
            continue
        target = row["artist"]
        target_mbid = valid_mbid(target.get("id"))
        target_name = target.get("name")
        kind = row.get("type")
        direction = row.get("direction")
        if (
            not target_mbid
            or not isinstance(target_name, str)
            or not target_name.strip()
            or not isinstance(kind, str)
            or not kind.strip()
            or direction not in ("forward", "backward")
        ):
            continue
        relations.append(
            MusicBrainzArtistRelationship(
                target_mbid,
                target_name,
                kind,
                direction,
                _optional_string(row.get("begin")),
                _optional_string(row.get("end")),
                row.get("ended") is True,
            )
        )
    return MusicBrainzArtistCatalog(artist, tuple(aliases), tuple(relations))


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
        self._now = now or (lambda: datetime.now(UTC).replace(tzinfo=None))

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

    def lookup_artist_catalog(self, mbid: str) -> MusicBrainzArtistCatalog | None:
        identifier = valid_mbid(mbid)
        if identifier is None:
            return None
        endpoint, params, now = f"artist/{identifier}", catalog_params(), self._now()
        fresh = self._cache.get_fresh(_SOURCE, endpoint, params, now=now)
        if fresh is not None:
            return _parse_catalog(fresh, identifier)
        stale = self._cache.get_stale_success(_SOURCE, endpoint, params)
        fallback = _parse_catalog(stale, identifier)
        self._rate_limiter.wait()
        try:
            response = self._http_client.get(
                f"{self._config.base_url.rstrip('/')}/{endpoint}", params=params
            )
            if not response.is_success:
                return fallback
            data = response.json()
            parsed = _parse_catalog(data, identifier)
            if parsed is None:
                return fallback
        except (httpx.HTTPError, ValueError):
            return fallback
        # Failed refreshes never replace the usable successful cache row.
        self._cache.write_response(
            source=_SOURCE,
            endpoint=endpoint,
            request_url=str(response.url),
            request_params=params,
            response_status=response.status_code,
            response_json=data,
            fetched_at=now,
            expires_at=now + timedelta(days=self._config.cache_ttl_days),
            error_message=None,
        )
        return parsed

    def lookup_recording(self, mbid: str) -> MusicBrainzRecording | None:
        identifier = valid_mbid(mbid)
        if identifier is None:
            return None
        endpoint, params, now = f"recording/{identifier}", recording_params(), self._now()
        cached = self._cache.get_success_entry(_SOURCE, endpoint, params)
        fallback = parse_recording(cached.response_json, identifier) if cached else None
        if cached is not None and cached.expires_at > now:
            return fallback
        self._rate_limiter.wait()
        try:
            response = self._http_client.get(
                f"{self._config.base_url.rstrip('/')}/{endpoint}", params=params
            )
            if not response.is_success:
                return fallback
            data = response.json()
            parsed = parse_recording(data, identifier)
            if parsed is None:
                return fallback
        except (httpx.HTTPError, ValueError):
            return fallback
        self._cache.write_response(
            source=_SOURCE,
            endpoint=endpoint,
            request_url=str(response.url),
            request_params=params,
            response_status=response.status_code,
            response_json=data,
            fetched_at=now,
            expires_at=now + timedelta(days=self._config.cache_ttl_days),
            error_message=None,
        )
        return parsed

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
