"""Persisted local experiment contracts; no ranking or provider calls."""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database

CONTROL = "00000000-0000-0000-0000-000000000101"
CHALLENGER = "00000000-0000-0000-0000-000000000102"


def seed_runs(conn):
    fixture = json.loads((Path(__file__).parent / "fixtures/experiment_slates.json").read_text())
    for identity, name in fixture["names"].items():
        conn.execute(
            "INSERT INTO artist (artist_id, canonical_name, artist_key) VALUES (?, ?, ?)",
            [int(identity), name, name.lower()],
        )
    for variant, run in [("control", CONTROL), ("challenger", CHALLENGER)]:
        context = deepcopy(fixture["context"])
        context["config_hash"] = f"PRIVATE_{variant}"
        conn.execute(
            "INSERT INTO recommendation_run (run_id, query_kind, mode, query_context, "
            "evidence_snapshot_id) VALUES (?, 'general', 'balanced', ?, 'frozen-evidence')",
            [run, json.dumps(context)],
        )
        for rank, artist in enumerate(fixture[variant], 1):
            conn.execute(
                "INSERT INTO recommendation (run_id, rank, artist_id, knownness, "
                "explanation, identity_confidence) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    run,
                    rank,
                    artist,
                    0.2 if variant == "control" else 0.4,
                    '{"summary":"PRIVATE_EXPLANATION"}',
                    0.8,
                ],
            )
    return fixture


def create(conn, seed=7):
    from scrobble_recs.models.experiments import create_blinded_experiment

    return create_blinded_experiment(
        conn, control_run_id=CONTROL, challenger_run_id=CHALLENGER, randomization_seed=seed
    )


def test_promotion_report_freezes_source_artifacts_and_effective_completion(experiment_db):
    from scrobble_recs.models.experiments import (
        experiment_report,
        next_blinded_exposure,
        record_judgment,
    )

    conn, config = experiment_db
    seed_runs(conn)
    for variant, run in (("control", CONTROL), ("challenger", CHALLENGER)):
        conn.execute(
            "UPDATE recommendation_run SET seed_summary = ? WHERE run_id = ?",
            [
                json.dumps(
                    {
                        "evaluation_artifact": {
                            "artifact_id": variant,
                            "behavior_config_fingerprint": variant,
                        }
                    }
                ),
                run,
            ],
        )
    experiment = create(conn)
    exposure = next_blinded_exposure(conn, experiment_id=experiment)
    record_judgment(conn, exposure_id=exposure.exposure_id, list_choice="tie")
    report = experiment_report(conn, experiment, outcome_cutoff=datetime(2030, 1, 1))
    assert report.definition["source_artifacts"]["control"]["artifact_id"] == "control"
    evidence = report.promotion_evidence
    assert evidence["exposures"][0]["effective_list_choice"] == "tie"
    assert evidence["exposures"][0]["judgment_id"]
    assert set(evidence["exposed_artist_ids"]) == set(map(int, report.definition["artists"]))
    # A stale stored completion timestamp cannot resurrect a replaced null list answer.
    record_judgment(conn, exposure_id=exposure.exposure_id, labels={2: ["strong_fit"]})
    conn.execute("UPDATE recommendation_exposure SET completed_at = current_timestamp")
    incomplete = experiment_report(conn, experiment, outcome_cutoff=datetime(2030, 1, 1))
    assert incomplete.exposure_counts["completed"] == 0
    assert incomplete.promotion_evidence["exposures"][0]["effective_list_choice"] is None
    assert incomplete.list_choices["denominator"] == 0


def test_historical_experiment_guards_do_not_backfill_current_catalog_identity_or_queue(
    experiment_db,
):
    from scrobble_recs.models.experiments import experiment_report

    conn, _ = experiment_db
    seed_runs(conn)
    experiment = create(conn)
    original = experiment_report(conn, experiment, outcome_cutoff=datetime(2025, 1, 1))
    definition_bytes = conn.execute(
        "SELECT definition::VARCHAR FROM recommendation_experiment WHERE experiment_id = ?",
        [experiment],
    ).fetchone()[0]
    conn.execute(
        "UPDATE artist SET resolution_status = 'resolved_musicbrainz', identity_confidence = 1"
    )
    conn.execute(
        "INSERT INTO artist_tag (artist_id, tag, source, normalized_weight, fetched_at) "
        "SELECT artist_id, 'current-catalog', 'lastfm', 1, '2026-01-01' FROM artist"
    )
    conn.execute(
        "INSERT INTO tag_top_artist (artist_id, tag, source, tag_rank, fetched_at) "
        "SELECT artist_id, 'current-catalog', 'lastfm', 1, '2026-01-01' FROM artist"
    )
    conn.execute(
        "INSERT INTO enrichment_queue (queue_id, source, endpoint, entity_type, entity_id, "
        "status, attempts, last_attempt_at) "
        "VALUES (uuid(), 'musicbrainz', 'artist', 'artist', 1, 'failed', 3, '2026-01-01')"
    )
    updated = experiment_report(conn, experiment, outcome_cutoff=datetime(2025, 1, 1))
    assert updated.guardrail_deltas == original.guardrail_deltas
    assert updated.definition == original.definition
    assert (
        conn.execute(
            "SELECT definition::VARCHAR FROM recommendation_experiment WHERE experiment_id = ?",
            [experiment],
        ).fetchone()[0]
        == definition_bytes
    )
    assert updated.guardrail_deltas["popularity"]["value"] is None
    assert (
        conn.execute("SELECT count(*) FROM enrichment_queue WHERE status = 'failed'").fetchone()[0]
        == 1
    )


