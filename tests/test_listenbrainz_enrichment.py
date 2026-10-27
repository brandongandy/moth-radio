import json
from datetime import datetime
from pathlib import Path

import pytest

from scrobble_recs.apis.listenbrainz import (
    ListenBrainzArtistRadioGroup,
    ListenBrainzArtistRadioRecording,
)
from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.enrichment.listenbrainz import (
    plan_listenbrainz_enrichment,
    run_listenbrainz_enrichment,
)
from scrobble_recs.enrichment.queue import enrichment_queue_status, retry_failed_enrichment

SEED = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
TARGET = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
RECORDING = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


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
                "apis": {"listenbrainz": {"enabled": True}},
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
            "musicbrainz_artist_mbid, resolution_status, identity_confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [identifier, f"Seed {identifier}", f"seed {identifier}", mbid, status, confidence],
        )
        conn.execute(
            "INSERT INTO artist_listening_fact (artist_id, play_count, "
            "recent_weight, lifetime_weight) VALUES (?, 10, 1, 1)",
            [identifier],
        )
    yield conn, config
    conn.close()


def group(mbid=TARGET, name="Candidate", count=42, position=0):
    return ListenBrainzArtistRadioGroup(
        mbid, name, (ListenBrainzArtistRadioRecording(RECORDING, mbid, name, count),), position
    )


class FakeClient:
    def __init__(self, groups=None):
        self.groups = tuple(groups if groups is not None else [group()])
        self.seeds = []

    def get_artist_radio(self, seed_artist_mbid):
        self.seeds.append(seed_artist_mbid)
        return self.groups


def test_only_resolved_high_confidence_seeds_and_unranked_provenance(staged):
    conn, config = staged
    client = FakeClient()
    stats = run_listenbrainz_enrichment(conn, config, client=client)
    assert client.seeds == [SEED]
    assert (stats.eligible_seeds, stats.skipped_seeds, stats.requests) == (1, 4, 1)
    assert (stats.artists_created, stats.observations_written, stats.recordings_written) == (
        1,
        1,
        1,
    )
    row = conn.execute(
        "SELECT family, relationship_kind, raw_score, source_rank, "
        "provider_mode, cache_key, fetched_at, payload_hash, evidence "
        "FROM artist_evidence_observation"
    ).fetchone()
    assert row[:5] == ("collaborative", "artist_radio_membership", None, None, "easy")
    assert row[5].startswith("listenbrainz:artist-radio:")
    assert isinstance(row[6], datetime)
    assert len(row[7]) == 64
    evidence = json.loads(row[8])
    assert evidence["response_position"] == 0
    assert evidence["pop_begin"] == 0 and evidence["pop_end"] == 100
    assert evidence["recordings"] == [{"recording_mbid": RECORDING, "total_listen_count": 42}]
    assert evidence["total_listen_count"] == 42
    assert evidence["cache_key"] == row[5]
    assert evidence["payload_hash"] == row[7]
    assert evidence["artist_mbid"] == TARGET
    assert evidence["artist_name"] == "Candidate"
    assert enrichment_queue_status(conn, source="listenbrainz").complete == 1


