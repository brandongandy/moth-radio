"""Deterministic contracts for normalized persisted evidence."""

from dataclasses import replace
from datetime import datetime
from importlib import import_module, util
from uuid import UUID

import pytest
from test_retrieval import stage_database as _stage_database

stage_database = _stage_database


def evidence_module():
    assert util.find_spec("scrobble_recs.models.evidence") is not None
    return import_module("scrobble_recs.models.evidence")


def observation(**changes):
    return evidence_module().EvidenceObservation(
        **dict(
            source="provider",
            family="collaborative",
            relationship_kind="similar_artist",
            source_artist_id=1,
            target_artist_id=2,
            raw_score=0.9,
            source_rank=None,
            provider_mode=None,
            fetched_at=datetime(2025, 1, 1),
            identity_confidence=0.9,
            evidence={},
            **changes,
        )
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("raw_score", float("nan")),
        ("raw_score", float("inf")),
        ("source_rank", 0),
        ("source_rank", -1),
        ("family", "unknown"),
        ("relationship_kind", "unknown"),
        ("identity_confidence", -0.1),
        ("identity_confidence", 1.1),
        ("identity_confidence", float("nan")),
        ("evidence", {"strength": "huge"}),
    ],
)
def test_observation_rejects_invalid_boundary_values(field, value):
    module = evidence_module()
    with pytest.raises(ValueError):
        replace(observation(), **{field: value})
    assert module is not None


def test_observation_copies_nested_evidence_and_candidate_sequence():
    payload = {"tags": ["rock"]}
    item = replace(observation(), evidence=payload)
    rows = [item]
    candidate = evidence_module().CandidateEvidence(2, rows)
    payload["tags"].append("pop")
    rows.clear()
    assert candidate.observations[0].evidence == {"tags": ["rock"]}
    assert candidate.observations[0].source_rank is None


def test_writer_is_uuid5_idempotent_and_distinguishes_null_source_tag_keys(stage_database):
    module = evidence_module()
    conn, _, _ = stage_database
    item = replace(
        observation(), family="semantic", relationship_kind="artist_tag", source_artist_id=None
    )
    rock = module.observation_key(tag="rock", provider_mode="mode")
    pop = module.observation_key(tag="pop", provider_mode="mode")
    first = module.persist_evidence_observation(
        conn, item, observation_key=rock, payload_hash="one"
    )
    assert UUID(first).version == 5
    assert (
        module.persist_evidence_observation(conn, item, observation_key=rock, payload_hash="two")
        == first
    )
    assert module.persist_evidence_observation(conn, item, observation_key=pop) != first
    assert conn.execute("SELECT count(*) FROM artist_evidence_observation").fetchone() == (2,)


def test_loader_cutoffs_feedback_shape_and_canonical_order(stage_database):
    module = evidence_module()
    conn, _, context = stage_database
    conn.execute("UPDATE artist_similarity_edge SET fetched_at = '2025-01-01'")
    conn.execute("UPDATE tag_top_artist SET fetched_at = '2025-01-02'")
    conn.execute(
        "INSERT INTO artist_tag (artist_id, tag, source, normalized_weight, fetched_at) "
        "VALUES (2, 'rock', 'lastfm', .7, '2025-01-01')"
    )
    conn.execute(
        "INSERT INTO feedback_event "
        "(feedback_id, created_at, artist_id, action, value, context, note) VALUES "
        "('00000000-0000-0000-0000-000000000001', '2024-12-31', 2, 'save', 1, "
        "'{\"reason\":\"fit\"}', 'private'), "
        "('00000000-0000-0000-0000-000000000002', '2025-01-01', 2, 'save', 1, '{}', 'private')"
    )
    context = replace(context, feedback_cutoff=datetime(2025, 1, 1))
    rows = module.load_candidate_evidence(conn, context=context)
    assert 4 not in rows
    observations = rows[2].observations
    assert [row.source for row in observations] == ["lastfm", "lastfm", "local_feedback"]
    feedback = observations[-1]
    assert (
        feedback.family,
        feedback.relationship_kind,
        feedback.source_artist_id,
        feedback.source_rank,
        feedback.provider_mode,
        feedback.raw_score,
    ) == ("personal", "personal_feedback", None, None, None, 1.0)
    assert feedback.evidence == {
        "feedback_id": "00000000-0000-0000-0000-000000000001",
        "action": "save",
        "context": {"reason": "fit"},
    }
    assert feedback.identity_confidence == 0.0


def test_migration_reapplies_without_rewriting_historical_candidate_nulls(stage_database):
    from scrobble_recs.db import apply_migrations

    conn, _, _ = stage_database
    conn.execute(
        "INSERT INTO recommendation_candidate (run_id, artist_id) "
        "VALUES ('00000000-0000-0000-0000-000000000010', 2)"
    )
    apply_migrations(conn)
    apply_migrations(conn)
    assert conn.execute("SELECT count(*) FROM artist_evidence_observation").fetchone() == (0,)
    assert (
        conn.execute(
            "SELECT evidence, feature_record, admission, policy_effects, candidate_score, "
            "relevance_score, policy_adjustment, known_status, backoff_stage "
            "FROM recommendation_candidate"
        ).fetchone()
        == (None,) * 9
    )


