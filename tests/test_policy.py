"""Slate decisions retain exclusion and movement reasons."""

from copy import deepcopy
from datetime import datetime
from importlib import import_module, util

import pytest
from test_retrieval import stage_database as stage_database


@pytest.mark.parametrize("diversity", [False, True])
def test_exact_mmr_and_score_ties_use_artist_ids(stage_database, diversity):
    from scrobble_recs.models.policy import apply_slate_policy
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    config.recommendation.diversity.enabled = diversity
    ranked = rank_candidates(
        conn,
        context=context,
        retrieval=retrieve_legacy_candidates(conn, context=context, config=config),
        config=config,
    )
    decisions = apply_slate_policy(
        conn, context=context, ranked=tuple(reversed(ranked)), limit=1, config=config
    )
    assert [item.artist_id for item in decisions if item.included] == [2]
    assert all(item.reasons for item in decisions if not item.included)


@pytest.mark.parametrize(
    ("action", "reason"), [("hide_artist", "hidden_artist"), ("reject", "feedback_cooldown")]
)
def test_hidden_and_recently_rejected_candidates_never_enter_slate(stage_database, action, reason):
    from scrobble_recs.models.feedback import record_feedback_event
    from scrobble_recs.models.policy import apply_slate_policy
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000001",
        artist_id=2,
        action=action,
        created_at=datetime(2024, 12, 31),
    )
    ranked = rank_candidates(
        conn,
        context=context,
        retrieval=retrieve_legacy_candidates(conn, context=context, config=config),
        config=config,
    )
    before = deepcopy(ranked)
    decisions = apply_slate_policy(conn, context=context, ranked=ranked, limit=20, config=config)
    decision = next(item for item in decisions if item.artist_id == 2)
    assert not decision.included
    assert decision.reasons == (reason,)
    assert ranked == before


def test_policy_ties_use_artist_id_and_current_quality_rejections_stay_excluded(stage_database):
    assert util.find_spec("scrobble_recs.models.policy") is not None
    retrieval = import_module("scrobble_recs.models.retrieval")
    ranking = import_module("scrobble_recs.models.ranking")
    policy = import_module("scrobble_recs.models.policy")
    conn, config, context = stage_database
    candidates = retrieval.retrieve_legacy_candidates(conn, context=context, config=config)
    ranked = ranking.rank_candidates(conn, context=context, retrieval=candidates, config=config)
    before = deepcopy(ranked)
    decisions = policy.apply_slate_policy(
        conn, context=context, ranked=ranked, limit=2, config=config
    )
    assert ranked == before
    assert [item.artist_id for item in decisions if item.included] == [2, 3]
    assert all(item.reasons for item in decisions if not item.included)
    rejected = next(item for item in decisions if item.artist_id == 4)
    assert not rejected.included
    assert rejected.reasons == ("quality_gate",)


def test_policy_record_copies_reason_sequence() -> None:
    assert util.find_spec("scrobble_recs.models.policy") is not None
    module = import_module("scrobble_recs.models.policy")
    reasons = ["hidden_artist"]
    decision = module.PolicyDecision(2, False, None, 0.0, -0.8, reasons)
    reasons.append("other")
    assert decision.reasons == ("hidden_artist",)


def test_policy_owns_hard_admission_before_candidate_calibration(stage_database):
    module = import_module("scrobble_recs.models.policy")
    assert hasattr(module, "_admission_exclusion_reason")
    from scrobble_recs.models.feedback import FeedbackProfile
    from scrobble_recs.models.modes import resolve_recommendation_mode
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    data = retrieve_legacy_candidates(conn, context=context, config=config).legacy.scoring_data
    mode = resolve_recommendation_mode("balanced", config)
    reason = module._admission_exclusion_reason(
        2,
        seed_ids={1},
        feedback_profile=FeedbackProfile(hidden_artist_ids={2}),
        cooldown_artist_ids=set(),
        scoring_data=data,
        mode=mode,
    )
    assert reason == "hidden_artist"


def test_policy_adjustment_exposes_fatigue_separately_from_relevance(stage_database):
    retrieval = import_module("scrobble_recs.models.retrieval")
    ranking = import_module("scrobble_recs.models.ranking")
    policy = import_module("scrobble_recs.models.policy")
    conn, config, context = stage_database
    conn.execute("""
        INSERT INTO recommendation_run (run_id, config_hash, mode, seed_summary, scoring_weights)
        VALUES ('00000000-0000-0000-0000-000000000001', 'x', 'balanced', '{}', '{}')
    """)
    conn.execute("""
        INSERT INTO recommendation (run_id, rank, artist_id, final_score, raw_score, explanation)
        VALUES ('00000000-0000-0000-0000-000000000001', 1, 2, 0.5, 0.5, '{}')
    """)
    candidates = retrieval.retrieve_legacy_candidates(conn, context=context, config=config)
    ranked = ranking.rank_candidates(conn, context=context, retrieval=candidates, config=config)
    decisions = policy.apply_slate_policy(
        conn, context=context, ranked=ranked, limit=2, config=config
    )
    decision = next(item for item in decisions if item.artist_id == 2)
    assert decision.adjustment < 0.0
    assert "fatigue" in decision.reasons


def test_primary_tag_cap_exclusion_names_the_actual_policy(stage_database):
    from scrobble_recs.models.policy import apply_slate_policy
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    config.recommendation.diversity.enabled = True
    config.recommendation.diversity.max_same_primary_tag_in_top_20 = 1
    conn.execute(
        "INSERT INTO artist_tag (artist_id, tag, source, normalized_weight) "
        "VALUES (2, 'rock', 'lastfm', 1.0), (3, 'rock', 'lastfm', 1.0)"
    )
    ranked = rank_candidates(
        conn,
        context=context,
        retrieval=retrieve_legacy_candidates(conn, context=context, config=config),
        config=config,
    )
    decisions = apply_slate_policy(conn, context=context, ranked=ranked, limit=3, config=config)
    assert [item.artist_id for item in decisions if item.included] == [2]
    excluded = next(item for item in decisions if item.artist_id == 3)
    assert excluded.reasons == ("primary_tag_cap",)