def state(conn):
    return [
        conn.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
        for table in (
            "recommendation_experiment",
            "recommendation_exposure",
            "recommendation_judgment",
            "recommendation_downstream_outcome",
        )
    ]


@pytest.fixture
def experiment_db(tmp_path):
    from test_tui_app import write_config

    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    yield conn, config
    conn.close()


def test_migration_preserves_experiment_history(experiment_db):
    conn, config = experiment_db
    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    assert {
        "recommendation_experiment",
        "recommendation_exposure",
        "recommendation_judgment",
        "recommendation_downstream_outcome",
    } <= tables
    conn.execute(
        "INSERT INTO recommendation_experiment (experiment_id, definition) "
        "VALUES ('00000000-0000-0000-0000-000000000009', '{}')"
    )
    before = conn.execute("SELECT * FROM recommendation_experiment").fetchall()
    initialize_database(config).close()
    assert conn.execute("SELECT * FROM recommendation_experiment").fetchall() == before


def test_migration_preserves_complete_history_and_enforces_unique_keys(experiment_db):
    import duckdb

    conn, config = experiment_db
    seed_runs(conn)
    experiment = "00000000-0000-0000-0000-000000000901"
    exposure = "00000000-0000-0000-0000-000000000902"
    judgment = "00000000-0000-0000-0000-000000000903"
    historical = "00000000-0000-0000-0000-000000000904"
    conn.execute(
        "INSERT INTO recommendation_run (run_id, created_at, mode, query_context, "
        "evidence_snapshot_id) VALUES (?, '2020-01-01', 'balanced', NULL, NULL)",
        [historical],
    )
    conn.execute(
        "INSERT INTO recommendation (run_id, rank, artist_id, explanation) "
        'VALUES (?, 1, 1, \'{"summary":"historical"}\')',
        [historical],
    )
    conn.execute(
        "INSERT INTO recommendation_experiment VALUES (?, '2025-01-01', 'general', ?, ?, "
        "'frozen-evidence', 7, '{\"version\":\"blinded-v1\"}', 'completed')",
        [experiment, CONTROL, CHALLENGER],
    )
    conn.execute(
        "INSERT INTO recommendation_exposure VALUES (?, ?, '2025-01-02', '{}', "
        '\'[{"artist_id":1,"artist":"One","side":"left","position":1}]\', '
        '\'{"1":[{"variant":"control","ranks":[1]}]}\', \'2025-01-03\')',
        [exposure, experiment],
    )
    conn.execute(
        "INSERT INTO recommendation_judgment VALUES (?, ?, '2025-01-03', 'left', 'left', "
        "1, '{\"1\":[\"strong_fit\"]}', 0.8, 'Recorded judgment')",
        [judgment, exposure],
    )
    conn.execute(
        "INSERT INTO recommendation_downstream_outcome VALUES (?, 1, '2025-01-04', 2, 1, "
        "'2025-01-05')",
        [exposure],
    )
    before = state(conn)
    historical_before = [
        conn.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
        for table in ("recommendation_run", "recommendation")
    ]
    assert all(len(rows) == 1 for rows in before)
    for _ in range(2):
        initialize_database(config).close()
        assert state(conn) == before
        assert [
            conn.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
            for table in ("recommendation_run", "recommendation")
        ] == historical_before
    for table in (
        "recommendation_experiment",
        "recommendation_exposure",
        "recommendation_judgment",
        "recommendation_downstream_outcome",
    ):
        with pytest.raises(duckdb.ConstraintException):
            conn.execute(f"INSERT INTO {table} SELECT * FROM {table}")
    assert state(conn) == before