def test_identity_matches_mbid_before_names_and_preserves_conflicts(staged):
    conn, config = staged
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, "
        "musicbrainz_artist_mbid, identity_confidence) VALUES (10, "
        "'Different Name', 'different name', ?, .95)",
        [TARGET],
    )
    client = FakeClient()
    first = run_listenbrainz_enrichment(conn, config, client=client)
    assert first.artists_created == 0
    assert (
        conn.execute("SELECT target_artist_id FROM artist_evidence_observation").fetchone()[0] == 10
    )
    other = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    client.groups = (group(other, "Different Name"), group("invalid"), group(""))
    stats = run_listenbrainz_enrichment(conn, config, client=client)
    assert stats.artists_created == 1
    assert stats.observations_written == 1
    rows = conn.execute(
        "SELECT artist_id, musicbrainz_artist_mbid, identity_warnings "
        "FROM artist WHERE canonical_name = 'Different Name' ORDER BY "
        "artist_id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] == TARGET and rows[1][1] == other
    assert "name_mbid_conflict" in json.loads(rows[1][2])


def test_rerun_refreshes_and_removes_obsolete_memberships(staged):
    conn, config = staged
    client = FakeClient()
    run_listenbrainz_enrichment(conn, config, client=client)
    client.groups = (group(count=80),)
    run_listenbrainz_enrichment(conn, config, client=client)
    assert conn.execute("SELECT count(*) FROM artist_evidence_observation").fetchone()[0] == 1
    assert (
        json.loads(conn.execute("SELECT evidence FROM artist_evidence_observation").fetchone()[0])[
            "total_listen_count"
        ]
        == 80
    )
    client.groups = ()
    run_listenbrainz_enrichment(conn, config, client=client)
    assert conn.execute("SELECT count(*) FROM artist_evidence_observation").fetchone()[0] == 0


def test_queue_plan_failure_and_explicit_retry(staged):
    conn, config = staged
    plan = plan_listenbrainz_enrichment(conn, config)
    assert plan.total_queued == 1
    assert plan_listenbrainz_enrichment(conn, config).total_queued == 0

    class Offline:
        def get_artist_radio(self, seed_artist_mbid):
            raise RuntimeError("offline")

    stats = run_listenbrainz_enrichment(conn, config, client=Offline())
    assert stats.observations_written == 0
    status = enrichment_queue_status(conn, source="listenbrainz")
    assert status.failed == 1 and status.running == 0
    assert retry_failed_enrichment(conn, source="listenbrainz") == 1
    assert run_listenbrainz_enrichment(conn, config, client=FakeClient()).observations_written == 1


def test_limits_and_disabled_never_requests(staged):
    conn, config = staged
    client = FakeClient([group(), group("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")])
    config.apis.listenbrainz.max_similar_artists = 1
    assert run_listenbrainz_enrichment(conn, config, client=client).observations_written == 1
    config.apis.listenbrainz.enabled = False
    client.seeds.clear()
    with pytest.raises(RuntimeError, match="disabled"):
        run_listenbrainz_enrichment(conn, config, client=client)
    assert client.seeds == []


def test_cached_provider_provenance_survives_enrichment_and_refresh(staged):
    import hashlib

    import httpx

    from scrobble_recs.apis.cache import ApiCache
    from scrobble_recs.apis.listenbrainz import ListenBrainzClient
    from scrobble_recs.apis.rate_limit import RateLimiter

    conn, config = staged
    content = Path("tests/fixtures/listenbrainz_artist_radio.json").read_bytes()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=content)

    fetched = datetime(2020, 1, 1)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = ListenBrainzClient(
            config=config.apis.listenbrainz,
            cache=ApiCache(conn),
            http_client=http,
            now=lambda: fetched,
            rate_limiter=RateLimiter(1000, sleep=lambda _: None),
        )
        run_listenbrainz_enrichment(conn, config, client=client)
        expected = conn.execute(
            "SELECT target_artist_id, cache_key, fetched_at, payload_hash, "
            "evidence FROM artist_evidence_observation ORDER BY "
            "target_artist_id"
        ).fetchall()
        run_listenbrainz_enrichment(conn, config, client=client)
        assert (
            conn.execute(
                "SELECT target_artist_id, cache_key, fetched_at, payload_hash, "
                "evidence FROM artist_evidence_observation ORDER BY "
                "target_artist_id"
            ).fetchall()
            == expected
        )
    assert len(requests) == 1
    assert all(
        row[2] == fetched and row[3] == hashlib.sha256(content).hexdigest() for row in expected
    )
    assert [json.loads(row[4])["response_position"] for row in expected] == [0, 1]


