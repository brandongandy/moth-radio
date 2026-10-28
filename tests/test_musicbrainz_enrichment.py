"""Offline catalog identity, persistence and queue contracts."""

import json

import duckdb
import pytest

from scrobble_recs.config import load_config
from scrobble_recs.db import apply_migrations, initialize_database

SEED = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
TARGET = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _recording_membership(
    conn, target=6, mbid=TARGET, *, source=1, records=None, day=1, key="radio"
):
    from dataclasses import replace
    from datetime import datetime

    from test_representative import radio

    from scrobble_recs.models.evidence import persist_evidence_observation

    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, musicbrainz_artist_mbid, "
        "resolution_status, identity_confidence, identity_warnings) SELECT ?, ?, ?, ?, "
        "'external_listenbrainz', .9, '[]' WHERE NOT EXISTS "
        "(SELECT 1 FROM artist WHERE artist_id = ?)",
        [target, f"Candidate {target}", f"candidate{target}", mbid, target],
    )
    source_mbid = conn.execute(
        "SELECT musicbrainz_artist_mbid FROM artist WHERE artist_id = ?", [source]
    ).fetchone()[0]
    row = replace(
        radio(
            artist_mbid=mbid,
            seed_artist_mbid=source_mbid,
            recordings=records,
            fetched=datetime(2026, 1, day),
        ),
        source_artist_id=source,
        target_artist_id=target,
    )
    persist_evidence_observation(conn, row, observation_key=key)


def test_recording_acquisition_uses_persisted_candidates_global_caps_and_dedup(staged):
    from test_representative import REC, REC2, metadata

    conn, config = staged
    config.enrichment.candidate_artist_limit = config.enrichment.queue_batch_size = 2
    config.apis.listenbrainz.max_recordings_per_artist = 2
    records = [
        {"recording_mbid": REC, "total_listen_count": 100},
        {"recording_mbid": REC2, "total_listen_count": 50},
        {"recording_mbid": "10000000-0000-4000-8000-000000000003", "total_listen_count": 1},
    ]
    _recording_membership(conn, records=records)
    _recording_membership(conn, target=7, mbid=OTHER, records=records, key="other")
    _recording_membership(conn, key="old", records=records)
    _recording_membership(
        conn, target=8, mbid="88888888-8888-4888-8888-888888888888", key="excluded"
    )

    class RecordingClient:
        def lookup_recording(self, mbid):
            assert mbid in (REC, REC2), "lookup escaped selected candidate/recording limits"
            return metadata(mbid)

    before = conn.execute("SELECT * FROM artist_evidence_observation").fetchall()
    stats = module().run_musicbrainz_recording_enrichment(conn, config, client=RecordingClient())
    assert (
        stats.recordings_requested,
        stats.recordings_successful,
        stats.recordings_missing_or_failed,
    ) == (2, 2, 0)
    assert conn.execute("SELECT * FROM artist_evidence_observation").fetchall() == before
    assert conn.execute("SELECT count(*) FROM enrichment_queue").fetchone()[0] == 0


def test_recording_acquisition_newest_candidate_and_individual_failure(staged):
    from test_representative import REC, REC2, metadata

    conn, config = staged
    config.enrichment.queue_batch_size = 1
    _recording_membership(conn)
    _recording_membership(
        conn,
        target=7,
        mbid=OTHER,
        day=2,
        key="newest",
        records=[
            {"recording_mbid": REC2, "total_listen_count": 10},
            {"recording_mbid": REC, "total_listen_count": 1},
        ],
    )

    class Partial:
        def lookup_recording(self, mbid):
            if mbid == REC2:
                raise RuntimeError("provider unavailable")
            return metadata(mbid)

    stats = module().run_musicbrainz_recording_enrichment(conn, config, client=Partial())
    assert (
        stats.recordings_requested,
        stats.recordings_successful,
        stats.recordings_missing_or_failed,
    ) == (2, 1, 1)
    config.apis.musicbrainz.enabled = False
    assert module().run_musicbrainz_recording_enrichment(conn, config).recordings_requested == 0