def test_source_admission_uses_local_strength_and_deduplicated_families():
    module = import_module("scrobble_recs.models.recommendation_quality")
    assert hasattr(module, "decide_candidate_admission")
    from scrobble_recs.config import DiscoveryEvidenceConfig

    def decide(rows, **kwargs):
        return module.decide_candidate_admission(
            evidence_module().CandidateEvidence(2, rows),
            stage=0,
            hidden=False,
            strongly_rejected=False,
            identity_confidence=0.9,
            config=DiscoveryEvidenceConfig(),
            **kwargs,
        )

    strong = replace(observation(), raw_score=999999, evidence={"strength": "strong"})
    semantic = replace(
        strong, source="tags", family="semantic", relationship_kind="artist_tag", raw_score=0.001
    )
    assert decide((strong, semantic)).reasons == ("strong_collaborative_with_independent_family",)
    assert not decide((replace(strong, evidence={}), replace(semantic, evidence={}))).admitted
    assert not decide((strong, replace(strong, source="other"))).admitted
    moderate = replace(strong, evidence={"strength": "moderate"})
    assert decide((moderate, replace(semantic, evidence={"strength": "moderate"}))).reasons == (
        "independent_family_agreement",
    )


@pytest.mark.parametrize(
    "hidden,rejected,confidence,stage,reason",
    [
        (True, True, 0.1, 4, "hidden_artist"),
        (False, True, 0.1, 4, "strongly_rejected"),
        (False, False, 0.49, 4, "identity_below_minimum"),
        (False, False, 0.79, 0, "identity_below_safety_threshold"),
    ],
)
def test_source_admission_safety_priority(hidden, rejected, confidence, stage, reason):
    module = import_module("scrobble_recs.models.recommendation_quality")
    assert hasattr(module, "decide_candidate_admission")
    from scrobble_recs.config import DiscoveryEvidenceConfig

    evidence = evidence_module().CandidateEvidence(2, (observation(),))
    result = module.decide_candidate_admission(
        evidence,
        stage=stage,
        hidden=hidden,
        strongly_rejected=rejected,
        identity_confidence=confidence,
        config=DiscoveryEvidenceConfig(),
    )
    assert result.reasons == (reason,)
    assert not result.admitted


def test_legacy_gate_exact_thresholds_identity_exception_and_tag_affinity(stage_database):
    module = import_module("scrobble_recs.models.recommendation_quality")
    assert hasattr(module, "decide_candidate_admission")
    from scrobble_recs.config import DiscoveryEvidenceConfig

    conn, _, context = stage_database
    translated = evidence_module().load_candidate_evidence(conn, context=context)[2].observations[0]
    edge = replace(
        translated,
        source="lastfm",
        raw_score=0.9,
        evidence={"legacy": True, "normalized_score": 0.05},
    )
    rows = evidence_module().CandidateEvidence(2, (edge, replace(edge, source_artist_id=3)))
    decision = module.decide_candidate_admission(
        rows,
        stage=0,
        hidden=False,
        strongly_rejected=False,
        identity_confidence=0.5,
        config=DiscoveryEvidenceConfig(),
    )
    assert decision.reasons == ("independent_seed_support", "legacy_identity_compatibility")
    one = evidence_module().CandidateEvidence(
        2, (replace(edge, evidence={"legacy": True, "normalized_score": 0.3}),)
    )
    assert not module.decide_candidate_admission(
        one,
        stage=0,
        hidden=False,
        strongly_rejected=False,
        identity_confidence=0.9,
        config=DiscoveryEvidenceConfig(),
        tag_score=0.049,
    ).admitted
    assert module.decide_candidate_admission(
        one,
        stage=0,
        hidden=False,
        strongly_rejected=False,
        identity_confidence=0.9,
        config=DiscoveryEvidenceConfig(),
        tag_score=0.05,
    ).admitted


def test_catalog_admission_requires_enabled_artist_context(stage_database):
    module = import_module("scrobble_recs.models.recommendation_quality")
    assert hasattr(module, "decide_candidate_admission")
    from scrobble_recs.config import DiscoveryEvidenceConfig

    _, _, context = stage_database
    item = replace(
        observation(),
        family="catalog",
        relationship_kind="catalog_relationship",
        evidence={"supported_relationship": True},
    )
    evidence = evidence_module().CandidateEvidence(2, (item,))

    def decide(ctx, enabled):
        return module.decide_candidate_admission(
            evidence,
            stage=0,
            hidden=False,
            strongly_rejected=False,
            identity_confidence=0.9,
            context=ctx,
            config=DiscoveryEvidenceConfig(catalog_relationships_enabled=enabled),
        )

    assert not decide(context, True).admitted
    artist = replace(context, query_kind="artist", target_artist_id=1)
    assert not decide(artist, False).admitted
    assert decide(artist, True).reasons == ("enabled_catalog_relationship",)


