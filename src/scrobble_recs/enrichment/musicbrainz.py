"""Bounded acquisition of MusicBrainz catalog facts, independent of ranking policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from duckdb import DuckDBPyConnection

from scrobble_recs.apis.cache import ApiCache, build_cache_key
from scrobble_recs.apis.musicbrainz import (
    MusicBrainzArtistCatalog,
    MusicBrainzClient,
    catalog_params,
    valid_mbid,
)
from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.evidence import (
    EvidenceObservation,
    observation_key,
    persist_evidence_observation,
)
from scrobble_recs.models.profile import select_seed_artists
from scrobble_recs.normalize.strings import normalize_key

SOURCE = "musicbrainz"
ENDPOINT = "artist-catalog"


class MusicBrainzCatalogClientProtocol(Protocol):
    def lookup_artist_catalog(self, mbid: str) -> MusicBrainzArtistCatalog | None: ...


@dataclass(frozen=True)
class MusicBrainzCatalogEnrichmentStats:
    artists_requested: int
    aliases_written: int
    relationships_written: int
    related_artists_created: int
    ambiguous_relationships: int


@dataclass(frozen=True)
class MusicBrainzCatalogEnrichmentPlan:
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
        matches = conn.execute(
            "SELECT count(*) FROM artist WHERE lower(musicbrainz_artist_mbid) = ?", [mbid]
        ).fetchone()
        assert matches is not None
        if (
            mbid
            and row[1] == "resolved_musicbrainz"
            and float(row[2] or 0) >= 0.9
            and not any(word in warnings for word in ("ambiguous", "conflict", "multiple"))
            and matches[0] == 1
        ):
            eligible[seed.artist_id] = mbid
    return eligible, len(selected) - len(eligible)


def plan_musicbrainz_catalog_enrichment(
    conn: DuckDBPyConnection, config: ScrobbleRecsConfig
) -> MusicBrainzCatalogEnrichmentPlan:
    if not config.apis.musicbrainz.enabled:
        return MusicBrainzCatalogEnrichmentPlan(0, 0, 0)
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
    return MusicBrainzCatalogEnrichmentPlan(len(seeds), skipped, queued)


def run_musicbrainz_catalog_enrichment(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    *,
    client: MusicBrainzCatalogClientProtocol | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> MusicBrainzCatalogEnrichmentStats:
    if not config.apis.musicbrainz.enabled:
        raise RuntimeError("MusicBrainz catalog enrichment is disabled")
    # Synchronous worker entry recovers unfinished attempts; planning never steals work.
    conn.execute(
        "UPDATE enrichment_queue SET status = 'queued', attempts = greatest(attempts - 1, 0) "
        "WHERE source = ? AND endpoint = ? AND status = 'running'",
        [SOURCE, ENDPOINT],
    )
    seeds, _ = _seeds(conn, config)
    plan_musicbrainz_catalog_enrichment(conn, config)
    resolved = client or MusicBrainzClient(config=config.apis.musicbrainz, cache=ApiCache(conn))
    requested = aliases = relationships = created = ambiguous = 0
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
        if progress:
            progress(0, len(rows))
        for completed, (queue_id, artist_id) in enumerate(rows, 1):
            if artist_id not in seeds:
                conn.execute(
                    "UPDATE enrichment_queue SET status = 'skipped' WHERE queue_id = ?", [queue_id]
                )
                if progress:
                    progress(completed, len(rows))
                continue
            conn.execute(
                "UPDATE enrichment_queue SET status = 'running', attempts = attempts + 1, "
                "last_attempt_at = current_timestamp WHERE queue_id = ?",
                [queue_id],
            )
            requested += 1
            try:
                # ApiCache owns transactions: finish HTTP/cache writes before evidence replacement.
                catalog = resolved.lookup_artist_catalog(seeds[artist_id])
                if catalog is None or valid_mbid(catalog.artist.mbid) != seeds[artist_id]:
                    raise ValueError("catalog lookup failed or mismatched source MBID")
            except (RuntimeError, ValueError) as exc:
                conn.execute(
                    "UPDATE enrichment_queue SET status = 'failed', error_message = ? WHERE "
                    "queue_id = ?",
                    [str(exc), queue_id],
                )
                if progress:
                    progress(completed, len(rows))
                continue
            result = _replace_catalog(conn, config, artist_id, catalog, str(queue_id))
            aliases += result[0]
            relationships += result[1]
            created += result[2]
            ambiguous += result[3]
            if progress:
                progress(completed, len(rows))
    finally:
        if client is None:
            assert isinstance(resolved, MusicBrainzClient)
            resolved.close()
    return MusicBrainzCatalogEnrichmentStats(requested, aliases, relationships, created, ambiguous)


def _ensure_artist(conn: DuckDBPyConnection, mbid: str, name: str) -> tuple[int, bool, list[str]]:
    matches = conn.execute(
        "SELECT artist_id, canonical_name, identity_warnings FROM artist "
        "WHERE lower(musicbrainz_artist_mbid) = ? "
        "ORDER BY identity_confidence DESC NULLS LAST, artist_id",
        [mbid],
    ).fetchall()
    if matches:
        artist_id, canonical, stored = matches[0]
        warnings = list(json.loads(stored or "[]"))
        if normalize_key(canonical) != normalize_key(name):
            warnings.append("name_mbid_conflict")
        if len(matches) > 1:
            warnings.append("multiple_local_mbid_matches")
        return int(artist_id), False, sorted(set(warnings))
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
            "external_musicbrainz",
            0.5 if conflict else 0.9,
            json.dumps(warnings),
        ],
    )
    return artist_id, True, warnings


def _replace_catalog(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    source_id: int,
    catalog: MusicBrainzArtistCatalog,
    queue_id: str,
) -> tuple[int, int, int, int]:
    mbid = catalog.artist.mbid
    key = build_cache_key(SOURCE, f"artist/{mbid}", catalog_params())
    cached = conn.execute(
        "SELECT fetched_at, response_json FROM api_cache WHERE cache_key = ? "
        "AND response_status BETWEEN 200 AND 299",
        [key],
    ).fetchone()
    fetched = cached[0] if cached else datetime.now(UTC).replace(tzinfo=None)
    payload = json.loads(cached[1]) if cached else asdict(catalog)
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    aliases = relationships = created = ambiguous = 0
    conn.execute("BEGIN TRANSACTION")
    try:
        aliases = _replace_aliases(conn, source_id, catalog, fetched)
        retained_keys, relationships, created, ambiguous = _write_relationships(
            conn, config, source_id, catalog, fetched, key, payload_hash
        )
        old_keys = conn.execute(
            "SELECT observation_key FROM artist_evidence_observation WHERE source = ? "
            "AND relationship_kind = 'catalog_relationship' AND source_artist_id = ?",
            [SOURCE, source_id],
        ).fetchall()
        for (old_key,) in old_keys:
            if old_key not in retained_keys:
                conn.execute(
                    "DELETE FROM artist_evidence_observation WHERE source = ? "
                    "AND relationship_kind = 'catalog_relationship' AND observation_key = ?",
                    [SOURCE, old_key],
                )
        conn.execute(
            "UPDATE enrichment_queue SET status = 'complete', error_message = NULL WHERE "
            "queue_id = ?",
            [queue_id],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.execute(
            "UPDATE enrichment_queue SET status = 'failed', error_message = 'catalog "
            "persistence failed' "
            "WHERE queue_id = ?",
            [queue_id],
        )
        raise
    return aliases, relationships, created, ambiguous


def _replace_aliases(
    conn: DuckDBPyConnection,
    source_id: int,
    catalog: MusicBrainzArtistCatalog,
    fetched: datetime,
) -> int:
    aliases = 0
    conn.execute("DELETE FROM artist_alias WHERE source = ? AND artist_id = ?", [SOURCE, source_id])
    seen_aliases = set()
    for alias in catalog.aliases:
        normalized, locale = normalize_key(alias.name), alias.locale or ""
        if not normalized or (normalized, locale) in seen_aliases:
            continue
        seen_aliases.add((normalized, locale))
        conn.execute(
            "INSERT INTO artist_alias VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                source_id,
                alias.name,
                normalized,
                locale,
                alias.primary_for_locale,
                alias.begin,
                alias.end,
                alias.type,
                SOURCE,
                fetched,
            ],
        )
        aliases += 1
    return aliases


def _write_relationships(
    conn: DuckDBPyConnection,
    config: ScrobbleRecsConfig,
    source_id: int,
    catalog: MusicBrainzArtistCatalog,
    fetched: datetime,
    key: str,
    payload_hash: str,
) -> tuple[set[str], int, int, int]:
    mbid = catalog.artist.mbid
    retained_keys: set[str] = set()
    relationships = created = ambiguous = 0
    seen_relations = set()
    for relation in catalog.artist_relationships[: config.enrichment.candidate_artist_limit]:
        target_mbid = valid_mbid(relation.target_mbid)
        if not target_mbid or target_mbid == mbid or not relation.target_name.strip():
            continue
        natural = observation_key(
            source_id=source_id,
            **{field: value for field, value in asdict(relation).items() if field != "target_name"},
        )
        if natural in seen_relations:
            continue
        seen_relations.add(natural)
        retained_keys.add(natural)
        target, is_new, warnings = _ensure_artist(conn, target_mbid, relation.target_name)
        identity = conn.execute(
            "SELECT identity_confidence, resolution_status FROM artist WHERE artist_id = ?",
            [target],
        ).fetchone()
        assert identity is not None
        evidence = {
            **asdict(relation),
            "source_mbid": mbid,
            "cache_key": key,
            "payload_hash": payload_hash,
            "identity_warnings": warnings,
            "identity_status": identity[1],
            "supported_relationship": relation.relationship_type
            in config.recommendation.catalog_evidence.allowed_relationship_types,
        }
        _store_relationship(
            conn,
            EvidenceObservation(
                SOURCE,
                "catalog",
                "catalog_relationship",
                source_id,
                target,
                None,
                None,
                None,
                fetched,
                float(identity[0] or 0),
                evidence,
            ),
            natural,
            key,
            payload_hash,
            identity[1],
        )
        created += int(is_new)
        ambiguous += int(bool(warnings))
        relationships += 1
    return retained_keys, relationships, created, ambiguous


def _store_relationship(
    conn: DuckDBPyConnection,
    observation: EvidenceObservation,
    natural: str,
    key: str,
    payload_hash: str,
    status: str | None,
) -> None:
    # Update in place: deleting/reinserting durable UUIDs with DuckDB's separate
    # natural-key index can conflict after reopening the database.
    existing = conn.execute(
        "SELECT observation_id FROM artist_evidence_observation WHERE source = ? "
        "AND relationship_kind = 'catalog_relationship' AND observation_key = ?",
        [SOURCE, natural],
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE artist_evidence_observation SET target_artist_id = ?, raw_score = NULL, "
            "source_rank = NULL, fetched_at = ?, cache_key = ?, "
            "payload_hash = ?, identity_status = ?, identity_confidence = ?, evidence = ?::JSON "
            "WHERE observation_id = ?",
            [
                observation.target_artist_id,
                observation.fetched_at,
                key,
                payload_hash,
                status,
                observation.identity_confidence,
                json.dumps(observation.evidence, sort_keys=True),
                existing[0],
            ],
        )
    else:
        persist_evidence_observation(
            conn,
            observation,
            observation_key=natural,
            cache_key=key,
            payload_hash=payload_hash,
            identity_status=status,
        )
