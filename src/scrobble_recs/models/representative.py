"""Pure, conservative selection of a corroborated recording after artist ranking."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from math import isfinite
from typing import Literal

from duckdb import DuckDBPyConnection

from scrobble_recs.apis.cache import build_cache_key
from scrobble_recs.apis.musicbrainz import (
    MusicBrainzRecording,
    parse_recording,
    recording_params,
    valid_mbid,
)
from scrobble_recs.models.evidence import EvidenceObservation
from scrobble_recs.models.profile_window import normalize_timestamp


@dataclass(frozen=True)
class StartHereItem:
    kind: Literal["recording", "release"]
    title: str
    musicbrainz_id: str
    source: Literal["listenbrainz", "musicbrainz"]
    reason: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class RadioRecordingEvidence:
    recording_mbid: str
    total_listen_count: int
    fetched_at: datetime
    provenance: dict[str, object]


def load_recording_metadata(
    conn: DuckDBPyConnection,
    identifiers: Sequence[str],
    *,
    cutoff: datetime | None = None,
) -> tuple[dict[str, MusicBrainzRecording], dict[str, dict[str, object]]]:
    """Read validated persisted successes, including expired ones, without clients."""
    metadata: dict[str, MusicBrainzRecording] = {}
    provenance: dict[str, dict[str, object]] = {}
    cutoff = normalize_timestamp(cutoff) if cutoff else None
    params = recording_params()
    for identifier in sorted(set(identifiers)):
        endpoint = f"recording/{identifier}"
        key = build_cache_key("musicbrainz", endpoint, params)
        row = conn.execute(
            "SELECT request_params, response_json, fetched_at, expires_at FROM api_cache "
            "WHERE cache_key = ? AND source = 'musicbrainz' AND endpoint = ? "
            "AND response_status BETWEEN 200 AND 299 AND (? IS NULL OR fetched_at <= ?)",
            [key, endpoint, cutoff, cutoff],
        ).fetchone()
        if row is None:
            continue
        try:
            if json.loads(row[0]) != params:
                continue
            raw = json.loads(row[1])
            recording = parse_recording(raw, identifier)
            if recording is None:
                continue
            digest = hashlib.sha256(
                json.dumps(raw, sort_keys=True, allow_nan=False).encode()
            ).hexdigest()
        except (TypeError, ValueError):
            continue
        metadata[identifier] = recording
        provenance[identifier] = {
            "cache_key": key,
            "payload_hash": digest,
            "request_params": deepcopy(params),
            "fetched_at": row[2].isoformat(),
            "expires_at": row[3].isoformat(),
            "stale_policy": "validated_success_allowed",
        }
    return metadata, provenance


def load_safe_artist_mbids(conn: DuckDBPyConnection) -> dict[int, str]:
    """Recheck current provider identity and canonical local uniqueness."""
    rows = conn.execute(
        "SELECT artist_id, musicbrainz_artist_mbid, resolution_status, identity_confidence, "
        "identity_warnings FROM artist"
    ).fetchall()
    claims: dict[str, int] = {}
    for row in rows:
        identifier = valid_mbid(row[1])
        if identifier:
            claims[identifier] = claims.get(identifier, 0) + 1
    safe = {}
    for artist_id, mbid, status, confidence, warnings in rows:
        identifier = valid_mbid(mbid)
        try:
            parsed = json.loads(warnings) if isinstance(warnings, str) else warnings
        except ValueError:
            continue
        if (
            identifier
            and claims[identifier] == 1
            and status in ("resolved_musicbrainz", "external_listenbrainz", "external_musicbrainz")
            and isinstance(confidence, (int, float))
            and isfinite(confidence)
            and confidence >= 0.9
            and safe_identity_warnings(parsed)
        ):
            safe[int(artist_id)] = identifier
    return safe


def safe_identity_warnings(warnings: object) -> bool:
    return isinstance(warnings, list) and all(
        isinstance(warning, str)
        and not any(word in warning.lower() for word in ("ambiguous", "conflict", "multiple"))
        for warning in warnings
    )


def _integer(value: object, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _radio_provenance(row: EvidenceObservation) -> dict[str, object] | None:
    data = row.evidence
    params = {
        key: data.get(key)
        for key in (
            "seed_artist_mbid",
            "mode",
            "max_similar_artists",
            "max_recordings_per_artist",
            "pop_begin",
            "pop_end",
        )
    }
    seed = valid_mbid(params["seed_artist_mbid"])
    begin, end = params["pop_begin"], params["pop_end"]
    if (
        not seed
        or params["mode"] not in ("easy", "medium", "hard")
        or params["mode"] != row.provider_mode
        or not _integer(params["max_similar_artists"], 1)
        or not _integer(params["max_recordings_per_artist"], 1)
        or not isinstance(begin, int)
        or isinstance(begin, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= begin <= end <= 100
        or not _integer(row.source_artist_id, 1)
        or not _integer(row.target_artist_id, 1)
        or not isinstance(row.fetched_at, datetime)
        or not isfinite(row.identity_confidence)
        or row.identity_confidence < 0.9
        or not safe_identity_warnings(data.get("identity_warnings"))
        or any(
            not isinstance(data.get(key), str) or not str(data[key]).strip()
            for key in ("cache_key", "payload_hash")
        )
    ):
        return None
    params["seed_artist_mbid"] = seed
    return {
        "source_artist_id": row.source_artist_id,
        "target_artist_id": row.target_artist_id,
        "identity_confidence": row.identity_confidence,
        "identity_warnings": deepcopy(data["identity_warnings"]),
        "provider_mode": row.provider_mode,
        "request_params": params,
        "cache_key": data["cache_key"],
        "payload_hash": data["payload_hash"],
        "fetched_at": row.fetched_at.isoformat(),
    }


def radio_recording_evidence(
    recommended_artist_mbid: str | None,
    observations: Sequence[EvidenceObservation],
) -> tuple[RadioRecordingEvidence, ...]:
    """Newest evidence wins; conflicting newest counts discard only that recording."""
    artist = valid_mbid(recommended_artist_mbid)
    if not artist:
        return ()
    relevant = [
        row
        for row in observations
        if row.source == "listenbrainz"
        and row.family == "collaborative"
        and row.relationship_kind == "artist_radio_membership"
    ]
    targets = {row.target_artist_id for row in relevant}
    if len(targets) > 1 or any(not _integer(target, 1) for target in targets):
        return ()
    choices: dict[str, list[RadioRecordingEvidence]] = {}
    for row in relevant:
        asserted = valid_mbid(row.evidence.get("artist_mbid"))
        warnings = row.evidence.get("identity_warnings")
        if (asserted and asserted != artist) or (
            isinstance(warnings, list)
            and any(
                isinstance(warning, str)
                and any(word in warning.lower() for word in ("ambiguous", "conflict", "multiple"))
                for warning in warnings
            )
        ):
            return ()
        provenance = _radio_provenance(row)
        if asserted != artist or provenance is None:
            continue
        entries = row.evidence.get("recordings")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identifier, count = (
                valid_mbid(entry.get("recording_mbid")),
                entry.get("total_listen_count"),
            )
            if not identifier or not isinstance(count, int) or not _integer(count):
                continue
            assert row.fetched_at is not None
            choices.setdefault(identifier, []).append(
                RadioRecordingEvidence(
                    identifier,
                    count,
                    row.fetched_at,
                    {
                        **deepcopy(provenance),
                        "recording_mbid": identifier,
                        "total_listen_count": count,
                    },
                )
            )
    selected = []
    for rows in choices.values():
        newest = max(row.fetched_at for row in rows)
        latest = [row for row in rows if row.fetched_at == newest]
        if len({row.total_listen_count for row in latest}) != 1:
            continue
        selected.append(min(latest, key=lambda row: json.dumps(row.provenance, sort_keys=True)))
    return tuple(sorted(selected, key=lambda row: (-row.total_listen_count, row.recording_mbid)))


def _validated_metadata(
    value: MusicBrainzRecording, identifier: str
) -> MusicBrainzRecording | None:
    if not isinstance(value, MusicBrainzRecording) or not isinstance(value.artist_mbids, tuple):
        return None
    if (value.release_mbid is None) != (value.release_title is None):
        return None
    if value.release_mbid is not None and (
        not valid_mbid(value.release_mbid)
        or not isinstance(value.release_title, str)
        or not value.release_title.strip()
    ):
        return None
    return parse_recording(
        {
            "id": value.mbid,
            "title": value.title,
            "artist-credit": [{"artist": {"id": artist}} for artist in value.artist_mbids],
            "releases": (
                [{"id": value.release_mbid, "title": value.release_title}]
                if value.release_mbid
                else []
            ),
        },
        identifier,
    )


def select_start_here_item(
    *,
    recommended_artist_mbid: str | None,
    observations: Sequence[EvidenceObservation],
    recording_metadata: Mapping[str, MusicBrainzRecording],
) -> StartHereItem | None:
    artist = valid_mbid(recommended_artist_mbid)
    normalized: dict[str, set[MusicBrainzRecording | None]] = {}
    for key, value in recording_metadata.items():
        identifier = valid_mbid(key)
        if identifier:
            normalized.setdefault(identifier, set()).add(_validated_metadata(value, identifier))
    for radio in radio_recording_evidence(artist, observations):
        values = normalized.get(radio.recording_mbid, set())
        if len(values) != 1:
            continue
        metadata = next(iter(values))
        if metadata is None or artist not in metadata.artist_mbids:
            continue
        recording = asdict(metadata)
        recording["artist_mbids"] = list(metadata.artist_mbids)
        return StartHereItem(
            "recording",
            metadata.title,
            metadata.mbid,
            "listenbrainz",
            "ListenBrainz radio recording preferred by listen count within the recorded "
            "popularity request; MusicBrainz confirmed the artist credit.",
            {
                "policy_version": "start-here-v1",
                "artist_mbid": artist,
                "listenbrainz": deepcopy(radio.provenance),
                "musicbrainz": {"recording": recording},
            },
        )
    return None