def test_snapshot_hashes_normalized_payload_feedback_identity_and_excludes_notes(stage_database):
    from scrobble_recs.models.recommendation_query import build_evidence_snapshot_id

    conn, _, _ = stage_database
    module = evidence_module()
    cutoff = datetime(2025, 1, 1)
    baseline = build_evidence_snapshot_id(conn, historical_cutoff=cutoff)
    module.persist_evidence_observation(
        conn, observation(), observation_key="pair", payload_hash="payload-one"
    )
    normalized = build_evidence_snapshot_id(conn, historical_cutoff=cutoff)
    assert normalized != baseline
    conn.execute("UPDATE artist_evidence_observation SET payload_hash = 'payload-two'")
    assert build_evidence_snapshot_id(conn, historical_cutoff=cutoff) != normalized
    conn.execute(
        "INSERT INTO feedback_event "
        "(feedback_id, created_at, artist_id, action, value, context, note) VALUES "
        "('00000000-0000-0000-0000-000000000001', '2024-12-31', 2, 'save', 1, '{}', 'one')"
    )
    feedback = build_evidence_snapshot_id(conn, historical_cutoff=cutoff)
    conn.execute("UPDATE feedback_event SET note = 'two'")
    assert build_evidence_snapshot_id(conn, historical_cutoff=cutoff) == feedback
    conn.execute('UPDATE feedback_event SET context = \'{"direction":"more"}\'')
    assert build_evidence_snapshot_id(conn, historical_cutoff=cutoff) != feedback


def test_legacy_retrieval_provider_cutoff_matches_evidence(stage_database):
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    conn.execute("UPDATE artist_similarity_edge SET fetched_at = '2025-01-01'")
    conn.execute("UPDATE tag_top_artist SET fetched_at = '2025-01-02'")
    result = retrieve_legacy_candidates(
        conn, context=replace(context, feedback_cutoff=datetime(2025, 1, 1)), config=config
    )
    assert [row.artist_id for row in result.candidates] == [2, 3]


def test_backoff_records_cumulative_stages_and_opt_in_identity(stage_database):
    module = import_module("scrobble_recs.models.retrieval")
    assert hasattr(module, "retrieve_with_backoff")
    conn, config, context = stage_database
    conn.execute("UPDATE artist SET identity_confidence = .9")
    conn.execute("DELETE FROM artist_similarity_edge")
    conn.execute("DELETE FROM tag_top_artist")
    weak = replace(observation(), evidence={"strength": "weak"})
    semantic = replace(
        weak, family="semantic", relationship_kind="artist_tag", source="tags", evidence={}
    )
    evidence_module().persist_evidence_observation(conn, weak, observation_key="pair")
    evidence_module().persist_evidence_observation(conn, semantic, observation_key="tag")
    result = module.retrieve_with_backoff(conn, context=context, config=config, limit=10)
    assert result.admissions[2].stage == 2
    assert result.admissions[2].admitted
    assert [stage["stage"] for stage in result.diagnostics["backoff_stages"]] == [0, 1, 2, 3]
    assert result.diagnostics["backoff_stages"][1]["depth_expansion"] == "legacy_exhaustive_noop"
    conn.execute("UPDATE artist SET identity_confidence = .6 WHERE artist_id = 2")
    strict = module.retrieve_with_backoff(conn, context=context, config=config, limit=10)
    assert not strict.admissions[2].admitted
    relaxed = module.retrieve_with_backoff(
        conn, context=context, config=config, limit=10, allow_low_confidence=True
    )
    assert relaxed.admissions[2].stage == 4
    assert relaxed.admissions[2].reasons[-1] == "low_confidence"
    assert sum(row.admitted for row in relaxed.admissions.values()) == 1


def test_pipeline_persists_separated_candidate_shapes_and_null_post_gate_values(stage_database):
    import json

    from scrobble_recs.models.policy import apply_slate_policy
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.recommendation_store import (
        RecommendationRunPayload,
        persist_recommendation_run,
    )
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    retrieval = retrieve_with_backoff(conn, context=context, config=config, limit=10)
    ranked = rank_candidates(conn, context=context, retrieval=retrieval, config=config)
    decisions = apply_slate_policy(conn, context=context, ranked=ranked, limit=10, config=config)
    payload = RecommendationRunPayload(
        "00000000-0000-0000-0000-000000000099",
        context,
        config,
        retrieval,
        ranked,
        decisions,
        tuple(row.legacy.diagnostic for row in ranked),
        "explicit",
    )
    persist_recommendation_run(conn, payload=payload)
    summary = conn.execute(
        "SELECT seed_summary FROM recommendation_run WHERE run_id = ?::UUID", [payload.run_id]
    ).fetchone()[0]
    assert json.loads(summary)["retrieval_diagnostics"] == {
        "candidate_count": 3,
        "admitted_count": 2,
        "selected_count": 2,
        "requested_limit": 10,
        "exhausted": True,
        "backoff_stages": [
            {
                "stage": stage,
                "admitted_count": 2,
                "depth_expansion": "legacy_exhaustive_noop" if stage == 1 else None,
            }
            for stage in range(4)
        ],
    }
    repeated = replace(payload, run_id="00000000-0000-0000-0000-000000000100")
    persist_recommendation_run(conn, payload=repeated)
    assert (
        conn.execute(
            "SELECT seed_summary FROM recommendation_run WHERE run_id = ?::UUID", [repeated.run_id]
        ).fetchone()[0]
        == summary
    )
    rows = conn.execute(
        "SELECT artist_id, evidence, feature_record, admission, policy_effects, "
        "candidate_score, relevance_score, policy_adjustment, known_status, backoff_stage, "
        "candidate_sources FROM recommendation_candidate WHERE run_id = ?::UUID ORDER BY artist_id",
        [payload.run_id],
    ).fetchall()
    assert len(rows) == 3
    first = rows[0]
    assert json.loads(first[1])["schema_version"] == "candidate-evidence-v1"
    assert json.loads(first[1])["observations"][0]["source"] == "lastfm"
    assert set(json.loads(first[2])) == {"schema_version", "values", "missing_signals"}
    assert json.loads(first[3]) == {
        "admitted": True,
        "stage": 0,
        "reasons": ["independent_seed_support", "legacy_identity_compatibility"],
    }
    assert set(json.loads(first[4])) == {
        "included",
        "final_rank",
        "final_score",
        "adjustment",
        "reasons",
    }
    assert first[5] is not None and first[6] is not None
    assert first[8:10] == ("new", 0)
    assert json.loads(first[10]) == [1]
    rejected = rows[-1]
    assert not json.loads(rejected[3])["admitted"]
    assert json.loads(rejected[2])["values"] == {}
    assert rejected[5:8] == (None, None, None)