def test_cli_musicbrainz_acquires_recordings_after_handled_catalog_failure(staged, monkeypatch):
    from test_representative import REC, metadata

    from scrobble_recs import cli

    conn, config = staged
    _recording_membership(conn)
    acquired = []

    class SharedClient:
        def __init__(self, **kwargs):
            pass

        def lookup_artist_catalog(self, mbid):
            return None

        def lookup_recording(self, mbid):
            acquired.append(mbid)
            return metadata(mbid)

        def close(self):
            pass

    def forbidden(*args, **kwargs):
        raise AssertionError("MusicBrainz enrichment requested ListenBrainz")

    monkeypatch.setattr(cli, "MusicBrainzClient", SharedClient)
    monkeypatch.setattr("scrobble_recs.apis.listenbrainz.ListenBrainzClient", forbidden)
    cli._run_musicbrainz_enrichment(conn, config)
    assert acquired == [REC]
    assert (
        conn.execute("SELECT status FROM enrichment_queue WHERE source = 'musicbrainz'").fetchone()[
            0
        ]
        == "failed"
    )
    assert conn.execute("SELECT count(*) FROM artist_evidence_observation").fetchone()[0] == 1


def test_recording_acquisition_interruption_resumes_from_committed_fresh_cache(staged):
    from datetime import datetime
    from pathlib import Path

    import httpx
    from test_representative import REC, REC2

    from scrobble_recs.apis.cache import ApiCache
    from scrobble_recs.apis.musicbrainz import MusicBrainzClient
    from scrobble_recs.apis.rate_limit import RateLimiter

    conn, config = staged
    _recording_membership(
        conn,
        records=[
            {"recording_mbid": REC, "total_listen_count": 100},
            {"recording_mbid": REC2, "total_listen_count": 50},
        ],
    )
    calls = []
    interrupted = False
    raw = json.loads(Path("tests/fixtures/musicbrainz_recording.json").read_text())

    def handler(request):
        nonlocal interrupted
        mbid = request.url.path.rsplit("/", 1)[1]
        calls.append(mbid)
        if mbid == REC2 and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return httpx.Response(200, json=dict(raw, id=mbid))

    before = conn.execute("SELECT * FROM artist_evidence_observation").fetchall()
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = MusicBrainzClient(
            config=config.apis.musicbrainz,
            cache=ApiCache(conn),
            http_client=http,
            rate_limiter=RateLimiter(999, sleep=lambda _: None),
            now=lambda: datetime(2026, 1, 2),
        )
        with pytest.raises(KeyboardInterrupt):
            module().run_musicbrainz_recording_enrichment(conn, config, client=client)
        assert conn.execute("SELECT count(*) FROM api_cache").fetchone()[0] == 1
        stats = module().run_musicbrainz_recording_enrichment(conn, config, client=client)
        assert stats.recordings_successful == 2
        module().run_musicbrainz_recording_enrichment(conn, config, client=client)
    assert calls == [REC, REC2, REC2]
    assert conn.execute("SELECT * FROM artist_evidence_observation").fetchall() == before


def test_recording_acquisition_caps_candidates_globally_across_seeds(staged):
    from test_representative import REC, REC2, metadata

    conn, config = staged
    conn.execute("DELETE FROM artist_listening_fact WHERE artist_id != 1")
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, musicbrainz_artist_mbid, "
        "resolution_status, identity_confidence, identity_warnings) VALUES "
        "(10, 'Seed Ten', 'seed ten', '99999999-9999-4999-8999-999999999999', "
        "'resolved_musicbrainz', .95, '[]')"
    )
    conn.execute(
        "INSERT INTO artist_listening_fact (artist_id, play_count, recent_weight, lifetime_weight) "
        "VALUES (10, 10, 1, 1)"
    )
    config.enrichment.seed_artist_limit = 2
    config.enrichment.candidate_artist_limit = 1
    _recording_membership(conn, records=[{"recording_mbid": REC, "total_listen_count": 100}])
    _recording_membership(
        conn,
        source=10,
        target=7,
        mbid=OTHER,
        key="new",
        day=2,
        records=[{"recording_mbid": REC2, "total_listen_count": 1}],
    )

    class OnlyNewest:
        def lookup_recording(self, mbid):
            assert mbid == REC2
            return metadata(mbid)

    stats = module().run_musicbrainz_recording_enrichment(conn, config, client=OnlyNewest())
    assert stats.recordings_requested == 1


