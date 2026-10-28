"""Bounded, explicit acquisition of unranked ListenBrainz memberships."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from duckdb import DuckDBPyConnection

from scrobble_recs.apis.cache import ApiCache, build_cache_key
from scrobble_recs.apis.listenbrainz import (
    ENDPOINT,
    SOURCE,
    ListenBrainzArtistRadioGroup,
    ListenBrainzClient,
    artist_radio_params,
    valid_mbid,
)
from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.evidence import (
    EvidenceObservation,
    listenbrainz_identity_warnings,
    observation_key,
    persist_evidence_observation,
)
from scrobble_recs.models.profile import select_seed_artists
from scrobble_recs.normalize.strings import normalize_key


class ListenBrainzClientProtocol(Protocol):
    def get_artist_radio(
        self, seed_artist_mbid: str
    ) -> tuple[ListenBrainzArtistRadioGroup, ...]: ...


@dataclass(frozen=True)
class ListenBrainzEnrichmentStats:
    eligible_seeds: int
    skipped_seeds: int
    requests: int
    artists_created: int
    observations_written: int
    recordings_written: int


@dataclass(frozen=True)
class ListenBrainzEnrichmentPlan:
    eligible_seeds: int
    skipped_seeds: int
    total_queued: int


def _seeds(conn: DuckDBPyConnection, config: ScrobbleRecsConfig) -> tuple[dict[int, str], int]:
    selected = select_seed_artists(
        conn,
        limit=config.enrichment.seed_artist_limit,
        min_scrobbles=config.recommendation.min_seed_scrobbles,
    )
    eligible = {}
    for seed in selected:
        row = conn.execute(
            "SELECT musicbrainz_artist_mbid, resolution_status, identity_confidence, "
            "identity_warnings FROM artist WHERE artist_id = ?",
            [seed.artist_id],
        ).fetchone()
        assert row is not None
        mbid = valid_mbid(row[0])
        warnings = str(row[3] or "").lower()
        if (
            mbid
            and row[1] == "resolved_musicbrainz"
            and float(row[2] or 0) >= 0.9
            and not any(word in warnings for word in ("ambiguous", "conflict", "multiple"))
        ):
            eligible[seed.artist_id] = mbid
    return eligible, len(selected) - len(eligible)


def plan_listenbrainz_enrichment(
    conn: DuckDBPyConnection, config: ScrobbleRecsConfig
) -> ListenBrainzEnrichmentPlan:
    seeds, skipped = _seeds(conn, config)
    queued = 0
    completed = []
    for artist_id in seeds:
        row = conn.execute(
            "SELECT queue_id, status FROM enrichment_queue WHERE source = ? AND endpoint = ? "
            "AND entity_type = 'artist' AND entity_id = ? ORDER BY created_at LIMIT 1",
            [SOURCE, ENDPOINT, artist_id],
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO enrichment_queue (queue_id, source, endpoint, entity_type, "
                "entity_id, priority, status) VALUES (?, ?, ?, 'artist', ?, 1, 'queued')",
                [str(uuid4()), SOURCE, ENDPOINT, artist_id],
            )
            queued += 1
        elif row[1] == "skipped":
            # Only currently eligible seeds reach this loop; failed rows require explicit retry.
            conn.execute(
                "UPDATE enrichment_queue SET status = 'queued', attempts = 0, "
                "error_message = NULL WHERE queue_id = ?",
                [row[0]],
            )
            queued += 1
        elif row[1] == "complete":
            completed.append(row[0])
    pending = conn.execute(
        "SELECT 1 FROM enrichment_queue WHERE source = ? AND endpoint = ? "
        "AND status IN ('queued', 'running') LIMIT 1",
        [SOURCE, ENDPOINT],
    ).fetchone()
    if pending is None:
        # Drain outstanding batches before starting another explicit refresh cycle.
        # The client still decides whether each refreshed request needs HTTP.
        for queue_id in completed:
            conn.execute(
                "UPDATE enrichment_queue SET status = 'queued', attempts = 0 WHERE queue_id = ?",
                [queue_id],
            )
            queued += 1
    return ListenBrainzEnrichmentPlan(len(seeds), skipped, queued)


def run_listenbrainz_enrichment(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    client: ListenBrainzClientProtocol | None = None,
) -> ListenBrainzEnrichmentStats:
    if not config.apis.listenbrainz.enabled:
        raise RuntimeError("ListenBrainz enrichment is disabled")
    # This CLI worker is synchronous. At a new run, persisted running rows belong
    # to an interrupted prior call; planning alone never resets an active request.
    # Return its unfinished attempt so interruption cannot exhaust the retry budget.
    conn.execute(
        "UPDATE enrichment_queue SET status = 'queued', attempts = greatest(attempts - 1, 0) "
        "WHERE source = ? AND endpoint = ? AND status = 'running'",
        [SOURCE, ENDPOINT],
    )
    seeds, skipped = _seeds(conn, config)
    plan_listenbrainz_enrichment(conn, config)
    resolved = client or ListenBrainzClient(config=config.apis.listenbrainz, cache=ApiCache(conn))
    requests = created = observations = recordings = 0
    try:
        rows = conn.execute(
            "SELECT queue_id, entity_id FROM enrichment_queue WHERE source = ? AND endpoint = ? "
            "AND status = 'queued' AND attempts < ? ORDER BY priority DESC, entity_id LIMIT ?",
            [
                SOURCE,
                ENDPOINT,
                config.enrichment.max_queue_attempts,
                config.enrichment.queue_batch_size,
            ],
        ).fetchall()
        for queue_id, artist_id in rows:
            if artist_id not in seeds:
                conn.execute(
                    "UPDATE enrichment_queue SET status = 'skipped' WHERE queue_id = ?", [queue_id]
                )
                continue
            conn.execute(
                "UPDATE enrichment_queue SET status = 'running', attempts = attempts + 1, "
                "last_attempt_at = current_timestamp WHERE queue_id = ?",
                [queue_id],
            )
            requests += 1
            try:
                # Cache writes have their own transaction; fetch before evidence transaction.
                groups = resolved.get_artist_radio(seeds[artist_id])
            except (RuntimeError, ValueError) as exc:
                conn.execute(
                    "UPDATE enrichment_queue SET status = 'failed', error_message = ? "
                    "WHERE queue_id = ?",
                    [str(exc), queue_id],
                )
                continue
            result = _replace_memberships(
                conn, config, artist_id, seeds[artist_id], groups, str(queue_id)
            )
            created += result[0]
            observations += result[1]
            recordings += result[2]
    finally:
        if client is None:
            assert isinstance(resolved, ListenBrainzClient)
            resolved.close()
    return ListenBrainzEnrichmentStats(
        len(seeds), skipped, requests, created, observations, recordings
    )


def _replace_memberships(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    seed_id: int,
    seed_mbid: str,
    groups: tuple[ListenBrainzArtistRadioGroup, ...],
    queue_id: str,
) -> tuple[int, int, int]:
    settings = config.apis.listenbrainz
    params = artist_radio_params(settings, seed_mbid)
    cache_key = build_cache_key(SOURCE, ENDPOINT, params)
    cached = conn.execute(
        "SELECT fetched_at, response_json FROM api_cache WHERE cache_key = ? "
        "AND response_status BETWEEN 200 AND 299",
        [cache_key],
    ).fetchone()
    fetched = cached[0] if cached else datetime.now(UTC).replace(tzinfo=None)
    payload_hash = (
        json.loads(cached[1])["payload_hash"]
        if cached
        else hashlib.sha256(
            json.dumps([asdict(row) for row in groups], sort_keys=True).encode()
        ).hexdigest()
    )
    created = written = recordings = 0
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "DELETE FROM artist_evidence_observation WHERE source = ? "
            "AND relationship_kind = 'artist_radio_membership' AND source_artist_id = ? "
            "AND cache_key = ?",
            [SOURCE, seed_id, cache_key],
        )
        seen: set[str] = set()
        limit = min(settings.max_similar_artists, config.enrichment.candidate_artist_limit)
        for group in groups[:limit]:
            mbid = valid_mbid(group.artist_mbid)
            if not mbid or mbid == seed_mbid or mbid in seen or not group.artist_name.strip():
                continue
            seen.add(mbid)
            target, is_new, warnings = _ensure_artist(conn, mbid, group.artist_name)
            created += int(is_new)
            identity = conn.execute(
                "SELECT identity_confidence, resolution_status FROM artist WHERE artist_id = ?",
                [target],
            ).fetchone()
            assert identity is not None
            selected_recordings = tuple(
                row
                for row in group.recordings[: settings.max_recordings_per_artist]
                if valid_mbid(row.recording_mbid) and valid_mbid(row.similar_artist_mbid) == mbid
            )
            representative = [
                {
                    "recording_mbid": valid_mbid(row.recording_mbid),
                    "total_listen_count": row.total_listen_count,
                }
                for row in selected_recordings
            ]
            observation = EvidenceObservation(
                SOURCE,
                "collaborative",
                "artist_radio_membership",
                seed_id,
                target,
                None,
                None,
                settings.mode,
                fetched,
                float(identity[0] or 0),
                {
                    **params,
                    "cache_key": cache_key,
                    "payload_hash": payload_hash,
                    "artist_mbid": mbid,
                    "artist_name": group.artist_name,
                    "response_position": group.response_position,
                    "total_listen_count": sum(
                        row.total_listen_count for row in selected_recordings
                    ),
                    "recordings": representative,
                    "identity_warnings": warnings,
                },
            )
            persist_evidence_observation(
                conn,
                observation,
                observation_key=observation_key(seed_id=seed_id, target_mbid=mbid, **params),
                cache_key=cache_key,
                payload_hash=payload_hash,
                identity_status=identity[1],
            )
            written += 1
            recordings += len(representative)
        conn.execute(
            "UPDATE enrichment_queue SET status = 'complete', error_message = "
            "NULL WHERE queue_id = ?",
            [queue_id],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.execute(
            "UPDATE enrichment_queue SET status = 'failed', error_message = "
            "'evidence persistence failed' WHERE queue_id = ?",
            [queue_id],
        )
        raise
    return created, written, recordings


def _ensure_artist(conn: DuckDBPyConnection, mbid: str, name: str) -> tuple[int, bool, list[str]]:
    rows = conn.execute(
        "SELECT artist_id FROM artist WHERE lower(musicbrainz_artist_mbid) = ? ORDER BY artist_id",
        [mbid],
    ).fetchall()
    if rows:
        # A deterministic attachment preserves the observation for inspection; multiple
        # claims remain quarantined and are never a canonical identity selection.
        artist_id = int(rows[0][0])
        return (
            artist_id,
            False,
            listenbrainz_identity_warnings(
                conn, artist_id, {"artist_mbid": mbid, "artist_name": name}
            ),
        )
    key = normalize_key(name)
    conflict = conn.execute(
        "SELECT 1 FROM artist WHERE split_part(artist_key, '::mbid:', 1) = ? "
        "OR lower(canonical_name) = lower(?) LIMIT 1",
        [key, name],
    ).fetchone()
    warnings = ["name_mbid_conflict"] if conflict else []
    next_id = conn.execute("SELECT coalesce(max(artist_id), 0) + 1 FROM artist").fetchone()
    assert next_id is not None
    artist_id = int(next_id[0])
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, musicbrainz_artist_mbid, "
        "resolution_status, identity_confidence, identity_warnings) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            artist_id,
            name,
            f"{key}::mbid:{mbid}",
            mbid,
            "external_listenbrainz",
            0.5 if conflict else 0.9,
            json.dumps(warnings),
        ],
    )
    return artist_id, True, warnings
