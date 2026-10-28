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


def recommend_acquired(conn, config, query_kind="general"):
    from scrobble_recs.models.profile_window import ListeningWindow
    from scrobble_recs.models.recommend import generate_recommendations
    from scrobble_recs.models.recommendation_query import RecommendationRequest

    config.apis.listenbrainz.candidate_generation_enabled = True
    config.recommendation.scoring.engine = "legacy" if query_kind == "artist" else "fusion"
    config.recommendation.fusion.enabled = True
    config.recommendation.calibration.enabled = False
    config.recommendation.artist_challenger.enabled = True
    conn.execute(
        "INSERT INTO scrobble (scrobble_id, artist_key, track_key, played_at, played_date) "
        "VALUES (1, 'seed 1', 'song', '2024-01-01', '2024-01-01')"
    )
    conn.execute("UPDATE artist_evidence_observation SET fetched_at = '2024-01-01'")
    return generate_recommendations(
        conn,
        config,
        limit=10,
        as_of=datetime(2025, 1, 1),
        history_window=ListeningWindow(datetime(2020, 1, 1), datetime(2025, 1, 1)),
        historical_cutoff=datetime(2025, 1, 1),
        request=RecommendationRequest(query_kind, 1 if query_kind == "artist" else None),
    )


@pytest.mark.parametrize("conflict", ["multiple", "name", "stored_warning", "late_multiple"])
def test_acquired_radio_quarantines_unsafe_identity_through_artist_recommendation(staged, conflict):
    conn, config = staged
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, "
        "musicbrainz_artist_mbid, identity_confidence, identity_warnings) "
        "VALUES (10, ?, 'candidate', ?, .99, ?)",
        [
            "Wrong Local" if conflict == "name" else "Candidate",
            TARGET,
            '["name_mbid_conflict"]' if conflict == "stored_warning" else "[]",
        ],
    )
    if conflict == "multiple":
        conn.execute(
            "INSERT INTO artist (artist_id, canonical_name, artist_key, "
            "musicbrainz_artist_mbid, identity_confidence) "
            "VALUES (11, 'Wrong Local', 'wrong', ?, .99)",
            [TARGET],
        )
    run_listenbrainz_enrichment(conn, config, client=FakeClient())
    if conflict == "late_multiple":
        conn.execute(
            "INSERT INTO artist (artist_id, canonical_name, artist_key, "
            "musicbrainz_artist_mbid, identity_confidence) "
            "VALUES (11, 'Wrong Local', 'wrong', ?, .99)",
            [TARGET],
        )
    # Deliberately stale confidence must not turn warning-bearing membership into evidence.
    conn.execute("UPDATE artist_evidence_observation SET identity_confidence = .99")
    stats = recommend_acquired(conn, config, "artist")
    assert (
        conn.execute(
            "SELECT artist_id FROM recommendation WHERE run_id = ?", [stats.run_id]
        ).fetchall()
        == []
    )
    diagnostics = json.loads(
        conn.execute(
            "SELECT seed_summary FROM recommendation_run WHERE run_id = ?", [stats.run_id]
        ).fetchone()[0]
    )
    quarantined = diagnostics["retrieval_diagnostics"]["invalid_observations"]
    assert quarantined[0]["reason"] == "unsafe_identity"
    assert quarantined[0]["identity_warnings"]
    if conflict != "late_multiple":
        payload = json.loads(
            conn.execute("SELECT evidence FROM artist_evidence_observation").fetchone()[0]
        )
        assert payload["identity_warnings"]


@pytest.mark.parametrize("corroboration", ["normalized_name", "musicbrainz_alias"])
def test_acquired_radio_accepts_corroborated_provider_names(staged, corroboration):
    conn, config = staged
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, "
        "musicbrainz_artist_mbid, identity_confidence) VALUES (10, ?, 'candidate', ?, .95)",
        [
            " ＣＡＮＤＩＤＡＴＥ " if corroboration == "normalized_name" else "Canonical Name",
            TARGET,
        ],
    )
    if corroboration == "musicbrainz_alias":
        conn.execute(
            "INSERT INTO artist_alias (artist_id, alias, normalized_alias, source, fetched_at) "
            "VALUES (10, 'Candidate', 'candidate', 'musicbrainz', '2024-01-01')"
        )
    run_listenbrainz_enrichment(conn, config, client=FakeClient())
    stats = recommend_acquired(conn, config, "artist")
    assert conn.execute(
        "SELECT artist_id FROM recommendation WHERE run_id = ?", [stats.run_id]
    ).fetchall() == [(10,)]
    payload = json.loads(
        conn.execute("SELECT evidence FROM artist_evidence_observation").fetchone()[0]
    )
    assert payload["identity_warnings"] == []