def recorded_relationship():
    return {
        "state": "sampled",
        "play_count": 2,
        "recency_weighted_play_count": 1.5,
        "unique_tracks": 2,
        "unique_releases": 1,
        "active_days": 2,
        "active_months": 2,
        "first_listen": "2024-06-01T00:00:00",
        "last_listen": "2024-07-01T00:00:00",
        "listening_span_days": 30,
        "recent_trend": 0.5,
        "replay_concentration": 0.5,
        "positive_feedback": 0.0,
        "negative_feedback": 0.0,
        "prior_recommendation_exposures": 0,
        "exposure_source": "run_appearance",
        "relationship_version": "artist-relationship-v1",
        "low_depth_sample": True,
        "thresholds": {
            "sampled_max_plays": 5,
            "established_min_plays": 20,
            "established_min_active_months": 3,
            "dormant_days": 730,
        },
    }


@pytest.mark.parametrize(
    "path,value",
    [
        *(
            ((field,), value)
            for field in ("history_cutoff", "feedback_cutoff")
            for value in ({}, [], True, 0, "invalid")
        ),
        *(
            (("evidence_freshness", "artist_tag", "latest_at"), value)
            for value in ({}, [], True, 0, "invalid")
        ),
        (("evidence_freshness", "artist_tag", "row_count"), True),
        (("evidence_freshness", "artist_tag", "row_count"), -1),
        (("evidence_freshness", "artist_tag"), []),
        (("history_window", "start"), {}),
        (("history_window", "end"), None),
        *(
            (("target_relationship", field), value)
            for field in (
                "play_count",
                "unique_tracks",
                "unique_releases",
                "active_days",
                "active_months",
                "listening_span_days",
                "prior_recommendation_exposures",
            )
            for value in ("invalid", -1, True)
        ),
        *(
            (("target_relationship", field), value)
            for field in (
                "recency_weighted_play_count",
                "recent_trend",
                "replay_concentration",
                "positive_feedback",
                "negative_feedback",
            )
            for value in ("invalid", -0.1, True)
        ),
        (("target_relationship", "replay_concentration"), 1.1),
        (("target_relationship", "first_listen"), "2024-08-01T00:00:00"),
        *(
            (("target_relationship", field), value)
            for field in ("first_listen", "last_listen")
            for value in ({}, [], True, "invalid")
        ),
        (("target_relationship", "low_depth_sample"), "false"),
        (("target_relationship", "exposure_source"), "invalid"),
        (("target_relationship", "relationship_version"), {}),
        (("target_relationship", "relationship_version"), ""),
        (("target_relationship", "thresholds"), []),
        (("target_relationship", "thresholds", "dormant_days"), -1),
        (("target_relationship", "thresholds", "dormant_days"), True),
        (("target_relationship", "thresholds", "dormant_days"), "invalid"),
    ],
)
def test_matching_malformed_semantics_reject_without_changing_history(experiment_db, path, value):
    from scrobble_recs.models.experiments import (
        experiment_report,
        next_blinded_exposure,
        record_judgment,
    )

    conn, _ = experiment_db
    fixture = seed_runs(conn)
    experiment = create(conn)
    exposure = next_blinded_exposure(conn, experiment_id=experiment)
    record_judgment(conn, exposure_id=exposure.exposure_id, list_choice="left")
    experiment_report(conn, experiment, outcome_cutoff=datetime(2030, 1, 1))
    context = deepcopy(fixture["context"])
    context["evidence_freshness"] = {"artist_tag": {"row_count": 1, "latest_at": None}}
    if path[0] == "target_relationship":
        context.update(
            query_kind="artist", target_artist_id=1, target_relationship=recorded_relationship()
        )
    parent = context
    for field in path[:-1]:
        parent = parent[field]
    parent[path[-1]] = value
    conn.execute(
        "UPDATE recommendation_run SET query_kind = ?, target_artist_id = ?, query_context = ?",
        [context["query_kind"], context.get("target_artist_id"), json.dumps(context)],
    )
    before = state(conn)
    with pytest.raises(ValueError):
        create(conn)
    assert state(conn) == before


@pytest.mark.parametrize("nullable_cutoffs", [False, True])
def test_valid_semantic_timestamps_preserve_timezone_equivalence_and_nulls(
    experiment_db, nullable_cutoffs
):
    conn, _ = experiment_db
    fixture = seed_runs(conn)
    for run, offset in ((CONTROL, False), (CHALLENGER, True)):
        context = deepcopy(fixture["context"])
        context.update(
            query_kind="artist", target_artist_id=1, target_relationship=recorded_relationship()
        )
        if nullable_cutoffs:
            context.update(history_cutoff=None, feedback_cutoff=None)
        context["evidence_freshness"] = {
            "artist_tag": {
                "row_count": 1,
                "latest_at": "2024-12-31T19:00:00-05:00" if offset else "2025-01-01T00:00:00",
            }
        }
        if offset:
            context["target_relationship"]["first_listen"] = "2024-05-31T19:00:00-05:00"
            context["target_relationship"]["last_listen"] = "2024-06-30T19:00:00-05:00"
        conn.execute(
            "UPDATE recommendation_run SET query_kind = 'artist', target_artist_id = 1, "
            "query_context = ? WHERE run_id = ?",
            [json.dumps(context), run],
        )
    assert create(conn)