def test_mixed_evidence_uses_lastfm_thresholds_without_cross_provider_scores():
    from scrobble_recs.config import DiscoveryEvidenceConfig
    from scrobble_recs.models.recommendation_quality import decide_candidate_admission

    edge = replace(
        observation(),
        source="lastfm",
        raw_score=0.01,
        evidence={"legacy": True, "normalized_score": 0.30},
    )
    tag = replace(observation(), source="tags", family="semantic", relationship_kind="artist_tag")
    result = decide_candidate_admission(
        evidence_module().CandidateEvidence(2, (edge, tag)),
        stage=0,
        hidden=False,
        strongly_rejected=False,
        identity_confidence=0.9,
        config=DiscoveryEvidenceConfig(),
    )
    assert result.reasons == ("strong_collaborative_with_independent_family",)


def test_legacy_semantic_family_does_not_replace_user_tag_affinity(stage_database):
    from scrobble_recs.config import DiscoveryEvidenceConfig
    from scrobble_recs.models.recommendation_quality import decide_candidate_admission

    conn, _, context = stage_database
    translated = evidence_module().load_candidate_evidence(conn, context=context)[2].observations[0]
    edge = replace(translated, evidence={"legacy": True, "normalized_score": 0.30})
    tag = replace(
        edge,
        family="semantic",
        relationship_kind="artist_tag",
        source_artist_id=None,
        evidence={"legacy": True, "tag": "unrelated"},
    )
    result = decide_candidate_admission(
        evidence_module().CandidateEvidence(2, (edge, tag)),
        stage=0,
        hidden=False,
        strongly_rejected=False,
        identity_confidence=0.9,
        config=DiscoveryEvidenceConfig(),
        tag_score=0.0,
    )
    assert not result.admitted


def test_snapshot_canonicalizes_nested_json_and_captures_freshness_once(
    stage_database, monkeypatch
):
    from scrobble_recs.models import recommendation_query
    from scrobble_recs.models.recommend import generate_recommendations

    conn, config, _ = stage_database
    evidence_module().persist_evidence_observation(
        conn, replace(observation(), evidence={"a": 1, "b": 2}), observation_key="pair"
    )
    snapshot = recommendation_query.build_evidence_snapshot_id(conn, historical_cutoff=None)
    conn.execute('UPDATE artist_evidence_observation SET evidence = \'{ "b": 2, "a": 1 }\'')
    assert recommendation_query.build_evidence_snapshot_id(conn, historical_cutoff=None) == snapshot
    captures = []
    original = recommendation_query.load_evidence_freshness

    def capture(*args, **kwargs):
        value = original(*args, **kwargs)
        captures.append(value)
        return value

    monkeypatch.setattr(recommendation_query, "load_evidence_freshness", capture)
    stats = generate_recommendations(conn, config, limit=2, as_of=datetime(2025, 1, 1))
    assert len(captures) == 1
    assert (
        conn.execute(
            "SELECT evidence_snapshot_id FROM recommendation_run WHERE run_id = ?::UUID",
            [stats.run_id],
        ).fetchone()[0]
        == snapshot
    )


def test_retrieval_shares_one_provider_read_between_evidence_and_legacy_scores(stage_database):
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates, retrieve_with_backoff

    conn, config, context = stage_database
    inputs = retrieve_legacy_candidates(conn, context=context, config=config).legacy.inputs
    reads = {"artist_similarity_edge": 0, "artist_tag": 0, "tag_top_artist": 0}

    class ObservedConnection:
        def execute(self, sql, parameters=None):
            for table in reads:
                if f"from {table}" in sql.lower():
                    reads[table] += 1
            return conn.execute(sql, parameters) if parameters is not None else conn.execute(sql)

    result = retrieve_with_backoff(
        ObservedConnection(), context=context, config=config, limit=3, inputs=inputs
    )
    assert reads == {"artist_similarity_edge": 1, "artist_tag": 1, "tag_top_artist": 1}
    assert result.evidence[2].observations[0].source == "lastfm"
    assert result.legacy.scoring_data.similarity_by_target[2] == [(1, 0.9)]


