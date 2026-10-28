"""Local preference contracts: explicit comparisons, deterministic fit and gated use."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from importlib import import_module, util
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_experiments import CHALLENGER, CONTROL, seed_runs
from test_promotion import evidence as evidence
from test_recommend import write_config

from scrobble_recs.config import load_config
from scrobble_recs.db import apply_migrations, initialize_database


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    import httpx

    def blocked(*args, **kwargs):
        raise AssertionError("preference workflows must not access HTTP")

    monkeypatch.setattr(httpx.Client, "send", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)


@pytest.fixture
def corpus_db(tmp_path):
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    fixture = json.loads((Path(__file__).parent / "fixtures/preference_judgments.json").read_text())
    seed_runs(conn)
    for run in (CONTROL, CHALLENGER):
        context = json.loads(
            conn.execute(
                "SELECT query_context FROM recommendation_run WHERE run_id = ?", [run]
            ).fetchone()[0]
        )
        context["versions"]["feature_schema"] = "fusion-features-v1"
        conn.execute(
            "UPDATE recommendation_run SET created_at = ?, query_context = ? WHERE run_id = ?",
            [fixture["source_time"], json.dumps(context), run],
        )
        for artist in (1, 2, 3, 4):
            conn.execute(
                "INSERT INTO recommendation_candidate "
                "(run_id, artist_id, feature_record, relevance_score) VALUES (?, ?, ?, ?)",
                [run, artist, json.dumps(features(tag_similarity_score=artist / 10)), artist / 10],
            )
    try:
        yield conn, config
    finally:
        conn.close()


def exposure(
    conn,
    *,
    identity=1,
    left=(1,),
    right=(4,),
    day=3,
    preferred="left",
    labels=None,
    list_choice=None,
):
    from scrobble_recs.models.experiments import create_blinded_experiment

    eid = str(UUID(int=identity))
    experiment = create_blinded_experiment(
        conn, control_run_id=CONTROL, challenger_run_id=CHALLENGER, randomization_seed=7
    )
    definition = json.loads(
        conn.execute(
            "SELECT definition FROM recommendation_experiment WHERE experiment_id = ?", [experiment]
        ).fetchone()[0]
    )
    definition["side_variants"] = {"left": "control", "right": "challenger"}
    conn.execute(
        "UPDATE recommendation_experiment SET definition = ? WHERE experiment_id = ?",
        [json.dumps(definition), experiment],
    )
    shown = [
        {"artist_id": aid, "side": side, "position": i}
        for side, ids in (("left", left), ("right", right))
        for i, aid in enumerate(ids, 1)
    ]
    attribution = {
        str(aid): [{"variant": variant, "run_id": run, "ranks": [1]}]
        for variant, run, ids in (("control", CONTROL, left), ("challenger", CHALLENGER, right))
        for aid in ids
    }
    conn.execute(
        "INSERT INTO recommendation_exposure (exposure_id, experiment_id, created_at, "
        "context, shown_items, attribution) VALUES (?, ?, '2025-01-02', ?, ?, ?)",
        [
            eid,
            experiment,
            '{"task":"general","target_artist_id":null}',
            json.dumps(shown),
            json.dumps(attribution),
        ],
    )
    judgment(
        conn,
        eid,
        identity=identity * 100,
        day=day,
        preferred=preferred,
        labels=labels,
        list_choice=list_choice,
    )
    return eid


def judgment(conn, eid, *, identity, day, preferred=None, labels=None, list_choice=None):
    conn.execute(
        "INSERT INTO recommendation_judgment VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, NULL)",
        [
            str(UUID(int=identity)),
            eid,
            datetime(2025, 1, day),
            preferred,
            list_choice,
            json.dumps(labels or {}),
        ],
    )


def preference():
    assert util.find_spec("scrobble_recs.models.preference") is not None
    return import_module("scrobble_recs.models.preference")


def test_conservative_config_rejects_invalid_training_gates():
    from scrobble_recs.config import RecommendationConfig, default_config_document

    config = RecommendationConfig()
    assert default_config_document()["recommendation"].get("preference") == {
        "enabled": False,
        "minimum_training_pairs": 50,
        "minimum_validation_pairs": 10,
        "l2_regularization": 1.0,
        "evaluation_artifact_id": None,
    }
    assert hasattr(config, "preference")
    assert config.preference.model_dump() == {
        "enabled": False,
        "minimum_training_pairs": 50,
        "minimum_validation_pairs": 10,
        "l2_regularization": 1.0,
        "evaluation_artifact_id": None,
    }
    for change in (
        {"minimum_training_pairs": 0},
        {"minimum_validation_pairs": True},
        {"l2_regularization": float("nan")},
        {"l2_regularization": 0},
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            type(config.preference)(**change)


def features(**updates):
    return {
        "schema_version": "fusion-features-v1",
        "values": {
            "tag_similarity_score": 0.5,
            "feedback_score": 0.0,
            "identity_confidence": 0.8,
            "knownness": 0.0,
            "fusion_family_collaborative": 0.8,
            **updates,
        },
        "missing_signals": [],
    }


def test_encoder_caps_family_and_uses_candidate_varying_context():
    p = preference()
    general = {"query_kind": "general"}
    artist = {
        "query_kind": "artist",
        "target_artist_id": 1,
        "target_relationship": {"play_count": 10, "recent_trend": 0.5},
    }
    a = p.encode_features(features(), general)
    b = p.encode_features(
        features(fusion_source_lastfm=999, fusion_source_listenbrainz=999), general
    )
    assert a == b
    capped = p.encode_features(features(fusion_family_collaborative=99), general)
    assert capped[p.FEATURE_NAMES.index("collaborative")] == 1
    assert p.encode_features(features(), artist) != a
    assert p.encode_features(features(), artist) != p.encode_features(
        features(), {**artist, "target_relationship": {"play_count": 100, "recent_trend": 0.5}}
    )
    for bad in (
        features(tag_similarity_score=None),
        features(feedback_score=float("inf")),
        {**features(), "schema_version": "legacy-features-v1"},
    ):
        with pytest.raises(ValueError):
            p.encode_features(bad, general)


def examples():
    p = preference()
    return tuple(
        p.PairwiseExample(
            str(i),
            2,
            3,
            (1.0,) + (0.0,) * (len(p.FEATURE_NAMES) - 1),
            datetime(2025, 1, 1) + timedelta(days=i),
            "general",
            control_difference=-0.5,
        )
        for i in range(8)
    )


def test_fit_is_repeatable_symmetric_zero_intercept_and_rejects_bad_input(monkeypatch):
    p = preference()
    args = dict(
        feature_schema_version=p.FEATURE_SCHEMA,
        feature_names=p.FEATURE_NAMES,
        l2_regularization=1.0,
        training_cutoff=datetime(2026, 1, 1),
    )
    a = p.train_pairwise_ranker(examples(), **args)
    b = p.train_pairwise_ranker(tuple(reversed(examples())), **args)
    assert a.coefficients[0] > 0
    assert a.intercept == 0
    assert a.coefficients == b.coefficients
    assert a.artifact_hash == b.artifact_hash
    assert a.artifact_hash != p.train_pairwise_ranker(examples()[:-1], **args).artifact_hash
    vector = examples()[0].feature_difference
    assert p.pair_probability(a, vector) + p.pair_probability(a, tuple(-x for x in vector)) == 1
    with pytest.raises(ValueError):
        p.train_pairwise_ranker(
            [replace(examples()[0], feature_difference=(float("nan"),))], **args
        )
    monkeypatch.setattr(p, "minimize", lambda *a, **kw: type("Failed", (), {"success": False})())
    with pytest.raises(ValueError, match="optimizer"):
        p.train_pairwise_ranker(examples(), **args)


def test_holdout_expands_timestamp_ties_and_whole_exposures():
    p = preference()
    data = list(examples())
    data[-2] = replace(data[-2], judged_at=data[-1].judged_at)
    data.append(replace(data[-1], preferred_artist_id=4))
    train, valid = p.split_examples(data, validation_count=1)
    assert len(train) == 6
    assert len(valid) == 3
    assert {x.exposure_id for x in train}.isdisjoint(x.exposure_id for x in valid)
    metrics = p.validation_metrics(
        p.train_pairwise_ranker(
            train,
            feature_schema_version=p.FEATURE_SCHEMA,
            feature_names=p.FEATURE_NAMES,
            l2_regularization=1,
            training_cutoff=datetime(2026, 1, 1),
        ),
        valid,
    )
    assert metrics["learned_accuracy"] == 1
    assert metrics["control_accuracy"] == 0
    assert metrics["learned_log_loss"] < metrics["control_log_loss"]


def test_pairs_use_cutoff_replacements_and_exact_displayed_source(corpus_db):
    p = preference()
    conn, _ = corpus_db
    eid = exposure(conn, labels={"1": ["strong_fit"], "4": ["reject"]})
    judgment(conn, eid, identity=101, day=4, preferred="right")
    judgment(conn, eid, identity=102, day=5)
    args = dict(feature_names=p.FEATURE_NAMES)
    first = p.build_pairwise_examples(conn, training_cutoff=datetime(2025, 1, 3), **args)
    assert [(x.preferred_artist_id, x.other_artist_id) for x in first] == [(1, 4)]
    assert first[0].feature_difference[0] == pytest.approx(-0.3)
    assert first[0].control_difference == pytest.approx(-0.3)
    assert first[0].provenance["source_runs"] == [CONTROL, CHALLENGER]
    later = p.build_pairwise_examples(conn, training_cutoff=datetime(2025, 1, 4), **args)
    assert [(x.preferred_artist_id, x.other_artist_id) for x in later] == [(4, 1)]
    assert p.build_pairwise_examples(conn, training_cutoff=datetime(2025, 1, 5), **args) == ()
    # A different run's feature cannot repair the missing owning-side feature.
    conn.execute(
        "DELETE FROM recommendation_candidate WHERE run_id = ? AND artist_id = 1", [CONTROL]
    )
    assert p.build_pairwise_examples(conn, training_cutoff=datetime(2025, 1, 3), **args) == ()


def test_pairs_never_expand_list_wins_or_conflicting_neutral_unshown_labels(corpus_db):
    p = preference()
    conn, _ = corpus_db
    exposure(conn, identity=1, left=(1, 2), right=(3, 4), preferred=None, list_choice="left")
    exposure(conn, identity=2, preferred="left", labels={"4": ["strong_fit"], "1": ["reject"]})
    exposure(conn, identity=3, preferred=None, labels={"1": ["already_known"], "4": ["reject"]})
    exposure(
        conn, identity=4, preferred=None, labels={"1": ["strong_fit", "reject"], "4": ["reject"]}
    )
    exposure(conn, identity=5, preferred=None, labels={"5": ["strong_fit"], "4": ["reject"]})
    exposure(
        conn,
        identity=6,
        left=(1, 2),
        right=(3, 4),
        preferred="tie",
        labels={"2": ["surprising_good"], "3": ["wrong_direction"]},
    )
    pairs = p.build_pairwise_examples(
        conn, training_cutoff=datetime(2025, 1, 9), feature_names=p.FEATURE_NAMES
    )
    assert [(x.preferred_artist_id, x.other_artist_id) for x in pairs] == [(2, 3)]


def test_migration_and_training_never_activate_and_report_insufficient_scope(corpus_db):
    p = preference()
    conn, config = corpus_db
    assert (
        conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'preference_model_artifact'"
        ).fetchone()[0]
        == 1
    )
    apply_migrations(conn)
    apply_migrations(conn)
    for i in range(1, 5):
        exposure(conn, identity=i, day=i + 2, preferred="right")
    config.recommendation.preference.minimum_training_pairs = 2
    config.recommendation.preference.minimum_validation_pairs = 1
    artifact = p.train_preference(
        conn, config=config, training_cutoff=datetime(2026, 1, 1), scope="shared"
    )
    report = p.preference_report(conn, artifact.artifact_id)
    assert report["status"] == "candidate"
    assert report["training_example_count"] == 3
    assert report["validation_example_count"] == 1
    assert report["promotion_decision"]["eligible"] is False
    assert (
        conn.execute(
            "SELECT count(*) FROM preference_lifecycle_event WHERE action = 'create'"
        ).fetchone()[0]
        == 1
    )
    with pytest.raises(ValueError, match="shared"):
        p.train_preference(
            conn, config=config, training_cutoff=datetime(2026, 1, 1), scope="general"
        )


def trained(conn, config):
    p = preference()
    for i in range(1, 5):
        exposure(conn, identity=i, day=i + 2, preferred="left")
    config.recommendation.preference.minimum_training_pairs = 2
    config.recommendation.preference.minimum_validation_pairs = 1
    return p.train_preference(conn, config=config, training_cutoff=datetime(2026, 1, 1))


def test_activation_rechecks_evidence_and_rollback_is_durable(corpus_db):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    with pytest.raises(ValueError, match="promotion"):
        p.activate_preference(conn, artifact_id=artifact.artifact_id, config=config)
    conn.execute("UPDATE preference_model_artifact SET promotion_decision = '{\"eligible\":true}'")
    with pytest.raises(ValueError, match="promotion"):
        p.activate_preference(conn, artifact_id=artifact.artifact_id, config=config)
    assert p.rollback_preference(conn, scope="general")["retired_count"] == 0
    assert conn.execute(
        "SELECT suppressed FROM preference_scope_selection WHERE query_scope='general'"
    ).fetchone() == (True,)
    assert p.rollback_preference(conn, scope="general")["retired_count"] == 0
    assert (
        p.load_preference_artifact(conn, artifact.artifact_id).artifact_hash
        == artifact.artifact_hash
    )


@pytest.mark.parametrize(
    "column,value",
    [
        ("coefficients", "[0]"),
        ("feature_names", '["unknown"]'),
        ("feature_schema_version", "wrong"),
        ("artifact_hash", "bad"),
        ("intercept", 1),
        ("regularization", -1),
        ("training_example_count", 0),
        ("status", "garbage"),
        ("validation_metrics", '{"learned_accuracy":99}'),
    ],
)
def test_artifact_integrity_rejects_corruption(corpus_db, column, value):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    conn.execute(f"UPDATE preference_model_artifact SET {column} = ?", [value])
    with pytest.raises(ValueError):
        p.load_preference_artifact(conn, artifact.artifact_id)


def test_promotion_requires_both_tasks_and_exact_candidate_identity(corpus_db, evidence):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    report = p.attest_preference(
        conn, artifact_id=artifact.artifact_id, config=config, evidence={"general": evidence}
    )
    assert not report["eligible"]
    assert "missing_artist_promotion" in report["reasons"]
    assert "artifact_link_mismatch" in report["reasons"]
    assert p.preference_report(conn, artifact.artifact_id)["status"] == "candidate"


def live_config(conn, config):
    config.recommendation.scoring.engine = "fusion"
    config.recommendation.fusion.enabled = True
    config.recommendation.fatigue.enabled = False
    config.recommendation.obviousness.enabled = False
    config.recommendation.diversity.enabled = False
    conn.execute(
        "UPDATE artist SET identity_confidence = 0.99, resolution_status='resolved_musicbrainz'"
    )
    conn.execute(
        "INSERT INTO scrobble (scrobble_id, played_at, played_date, artist_key, track_key) VALUES "
        "(1, '2024-12-01', '2024-12-01', 'unexposed', 'a'), "
        "(2, '2024-12-02', '2024-12-02', 'unexposed', 'b')"
    )
    conn.execute(
        "INSERT INTO artist_similarity_edge (source_artist_id, target_artist_id, source, "
        "normalized_score, fetched_at) VALUES (5, 1, 'lastfm', 0.9, '2024-12-03'), "
        "(5, 4, 'lastfm', 0.6, '2024-12-03')"
    )
    conn.execute(
        "INSERT INTO artist_tag (artist_id, tag, source, normalized_weight, fetched_at) VALUES "
        "(5, 'rock', 'lastfm', 1, '2024-12-03'), (4, 'rock', 'lastfm', 1, '2024-12-03')"
    )


def run_local(conn, config, kind="general", *, target_artist_id=5):
    from scrobble_recs.models.profile_window import ListeningWindow
    from scrobble_recs.models.recommend import generate_recommendations
    from scrobble_recs.models.recommendation_query import RecommendationRequest

    return generate_recommendations(
        conn,
        config,
        limit=5,
        as_of=datetime(2025, 2, 1),
        historical_cutoff=datetime(2025, 2, 1),
        history_window=ListeningWindow(datetime(2024, 1, 1), datetime(2025, 2, 1)),
        request=RecommendationRequest(kind, target_artist_id if kind == "artist" else None),
    ).run_id


def fingerprint(conn, run):
    return {
        "candidates": conn.execute(
            "SELECT artist_id, candidate_score, relevance_score, policy_adjustment, "
            "eligibility_state, admission, policy_effects FROM recommendation_candidate "
            "WHERE run_id = ? ORDER BY artist_id",
            [run],
        ).fetchall(),
        "selected": conn.execute(
            "SELECT artist_id, rank, final_score FROM recommendation "
            "WHERE run_id = ? ORDER BY rank",
            [run],
        ).fetchall(),
    }


@pytest.mark.parametrize("kind", ["general", "artist"])
def test_candidate_evaluation_changes_actual_slate_without_activation(corpus_db, kind):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    control = run_local(conn, config, kind)
    config.recommendation.preference.evaluation_artifact_id = artifact.artifact_id
    evaluated = run_local(conn, config, kind)
    assert fingerprint(conn, evaluated)["selected"][0][0] == 1
    assert fingerprint(conn, control)["selected"][0][0] == 4
    summary = json.loads(
        conn.execute(
            "SELECT seed_summary FROM recommendation_run WHERE run_id = ?", [evaluated]
        ).fetchone()[0]
    )
    assert summary["preference"]["use"] == "evaluation"
    assert summary["evaluation_artifact"]["preference"]["artifact_hash"] == artifact.artifact_hash
    assert p.preference_report(conn, artifact.artifact_id)["status"] == "candidate"
    for (payload,) in conn.execute(
        "SELECT explanation FROM recommendation WHERE run_id = ?", [evaluated]
    ).fetchall():
        assert json.loads(payload)["preference"]["used"]


@pytest.mark.parametrize("kind", ["general", "artist"])
@pytest.mark.parametrize("state", ["missing", "candidate", "corrupt", "rollback", "unsupported"])
def test_runwide_fallback_preserves_control_scores_policy_and_order(corpus_db, kind, state):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    if state == "unsupported":
        config.recommendation.scoring.engine = "legacy"
    control = run_local(conn, config, kind)
    config.recommendation.preference.enabled = True
    if state == "corrupt":
        config.recommendation.preference.evaluation_artifact_id = artifact.artifact_id
        conn.execute("UPDATE preference_model_artifact SET coefficients = '[0]'")
    if state == "rollback":
        p.rollback_preference(conn, scope=kind)
    if state == "missing":
        conn.execute("DELETE FROM preference_model_artifact")
    fallback = run_local(conn, config, kind)
    assert fingerprint(conn, fallback) == fingerprint(conn, control)
    summary = json.loads(
        conn.execute(
            "SELECT seed_summary FROM recommendation_run WHERE run_id = ?", [fallback]
        ).fetchone()[0]
    )
    assert summary["preference"]["ranker_fallback_reason"]
    assert not summary["evaluation_artifact"].get("preference")


def test_manifest_binds_coefficients_and_fallback_effective_identity(corpus_db):
    from scrobble_recs.models.evaluation_identity import artifact_manifest

    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    versions = {
        "ranker": "preference-v1",
        "feature_schema": "fusion-features-v1",
        "slate_policy": "legacy-mmr-v1",
    }
    identity = p.preference_identity(artifact)
    a = artifact_manifest(config, versions, preference=identity)
    b = artifact_manifest(config, versions, preference={**identity, "artifact_hash": "other"})
    assert a["artifact_id"] != b["artifact_id"]
    assert a["content_fingerprint"] == b["content_fingerprint"]


def linked_evidence(evidence, config, artifact):
    from scrobble_recs.models.evaluation_identity import artifact_manifest
    from scrobble_recs.models.rolling_compare import _scoped_ablation

    p = preference()
    general = deepcopy(evidence)
    versions = {
        "ranker": "fusion-v1",
        "feature_schema": "fusion-features-v1",
        "slate_policy": "legacy-mmr-v1",
    }
    control = artifact_manifest(config, versions)
    challenger = artifact_manifest(
        config, {**versions, "ranker": "preference-v1"}, preference=p.preference_identity(artifact)
    )
    for pair in general["comparison"]["pairs"]:
        pair["control"]["artifact"] = deepcopy(control)
        pair["challenger"]["artifact"] = deepcopy(challenger)
        for row in pair["ablations"]:
            row["baseline_artifact"] = deepcopy(challenger)
            row["metrics"]["artifact"] = artifact_manifest(
                _scoped_ablation(config, row["ablation_id"]),
                {**versions, "ranker": "preference-v1"},
                preference=p.preference_identity(artifact),
            )
        for variant in [
            pair["control"],
            pair["challenger"],
            *[row["metrics"] for row in pair["ablations"]],
        ]:
            for key in ("artifact_files", "artifact_bytes"):
                variant["storage"][key] = variant["artifact"][key]
    general["blinded_experiments"][0]["definition"]["source_artifacts"] = {
        "control": control,
        "challenger": challenger,
    }
    rollback = general["reversibility"]
    rollback.update(
        control_artifact=control,
        challenger_artifact=challenger,
        compatible_feature_schema="fusion-features-v1",
    )
    rollback["restore_operation"]["artifact"] = control
    rollback["verification"]["restored_artifact"] = control
    artist = deepcopy(general)
    relationship = dict(
        state="unheard",
        play_count=0,
        recency_weighted_play_count=0,
        unique_tracks=0,
        unique_releases=0,
        active_days=0,
        active_months=0,
        first_listen=None,
        last_listen=None,
        listening_span_days=0,
        recent_trend=0,
        replay_concentration=0,
        positive_feedback=0,
        negative_feedback=0,
        prior_recommendation_exposures=0,
    )
    comparison = artist["comparison"]
    comparison.update(evaluation_task="artist", target_artist_ids=[5])
    for pair in comparison["pairs"]:
        pair["key"].update(evaluation_task="artist", target_artist_id=5)
        for variant in [
            pair["control"],
            pair["challenger"],
            *[row["metrics"] for row in pair["ablations"]],
        ]:
            variant["result"].update(evaluation_task="artist", target_artist_id=5)
            variant["source_context"].update(
                query_kind="artist", target_artist_id=5, target_relationship=deepcopy(relationship)
            )
        for row in pair["ablations"]:
            row.update(evaluation_task="artist", target_artist_id=5)
    experiment = artist["blinded_experiments"][0]
    experiment["task"] = "artist"
    experiment["definition"]["target_artist_id"] = 5
    for context in experiment["definition"]["source_contexts"].values():
        context.update(
            query_kind="artist", target_artist_id=5, target_relationship=deepcopy(relationship)
        )
    return {"general": general, "artist": artist}


def test_real_promotion_activation_rollback_and_history(corpus_db, evidence):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    proof = linked_evidence(evidence, config, artifact)
    decision = p.attest_preference(
        conn, artifact_id=artifact.artifact_id, config=config, evidence=proof
    )
    assert decision["eligible"], decision
    p.activate_preference(conn, artifact_id=artifact.artifact_id, config=config)
    assert p.preference_report(conn, artifact.artifact_id)["status"] == "active"
    config.recommendation.preference.enabled = True
    active = run_local(conn, config)
    assert fingerprint(conn, active)["selected"][0][0] == 1
    before = conn.execute(
        "SELECT explanation FROM recommendation WHERE run_id=? ORDER BY rank", [active]
    ).fetchall()
    # A scoped rollback must suppress an otherwise active shared fallback.
    p.rollback_preference(conn, scope="general")
    rolled_back = run_local(conn, config)
    assert fingerprint(conn, rolled_back)["selected"][0][0] == 4
    assert p.preference_report(conn, artifact.artifact_id)["status"] == "active"
    assert (
        conn.execute(
            "SELECT explanation FROM recommendation WHERE run_id=? ORDER BY rank", [active]
        ).fetchall()
        == before
    )
    assert fingerprint(conn, run_local(conn, config, "artist"))["selected"][0][0] == 1
    p.rollback_preference(conn, scope="shared")
    assert p.preference_report(conn, artifact.artifact_id)["status"] == "retired"


@pytest.mark.parametrize(
    "mutation",
    ["content", "configuration", "scope", "coefficient_hash", "feature_order", "no_artist"],
)
def test_other_candidate_configuration_or_task_cannot_authorize_activation(
    corpus_db, evidence, mutation
):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    proof = linked_evidence(evidence, config, artifact)
    if mutation == "no_artist":
        del proof["artist"]
    else:
        for task in proof.values():
            for pair in task["comparison"]["pairs"]:
                manifest = pair["challenger"]["artifact"]
                if mutation == "content":
                    manifest["content_fingerprint"] = "another-codebase"
                elif mutation == "configuration":
                    config.recommendation.weights.feedback_model = 0.333
                elif mutation == "scope":
                    manifest["preference"]["query_scope"] = "general"
                elif mutation == "coefficient_hash":
                    manifest["preference"]["artifact_hash"] = "other"
                else:
                    manifest["preference"]["feature_names"].reverse()
    decision = p.attest_preference(
        conn, artifact_id=artifact.artifact_id, config=config, evidence=proof
    )
    assert not decision["eligible"]
    with pytest.raises(ValueError):
        p.activate_preference(conn, artifact_id=artifact.artifact_id, config=config)


def test_activation_audit_failure_rolls_back_status_and_scope(corpus_db, evidence, monkeypatch):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    assert p.attest_preference(
        conn,
        artifact_id=artifact.artifact_id,
        config=config,
        evidence=linked_evidence(evidence, config, artifact),
    )["eligible"]
    before = conn.execute("SELECT * FROM preference_lifecycle_event ORDER BY ALL").fetchall()

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(p, "_audit", fail)
    with pytest.raises(RuntimeError):
        p.activate_preference(conn, artifact_id=artifact.artifact_id, config=config)
    assert p.preference_report(conn, artifact.artifact_id)["status"] == "candidate"
    assert conn.execute("SELECT * FROM preference_scope_selection").fetchall() == []
    assert (
        conn.execute("SELECT * FROM preference_lifecycle_event ORDER BY ALL").fetchall() == before
    )


def test_evaluation_requires_explicit_candidate_and_never_loads_active_state(corpus_db):
    from scrobble_recs.models.evaluate import evaluate_recommendations

    conn, config = corpus_db
    trained(conn, config)
    live_config(conn, config)
    config.recommendation.preference.enabled = True
    result = evaluate_recommendations(
        conn,
        config,
        train_until=datetime(2025, 2, 1),
        test_from=datetime(2025, 2, 1),
        test_until=datetime(2025, 3, 1),
    )
    summary = json.loads(
        conn.execute(
            "SELECT seed_summary FROM recommendation_run WHERE run_id=?", [result.run_id]
        ).fetchone()[0]
    )
    assert "preference" not in summary
    assert config.recommendation.preference.enabled is True


def test_cli_train_report_reject_activation_and_rollback_are_local(corpus_db, tmp_path):
    from typer.testing import CliRunner

    from scrobble_recs.cli import app

    conn, config = corpus_db
    for i in range(1, 5):
        exposure(conn, identity=i, day=i + 2)
    config.recommendation.preference.minimum_training_pairs = 2
    config.recommendation.preference.minimum_validation_pairs = 1
    path = tmp_path / "preference-config.json"
    path.write_text(config.model_dump_json())
    runner = CliRunner()
    train = runner.invoke(
        app,
        [
            "preference-train",
            "-c",
            str(path),
            "--training-cutoff",
            "2026-01-01",
            "--scope",
            "shared",
        ],
    )
    assert train.exit_code == 0, train.output
    artifact_id = json.loads(train.output)["artifact_id"]
    report = runner.invoke(
        app, ["preference-report", "-c", str(path), "--artifact-id", artifact_id]
    )
    assert report.exit_code == 0, report.output
    assert json.loads(report.output)["status"] == "candidate"
    activation = runner.invoke(
        app, ["preference-activate", "-c", str(path), "--artifact-id", artifact_id]
    )
    assert activation.exit_code != 0
    rollback = runner.invoke(app, ["preference-rollback", "-c", str(path), "--scope", "shared"])
    assert rollback.exit_code == 0
    assert json.loads(rollback.output)["effective_ranker"] == "deterministic_control"
    unknown = runner.invoke(app, ["preference-report", "-c", str(path), "--artifact-id", "unknown"])
    assert unknown.exit_code != 0


def test_partial_bad_feature_reverts_entire_run(corpus_db, monkeypatch):
    from scrobble_recs.models import recommend

    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    control = run_local(conn, config)
    original = recommend.rank_candidates

    def missing_feature(*args, **kwargs):
        result = original(*args, **kwargs)
        item = result[-1]
        return (
            *result[:-1],
            replace(
                item,
                features=replace(
                    item.features,
                    values={
                        k: v for k, v in item.features.values.items() if k != "identity_confidence"
                    },
                ),
            ),
        )

    monkeypatch.setattr(recommend, "rank_candidates", missing_feature)
    config.recommendation.preference.evaluation_artifact_id = artifact.artifact_id
    fallback = run_local(conn, config)
    assert fingerprint(conn, control) == fingerprint(conn, fallback)
    assert p.preference_report(conn, artifact.artifact_id)["status"] == "candidate"


def test_frozen_blinded_manifest_cannot_claim_a_different_model(corpus_db, evidence):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    proof = linked_evidence(evidence, config, artifact)
    proof["general"]["blinded_experiments"][0]["definition"]["source_artifacts"]["challenger"][
        "preference"
    ]["artifact_hash"] = "different-model"
    assert not p.attest_preference(
        conn, artifact_id=artifact.artifact_id, config=config, evidence=proof
    )["eligible"]


def test_successful_replacement_is_atomic_and_unknown_status_conflicts_fallback(
    corpus_db, evidence
):
    p = preference()
    conn, config = corpus_db
    first = trained(conn, config)
    live_config(conn, config)
    p.attest_preference(
        conn,
        artifact_id=first.artifact_id,
        config=config,
        evidence=linked_evidence(evidence, config, first),
    )
    p.activate_preference(conn, artifact_id=first.artifact_id, config=config)
    exposure(conn, identity=10, day=10)
    second = p.train_preference(conn, config=config, training_cutoff=datetime(2026, 1, 1))
    p.attest_preference(
        conn,
        artifact_id=second.artifact_id,
        config=config,
        evidence=linked_evidence(evidence, config, second),
    )
    p.activate_preference(conn, artifact_id=second.artifact_id, config=config)
    assert p.preference_report(conn, first.artifact_id)["status"] == "retired"
    assert p.preference_report(conn, second.artifact_id)["status"] == "active"
    control = run_local(conn, config)
    config.recommendation.preference.enabled = True
    # Simulate storage corruption, never use this route to establish valid activation.
    conn.execute(
        "UPDATE preference_model_artifact SET status='active' WHERE artifact_id=?",
        [first.artifact_id],
    )
    fallback = run_local(conn, config)
    assert fingerprint(conn, fallback) == fingerprint(conn, control)
    summary = json.loads(
        conn.execute(
            "SELECT seed_summary FROM recommendation_run WHERE run_id=?", [fallback]
        ).fetchone()[0]
    )
    assert summary["preference"]["ranker_fallback_reason"] == "conflicting_active_artifacts"


@pytest.mark.parametrize(
    "corruption",
    [
        "nan",
        "infinity",
        "malformed",
        "duplicate_feature",
        "wrong_order",
        "wrong_scope",
        "missing_table",
    ],
)
def test_corrupt_candidate_attempt_finishes_with_finite_control(corpus_db, corruption):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    control = run_local(conn, config)
    config.recommendation.preference.evaluation_artifact_id = artifact.artifact_id
    if corruption in ("nan", "infinity"):
        value = "NaN" if corruption == "nan" else "Infinity"
        conn.execute(
            "UPDATE preference_model_artifact SET coefficients=?",
            ["[" + ",".join([value] * len(p.FEATURE_NAMES)) + "]"],
        )
    elif corruption == "malformed":
        conn.execute("ALTER TABLE preference_model_artifact ALTER coefficients TYPE VARCHAR")
        conn.execute("UPDATE preference_model_artifact SET coefficients='{' ")
    elif corruption in ("duplicate_feature", "wrong_order"):
        names = list(p.FEATURE_NAMES)
        if corruption == "duplicate_feature":
            names[0] = names[1]
        else:
            names.reverse()
        conn.execute("UPDATE preference_model_artifact SET feature_names=?", [json.dumps(names)])
    elif corruption == "wrong_scope":
        conn.execute("UPDATE preference_model_artifact SET query_scope='artist'")
    else:
        conn.execute("DROP TABLE preference_model_artifact")
    assert fingerprint(conn, run_local(conn, config)) == fingerprint(conn, control)


def test_learned_explanation_reports_the_actual_personal_score(corpus_db):
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    config.recommendation.preference.evaluation_artifact_id = artifact.artifact_id
    run = run_local(conn, config)
    for raw, score in conn.execute(
        "SELECT r.explanation, c.relevance_score FROM recommendation r "
        "JOIN recommendation_candidate c USING (run_id, artist_id) WHERE r.run_id=?",
        [run],
    ).fetchall():
        assert json.loads(raw)["personal_relevance"]["score"] == score


def test_split_normalizes_utc_aware_and_naive_judgment_times():
    from datetime import UTC

    p = preference()
    data = list(examples())
    data[-1] = replace(data[-1], judged_at=data[-1].judged_at.replace(tzinfo=UTC))
    train, valid = p.split_examples(data, validation_count=1)
    assert len(train) == 7 and len(valid) == 1


def test_source_schema_must_match_frozen_run_schema(corpus_db):
    p = preference()
    conn, _ = corpus_db
    eid = exposure(conn)
    context = json.loads(
        conn.execute(
            "SELECT query_context FROM recommendation_run WHERE run_id=?", [CONTROL]
        ).fetchone()[0]
    )
    context["versions"]["feature_schema"] = "legacy-features-v1"
    conn.execute(
        "UPDATE recommendation_run SET query_context=? WHERE run_id=?",
        [json.dumps(context), CONTROL],
    )
    experiment = conn.execute(
        "SELECT experiment_id FROM recommendation_exposure WHERE exposure_id=?", [eid]
    ).fetchone()[0]
    definition = json.loads(
        conn.execute(
            "SELECT definition FROM recommendation_experiment WHERE experiment_id=?", [experiment]
        ).fetchone()[0]
    )
    definition["source_contexts"]["control"] = context
    conn.execute(
        "UPDATE recommendation_experiment SET definition=? WHERE experiment_id=?",
        [json.dumps(definition), experiment],
    )
    assert (
        p.build_pairwise_examples(
            conn, training_cutoff=datetime(2026, 1, 1), feature_names=p.FEATURE_NAMES
        )
        == ()
    )


def artist_exposure(conn, identity, day):
    eid = exposure(conn, identity=identity, day=day)
    experiment = conn.execute(
        "SELECT experiment_id FROM recommendation_exposure WHERE exposure_id=?", [eid]
    ).fetchone()[0]
    definition = json.loads(
        conn.execute(
            "SELECT definition FROM recommendation_experiment WHERE experiment_id=?", [experiment]
        ).fetchone()[0]
    )
    attribution = json.loads(
        conn.execute(
            "SELECT attribution FROM recommendation_exposure WHERE exposure_id=?", [eid]
        ).fetchone()[0]
    )
    for variant, source in (("control", CONTROL), ("challenger", CHALLENGER)):
        run = str(UUID(int=10000 + identity * 2 + (variant == "challenger")))
        context = deepcopy(definition["source_contexts"][variant])
        context.update(
            query_kind="artist",
            target_artist_id=5,
            target_relationship=json.loads(
                (Path(__file__).parent / "fixtures/preference_judgments.json").read_text()
            )["relationship"],
        )
        conn.execute(
            "INSERT INTO recommendation_run SELECT * REPLACE (?::UUID AS run_id, "
            "?::JSON AS query_context, 'artist' AS query_kind, 5 AS target_artist_id) "
            "FROM recommendation_run WHERE run_id=?",
            [run, json.dumps(context), source],
        )
        conn.execute(
            "INSERT INTO recommendation_candidate SELECT * REPLACE (?::UUID AS run_id) "
            "FROM recommendation_candidate WHERE run_id=?",
            [run, source],
        )
        definition["source_run_ids"][variant] = run
        definition["source_contexts"][variant] = context
        for memberships in attribution.values():
            for membership in memberships:
                if membership["variant"] == variant:
                    membership["run_id"] = run
    definition["target_artist_id"] = 5
    conn.execute(
        "UPDATE recommendation_experiment SET task='artist', definition=? WHERE experiment_id=?",
        [json.dumps(definition), experiment],
    )
    conn.execute(
        "UPDATE recommendation_exposure SET context=?, attribution=? WHERE exposure_id=?",
        ['{"task":"artist","target_artist_id":5}', json.dumps(attribution), eid],
    )


def test_separate_scope_requires_both_populations_and_cannot_cross_serve(corpus_db):
    p = preference()
    conn, config = corpus_db
    trained(conn, config)
    for i in range(10, 14):
        artist_exposure(conn, i, i)
    general = p.train_preference(
        conn, config=config, training_cutoff=datetime(2026, 1, 1), scope="general"
    )
    artist = p.train_preference(
        conn, config=config, training_cutoff=datetime(2026, 1, 1), scope="artist"
    )
    assert general.query_scope == "general"
    assert artist.query_scope == "artist"
    assert general.training_provenance["training_per_kind"] == {"general": 3, "artist": 0}
    assert artist.training_provenance["training_per_kind"] == {"general": 0, "artist": 3}
    live_config(conn, config)
    control = run_local(conn, config, "artist")
    config.recommendation.preference.evaluation_artifact_id = general.artifact_id
    assert fingerprint(conn, run_local(conn, config, "artist")) == fingerprint(conn, control)


def test_new_evidence_cannot_bypass_current_minimum_pair_gates(corpus_db, evidence):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    config.recommendation.preference.minimum_training_pairs = 50
    decision = p.attest_preference(
        conn,
        artifact_id=artifact.artifact_id,
        config=config,
        evidence=linked_evidence(evidence, config, artifact),
    )
    assert not decision["eligible"]
    assert "insufficient_pairs" in decision["reasons"]


def test_repeated_training_does_not_overwrite_an_immutable_artifact(corpus_db):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    before = conn.execute("SELECT * FROM preference_model_artifact").fetchall()
    with pytest.raises(ValueError, match="already_exists"):
        p.train_preference(conn, config=config, training_cutoff=datetime(2026, 1, 1))
    assert conn.execute("SELECT * FROM preference_model_artifact").fetchall() == before
    assert p.preference_report(conn, artifact.artifact_id)["status"] == "candidate"


def test_learned_run_export_and_tui_ignore_current_artifact_storage(
    corpus_db, tmp_path, monkeypatch
):
    from scrobble_recs.export import export_recommendations
    from scrobble_recs.tui.data import TuiRecommendationStore

    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    config.recommendation.preference.evaluation_artifact_id = artifact.artifact_id
    run = run_local(conn, config)
    path = tmp_path / "learned.jsonl"
    export_recommendations(conn, path, format_name="jsonl", run_id=run)
    original = path.read_bytes()
    detail = TuiRecommendationStore(conn).get_detail(run, artist_id=1)
    conn.execute("DELETE FROM preference_model_artifact")

    def forbidden(*args, **kwargs):
        raise AssertionError("persisted readers must not select or train a ranker")

    monkeypatch.setattr(p, "apply_preference_ranker", forbidden)
    monkeypatch.setattr(p, "train_preference", forbidden)
    export_recommendations(conn, path, format_name="jsonl", run_id=run)
    assert path.read_bytes() == original
    assert TuiRecommendationStore(conn).get_detail(run, artist_id=1) == detail


def test_rolling_candidate_replay_preserves_candidate_status_and_binds_every_manifest(corpus_db):
    from scrobble_recs.models.evaluate import EvaluationWindow
    from scrobble_recs.models.rolling_compare import compare_rolling_recommendations

    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    challenger = config.model_copy(deep=True)
    challenger.recommendation.preference.evaluation_artifact_id = artifact.artifact_id
    report = compare_rolling_recommendations(
        conn,
        config,
        challenger,
        windows=[
            EvaluationWindow(datetime(2025, 2, 1), datetime(2025, 2, 1), datetime(2025, 3, 1))
        ],
    )
    pair = report.pairs[0]
    assert pair["challenger"]["artifact"]["preference"]["artifact_hash"] == artifact.artifact_hash
    assert not pair["control"]["artifact"].get("preference")
    assert pair["control"]["selected_ids"][0] == 4
    assert pair["challenger"]["selected_ids"][0] == 1
    assert all(
        row["metrics"]["artifact"]["preference"]["artifact_hash"] == artifact.artifact_hash
        for row in pair["ablations"]
    )
    assert p.preference_report(conn, artifact.artifact_id)["status"] == "candidate"


@pytest.mark.parametrize("change", [None, "code", "acquisition_inputs"])
def test_generated_preference_provider_failure_applicability(corpus_db, evidence, change):
    from scrobble_recs.models.evaluate import EvaluationWindow
    from scrobble_recs.models.promotion import _check_guardrails, promotion_decision
    from scrobble_recs.models.rolling_compare import (
        compare_rolling_recommendations,
        guardrail_applicability,
    )

    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    challenger = config.model_copy(deep=True)
    challenger.recommendation.preference.evaluation_artifact_id = artifact.artifact_id
    comparison = compare_rolling_recommendations(
        conn,
        config,
        challenger,
        windows=[
            EvaluationWindow(datetime(2025, 2, 1), datetime(2025, 2, 1), datetime(2025, 3, 1))
        ],
    )
    generated = comparison.pairs[0]
    assert (
        generated["control"]["artifact"]["artifact_id"]
        != generated["challenger"]["artifact"]["artifact_id"]
    )
    for variant in ("control", "challenger"):
        metric = generated[variant]["result"]["guardrails"]["provider_failure_rate"]
        assert metric["value"] is None and metric["available"] is False
        assert metric["availability"] == "not_applicable"
    reasons = set()
    checks = _check_guardrails(generated, config.recommendation.promotion, reasons)
    assert checks["provider_failure_rate"]["availability"] == "not_applicable"

    # Keep all other Task 11 requirements independently satisfied by the existing synthetic
    # evidence fixture. Transfer only the naturally emitted absent-measurement fields; retain
    # each fixture observation's cutoff/snapshot. Never invent a provider failure measurement.
    proof = linked_evidence(evidence, config, artifact)["general"]
    absent = generated["control"]["result"]["guardrails"]["provider_failure_rate"]
    for pair in proof["comparison"]["pairs"]:
        for variant in [
            pair["control"],
            pair["challenger"],
            *[row["metrics"] for row in pair["ablations"]],
        ]:
            previous = variant["result"]["guardrails"]["provider_failure_rate"]
            variant["result"]["guardrails"]["provider_failure_rate"] = {
                **deepcopy(absent),
                "cutoff": previous["cutoff"],
                "snapshot_id": previous["snapshot_id"],
            }
        pair["guardrail_applicability"] = guardrail_applicability(
            pair["control"], pair["challenger"]
        )
    assert promotion_decision(config=config.recommendation.promotion, **proof).eligible
    if change:
        manifest = proof["comparison"]["pairs"][0]["challenger"]["artifact"]
        if change == "code":
            manifest["content_fingerprint"] = "changed-acquisition-code"
        else:
            manifest["behavior_config"]["enrichment"]["seed_artist_limit"] += 1
        decision = promotion_decision(config=config.recommendation.promotion, **proof)
        assert not decision.eligible
        assert "incomplete_guardrails" in decision.reasons


def test_missing_historical_context_is_skipped_even_when_frozen_copies_match(corpus_db):
    p = preference()
    conn, _ = corpus_db
    eid = exposure(conn)
    row = conn.execute(
        "SELECT experiment_id FROM recommendation_exposure WHERE exposure_id=?", [eid]
    ).fetchone()
    definition = json.loads(
        conn.execute(
            "SELECT definition FROM recommendation_experiment WHERE experiment_id=?", [row[0]]
        ).fetchone()[0]
    )
    context = definition["source_contexts"]["control"]
    del context["history_window"]
    conn.execute(
        "UPDATE recommendation_run SET query_context=? WHERE run_id=?",
        [json.dumps(context), CONTROL],
    )
    conn.execute(
        "UPDATE recommendation_experiment SET definition=? WHERE experiment_id=?",
        [json.dumps(definition), row[0]],
    )
    diagnostics = []
    assert (
        p.build_pairwise_examples(
            conn,
            training_cutoff=datetime(2026, 1, 1),
            feature_names=p.FEATURE_NAMES,
            diagnostics=diagnostics,
        )
        == ()
    )
    assert diagnostics[0]["reason"] == "invalid_source_features_or_attribution"


def test_report_includes_rollback_event_affecting_the_artifact(corpus_db, evidence):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    p.attest_preference(
        conn,
        artifact_id=artifact.artifact_id,
        config=config,
        evidence=linked_evidence(evidence, config, artifact),
    )
    p.activate_preference(conn, artifact_id=artifact.artifact_id, config=config)
    p.rollback_preference(conn, scope="shared")
    assert "rollback" in [
        event["action"] for event in p.preference_report(conn, artifact.artifact_id)["lifecycle"]
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"general": None},
        {"general": {"comparison": []}},
        {"general": {"comparison": {"pairs": [None]}}},
    ],
)
def test_malformed_promotion_envelopes_are_ineligible_not_runtime_errors(corpus_db, payload):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    decision = p.attest_preference(
        conn, artifact_id=artifact.artifact_id, config=config, evidence=payload
    )
    assert not decision["eligible"]
    assert "invalid_promotion_evidence" in decision["reasons"]
    with pytest.raises(ValueError, match="promotion_ineligible"):
        p.activate_preference(conn, artifact_id=artifact.artifact_id, config=config)


@pytest.mark.parametrize(
    "corruption",
    [
        "feature_root",
        "feature_values",
        "missing_signals",
        "evidence_root",
        "preference_null",
        "preference_list",
        "labels_root",
        "labels_value",
        "context_root",
        "definition_root",
        "side_map",
        "source_map",
        "source_contexts",
        "attribution_root",
        "memberships",
        "member_item",
        "invalid_run_id",
        "shown_root",
        "shown_item",
    ],
)
def test_malformed_historical_containers_skip_only_affected_examples(corpus_db, corruption):
    p = preference()
    conn, _ = corpus_db
    bad = exposure(conn)
    good = exposure(conn, identity=2, left=(2,), right=(3,))
    if corruption in ("feature_root", "feature_values", "missing_signals"):
        value = [] if corruption == "feature_root" else features()
        if corruption != "feature_root":
            value["values" if corruption == "feature_values" else "missing_signals"] = (
                [] if corruption == "feature_values" else {}
            )
        conn.execute(
            "UPDATE recommendation_candidate SET feature_record=? WHERE run_id=? AND artist_id=1",
            [json.dumps(value), CONTROL],
        )
    elif corruption in ("evidence_root", "preference_null", "preference_list"):
        value = (
            []
            if corruption == "evidence_root"
            else {"preference": None if corruption == "preference_null" else ["used"]}
        )
        conn.execute(
            "UPDATE recommendation_candidate SET evidence=? WHERE run_id=? AND artist_id=1",
            [json.dumps(value), CONTROL],
        )
    elif corruption in ("labels_root", "labels_value"):
        value = [] if corruption == "labels_root" else {"1": {"strong_fit": True}}
        conn.execute(
            "UPDATE recommendation_judgment SET labels=? WHERE exposure_id=?",
            [json.dumps(value), bad],
        )
    elif corruption in ("definition_root", "side_map", "source_map", "source_contexts"):
        row = conn.execute(
            "SELECT x.experiment_id, x.definition FROM recommendation_experiment x "
            "JOIN recommendation_exposure e USING (experiment_id) WHERE e.exposure_id=?",
            [bad],
        ).fetchone()
        value = [] if corruption == "definition_root" else json.loads(row[1])
        if corruption != "definition_root":
            value[
                {
                    "side_map": "side_variants",
                    "source_map": "source_run_ids",
                    "source_contexts": "source_contexts",
                }[corruption]
            ] = []
        conn.execute(
            "UPDATE recommendation_experiment SET definition=? WHERE experiment_id=?",
            [json.dumps(value), row[0]],
        )
    else:
        column = (
            "context"
            if corruption == "context_root"
            else "shown_items"
            if corruption.startswith("shown_")
            else "attribution"
        )
        value = json.loads(
            conn.execute(
                f"SELECT {column} FROM recommendation_exposure WHERE exposure_id=?", [bad]
            ).fetchone()[0]
        )
        if corruption in ("context_root", "attribution_root"):
            value = []
        elif corruption == "shown_root":
            value = {}
        elif corruption == "shown_item":
            value[0] = None
        elif corruption == "memberships":
            value["1"] = {"variant": "control"}
        elif corruption == "member_item":
            value["1"] = [None]
        else:
            value["1"][0]["run_id"] = ["invalid"]
        conn.execute(
            f"UPDATE recommendation_exposure SET {column}=? WHERE exposure_id=?",
            [json.dumps(value), bad],
        )
    diagnostics = []
    pairs = p.build_pairwise_examples(
        conn,
        training_cutoff=datetime(2026, 1, 1),
        feature_names=p.FEATURE_NAMES,
        diagnostics=diagnostics,
    )
    assert [(x.exposure_id, x.preferred_artist_id, x.other_artist_id) for x in pairs] == [
        (good, 2, 3)
    ]
    assert any(
        x["exposure_id"] == bad
        and x["reason"] in ("invalid_exposure", "invalid_source_features_or_attribution")
        for x in diagnostics
    )


@pytest.mark.parametrize(
    "corruption", ["feature_root", "feature_values", "context_root", "relationship"]
)
def test_malformed_runtime_containers_fall_back_for_entire_run(corpus_db, monkeypatch, corruption):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    control = run_local(conn, config, "artist")
    original = p.encode_features

    def corrupt(record, context):
        if record["values"]["tag_similarity_score"] == 0:
            if corruption == "feature_root":
                record = []
            elif corruption == "feature_values":
                record = {**record, "values": []}
            elif corruption == "context_root":
                context = []
            else:
                context = {**context, "target_relationship": ["invalid"]}
        return original(record, context)

    monkeypatch.setattr(p, "encode_features", corrupt)
    config.recommendation.preference.evaluation_artifact_id = artifact.artifact_id
    fallback = run_local(conn, config, "artist")
    assert fingerprint(conn, fallback) == fingerprint(conn, control)
    summary = json.loads(
        conn.execute(
            "SELECT seed_summary FROM recommendation_run WHERE run_id=?", [fallback]
        ).fetchone()[0]
    )
    assert summary["preference"]["ranker_fallback_reason"] == "invalid_artifact_or_features"
    assert not summary["evaluation_artifact"].get("preference")


@pytest.mark.parametrize("kind", ["general", "artist"])
def test_missing_lifecycle_table_preserves_control_and_persists_fallback(corpus_db, kind):
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    control = run_local(conn, config, kind)
    config.recommendation.preference.evaluation_artifact_id = artifact.artifact_id
    conn.execute("DROP TABLE preference_lifecycle_event")
    fallback = run_local(conn, config, kind)
    assert fingerprint(conn, fallback) == fingerprint(conn, control)
    summary = json.loads(
        conn.execute(
            "SELECT seed_summary FROM recommendation_run WHERE run_id=?", [fallback]
        ).fetchone()[0]
    )
    assert summary["preference"]["ranker_fallback_reason"] == "missing_preference_storage"
    assert not summary["evaluation_artifact"].get("preference")


@pytest.mark.parametrize("corruption", ["coefficients", "promotion", "target"])
def test_production_fallback_preserves_selected_artifact_identity(corpus_db, evidence, corruption):
    p = preference()
    conn, config = corpus_db
    artifact = trained(conn, config)
    live_config(conn, config)
    proof = linked_evidence(evidence, config, artifact)
    p.attest_preference(conn, artifact_id=artifact.artifact_id, config=config, evidence=proof)
    p.activate_preference(conn, artifact_id=artifact.artifact_id, config=config)
    kind = "artist" if corruption == "target" else "general"
    target = 1 if corruption == "target" else 5
    control = run_local(conn, config, kind, target_artist_id=target)
    if corruption == "coefficients":
        conn.execute(
            "UPDATE preference_model_artifact SET coefficients='[0]' WHERE artifact_id=?",
            [artifact.artifact_id],
        )
    elif corruption == "promotion":
        conn.execute(
            "DELETE FROM preference_promotion_evidence WHERE artifact_id=?", [artifact.artifact_id]
        )
    config.recommendation.preference.enabled = True
    fallback = run_local(conn, config, kind, target_artist_id=target)
    assert fingerprint(conn, fallback) == fingerprint(conn, control)
    summary = json.loads(
        conn.execute(
            "SELECT seed_summary FROM recommendation_run WHERE run_id=?", [fallback]
        ).fetchone()[0]
    )
    assert summary["preference"]["attempted_artifact_id"] == artifact.artifact_id
    assert summary["preference"]["ranker_fallback_reason"]
    assert not summary["evaluation_artifact"].get("preference")


def test_singleton_list_does_not_overrule_an_explicit_artist_tie(corpus_db):
    p = preference()
    conn, _ = corpus_db
    exposure(conn, preferred="tie", list_choice="left")
    assert (
        p.build_pairwise_examples(
            conn, training_cutoff=datetime(2026, 1, 1), feature_names=p.FEATURE_NAMES
        )
        == ()
    )