@pytest.mark.parametrize(
    "field,value",
    [
        ("query_kind", "artist"),
        ("mode", "adventurous"),
        ("evidence_snapshot_id", "different"),
        ("history_cutoff", "2024-12-01T00:00:00"),
        ("feedback_cutoff", None),
        ("history_window", {"start": "2023-01-01", "end": "2025-01-01"}),
        ("novelty_intent", "mixed"),
        ("taste_region_seeds", []),
        ("evidence_freshness", {"changed": {"row_count": 1}}),
        ("target_artist_id", 1),
    ],
)
def test_rejects_incompatible_or_contradictory_context_without_writes(experiment_db, field, value):
    conn, _ = experiment_db
    fixture = seed_runs(conn)
    context = fixture["context"] | {field: value}
    conn.execute(
        "UPDATE recommendation_run SET query_context = ? WHERE run_id = ?",
        [json.dumps(context), CHALLENGER],
    )
    before = state(conn)
    with pytest.raises(ValueError):
        create(conn)
    assert state(conn) == before


@pytest.mark.parametrize(
    "context,snapshot", [(None, None), ({}, "frozen-evidence"), ([], "frozen-evidence")]
)
def test_rejects_missing_recorded_provenance(experiment_db, context, snapshot):
    conn, _ = experiment_db
    seed_runs(conn)
    conn.execute(
        "UPDATE recommendation_run SET query_context = ?, evidence_snapshot_id = ?",
        [json.dumps(context) if context is not None else None, snapshot],
    )
    with pytest.raises(ValueError):
        create(conn)
    assert not state(conn)[0]


def test_frozen_blinded_pagination_resume_and_exhaustion(experiment_db):
    from scrobble_recs.models.experiments import next_blinded_exposure, record_judgment

    conn, config = experiment_db
    seed_runs(conn)
    experiment = create(conn)
    conn.execute("DELETE FROM recommendation")
    conn.execute("UPDATE artist SET canonical_name = 'PRIVATE_CHANGED'")
    first = next_blinded_exposure(conn, experiment_id=experiment, slate_size=1)
    assert first is not None
    assert len(first.left) <= 1 and len(first.right) <= 1
    public = json.dumps(asdict(first), default=str)
    assert all(secret not in public for secret in [CONTROL, CHALLENGER, "PRIVATE", "variant"])
    assert next_blinded_exposure(conn, experiment_id=experiment, slate_size=9) == replace(
        first, resumed=True
    )
    other = initialize_database(config)
    assert next_blinded_exposure(other, experiment_id=experiment) == replace(first, resumed=True)
    other.close()
    shown = []
    while first is not None:
        shown.extend(item.artist_id for item in first.left + first.right)
        record_judgment(conn, exposure_id=first.exposure_id, list_choice="tie")
        first = next_blinded_exposure(conn, experiment_id=experiment, slate_size=1)
    assert sorted(shown) == [1, 2, 3, 4]
    before = state(conn)
    assert next_blinded_exposure(conn, experiment_id=experiment) is None
    assert state(conn) == before


def test_seeded_mapping_and_shared_memberships_are_frozen(experiment_db):
    from scrobble_recs.models.experiments import next_blinded_exposure

    conn, _ = experiment_db
    seed_runs(conn)
    definitions = []
    for seed in (7, 7, 8):
        experiment = create(conn, seed)
        definition = json.loads(
            conn.execute(
                "SELECT definition FROM recommendation_experiment WHERE experiment_id = ?",
                [experiment],
            ).fetchone()[0]
        )
        definitions.append(definition)
        exposure = next_blinded_exposure(conn, experiment_id=experiment)
        assert exposure is not None
        assert sorted(item.artist_id for item in exposure.left + exposure.right) == [1, 2, 3, 4]
        assert {m["variant"] for m in definition["artists"]["2"]["memberships"]} == {
            "control",
            "challenger",
        }
    assert definitions[0] == definitions[1]
    assert definitions[0]["side_variants"] != definitions[2]["side_variants"]


def test_partial_replacement_completion_and_reopening(experiment_db):
    from scrobble_recs.models.experiments import next_blinded_exposure, record_judgment

    conn, _ = experiment_db
    seed_runs(conn)
    experiment = create(conn)
    exposure = next_blinded_exposure(conn, experiment_id=experiment)
    identity = exposure.exposure_id
    record_judgment(
        conn,
        exposure_id=identity,
        labels={2: ["want_to_try", "want_to_try"]},
        preferred_side="left",
        confidence=0.8,
        note="Listen later",
    )
    resumed = next_blinded_exposure(conn, experiment_id=experiment)
    assert resumed.judgment.labels == {2: ("want_to_try",)}
    assert resumed.judgment.preferred_side == "left"
    assert not resumed.completed
    record_judgment(conn, exposure_id=identity, list_choice="none")
    completed_at = conn.execute("SELECT completed_at FROM recommendation_exposure").fetchone()[0]
    assert completed_at is not None
    record_judgment(conn, exposure_id=identity, list_choice="right", best_discovery_artist_id=2)
    assert (
        conn.execute("SELECT completed_at FROM recommendation_exposure").fetchone()[0]
        == completed_at
    )
    record_judgment(conn, exposure_id=identity)
    reopened = next_blinded_exposure(conn, experiment_id=experiment)
    assert reopened.judgment.labels == {} and reopened.judgment.list_choice is None
    assert not reopened.completed