def test_recording_acquisition_preserves_seed_support_before_provenance_dedup(staged):
    from test_representative import REC, REC2, metadata

    conn, config = staged
    conn.execute("DELETE FROM artist_listening_fact WHERE artist_id != 1")
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, musicbrainz_artist_mbid, "
        "resolution_status, identity_confidence, identity_warnings) VALUES "
        "(10, 'Seed Ten', 'seed ten', 'ffffffff-ffff-4fff-8fff-ffffffffffff', "
        "'resolved_musicbrainz', .95, '[]')"
    )
    conn.execute(
        "INSERT INTO artist_listening_fact (artist_id, play_count, recent_weight, lifetime_weight) "
        "VALUES (10, 10, 1, 1)"
    )
    config.enrichment.seed_artist_limit = 2
    config.enrichment.candidate_artist_limit = config.enrichment.queue_batch_size = 2
    config.apis.listenbrainz.max_similar_artists = 1
    _recording_membership(conn, source=1, key="first-support")
    _recording_membership(conn, source=10, key="second-support")
    _recording_membership(
        conn,
        source=1,
        target=7,
        mbid=OTHER,
        key="new",
        day=2,
        records=[{"recording_mbid": REC2, "total_listen_count": 1}],
    )
    acquired = []

    class RecordingClient:
        def lookup_recording(self, mbid):
            acquired.append(mbid)
            return metadata(mbid)

    stats = module().run_musicbrainz_recording_enrichment(conn, config, client=RecordingClient())
    assert stats.recordings_requested == 2
    assert acquired == [REC2, REC]


@pytest.fixture
def staged(tmp_path):
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
                "apis": {"musicbrainz": {"enabled": True}},
                "recommendation": {"min_seed_scrobbles": 1},
            }
        )
    )
    config = load_config(path)
    conn = initialize_database(config)
    for identifier, status, confidence, mbid in [
        (1, "resolved_musicbrainz", 0.95, SEED),
        (2, "provisional_mbid", 0.99, "22222222-2222-4222-8222-222222222222"),
        (3, "ambiguous_musicbrainz", 0.99, "33333333-3333-4333-8333-333333333333"),
        (4, "resolved_musicbrainz", 0.6, "44444444-4444-4444-8444-444444444444"),
        (5, "resolved_musicbrainz", 0.99, "invalid"),
    ]:
        conn.execute(
            "INSERT INTO artist (artist_id, canonical_name, artist_key, "
            "musicbrainz_artist_mbid, resolution_status, identity_confidence) VALUES (?, ?, ?, "
            "?, ?, ?)",
            [identifier, f"Seed {identifier}", f"seed {identifier}", mbid, status, confidence],
        )
        conn.execute(
            "INSERT INTO artist_listening_fact (artist_id, play_count, recent_weight, "
            "lifetime_weight) VALUES (?, 10, 1, 1)",
            [identifier],
        )
    yield conn, config
    conn.close()


def test_alias_migration_replay_shared_alias_and_null_locale_unique(staged):
    conn, _ = staged
    for artist in (1, 2):
        conn.execute(
            "INSERT INTO artist_alias (artist_id, alias, normalized_alias, source, fetched_at) "
            "VALUES (?, 'Shared', 'shared', 'musicbrainz', '2025-01-01')",
            [artist],
        )
    apply_migrations(conn)
    assert conn.execute(
        "SELECT artist_id, locale FROM artist_alias ORDER BY artist_id"
    ).fetchall() == [(1, ""), (2, "")]
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO artist_alias (artist_id, alias, normalized_alias, source, fetched_at) "
            "VALUES (1, 'SHARED', 'shared', 'musicbrainz', '2025-01-01')"
        )
    assert conn.execute("SELECT count(*) FROM artist").fetchone()[0] == 5


def module():
    from importlib import import_module, util

    assert util.find_spec("scrobble_recs.enrichment.musicbrainz") is not None
    return import_module("scrobble_recs.enrichment.musicbrainz")


def catalog(mbid=SEED, *, name="Seed 1", aliases=True, relations=None):
    from scrobble_recs.apis.musicbrainz import (
        MusicBrainzArtistAlias,
        MusicBrainzArtistCatalog,
        MusicBrainzArtistRelationship,
        MusicBrainzArtistResult,
    )

    return MusicBrainzArtistCatalog(
        MusicBrainzArtistResult(mbid, name, "", "", "", "", ""),
        (
            MusicBrainzArtistAlias("Shared"),
            MusicBrainzArtistAlias("SHARED"),
            MusicBrainzArtistAlias("Shared", "en"),
        )
        if aliases
        else (),
        tuple(relations)
        if relations is not None
        else (
            MusicBrainzArtistRelationship(
                TARGET, "Candidate", "member of band", "forward", "1990", None, False
            ),
        ),
    )


