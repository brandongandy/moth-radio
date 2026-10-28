"""Cross-import identity contracts through acquisition, exposures and outcomes."""

import json
from datetime import datetime
from pathlib import Path

import pytest
from test_import import write_config
from test_listenbrainz_enrichment import TARGET, FakeClient

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.enrichment.listenbrainz import run_listenbrainz_enrichment
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
from scrobble_recs.models.experiments import (
    create_blinded_experiment,
    experiment_report,
    next_blinded_exposure,
    record_judgment,
)
from scrobble_recs.models.profile_window import ListeningWindow
from scrobble_recs.models.recommend import generate_recommendations
from scrobble_recs.models.recommendation_query import RecommendationRequest
from scrobble_recs.models.relationship import build_target_relationship_snapshot

INITIAL = Path("tests/fixtures/identity_history_initial.csv")
EXTENDED = Path("tests/fixtures/identity_history_extended.csv")
CUTOFF = datetime(2025, 1, 4)


@pytest.fixture
def exposed_history(tmp_path, request):
    imported = getattr(request, "param", "provider") == "imported"
    initial = Path("tests/fixtures/identity_history_known_initial.csv") if imported else INITIAL
    extended = Path("tests/fixtures/identity_history_known_extended.csv") if imported else EXTENDED
    config = load_config(write_config(tmp_path, INITIAL))
    config.apis.listenbrainz.enabled = True
    config.apis.listenbrainz.candidate_generation_enabled = True
    config.recommendation.min_seed_scrobbles = 1
    config.recommendation.artist_challenger.enabled = True
    config.recommendation.calibration.enabled = False
    config.recommendation.fatigue.enabled = False
    conn = initialize_database(config)
    import_lastfm_csv(conn, initial)
    conn.execute(
        "UPDATE artist SET resolution_status = 'resolved_musicbrainz', identity_confidence = .95"
    )
    run_listenbrainz_enrichment(conn, config, client=FakeClient())
    conn.execute("UPDATE artist_evidence_observation SET fetched_at = '2024-01-01'")
    candidate = conn.execute("SELECT target_artist_id FROM artist_evidence_observation").fetchone()[
        0
    ]
    seed = conn.execute("SELECT artist_id FROM artist WHERE canonical_name = 'Seed 1'").fetchone()[
        0
    ]
    runs = [
        generate_recommendations(
            conn,
            config,
            limit=10,
            as_of=datetime(2025, 1, 1),
            history_window=ListeningWindow(datetime(2020, 1, 1), datetime(2025, 1, 1)),
            request=RecommendationRequest("artist", seed),
        ).run_id
        for _ in range(2)
    ]
    assert conn.execute(
        "SELECT artist_id FROM recommendation WHERE run_id = ?", [runs[0]]
    ).fetchall() == [(candidate,)]
    experiment = create_blinded_experiment(
        conn, control_run_id=runs[0], challenger_run_id=runs[1], randomization_seed=7
    )
    exposure = next_blinded_exposure(conn, experiment_id=experiment)
    assert exposure is not None
    conn.execute("UPDATE recommendation_exposure SET created_at = '2025-01-01'")
    record_judgment(conn, exposure_id=exposure.exposure_id, labels={candidate: ["strong_fit"]})
    yield conn, config, candidate, experiment, initial, extended
    conn.close()


def snapshot(conn, config, artist):
    return build_target_relationship_snapshot(
        conn,
        artist_id=artist,
        as_of=CUTOFF,
        history_window=ListeningWindow(datetime(2020, 1, 1), CUTOFF),
        feedback_cutoff=CUTOFF,
        thresholds=config.recommendation.artist_relationship,
    )