@pytest.mark.parametrize(
    "arguments",
    [
        {"labels": {5: ["reject"]}},
        {"labels": {True: ["reject"]}},
        {"labels": {2: ["unknown"]}},
        {"labels": {2: "reject"}},
        {"best_discovery_artist_id": 5},
        {"best_discovery_artist_id": True},
        {"confidence": float("nan")},
        {"confidence": float("inf")},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"confidence": True},
        {"confidence": 10**1000},
        {"note": 1},
        {"preferred_side": "control"},
        {"list_choice": "challenger"},
    ],
)
def test_invalid_judgment_is_atomic(experiment_db, arguments):
    from scrobble_recs.models.experiments import next_blinded_exposure, record_judgment

    conn, _ = experiment_db
    seed_runs(conn)
    exposure = next_blinded_exposure(conn, experiment_id=create(conn))
    record_judgment(conn, exposure_id=exposure.exposure_id, list_choice="tie")
    before = state(conn)
    with pytest.raises(ValueError):
        record_judgment(conn, exposure_id=exposure.exposure_id, **arguments)
    assert state(conn) == before


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_invalid_size_never_writes(experiment_db, size):
    from scrobble_recs.models.experiments import next_blinded_exposure

    conn, _ = experiment_db
    seed_runs(conn)
    experiment = create(conn)
    before = state(conn)
    with pytest.raises(ValueError):
        next_blinded_exposure(conn, experiment_id=experiment, slate_size=size)
    assert state(conn) == before


def test_candidate_only_and_empty_slates_are_not_exposed(experiment_db):
    from scrobble_recs.models.experiments import next_blinded_exposure

    conn, _ = experiment_db
    seed_runs(conn)
    conn.execute("UPDATE recommendation SET score_band = 'candidate_only'")
    assert next_blinded_exposure(conn, experiment_id=create(conn)) is None
    assert not state(conn)[1]


def test_report_fractional_credit_strict_windows_and_frozen_guards(experiment_db):
    from scrobble_recs.models.experiments import (
        experiment_report,
        next_blinded_exposure,
        record_judgment,
    )

    conn, _ = experiment_db
    seed_runs(conn)
    experiment = create(conn)
    exposure = next_blinded_exposure(conn, experiment_id=experiment)
    start = datetime(2025, 2, 1)
    conn.execute("UPDATE recommendation_exposure SET created_at = ?", [start])
    for i, (artist, offset, duplicate) in enumerate(
        [
            ("shared", -1, False),
            ("shared", 0, False),
            ("shared", 1, False),
            ("shared", 86401, False),
            ("shared", 2, True),
            ("one", 2, False),
            ("unexposed", 3, False),
            ("four", 172800, False),
        ],
        1,
    ):
        conn.execute(
            "INSERT INTO scrobble (scrobble_id, artist_key, played_at, is_duplicate) "
            "VALUES (?, ?, ?, ?)",
            [i, artist, start + timedelta(seconds=offset), duplicate],
        )
    record_judgment(
        conn,
        exposure_id=exposure.exposure_id,
        list_choice="left",
        best_discovery_artist_id=2,
        labels={2: ["strong_fit"]},
    )
    record_judgment(
        conn,
        exposure_id=exposure.exposure_id,
        list_choice="left",
        best_discovery_artist_id=2,
        labels={2: ["strong_fit"]},
    )
    cutoff = start + timedelta(days=2)
    report = experiment_report(conn, experiment, outcome_cutoff=cutoff)
    assert report.definition["source_run_ids"] == {"control": CONTROL, "challenger": CHALLENGER}
    assert report.exposure_counts == {"total": 1, "completed": 1, "partial": 0, "unjudged": 0}
    assert report.list_choices == {
        "control": 1,
        "challenger": 0,
        "tie": 0,
        "abstentions": 0,
        "unanswered": 0,
        "denominator": 1,
    }
    assert report.best_discovery == {"control": 0.5, "challenger": 0.5, "denominator": 1}
    assert report.labels["strong_fit"] == {"control": 0.5, "challenger": 0.5}
    assert report.downstream["unique_exposed_artists"] == 4
    assert report.downstream["shared_artists"] == 1
    assert report.downstream["control"] == {
        "denominator": 2.5,
        "adopted": 1.5,
        "repeated": 0.5,
        "play_count": 2.0,
        "artist_active_days": 2.0,
        "adoption_rate": 0.6,
        "repeat_rate": 0.2,
    }
    assert report.downstream["challenger"]["play_count"] == 1.0
    assert report.guardrail_deltas["novelty"]["value"] == pytest.approx(-0.2)
    assert report.guardrail_deltas["popularity"]["value"] is None
    before = state(conn)
    assert experiment_report(conn, experiment, outcome_cutoff=cutoff) == report
    assert state(conn) == before
    conn.execute("UPDATE recommendation SET knownness = 1, identity_confidence = 0")
    conn.execute("UPDATE artist SET canonical_name = 'Changed'")
    assert (
        experiment_report(conn, experiment, outcome_cutoff=cutoff).guardrail_deltas
        == report.guardrail_deltas
    )
    earlier = experiment_report(conn, experiment, outcome_cutoff=start)
    assert earlier.downstream["control"]["denominator"] == 0
    assert earlier.downstream["control"]["adoption_rate"] is None
    assert experiment_report(conn, experiment, outcome_cutoff=cutoff) == report