class FakeClient:
    def __init__(self, result=None):
        self.result = result or catalog()
        self.seeds = []

    def lookup_artist_catalog(self, mbid):
        from dataclasses import replace

        self.seeds.append(mbid)
        return replace(self.result, artist=replace(self.result.artist, mbid=mbid))


def test_only_safe_resolved_sources_with_unranked_catalog_provenance(staged):
    conn, config = staged
    fake = FakeClient()
    stats = module().run_musicbrainz_catalog_enrichment(conn, config, client=fake)
    assert fake.seeds == [SEED]
    assert (
        stats.artists_requested,
        stats.aliases_written,
        stats.relationships_written,
        stats.related_artists_created,
        stats.ambiguous_relationships,
    ) == (1, 2, 1, 1, 0)
    row = conn.execute(
        "SELECT family, relationship_kind, raw_score, source_rank, identity_status, cache_key, "
        "payload_hash, evidence FROM artist_evidence_observation"
    ).fetchone()
    assert row[:5] == ("catalog", "catalog_relationship", None, None, "external_musicbrainz")
    assert row[5].startswith("musicbrainz:artist/") and len(row[6]) == 64
    evidence = json.loads(row[7])
    assert evidence["relationship_type"] == "member of band"
    assert evidence["source_mbid"] == SEED and evidence["target_mbid"] == TARGET
    assert evidence["direction"] == "forward" and evidence["begin"] == "1990"
    assert evidence["supported_relationship"] is True
    assert evidence["identity_warnings"] == []
    assert conn.execute("SELECT status FROM enrichment_queue").fetchone()[0] == "complete"


def test_exact_mbid_reuse_names_do_not_merge_and_ambiguity_is_inspectable(staged):
    conn, config = staged
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, musicbrainz_artist_mbid, "
        "identity_confidence, resolution_status) VALUES (10, 'Different Name', 'different "
        "name', ?, .6, 'ambiguous_musicbrainz')",
        [TARGET],
    )
    stats = module().run_musicbrainz_catalog_enrichment(conn, config, client=FakeClient())
    assert stats.related_artists_created == 0 and stats.ambiguous_relationships == 1
    row = conn.execute(
        "SELECT target_artist_id, identity_confidence, identity_status, evidence FROM "
        "artist_evidence_observation"
    ).fetchone()
    assert row[:3] == (10, 0.6, "ambiguous_musicbrainz")
    assert "name_mbid_conflict" in json.loads(row[3])["identity_warnings"]
    from dataclasses import replace

    relation = replace(
        catalog().artist_relationships[0], target_mbid=OTHER, target_name="Different Name"
    )
    stats = module().run_musicbrainz_catalog_enrichment(
        conn, config, client=FakeClient(catalog(relations=[relation]))
    )
    assert stats.related_artists_created == 1 and stats.ambiguous_relationships == 1
    assert (
        conn.execute(
            "SELECT count(*) FROM artist WHERE canonical_name = 'Different Name'"
        ).fetchone()[0]
        == 2
    )
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, musicbrainz_artist_mbid, "
        "identity_confidence) VALUES (20, 'Duplicate', 'duplicate', ?, .99)",
        [TARGET],
    )
    stats = module().run_musicbrainz_catalog_enrichment(conn, config, client=FakeClient())
    row = conn.execute(
        "SELECT target_artist_id, evidence FROM artist_evidence_observation"
    ).fetchone()
    assert row[0] == 20 and "multiple_local_mbid_matches" in json.loads(row[1])["identity_warnings"]
    assert stats.ambiguous_relationships == 1
    assert (
        conn.execute(
            "SELECT count(*) FROM artist WHERE musicbrainz_artist_mbid = ?", [TARGET]
        ).fetchone()[0]
        == 2
    )