def test_legacy_normalization_preserves_missing_raw_scores_and_tag_provider_counts(stage_database):
    conn, _, context = stage_database
    conn.execute(
        "INSERT INTO artist_tag (artist_id, tag, source, tag_count, normalized_weight) "
        "VALUES (2, 'rock', 'lastfm', 75, .75)"
    )
    rows = evidence_module().load_candidate_evidence(conn, context=context)[2].observations
    assert rows[0].raw_score == 75
    assert rows[0].evidence["normalized_weight"] == 0.75
    assert rows[1].raw_score is None
    assert rows[1].evidence["normalized_score"] == 0.9


def test_backoff_graph_stage_uses_existing_bounded_walk_and_first_admitting_stage(stage_database):
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    conn.execute("UPDATE artist SET identity_confidence = .9")
    config.recommendation.discovery_evidence.minimum_seed_count = 2
    config.recommendation.scoring.engine = "walk"
    config.recommendation.walk.min_distinct_seeds = 1
    config.recommendation.walk.min_lift = -100
    evidence_module().persist_evidence_observation(
        conn, replace(observation(), evidence={"strength": "weak"}), observation_key="pair"
    )
    result = retrieve_with_backoff(conn, context=context, config=config, limit=10)
    assert result.admissions[2].admitted
    assert result.admissions[2].stage == 3
    assert result.admissions[2].reasons == ("walk_lift_and_reachability",)
    assert result.admissions[3].stage == 0
    assert result.legacy.walk_result.nodes[2].hop_distance <= config.recommendation.walk.max_hops


@pytest.mark.parametrize("stage", range(5))
def test_positive_personal_evidence_never_restores_sub_floor_identities(stage):
    from scrobble_recs.config import DiscoveryEvidenceConfig
    from scrobble_recs.models.recommendation_quality import decide_candidate_admission

    personal = replace(
        observation(),
        source="local_feedback",
        family="personal",
        relationship_kind="personal_feedback",
        source_artist_id=None,
        evidence={"action": "save"},
        raw_score=1,
    )
    evidence = evidence_module().CandidateEvidence(2, (personal,))
    decision = decide_candidate_admission(
        evidence,
        stage=stage,
        hidden=False,
        strongly_rejected=False,
        identity_confidence=0.49,
        config=DiscoveryEvidenceConfig(),
    )
    assert not decision.admitted
    assert decision.reasons == ("identity_below_minimum",)
    safe = decide_candidate_admission(
        evidence,
        stage=stage,
        hidden=False,
        strongly_rejected=False,
        identity_confidence=0.8,
        config=DiscoveryEvidenceConfig(),
    )
    assert safe.reasons == ("positive_personal_evidence",)


def test_observation_keys_distinguish_modes_catalog_directions_and_feedback_events(stage_database):
    conn, _, _ = stage_database
    module = evidence_module()
    keys = [
        module.observation_key(target_artist_id=2, provider_mode=mode, tag="rock")
        for mode in ("easy", "hard")
    ]
    keys += [
        module.observation_key(
            source_artist_id=1, target_artist_id=2, relation_type="member_of", direction=direction
        )
        for direction in ("forward", "backward")
    ]
    keys += [
        module.observation_key(feedback_id=event)
        for event in (
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
        )
    ]
    identifiers = [
        module.persist_evidence_observation(conn, observation(), observation_key=key)
        for key in keys
    ]
    assert len(set(identifiers)) == 6
    assert conn.execute("SELECT count(*) FROM artist_evidence_observation").fetchone() == (6,)
    assert [
        module.persist_evidence_observation(conn, observation(), observation_key=key)
        for key in reversed(keys)
    ] == list(reversed(identifiers))


def test_normalized_cutoff_is_inclusive_and_unbounded_keeps_missing_fetch_times(stage_database):
    conn, _, context = stage_database
    module = evidence_module()
    for target, fetched in ((2, datetime(2025, 1, 1)), (3, datetime(2025, 1, 2)), (4, None)):
        module.persist_evidence_observation(
            conn,
            replace(observation(), target_artist_id=target, fetched_at=fetched),
            observation_key=str(target),
        )
    bounded = module.load_candidate_evidence(
        conn, context=replace(context, feedback_cutoff=datetime(2025, 1, 1))
    )
    assert {
        target
        for target, rows in bounded.items()
        if any(row.source == "provider" for row in rows.observations)
    } == {2}
    all_rows = module.load_candidate_evidence(conn, context=context)
    assert {
        target
        for target, rows in all_rows.items()
        if any(row.source == "provider" for row in rows.observations)
    } == {2, 3, 4}


def test_source_aware_admission_precedes_calibration_and_uses_current_target_identity(
    stage_database,
):
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    config.recommendation.calibration.enabled = True
    conn.execute("UPDATE artist SET identity_confidence = .9")
    config.recommendation.discovery_evidence.minimum_seed_count = 2
    strong = replace(observation(), evidence={"strength": "strong"}, identity_confidence=0.99)
    evidence_module().persist_evidence_observation(conn, strong, observation_key="pair")
    evidence_module().persist_evidence_observation(
        conn,
        replace(strong, source="tags", family="semantic", relationship_kind="artist_tag"),
        observation_key="tag",
    )
    retrieval = retrieve_with_backoff(conn, context=context, config=config, limit=10)
    ranked = {
        row.artist_id: row
        for row in rank_candidates(conn, context=context, retrieval=retrieval, config=config)
    }
    assert ranked[2].features.values["calibrated_lastfm_similarity"] == 1.0
    assert ranked[3].features.values == {}
    conn.execute("UPDATE artist SET identity_confidence = .6 WHERE artist_id = 2")
    strict = retrieve_with_backoff(conn, context=context, config=config, limit=10)
    assert not strict.admissions[2].admitted
    assert any(row.identity_confidence == 0.99 for row in strict.evidence[2].observations)
    assert strict.admissions[2].reasons == ("identity_below_safety_threshold",)