def test_report_counts_partial_unjudged_and_abstention_without_fabricating_ties(experiment_db):
    from scrobble_recs.models.experiments import (
        experiment_report,
        next_blinded_exposure,
        record_judgment,
    )

    conn, _ = experiment_db
    seed_runs(conn)
    experiment = create(conn)
    exposure = next_blinded_exposure(conn, experiment_id=experiment, slate_size=1)
    cutoff = datetime(2030, 1, 1)
    assert (
        experiment_report(conn, experiment, outcome_cutoff=cutoff).exposure_counts["unjudged"] == 1
    )
    record_judgment(conn, exposure_id=exposure.exposure_id, labels={1: ["want_to_try"]})
    partial = experiment_report(conn, experiment, outcome_cutoff=cutoff)
    assert partial.exposure_counts["partial"] == 1
    assert partial.list_choices["denominator"] == 0 and partial.list_choices["tie"] == 0
    record_judgment(conn, exposure_id=exposure.exposure_id, list_choice="none")
    abstention = experiment_report(conn, experiment, outcome_cutoff=cutoff)
    assert abstention.list_choices["abstentions"] == 1
    assert abstention.list_choices["denominator"] == 0


def test_equal_clock_uses_submission_order_and_failed_judgment_rolls_back(
    experiment_db, monkeypatch
):
    import scrobble_recs.models.experiments as module

    conn, _ = experiment_db
    seed_runs(conn)
    exposure = module.next_blinded_exposure(conn, experiment_id=create(conn))
    monkeypatch.setattr(module, "_now", lambda: datetime(2025, 1, 1))
    module.record_judgment(conn, exposure_id=exposure.exposure_id, note="first")
    module.record_judgment(conn, exposure_id=exposure.exposure_id, note="second")
    assert (
        module.next_blinded_exposure(conn, experiment_id=exposure.experiment_id).judgment.note
        == "second"
    )
    before = state(conn)

    def fail(*args):
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(module, "_refresh_status", fail)
    with pytest.raises(RuntimeError):
        module.record_judgment(conn, exposure_id=exposure.exposure_id, list_choice="tie")
    assert state(conn) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("taste_region_seeds", [{"artist_id": True, "weight": 1, "regions": []}]),
        ("taste_region_seeds", [{"artist_id": 1, "weight": -1, "regions": []}]),
        ("taste_region_seeds", [{"artist_id": 1, "weight": 1, "regions": "wrong"}]),
        ("evidence_freshness", []),
        ("target_relationship", {"state": "sampled"}),
    ],
)
def test_malformed_common_context_is_not_compatible_provenance(experiment_db, field, value):
    conn, _ = experiment_db
    fixture = seed_runs(conn)
    context = fixture["context"] | {field: value}
    conn.execute("UPDATE recommendation_run SET query_context = ?", [json.dumps(context)])
    with pytest.raises(ValueError):
        create(conn)
    assert not state(conn)[0]


def test_canonical_inputs_ignore_implementation_settings_and_timezone_spelling(experiment_db):
    conn, _ = experiment_db
    fixture = seed_runs(conn)
    context = fixture["context"]
    context["feedback_cutoff"] = "2024-12-31T19:00:00-05:00"
    context["taste_region_seeds"][0]["regions"].reverse()
    context["versions"] = {"ranker": "different-version"}
    context["artist_settings"] = {"profile": "challenger"}
    conn.execute(
        "UPDATE recommendation_run SET query_context = ? WHERE run_id = ?",
        [json.dumps(context), CHALLENGER],
    )
    assert create(conn)