def test_refresh_replaces_scoped_aliases_and_relations_and_shared_aliases(staged):
    conn, config = staged
    conn.execute("UPDATE artist SET resolution_status = 'resolved_musicbrainz' WHERE artist_id = 2")
    fake = FakeClient()
    module().run_musicbrainz_catalog_enrichment(conn, config, client=fake)
    assert len(fake.seeds) == 2  # related targets never become acquisition seeds
    assert conn.execute("SELECT count(*) FROM artist_alias").fetchone()[0] == 4
    conn.execute(
        "INSERT INTO artist_alias (artist_id, alias, normalized_alias, source, fetched_at) "
        "VALUES (1, 'Other', 'other', 'other', '2025-01-01')"
    )
    fake.result = catalog(aliases=False, relations=[])
    stats = module().run_musicbrainz_catalog_enrichment(conn, config, client=fake)
    assert stats.relationships_written == stats.aliases_written == 0
    assert conn.execute("SELECT count(*) FROM artist_evidence_observation").fetchone()[0] == 0
    assert conn.execute("SELECT source FROM artist_alias").fetchall() == [("other",)]


def test_failed_refresh_retains_prior_catalog_and_requires_explicit_retry(staged):
    from scrobble_recs.enrichment.queue import retry_failed_enrichment

    conn, config = staged
    module().run_musicbrainz_catalog_enrichment(conn, config, client=FakeClient())
    before = conn.execute("SELECT * FROM artist_evidence_observation").fetchall()

    class Offline:
        def lookup_artist_catalog(self, mbid):
            return None

    assert (
        module()
        .run_musicbrainz_catalog_enrichment(conn, config, client=Offline())
        .relationships_written
        == 0
    )
    assert conn.execute("SELECT * FROM artist_evidence_observation").fetchall() == before
    assert conn.execute("SELECT count(*) FROM artist_alias").fetchone()[0] == 2
    assert conn.execute("SELECT status FROM enrichment_queue").fetchone()[0] == "failed"
    assert (
        module()
        .run_musicbrainz_catalog_enrichment(conn, config, client=FakeClient())
        .artists_requested
        == 0
    )
    assert retry_failed_enrichment(conn, source="musicbrainz") == 1
    assert (
        module()
        .run_musicbrainz_catalog_enrichment(conn, config, client=FakeClient())
        .artists_requested
        == 1
    )


def test_catalog_persistence_rollback_restores_previous_aliases_and_evidence(staged):
    conn, config = staged
    module().run_musicbrainz_catalog_enrichment(conn, config, client=FakeClient())
    before = conn.execute("SELECT * FROM artist_evidence_observation").fetchall()
    aliases = conn.execute("SELECT * FROM artist_alias").fetchall()
    # Exercise a real database constraint failure after replacement has begun.
    conn.execute(
        "CREATE UNIQUE INDEX catalog_one_target ON artist_evidence_observation "
        "(source_artist_id, target_artist_id)"
    )
    from dataclasses import replace

    first = catalog().artist_relationships[0]
    fake = FakeClient(
        catalog(aliases=False, relations=[first, replace(first, direction="backward")])
    )
    with pytest.raises(duckdb.ConstraintException):
        module().run_musicbrainz_catalog_enrichment(conn, config, client=fake)
    assert conn.execute("SELECT * FROM artist_evidence_observation").fetchall() == before
    assert conn.execute("SELECT * FROM artist_alias").fetchall() == aliases
    assert conn.execute("SELECT status FROM enrichment_queue").fetchone()[0] == "failed"


def test_catalog_acquisition_bounds_and_disabled_queue_immutability(staged):
    conn, config = staged
    from dataclasses import replace

    relation = catalog().artist_relationships[0]
    config.enrichment.candidate_artist_limit = 1
    stats = module().run_musicbrainz_catalog_enrichment(
        conn,
        config,
        client=FakeClient(catalog(relations=[relation, replace(relation, target_mbid=OTHER)])),
    )
    assert stats.relationships_written == 1
    config.apis.musicbrainz.enabled = False
    before = conn.execute("SELECT * FROM enrichment_queue").fetchall()
    assert module().plan_musicbrainz_catalog_enrichment(conn, config).total_queued == 0
    with pytest.raises(RuntimeError, match="disabled"):
        module().run_musicbrainz_catalog_enrichment(conn, config, client=FakeClient())
    assert conn.execute("SELECT * FROM enrichment_queue").fetchall() == before


def test_bounded_batches_drain_pending_before_refresh(staged):
    conn, config = staged
    config.enrichment.queue_batch_size = 1
    conn.execute("UPDATE artist SET resolution_status = 'resolved_musicbrainz' WHERE artist_id = 2")
    fake = FakeClient()
    for _ in range(2):
        assert (
            module().run_musicbrainz_catalog_enrichment(conn, config, client=fake).artists_requested
            == 1
        )
    assert fake.seeds == [SEED, "22222222-2222-4222-8222-222222222222"]
    for _ in range(2):
        module().run_musicbrainz_catalog_enrichment(conn, config, client=fake)
    assert fake.seeds == [SEED, "22222222-2222-4222-8222-222222222222"] * 2