def test_existing_identity_confidence_and_status_are_not_upgraded(staged):
    conn, config = staged
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, "
        "musicbrainz_artist_mbid, identity_confidence, resolution_status) "
        "VALUES (10, 'Candidate', 'candidate', ?, .6, "
        "'ambiguous_musicbrainz')",
        [TARGET],
    )
    run_listenbrainz_enrichment(conn, config, client=FakeClient())
    row = conn.execute(
        "SELECT identity_confidence, identity_status FROM artist_evidence_observation"
    ).fetchone()
    assert row == (0.6, "ambiguous_musicbrainz")


def test_ineligible_pending_seed_is_skipped_and_not_requested(staged):
    conn, config = staged
    plan_listenbrainz_enrichment(conn, config)
    conn.execute(
        "UPDATE artist SET identity_warnings = "
        "'[\"multiple_possible_matches\"]' WHERE artist_id = 1"
    )
    fake = FakeClient()
    stats = run_listenbrainz_enrichment(conn, config, client=fake)
    assert stats.eligible_seeds == 0 and stats.skipped_seeds == 5
    assert not fake.seeds
    assert enrichment_queue_status(conn, source="listenbrainz").skipped == 1


def test_multiple_recordings_are_bounded_and_foreign_credits_skipped(staged):
    conn, config = staged
    config.apis.listenbrainz.max_recordings_per_artist = 1
    first = group()
    fake = FakeClient(
        [
            ListenBrainzArtistRadioGroup(
                first.artist_mbid, first.artist_name, first.recordings * 3, 0
            )
        ]
    )
    assert run_listenbrainz_enrichment(conn, config, client=fake).recordings_written == 1