def test_frozen_persisted_diversity_and_provider_guards(experiment_db):
    from scrobble_recs.models.experiments import experiment_report

    conn, _ = experiment_db
    seed_runs(conn)
    conn.execute(
        "UPDATE recommendation SET explanation = ? WHERE run_id = ?",
        ['{"tags":[{"tag":"rock"}]}', CONTROL],
    )
    conn.execute(
        "UPDATE recommendation SET explanation = ? WHERE run_id = ? AND artist_id = 2",
        ['{"tags":[{"tag":"rock"}]}', CHALLENGER],
    )
    conn.execute(
        "UPDATE recommendation SET explanation = ? WHERE run_id = ? AND artist_id = 4",
        ['{"tags":[{"tag":"jazz"}]}', CHALLENGER],
    )
    for run, artist, source in [
        (CONTROL, 1, "lastfm"),
        (CONTROL, 2, "lastfm"),
        (CONTROL, 3, "lastfm"),
        (CHALLENGER, 2, "lastfm"),
        (CHALLENGER, 4, "listenbrainz"),
    ]:
        conn.execute(
            "INSERT INTO recommendation_candidate (run_id, artist_id, evidence) VALUES (?, ?, ?)",
            [run, artist, json.dumps({"fusion": {"source_ranks": {source: 1}}})],
        )
    experiment = create(conn)
    report = experiment_report(conn, experiment, outcome_cutoff=datetime(2030, 1, 1))
    assert report.guardrail_deltas["diversity"]["value"] == 1.0
    assert report.guardrail_deltas["provider_concentration"]["value"] == -0.5
    conn.execute("DELETE FROM recommendation_candidate")
    conn.execute("DELETE FROM recommendation")
    assert experiment_report(conn, experiment, outcome_cutoff=datetime(2030, 1, 1)) == report


def test_repeated_exposure_credit_once_and_atomic_report_recomputation(experiment_db, monkeypatch):
    import scrobble_recs.models.experiments as module

    conn, config = experiment_db
    seed_runs(conn)
    experiment = create(conn)
    module.next_blinded_exposure(conn, experiment_id=experiment)
    conn.execute("UPDATE recommendation_exposure SET created_at = '2025-01-01'")
    conn.execute(
        "INSERT INTO recommendation_exposure SELECT '00000000-0000-0000-0000-000000000800', "
        "experiment_id, TIMESTAMP '2025-01-02', context, shown_items, attribution, NULL "
        "FROM recommendation_exposure"
    )
    conn.execute(
        "INSERT INTO scrobble (scrobble_id, artist_key, played_at) "
        "VALUES (1, 'shared', '2025-01-03')"
    )
    report = module.experiment_report(conn, experiment, outcome_cutoff=datetime(2025, 1, 4))
    assert report.downstream["control"]["adopted"] == 0.5
    assert report.downstream["challenger"]["adopted"] == 0.5
    assert conn.execute(
        "SELECT sum(play_count), count(*) FROM recommendation_downstream_outcome"
    ).fetchone() == (1, 8)
    before = state(conn)
    initialize_database(config).close()
    assert state(conn) == before
    original = module._replace_outcomes

    def fail_after_write(*args):
        original(*args)
        raise RuntimeError("injected outcome failure")

    monkeypatch.setattr(module, "_replace_outcomes", fail_after_write)
    with pytest.raises(RuntimeError):
        module.experiment_report(conn, experiment, outcome_cutoff=datetime(2025, 1, 5))
    assert state(conn) == before


def test_seed_fixture_order_and_new_exposure_are_distinct_from_resume(experiment_db):
    from scrobble_recs.models.experiments import next_blinded_exposure

    conn, _ = experiment_db
    seed_runs(conn)
    experiment = create(conn)
    fresh = next_blinded_exposure(conn, experiment_id=experiment)
    assert [item.artist_id for item in fresh.left] == [1, 2, 3]
    assert [item.artist_id for item in fresh.right] == [4]
    assert fresh.resumed is False
    assert next_blinded_exposure(conn, experiment_id=experiment).resumed is True


def test_duplicate_memberships_keep_all_source_ranks_once_per_variant(experiment_db):
    conn, _ = experiment_db
    seed_runs(conn)
    conn.execute("DROP INDEX recommendation_run_artist_key")
    conn.execute("INSERT INTO recommendation (run_id, rank, artist_id) VALUES (?, 4, 2)", [CONTROL])
    experiment = create(conn)
    definition = json.loads(
        conn.execute(
            "SELECT definition FROM recommendation_experiment WHERE experiment_id = ?", [experiment]
        ).fetchone()[0]
    )
    assert definition["artists"]["2"]["memberships"] == [
        {"variant": "control", "run_id": CONTROL, "ranks": [2, 4]},
        {"variant": "challenger", "run_id": CHALLENGER, "ranks": [1]},
    ]