@pytest.mark.parametrize(
    "mode,status,admitted", [("balanced", "known", False), ("rediscovery", "rediscovery", True)]
)
def test_persistence_labels_known_and_rediscovery_candidates(
    stage_database, mode, status, admitted
):
    import json

    from scrobble_recs.models.policy import apply_slate_policy
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.recommendation_store import (
        RecommendationRunPayload,
        persist_recommendation_run,
    )
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    context = replace(context, mode=mode)
    conn.execute(
        "INSERT INTO artist_listening_fact "
        "(artist_id, play_count, active_days, knownness, rediscovery_score) "
        "VALUES (2, 5, 3, .6, .5)"
    )
    retrieval = retrieve_with_backoff(conn, context=context, config=config, limit=2)
    ranked = rank_candidates(conn, context=context, retrieval=retrieval, config=config)
    decisions = apply_slate_policy(conn, context=context, ranked=ranked, limit=2, config=config)
    payload = RecommendationRunPayload(
        "00000000-0000-0000-0000-000000000098",
        context,
        config,
        retrieval,
        ranked,
        decisions,
        tuple(row.legacy.diagnostic for row in ranked),
        "explicit",
    )
    persist_recommendation_run(conn, payload=payload)
    stored_status, admission = conn.execute(
        "SELECT known_status, admission FROM recommendation_candidate WHERE artist_id = 2"
    ).fetchone()
    assert stored_status == status
    assert json.loads(admission)["admitted"] is admitted


def test_snapshot_includes_each_feedback_event_time_not_only_latest(stage_database):
    from scrobble_recs.models.recommendation_query import build_evidence_snapshot_id

    conn, _, _ = stage_database
    conn.execute("""INSERT INTO feedback_event
        (feedback_id, created_at, artist_id, action, value, context) VALUES
        ('00000000-0000-0000-0000-000000000001', '2024-01-01', 2, 'save', 1, '{}'),
        ('00000000-0000-0000-0000-000000000002', '2024-02-01', 2, 'save', 1, '{}')""")
    snapshot = build_evidence_snapshot_id(conn, historical_cutoff=None)
    conn.execute(
        "UPDATE feedback_event SET created_at = '2024-01-02' "
        "WHERE feedback_id = '00000000-0000-0000-0000-000000000001'"
    )
    assert build_evidence_snapshot_id(conn, historical_cutoff=None) != snapshot


def test_personal_only_backoff_does_not_inherit_legacy_zero_confidence_projection(stage_database):
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    conn.execute("DELETE FROM artist_similarity_edge")
    conn.execute("DELETE FROM tag_top_artist")
    conn.execute("""INSERT INTO feedback_event
        (feedback_id, created_at, artist_id, action, value, context) VALUES
        ('00000000-0000-0000-0000-000000000001', '2024-01-01', 4, 'save', 1, '{}')""")
    result = retrieve_with_backoff(
        conn, context=context, config=config, limit=10, allow_low_confidence=True
    )
    assert not result.admissions[4].admitted
    assert result.admissions[4].reasons == ("identity_below_minimum",)


def test_explicit_positive_rediscovery_feedback_can_admit_safe_personal_candidate():
    from scrobble_recs.config import DiscoveryEvidenceConfig
    from scrobble_recs.models.recommendation_quality import decide_candidate_admission

    item = replace(
        observation(),
        source="local_feedback",
        family="personal",
        relationship_kind="personal_feedback",
        source_artist_id=None,
        raw_score=1,
        evidence={"action": "rediscovery_good"},
    )
    result = decide_candidate_admission(
        evidence_module().CandidateEvidence(2, (item,)),
        stage=0,
        hidden=False,
        strongly_rejected=False,
        identity_confidence=0.9,
        config=DiscoveryEvidenceConfig(),
    )
    assert result.reasons == ("positive_personal_evidence",)


def test_unrelated_legacy_tags_do_not_veto_safe_explicit_personal_admission():
    from scrobble_recs.config import DiscoveryEvidenceConfig
    from scrobble_recs.models.recommendation_quality import decide_candidate_admission

    tag = replace(
        observation(),
        source="lastfm",
        family="semantic",
        relationship_kind="artist_tag",
        source_artist_id=None,
        evidence={"legacy": True, "tag": "rock"},
    )
    personal = replace(
        tag,
        source="local_feedback",
        family="personal",
        relationship_kind="personal_feedback",
        raw_score=1,
        evidence={"action": "save"},
    )
    result = decide_candidate_admission(
        evidence_module().CandidateEvidence(2, (tag, personal)),
        stage=0,
        hidden=False,
        strongly_rejected=False,
        identity_confidence=0.9,
        config=DiscoveryEvidenceConfig(),
    )
    assert result.reasons == ("positive_personal_evidence",)