def test_skipped_recovers_and_failed_requires_retry(staged):
    conn, config = staged
    conn.execute("UPDATE artist SET resolution_status = 'resolved_musicbrainz' WHERE artist_id = 2")
    assert module().plan_musicbrainz_catalog_enrichment(conn, config).total_queued == 2
    conn.execute(
        "UPDATE artist SET identity_warnings = '[\"multiple_matches\"]' WHERE artist_id = 1"
    )

    class Offline:
        def lookup_artist_catalog(self, mbid):
            raise RuntimeError("offline")

    module().run_musicbrainz_catalog_enrichment(conn, config, client=Offline())
    assert conn.execute(
        "SELECT entity_id, status FROM enrichment_queue ORDER BY entity_id"
    ).fetchall() == [(1, "skipped"), (2, "failed")]
    conn.execute("UPDATE artist SET identity_warnings = '[]' WHERE artist_id = 1")
    assert module().plan_musicbrainz_catalog_enrichment(conn, config).total_queued == 1
    fake = FakeClient()
    module().run_musicbrainz_catalog_enrichment(conn, config, client=fake)
    assert fake.seeds == [SEED]
    assert conn.execute(
        "SELECT entity_id, status, attempts, error_message FROM enrichment_queue ORDER BY entity_id"
    ).fetchall() == [(1, "complete", 1, None), (2, "failed", 1, "offline")]


def test_interrupted_running_resumes_before_refresh(staged):
    conn, config = staged
    config.enrichment.queue_batch_size = config.enrichment.max_queue_attempts = 1
    conn.execute("UPDATE artist SET resolution_status = 'resolved_musicbrainz' WHERE artist_id = 2")
    module().run_musicbrainz_catalog_enrichment(conn, config, client=FakeClient())

    class Interrupted:
        def lookup_artist_catalog(self, mbid):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        module().run_musicbrainz_catalog_enrichment(conn, config, client=Interrupted())
    assert conn.execute(
        "SELECT entity_id, status FROM enrichment_queue ORDER BY entity_id"
    ).fetchall() == [(1, "complete"), (2, "running")]
    fake = FakeClient()
    module().run_musicbrainz_catalog_enrichment(conn, config, client=fake)
    assert fake.seeds == ["22222222-2222-4222-8222-222222222222"]
    assert conn.execute(
        "SELECT status, attempts FROM enrichment_queue ORDER BY entity_id"
    ).fetchall() == [("complete", 1), ("complete", 1)]


def test_planning_during_active_catalog_request_preserves_running_barrier(staged):
    conn, config = staged
    config.enrichment.queue_batch_size = 1
    conn.execute("UPDATE artist SET resolution_status = 'resolved_musicbrainz' WHERE artist_id = 2")
    module().run_musicbrainz_catalog_enrichment(conn, config, client=FakeClient())

    class Inspecting(FakeClient):
        def lookup_artist_catalog(self, mbid):
            assert module().plan_musicbrainz_catalog_enrichment(conn, config).total_queued == 0
            assert conn.execute(
                "SELECT entity_id, status FROM enrichment_queue ORDER BY entity_id"
            ).fetchall() == [(1, "complete"), (2, "running")]
            return super().lookup_artist_catalog(mbid)

    module().run_musicbrainz_catalog_enrichment(conn, config, client=Inspecting())
    assert (
        conn.execute("SELECT count(*) FROM enrichment_queue WHERE status = 'complete'").fetchone()[
            0
        ]
        == 2
    )


def test_catalog_policy_defaults_and_generated_configuration(staged):
    from scrobble_recs.config import default_config_document

    _, config = staged
    assert config.recommendation.catalog_evidence.enabled is False
    assert config.recommendation.catalog_evidence.allowed_relationship_types == [
        "member of band",
        "collaboration",
        "supporting musician",
        "instrumental supporting musician",
    ]
    assert (
        default_config_document()["recommendation"]["catalog_evidence"]
        == config.recommendation.catalog_evidence.model_dump()
    )