@pytest.mark.parametrize(
    "reinforcement, expected",
    [
        ("semantic", True),
        ("catalog", True),
        ("none", False),
        ("unrelated", False),
        ("unsupported_catalog", False),
        ("unrelated_catalog", False),
        ("conflicting_catalog", False),
        ("unsafe", False),
    ],
)
def test_production_radio_general_fusion_requires_independent_relevant_evidence(
    staged, reinforcement, expected
):
    from scrobble_recs.models.evidence import EvidenceObservation, persist_evidence_observation

    conn, config = staged
    run_listenbrainz_enrichment(conn, config, client=FakeClient())
    candidate = conn.execute("SELECT target_artist_id FROM artist_evidence_observation").fetchone()[
        0
    ]
    if reinforcement in ("semantic", "unrelated", "unsafe"):
        conn.execute(
            "INSERT INTO artist_tag (artist_id, tag, source, normalized_weight, fetched_at) "
            "VALUES (1, 'dream pop', 'lastfm', 1, '2024-01-01'), (?, ?, 'lastfm', 1, '2024-01-01')",
            [candidate, "metal" if reinforcement == "unrelated" else "dream pop"],
        )
    if "catalog" in reinforcement:
        config.apis.musicbrainz.enabled = True
        config.recommendation.catalog_evidence.enabled = True
        config.recommendation.discovery_evidence.catalog_relationships_enabled = True
        config.recommendation.catalog_evidence.allowed_relationship_types = ["member of band"]
        persist_evidence_observation(
            conn,
            EvidenceObservation(
                "musicbrainz",
                "catalog",
                "catalog_relationship",
                5 if reinforcement == "unrelated_catalog" else 1,
                candidate,
                None,
                None,
                None,
                datetime(2024, 1, 1),
                0.95,
                {
                    "relationship_type": "influenced by"
                    if reinforcement == "unsupported_catalog"
                    else "member of band",
                    "supported_relationship": True,
                },
            ),
            observation_key="catalog-corroboration",
        )
        if reinforcement == "conflicting_catalog":
            conn.execute(
                "UPDATE artist_evidence_observation SET evidence = json_merge_patch(evidence, "
                "'{\"identity_warnings\":[\"name_mbid_conflict\"]}') WHERE source = 'musicbrainz'"
            )
    if reinforcement == "unsafe":
        conn.execute(
            "UPDATE artist SET identity_warnings = '[\"name_mbid_conflict\"]' WHERE artist_id = ?",
            [candidate],
        )
    stats = recommend_acquired(conn, config)
    ids = conn.execute(
        "SELECT artist_id FROM recommendation WHERE run_id = ?", [stats.run_id]
    ).fetchall()
    assert ids == ([(candidate,)] if reinforcement == "semantic" else [])
    candidate_row = conn.execute(
        "SELECT admission, evidence, feature_record FROM recommendation_candidate "
        "WHERE run_id = ? AND artist_id = ?",
        [stats.run_id, candidate],
    ).fetchone()
    assert bool(candidate_row and json.loads(candidate_row[0])["admitted"]) is expected
    if expected:
        admission, evidence, features = candidate_row
        assert "unranked_radio_independent_reinforcement_v1" in json.loads(admission)["reasons"]
        assert all(
            row["source_rank"] is None and row["raw_score"] is None
            for row in json.loads(evidence)["observations"]
            if row["source"] == "listenbrainz"
        )
        assert "fusion_source_listenbrainz" not in json.loads(features)["values"]


def test_listenbrainz_does_not_implicitly_acquire_musicbrainz_recordings(staged, monkeypatch):
    from scrobble_recs.apis.musicbrainz import MusicBrainzClient

    conn, config = staged
    config.apis.musicbrainz.enabled = True

    def forbidden(*args, **kwargs):
        raise AssertionError("implicit MusicBrainz lookup during ListenBrainz enrichment")

    monkeypatch.setattr(MusicBrainzClient, "__init__", forbidden)
    monkeypatch.setattr(MusicBrainzClient, "lookup_recording", forbidden)
    stats = run_listenbrainz_enrichment(conn, config, client=FakeClient())
    assert stats.observations_written == 1
    assert (
        conn.execute("SELECT count(*) FROM api_cache WHERE source = 'musicbrainz'").fetchone()[0]
        == 0
    )


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