@pytest.mark.parametrize("strength,stage", [("strong", 0), ("weak", 2)])
@pytest.mark.parametrize(
    "action,value,admitted",
    [
        ("reject", 1, False),
        ("less_like_this", 1, False),
        ("note", 1, False),
        ("opened_link", 1, False),
        ("save", 0, False),
        ("save", -1, False),
        ("save", 1, True),
        ("more_like_this", 1, True),
        ("rediscovery_good", 1, True),
    ],
)
def test_only_positive_personal_actions_reinforce_other_families(
    strength, stage, action, value, admitted
):
    from scrobble_recs.config import DiscoveryEvidenceConfig
    from scrobble_recs.models.recommendation_quality import decide_candidate_admission

    collaborative = replace(observation(), evidence={"strength": strength})
    personal = replace(
        observation(),
        source="local_feedback",
        family="personal",
        relationship_kind="personal_feedback",
        raw_score=value,
        evidence={"action": action, "strength": "moderate"},
    )
    result = decide_candidate_admission(
        evidence_module().CandidateEvidence(2, (collaborative, personal)),
        stage=stage,
        hidden=False,
        strongly_rejected=False,
        identity_confidence=0.9,
        config=DiscoveryEvidenceConfig(),
    )
    assert result.admitted is admitted


@pytest.mark.parametrize(
    "field,value",
    [
        ("raw_score", -1),
        ("family", "semantic"),
        ("source_artist_id", 3),
        ("target_artist_id", 3),
        ("source_rank", 2),
        ("provider_mode", "hard"),
        ("identity_confidence", 0.6),
        ("identity_status", "ambiguous_musicbrainz"),
        ("cache_key", "another-cache"),
        ("source", "another-provider"),
        ("relationship_kind", "artist_radio_membership"),
        ("observation_key", "another-key"),
    ],
)
def test_snapshot_tracks_every_normalized_structured_value(stage_database, field, value):
    from scrobble_recs.models.recommendation_query import build_evidence_snapshot_id

    conn, _, _ = stage_database
    item = replace(
        observation(),
        family="personal",
        relationship_kind="personal_feedback",
        source_artist_id=None,
        raw_score=1,
        evidence={"action": "save"},
    )
    evidence_module().persist_evidence_observation(conn, item, observation_key="key")
    before = build_evidence_snapshot_id(conn, historical_cutoff=None)
    conn.execute(f"UPDATE artist_evidence_observation SET {field} = ?", [value])
    assert build_evidence_snapshot_id(conn, historical_cutoff=None) != before


@pytest.mark.parametrize("key", ["legacy", "confidence_projection", "stored_identity_confidence"])
def test_normalized_writer_reserves_translated_legacy_provenance_keys(stage_database, key):
    conn, _, _ = stage_database
    with pytest.raises(ValueError, match="reserved"):
        evidence_module().persist_evidence_observation(
            conn, replace(observation(), evidence={key: True}), observation_key="spoof"
        )


@pytest.mark.parametrize(
    "confidence,reason", [(0.6, "identity_below_safety_threshold"), (0.0, "identity_below_minimum")]
)
@pytest.mark.parametrize("legacy_score", [0.01, 0.9])
def test_normalized_lastfm_rows_cannot_spoof_legacy_identity_compatibility(
    stage_database, confidence, reason, legacy_score
):
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    conn.execute("UPDATE artist SET identity_confidence = ? WHERE artist_id = 2", [confidence])
    conn.execute(
        "UPDATE artist_similarity_edge SET normalized_score = ? WHERE target_artist_id = 2",
        [legacy_score],
    )
    evidence_module().persist_evidence_observation(
        conn, replace(observation(), source="lastfm"), observation_key="normalized-pair"
    )
    # Previously stored or externally inserted rows cannot self-declare trusted origin.
    conn.execute(
        "UPDATE artist_evidence_observation "
        'SET evidence = \'{"legacy":true,"normalized_score":0.9}\''
    )
    result = retrieve_with_backoff(conn, context=context, config=config, limit=10)
    assert not result.admissions[2].admitted
    assert result.admissions[2].reasons == (reason,)


@pytest.mark.parametrize("limit", [1, 2])
@pytest.mark.parametrize("exclusion", ["locally_listened", "obvious_candidate"])
def test_backoff_counts_usable_candidates_after_mode_exclusions(stage_database, limit, exclusion):
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    conn.execute("UPDATE artist SET identity_confidence = .9")
    if exclusion == "locally_listened":
        conn.execute(
            "INSERT INTO artist_listening_fact (artist_id, play_count, active_days, knownness) "
            "VALUES (2, 5, 3, .6), (3, 5, 3, .6)"
        )
    else:
        config.recommendation.obviousness.enabled = True
        config.recommendation.obviousness.top_n_per_tag_hide = 1
        conn.execute(
            "INSERT INTO artist_tag (artist_id, tag, source, normalized_weight) "
            "VALUES (1, 'rock', 'lastfm', 1)"
        )
        conn.execute(
            "INSERT INTO tag_top_artist (artist_id, tag, source, tag_rank) "
            "VALUES (2, 'rock', 'lastfm', 1), (3, 'rock', 'lastfm', 1)"
        )
    conn.execute(
        "INSERT INTO artist_similarity_edge "
        "(source_artist_id, target_artist_id, source, normalized_score) "
        "VALUES (1, 4, 'lastfm', .01)"
    )
    weak = replace(observation(), target_artist_id=4, evidence={"strength": "weak"})
    evidence_module().persist_evidence_observation(conn, weak, observation_key="weak")
    evidence_module().persist_evidence_observation(
        conn,
        replace(
            weak, source="tags", family="semantic", relationship_kind="artist_tag", evidence={}
        ),
        observation_key="tag",
    )
    retrieval = retrieve_with_backoff(conn, context=context, config=config, limit=limit)
    assert retrieval.admissions[4].stage == 2
    assert retrieval.admissions[4].admitted
    assert retrieval.diagnostics["admitted_count"] == 1
    assert retrieval.diagnostics["exhausted"] is (limit > 1)
    assert retrieval.diagnostics["backoff_stages"][0]["admitted_count"] == 0
    ranked = rank_candidates(conn, context=context, retrieval=retrieval, config=config)
    assert [row.artist_id for row in ranked if row.legacy.diagnostic.state == "eligible"] == [4]
    assert next(row for row in ranked if row.artist_id == 4).candidate_score > 0
    assert all(
        row.legacy.diagnostic.reasons == (exclusion,) for row in ranked if row.artist_id in (2, 3)
    )