@pytest.mark.parametrize("exposed_history", ["provider", "imported"], indirect=True)
def test_extended_replacement_import_credits_original_exposure_and_relationship(exposed_history):
    conn, config, artist, experiment, initial, extended = exposed_history
    baseline = 1 if "known" in initial.name else 0
    frozen = [
        conn.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
        for table in (
            "recommendation_experiment",
            "recommendation_exposure",
            "recommendation_judgment",
        )
    ]
    before_identity = conn.execute("SELECT * FROM artist WHERE artist_id = ?", [artist]).fetchone()
    for _ in range(2):
        import_lastfm_csv(conn, extended)
        report = experiment_report(conn, experiment, outcome_cutoff=CUTOFF)
        assert report.downstream["control"]["play_count"] == 1.0
        assert report.downstream["challenger"]["adopted"] == 0.5
        relationship = snapshot(conn, config, artist)
        assert (relationship.state, relationship.play_count, relationship.active_days) == (
            "sampled",
            2 + baseline,
            2 + baseline,
        )
        assert (relationship.prior_recommendation_exposures, relationship.exposure_source) == (
            1,
            "recorded_exposure",
        )
        assert (
            conn.execute("SELECT * FROM artist WHERE artist_id = ?", [artist]).fetchone()
            == before_identity
        )
        assert conn.execute("SELECT count(*) FROM artist").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM scrobble").fetchone() == (3 + baseline,)
        assert frozen == [
            conn.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
            for table in (
                "recommendation_experiment",
                "recommendation_exposure",
                "recommendation_judgment",
            )
        ]
    # Replacement history can remove listening while preserving identities and exposure history.
    import_lastfm_csv(conn, initial)
    assert snapshot(conn, config, artist).play_count == baseline
    assert snapshot(conn, config, artist).prior_recommendation_exposures == 1
    assert conn.execute("SELECT count(*) FROM scrobble").fetchone() == (1 + baseline,)
    assert conn.execute("SELECT count(*) FROM track").fetchone() == (1 + baseline,)


def test_reimport_updates_track_metadata_without_reassigning_artist(tmp_path):
    config = load_config(write_config(tmp_path, INITIAL))
    conn = initialize_database(config)
    try:
        import_lastfm_csv(conn, INITIAL)
        artist = conn.execute("SELECT artist_id FROM artist").fetchone()[0]
        changed = tmp_path / "updated.csv"
        changed.write_text(INITIAL.read_text().replace("First,", "FIRST,recording-hint"))
        import_lastfm_csv(conn, changed)
        assert conn.execute(
            "SELECT artist_id, canonical_title, musicbrainz_recording_mbid FROM track"
        ).fetchall() == [(artist, "FIRST", "recording-hint")]
    finally:
        conn.close()


@pytest.mark.parametrize(
    "conflict",
    [
        "different_mbid",
        "multiple_local",
        "different_name",
        "multiple_csv",
        "missing_hint_multiple_local",
        "ambiguous_status",
    ],
)
def test_conflicting_import_hints_never_credit_exposed_identity(
    exposed_history, tmp_path, conflict
):
    conn, config, artist, experiment, _, _ = exposed_history
    text = EXTENDED.read_text()
    if conflict == "different_mbid":
        text = text.replace(TARGET, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    elif conflict == "different_name":
        text = text.replace("Candidate", "Other Artist")
    elif conflict == "multiple_csv":
        text = text.replace(TARGET, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 1)
    elif conflict == "ambiguous_status":
        conn.execute(
            "UPDATE artist SET resolution_status = 'ambiguous_musicbrainz' WHERE artist_id = ?",
            [artist],
        )
    else:
        conn.execute(
            "INSERT INTO artist (artist_id, canonical_name, artist_key, "
            "musicbrainz_artist_mbid, identity_confidence) "
            "VALUES (20, 'Other Artist', 'other', ?, .99)",
            [TARGET],
        )
        if conflict == "missing_hint_multiple_local":
            text = text.replace(TARGET, "")
    changed = tmp_path / "conflicted.csv"
    changed.write_text(text)
    for _ in range(2):
        import_lastfm_csv(conn, changed)
        report = experiment_report(conn, experiment, outcome_cutoff=CUTOFF)
        assert report.downstream["control"]["play_count"] == 0
        assert snapshot(conn, config, artist).play_count == 0
        assert snapshot(conn, config, artist).prior_recommendation_exposures == 1
        assert conn.execute(
            "SELECT musicbrainz_artist_mbid FROM artist WHERE artist_id = ?", [artist]
        ).fetchone() == (TARGET,)
        facts = conn.execute(
            "SELECT a.artist_id, a.identity_warnings FROM artist a "
            "JOIN artist_listening_fact f USING (artist_id) WHERE f.play_count = 2"
        ).fetchall()
        assert len(facts) == 1 and facts[0][0] != artist
        assert any(
            "conflict" in warning or "multiple" in warning for warning in json.loads(facts[0][1])
        )
        current = conn.execute(
            "SELECT artist_id, artist_key FROM artist ORDER BY artist_id"
        ).fetchall()
        if _ == 0:
            first = current
        else:
            assert current == first