def test_normalized_name_conflicts_between_provider_artists_remain_separate(staged):
    conn, config = staged
    first = FakeClient([group(name="Same-Name")])
    run_listenbrainz_enrichment(conn, config, client=first)
    second = FakeClient([group("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "Same Name")])
    run_listenbrainz_enrichment(conn, config, client=second)
    rows = conn.execute(
        "SELECT musicbrainz_artist_mbid, identity_warnings FROM artist "
        "WHERE canonical_name IN ('Same-Name', 'Same Name') ORDER BY artist_id"
    ).fetchall()
    assert len(rows) == 2 and rows[0][0] != rows[1][0]
    assert json.loads(rows[1][1]) == ["name_mbid_conflict"]


def test_bounded_batches_drain_pending_seeds_before_refreshing_completed_work(staged):
    conn, config = staged
    config.enrichment.queue_batch_size = 1
    conn.execute("UPDATE artist SET resolution_status = 'resolved_musicbrainz' WHERE artist_id = 2")
    second_seed = "22222222-2222-4222-8222-222222222222"
    client = FakeClient()

    assert run_listenbrainz_enrichment(conn, config, client=client).requests == 1
    assert plan_listenbrainz_enrichment(conn, config).total_queued == 0
    assert enrichment_queue_status(conn, source="listenbrainz").complete == 1
    assert run_listenbrainz_enrichment(conn, config, client=client).requests == 1
    assert client.seeds == [SEED, second_seed]
    assert enrichment_queue_status(conn, source="listenbrainz").complete == 2

    client.groups = (group(count=80),)
    for _ in range(2):
        assert run_listenbrainz_enrichment(conn, config, client=client).requests == 1
    assert client.seeds == [SEED, second_seed, SEED, second_seed]
    rows = conn.execute(
        "SELECT source_artist_id, evidence FROM artist_evidence_observation "
        "ORDER BY source_artist_id"
    ).fetchall()
    assert [row[0] for row in rows] == [1, 2]
    assert [json.loads(row[1])["total_listen_count"] for row in rows] == [80, 80]
    assert enrichment_queue_status(conn, source="listenbrainz").complete == 2


def test_skipped_seed_recovers_when_safe_without_automatically_retrying_failures(staged):
    conn, config = staged
    conn.execute("UPDATE artist SET resolution_status = 'resolved_musicbrainz' WHERE artist_id = 2")
    assert plan_listenbrainz_enrichment(conn, config).total_queued == 2
    conn.execute(
        "UPDATE artist SET identity_warnings = '[\"multiple_possible_matches\"]' "
        "WHERE artist_id = 1"
    )

    class Offline:
        def get_artist_radio(self, seed_artist_mbid):
            raise RuntimeError("offline")

    assert run_listenbrainz_enrichment(conn, config, client=Offline()).requests == 1
    status = enrichment_queue_status(conn, source="listenbrainz")
    assert status.skipped == 1 and status.failed == 1
    assert plan_listenbrainz_enrichment(conn, config).total_queued == 0
    still_unsafe = run_listenbrainz_enrichment(conn, config, client=FakeClient())
    assert still_unsafe.requests == 0

    conn.execute("UPDATE artist SET identity_warnings = '[]' WHERE artist_id = 1")
    assert plan_listenbrainz_enrichment(conn, config).total_queued == 1
    client = FakeClient()
    recovered = run_listenbrainz_enrichment(conn, config, client=client)
    assert recovered.eligible_seeds == 2 and recovered.requests == 1
    assert client.seeds == [SEED]
    rows = conn.execute(
        "SELECT entity_id, status, attempts, error_message FROM enrichment_queue "
        "WHERE source = 'listenbrainz' ORDER BY entity_id"
    ).fetchall()
    assert rows == [(1, "complete", 1, None), (2, "failed", 1, "offline")]
    assert retry_failed_enrichment(conn, source="listenbrainz") == 1


def test_interrupted_running_seed_resumes_before_completed_refresh(staged):
    conn, config = staged
    config.enrichment.queue_batch_size = 1
    config.enrichment.max_queue_attempts = 1
    conn.execute("UPDATE artist SET resolution_status = 'resolved_musicbrainz' WHERE artist_id = 2")
    assert run_listenbrainz_enrichment(conn, config, client=FakeClient()).requests == 1

    class Interrupted:
        def get_artist_radio(self, seed_artist_mbid):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_listenbrainz_enrichment(conn, config, client=Interrupted())
    assert conn.execute(
        "SELECT entity_id, status FROM enrichment_queue WHERE source = 'listenbrainz' "
        "ORDER BY entity_id"
    ).fetchall() == [(1, "complete"), (2, "running")]

    resumed = FakeClient()
    assert run_listenbrainz_enrichment(conn, config, client=resumed).requests == 1
    assert resumed.seeds == ["22222222-2222-4222-8222-222222222222"]
    assert enrichment_queue_status(conn, source="listenbrainz").complete == 2
    for _ in range(2):
        assert run_listenbrainz_enrichment(conn, config, client=resumed).requests == 1
    assert resumed.seeds == [
        "22222222-2222-4222-8222-222222222222",
        SEED,
        "22222222-2222-4222-8222-222222222222",
    ]
    assert enrichment_queue_status(conn, source="listenbrainz").complete == 2
    assert conn.execute("SELECT count(*) FROM artist_evidence_observation").fetchone()[0] == 2


def test_planning_during_active_request_leaves_running_work_and_refresh_gate_untouched(staged):
    conn, config = staged
    config.enrichment.queue_batch_size = 1
    conn.execute("UPDATE artist SET resolution_status = 'resolved_musicbrainz' WHERE artist_id = 2")
    run_listenbrainz_enrichment(conn, config, client=FakeClient())

    class InspectingClient:
        def get_artist_radio(self, seed_artist_mbid):
            assert plan_listenbrainz_enrichment(conn, config).total_queued == 0
            rows = conn.execute(
                "SELECT entity_id, status, attempts FROM enrichment_queue "
                "WHERE source = 'listenbrainz' ORDER BY entity_id"
            ).fetchall()
            assert rows == [(1, "complete", 1), (2, "running", 1)]
            return (group(),)

    assert run_listenbrainz_enrichment(conn, config, client=InspectingClient()).requests == 1
    assert enrichment_queue_status(conn, source="listenbrainz").complete == 2