def test_exposure_write_failure_rolls_back_and_invalid_report_cutoff_is_read_only(experiment_db):
    from scrobble_recs.models.experiments import experiment_report, next_blinded_exposure

    conn, _ = experiment_db
    seed_runs(conn)
    experiment = create(conn)
    before = state(conn)

    class FailStatusConnection:
        def execute(self, query, *args):
            if query.startswith("UPDATE recommendation_experiment"):
                raise RuntimeError("injected exposure failure")
            return conn.execute(query, *args)

    with pytest.raises(RuntimeError):
        next_blinded_exposure(FailStatusConnection(), experiment_id=experiment)
    assert state(conn) == before
    with pytest.raises(ValueError):
        experiment_report(conn, experiment, outcome_cutoff="invalid")
    assert state(conn) == before


def test_artist_query_compatibility_uses_target_relationship_not_scorer_settings(experiment_db):
    from dataclasses import asdict

    from scrobble_recs.config import ArtistRelationshipConfig
    from scrobble_recs.models.profile_window import ListeningWindow
    from scrobble_recs.models.relationship import build_target_relationship_snapshot

    conn, _ = experiment_db
    fixture = seed_runs(conn)
    relationship = asdict(
        build_target_relationship_snapshot(
            conn,
            artist_id=1,
            as_of=datetime(2025, 1, 1),
            history_window=ListeningWindow(datetime(2024, 1, 1), datetime(2025, 1, 1)),
            feedback_cutoff=datetime(2025, 1, 1),
            thresholds=ArtistRelationshipConfig(),
        )
    )
    context = fixture["context"] | {
        "query_kind": "artist",
        "target_artist_id": 1,
        "target_relationship": relationship,
    }
    conn.execute(
        "UPDATE recommendation_run SET query_kind = 'artist', target_artist_id = 1, "
        "query_context = ?",
        [json.dumps(context)],
    )
    create(conn)
    context["target_relationship"] = {}
    conn.execute("UPDATE recommendation_run SET query_context = ?", [json.dumps(context)])
    before = state(conn)
    with pytest.raises(ValueError):
        create(conn)
    assert state(conn) == before


def test_seed_and_attribution_ignore_sql_order_and_global_rng(experiment_db):
    import random

    from scrobble_recs.models.experiments import next_blinded_exposure

    conn, _ = experiment_db
    seed_runs(conn)
    first = create(conn)
    first_definition = json.loads(
        conn.execute(
            "SELECT definition FROM recommendation_experiment WHERE experiment_id = ?", [first]
        ).fetchone()[0]
    )
    first_shown = next_blinded_exposure(conn, experiment_id=first)
    conn.execute(
        "CREATE TEMP TABLE reversed_rows AS SELECT * FROM recommendation ORDER BY rank DESC"
    )
    conn.execute("DELETE FROM recommendation")
    conn.execute("INSERT INTO recommendation SELECT * FROM reversed_rows")
    random.seed(999)
    for _ in range(100):
        random.random()
    second = create(conn)
    second_definition = json.loads(
        conn.execute(
            "SELECT definition FROM recommendation_experiment WHERE experiment_id = ?", [second]
        ).fetchone()[0]
    )
    second_shown = next_blinded_exposure(conn, experiment_id=second)
    assert first_definition == second_definition
    assert first_shown.left == second_shown.left and first_shown.right == second_shown.right


def test_outcomes_normalize_cutoff_to_utc_and_keep_first_timestamp(experiment_db):
    from datetime import timezone

    from scrobble_recs.models.experiments import experiment_report, next_blinded_exposure
    from scrobble_recs.models.profile_window import normalize_timestamp

    conn, _ = experiment_db
    seed_runs(conn)
    experiment = create(conn)
    next_blinded_exposure(conn, experiment_id=experiment)
    conn.execute("UPDATE recommendation_exposure SET created_at = '2025-01-01'")
    for identity, value in enumerate(
        [
            datetime(2025, 1, 1, 23, 30, tzinfo=timezone(timedelta(hours=2))),
            datetime(2025, 1, 2, 3, 0, tzinfo=timezone(timedelta(hours=2))),
        ],
        1,
    ):
        conn.execute(
            "INSERT INTO scrobble (scrobble_id, artist_key, played_at) VALUES (?, 'shared', ?)",
            [identity, normalize_timestamp(value)],
        )
    report = experiment_report(conn, experiment, outcome_cutoff=datetime(2025, 1, 3, tzinfo=UTC))
    assert report.downstream["control"]["artist_active_days"] == 1.0
    first, plays, days = conn.execute(
        "SELECT first_listened_at, play_count, active_days "
        "FROM recommendation_downstream_outcome WHERE artist_id = 2"
    ).fetchone()
    assert (first, plays, days) == (datetime(2025, 1, 1, 21, 30), 2, 2)