def test_refresh_updates_existing_target_identity_and_stable_relationship_key(staged):
    from dataclasses import replace

    conn, config = staged
    first = catalog().artist_relationships[0]
    module().run_musicbrainz_catalog_enrichment(conn, config, client=FakeClient())
    identifier = conn.execute("SELECT observation_id FROM artist_evidence_observation").fetchone()[
        0
    ]
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, musicbrainz_artist_mbid, "
        "identity_confidence, resolution_status) VALUES (20, 'Preferred', 'preferred', ?, .99, "
        "'resolved_musicbrainz')",
        [TARGET],
    )
    changed = replace(first, target_name="Preferred")
    module().run_musicbrainz_catalog_enrichment(
        conn, config, client=FakeClient(catalog(relations=[changed]))
    )
    row = conn.execute(
        "SELECT observation_id, target_artist_id, identity_confidence, identity_status, "
        "evidence FROM artist_evidence_observation"
    ).fetchone()
    assert row[:4] == (identifier, 20, 0.99, "resolved_musicbrainz")
    assert json.loads(row[4])["target_name"] == "Preferred"
    assert "multiple_local_mbid_matches" in json.loads(row[4])["identity_warnings"]


def test_source_identity_ambiguity_is_ineligible_and_other_provider_queue_untouched(staged):
    conn, config = staged
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, musicbrainz_artist_mbid, "
        "identity_confidence) VALUES (10, 'Other', 'other', ?, .1)",
        [SEED],
    )
    conn.execute(
        "INSERT INTO enrichment_queue (queue_id, source, endpoint, entity_type, entity_id, "
        "status, attempts) VALUES ('00000000-0000-0000-0000-000000000001', 'listenbrainz', "
        "'artist-radio', 'artist', 1, 'running', 1)"
    )
    fake = FakeClient()
    assert (
        module().run_musicbrainz_catalog_enrichment(conn, config, client=fake).artists_requested
        == 0
    )
    assert fake.seeds == []
    assert conn.execute(
        "SELECT status, attempts FROM enrichment_queue WHERE source = 'listenbrainz'"
    ).fetchone() == ("running", 1)


def test_catalog_http_cache_provenance_survives_stale_fallback(staged):
    import hashlib
    from datetime import datetime
    from pathlib import Path

    import httpx

    from scrobble_recs.apis.cache import ApiCache
    from scrobble_recs.apis.musicbrainz import MusicBrainzClient
    from scrobble_recs.apis.rate_limit import RateLimiter

    conn, config = staged
    payload = json.loads(Path("tests/fixtures/musicbrainz_artist_relationships.json").read_text())
    fetched = datetime(2020, 1, 1)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload) if len(calls) == 1 else httpx.Response(503)

    clock = [fetched]
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = MusicBrainzClient(
            config=config.apis.musicbrainz,
            cache=ApiCache(conn),
            http_client=http,
            rate_limiter=RateLimiter(999, sleep=lambda _: None),
            now=lambda: clock[0],
        )
        module().run_musicbrainz_catalog_enrichment(conn, config, client=client)
        expected = conn.execute(
            "SELECT * FROM artist_evidence_observation ORDER BY observation_id"
        ).fetchall()
        clock[0] = datetime(2040, 1, 1)
        module().run_musicbrainz_catalog_enrichment(conn, config, client=client)
        assert (
            conn.execute(
                "SELECT * FROM artist_evidence_observation ORDER BY observation_id"
            ).fetchall()
            == expected
        )
        assert len(calls) == 2
    rows = conn.execute(
        "SELECT fetched_at, payload_hash, evidence FROM artist_evidence_observation"
    ).fetchall()
    assert len(rows) == 3
    assert all(
        row[0] == fetched
        and row[1] == hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        for row in rows
    )
    teacher = next(
        json.loads(row[2]) for row in rows if json.loads(row[2])["relationship_type"] == "teacher"
    )
    assert teacher["supported_relationship"] is False


def test_catalog_progress_reports_bounded_work_including_failed_lookup(staged):
    conn, config = staged
    conn.execute("UPDATE artist SET resolution_status = 'resolved_musicbrainz' WHERE artist_id = 2")
    config.enrichment.queue_batch_size = 1
    progress = []

    class Offline:
        def lookup_artist_catalog(self, mbid):
            return None

    module().run_musicbrainz_catalog_enrichment(
        conn,
        config,
        client=Offline(),
        progress=lambda completed, total: progress.append((completed, total)),
    )
    assert progress == [(0, 1), (1, 1)]