@pytest.mark.parametrize("extra,expected_rows", [(0, 3), (998, 1001)])
def test_candidate_details_are_written_in_one_set_insert(stage_database, extra, expected_rows):
    from scrobble_recs.models.policy import apply_slate_policy
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.recommendation_store import (
        RecommendationRunPayload,
        persist_recommendation_run,
    )
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    if extra:
        conn.execute(
            "INSERT INTO artist (artist_id, canonical_name, artist_key) "
            "SELECT 100+range, 'Artist '||range, 'artist-'||range FROM range(?)",
            [extra],
        )
        conn.execute(
            "INSERT INTO tag_top_artist (artist_id, tag, source, tag_rank) "
            "SELECT 100+range, 'rock', 'lastfm', 50 FROM range(?)",
            [extra],
        )
    retrieval = retrieve_with_backoff(conn, context=context, config=config, limit=2)
    ranked = rank_candidates(conn, context=context, retrieval=retrieval, config=config)
    decisions = apply_slate_policy(conn, context=context, ranked=ranked, limit=2, config=config)
    payload = RecommendationRunPayload(
        "00000000-0000-0000-0000-000000000090",
        context,
        config,
        retrieval,
        ranked,
        decisions,
        tuple(row.legacy.diagnostic for row in ranked),
        "explicit",
    )
    inserts, updates = [], []

    class ObservedConnection:
        def execute(self, sql, parameters=None):
            normalized = " ".join(sql.lower().split())
            if normalized.startswith("insert into recommendation_candidate "):
                inserts.append(len(parameters[0]))
            if "update recommendation_candidate" in normalized:
                updates.append(sql)
            return conn.execute(sql, parameters) if parameters is not None else conn.execute(sql)

        def executemany(self, sql, parameters):
            raise AssertionError("candidate persistence must not use executemany")

    persist_recommendation_run(ObservedConnection(), payload=payload)
    assert inserts == [expected_rows]
    assert updates == []
    assert conn.execute(
        "SELECT count(*) FROM recommendation_candidate "
        "WHERE evidence IS NOT NULL AND admission IS NOT NULL"
    ).fetchone() == (extra + 3,)


def test_backoff_does_not_count_source_admitted_but_walk_unreachable_candidates(stage_database):
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    conn.execute("UPDATE artist SET identity_confidence = .9")
    conn.execute("DELETE FROM artist_similarity_edge WHERE target_artist_id = 3")
    conn.execute("DELETE FROM tag_top_artist")
    conn.execute("INSERT INTO artist_listening_fact (artist_id, play_count) VALUES (2, 5)")
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key, identity_confidence) "
        "VALUES (5, 'Unreachable', 'unreachable', .9)"
    )
    conn.execute(
        "INSERT INTO artist_similarity_edge "
        "(source_artist_id, target_artist_id, source, normalized_score) "
        "VALUES (2, 4, 'lastfm', .9)"
    )
    config.recommendation.scoring.engine = "walk"
    config.recommendation.walk.min_distinct_seeds = 1
    config.recommendation.walk.min_lift = -100
    weak = replace(observation(), target_artist_id=4, evidence={"strength": "weak"})
    evidence_module().persist_evidence_observation(conn, weak, observation_key="reachable")
    strong = replace(observation(), target_artist_id=5, evidence={"strength": "strong"})
    evidence_module().persist_evidence_observation(conn, strong, observation_key="unreachable")
    evidence_module().persist_evidence_observation(
        conn,
        replace(strong, source="tags", family="semantic", relationship_kind="artist_tag"),
        observation_key="tag",
    )
    retrieval = retrieve_with_backoff(conn, context=context, config=config, limit=1)
    assert retrieval.admissions[5].admitted and retrieval.admissions[5].stage == 0
    assert retrieval.admissions[4].admitted and retrieval.admissions[4].stage == 3
    assert retrieval.diagnostics["backoff_stages"][0]["admitted_count"] == 0
    assert retrieval.diagnostics["admitted_count"] == 1
    ranked = {
        row.artist_id: row
        for row in rank_candidates(conn, context=context, retrieval=retrieval, config=config)
    }
    assert ranked[5].legacy.diagnostic.reasons == ("walk_unreachable",)
    assert ranked[4].legacy.diagnostic.state == "eligible"
