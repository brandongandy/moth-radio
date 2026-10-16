"""Contracts for explicit, inspectable relevance records."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from importlib import import_module, util
from math import isfinite

import pytest
from test_retrieval import stage_database as stage_database


def test_ranking_retains_rejected_pool_and_names_missing_feedback(stage_database):
    assert util.find_spec("scrobble_recs.models.ranking") is not None
    retrieval = import_module("scrobble_recs.models.retrieval")
    ranking = import_module("scrobble_recs.models.ranking")
    conn, config, context = stage_database
    candidates = retrieval.retrieve_legacy_candidates(conn, context=context, config=config)
    before = deepcopy(candidates)
    ranked = ranking.rank_candidates(conn, context=context, retrieval=candidates, config=config)
    assert candidates == before
    assert [item.artist_id for item in ranked] == [2, 3, 4]
    assert ranked[0].features.values["lastfm_similarity_score"] == 0.9
    assert "feedback" in ranked[0].features.missing_signals
    assert ranked[-1].legacy.diagnostic.state == "rejected"


def test_ranking_records_copy_features_and_explanations() -> None:
    assert util.find_spec("scrobble_recs.models.ranking") is not None
    module = import_module("scrobble_recs.models.ranking")
    values = {"feedback_score": None}
    missing = ["feedback"]
    features = module.RelevanceFeatureRecord("legacy-features-v1", values, missing)
    explanation = {"paths": [[1, 2]]}
    ranked = module.RankedCandidate(2, 0.8, 0.7, features, explanation)
    before = deepcopy(ranked)
    values["feedback_score"] = 0.0
    missing.append("tags")
    explanation["paths"][0].append(3)
    assert ranked == before
    assert ranked.features.missing_signals == ("feedback",)
    assert ranked.features.values["feedback_score"] is None


def test_calibration_uses_only_the_current_eligible_candidate_set(stage_database):
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    config.recommendation.calibration.enabled = True
    conn.execute(
        "UPDATE artist_similarity_edge SET normalized_score = 0.5 WHERE target_artist_id = 3"
    )
    conn.execute("INSERT INTO artist_listening_fact (artist_id, play_count) VALUES (3, 1)")
    ranked = rank_candidates(
        conn,
        context=context,
        retrieval=retrieve_legacy_candidates(conn, context=context, config=config),
        config=config,
    )
    survivor = next(item for item in ranked if item.artist_id == 2)
    assert survivor.features.values["calibrated_lastfm_similarity"] == 1.0
    excluded = next(item for item in ranked if item.artist_id == 3)
    assert excluded.legacy.diagnostic.reasons == ("locally_listened",)


def test_ranking_respects_the_supplied_retrieval_pool_without_mutating_details(stage_database):
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    config.recommendation.calibration.enabled = True
    pool = retrieve_legacy_candidates(conn, context=context, config=config)
    subset = replace(pool, candidates=(pool.candidates[1],))
    before = deepcopy(subset)
    ranked = rank_candidates(conn, context=context, retrieval=subset, config=config)
    assert [item.artist_id for item in ranked] == [3]
    assert ranked[0].features.values["calibrated_lastfm_similarity"] == 1.0
    assert subset == before


def test_ranking_owns_one_shared_compatibility_snapshot(stage_database):
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    pool = retrieve_legacy_candidates(conn, context=context, config=config)
    ranked = rank_candidates(conn, context=context, retrieval=pool, config=config)
    before = deepcopy(ranked)
    owned_details = ranked[0].legacy.retrieval
    assert all(item.legacy.retrieval is owned_details for item in ranked)
    pool.legacy.inputs.tag_profile["changed after ranking"] = 1.0
    pool.legacy.inputs.idf_weights.effective_weight_by_seed[1] = 99.0
    pool.legacy.inputs.feedback_profile.hidden_artist_ids.add(2)
    pool.legacy.scoring_data.similarity_by_target[2].append((99, 0.8))
    pool.legacy.scoring_data.artist_names[2] = "Changed Artist"
    assert ranked == before
    assert owned_details is not pool.legacy
    assert owned_details.scoring_data is not pool.legacy.scoring_data


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity"])
def test_walk_nonfinite_edges_cannot_become_ranked_evidence(stage_database, invalid):
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    config.recommendation.scoring.engine = "walk"
    config.recommendation.walk.min_distinct_seeds = 1
    conn.execute(
        "UPDATE artist_similarity_edge SET normalized_score = ? WHERE target_artist_id = 3",
        [float(invalid)],
    )
    ranked = rank_candidates(
        conn,
        context=context,
        retrieval=retrieve_legacy_candidates(conn, context=context, config=config),
        config=config,
    )
    invalid_candidate = next(item for item in ranked if item.artist_id == 3)
    assert invalid_candidate.legacy.diagnostic.reasons == ("walk_unreachable",)
    assert invalid_candidate.relevance_score == 0.0


def test_tag_fit_is_explicitly_missing_when_the_listener_has_no_tag_profile(stage_database):
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    conn.execute(
        "INSERT INTO artist_tag (artist_id, tag, source, normalized_weight) "
        "VALUES (2, 'rock', 'lastfm', 1.0)"
    )
    ranked = rank_candidates(
        conn,
        context=context,
        retrieval=retrieve_legacy_candidates(conn, context=context, config=config),
        config=config,
    )
    assert "tag_similarity" in ranked[0].features.missing_signals


def test_runtime_as_of_is_distinct_from_explicit_history_window(stage_database):
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    context = replace(context, mode="rediscovery", novelty_intent="rediscover")
    conn.execute(
        "INSERT INTO artist_listening_fact "
        "(artist_id, play_count, active_days, last_played_at, rediscovery_score) "
        "VALUES (2, 3, 2, '2020-01-01', 0.5)"
    )
    pool = retrieve_legacy_candidates(conn, context=context, config=config)
    pool = replace(
        pool,
        legacy=replace(pool.legacy, inputs=replace(pool.legacy.inputs, as_of=datetime(2030, 1, 1))),
    )
    ranked = rank_candidates(conn, context=context, retrieval=pool, config=config)
    candidate = next(item for item in ranked if item.artist_id == 2)
    assert candidate.legacy.score.years_since_last_listen == pytest.approx(10.001368925393566)
    assert context.history_window.end == datetime(2025, 1, 1)


@pytest.mark.parametrize(
    ("edge", "state"),
    [(float("nan"), "rejected"), (float("-inf"), "rejected"), (float("inf"), "eligible")],
)
def test_legacy_calibration_preserves_nonfinite_projection(stage_database, edge, state):
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    config.recommendation.calibration.enabled = True
    conn.execute(
        "UPDATE artist_similarity_edge SET normalized_score = ? WHERE target_artist_id = 2", [edge]
    )
    ranked = rank_candidates(
        conn,
        context=context,
        retrieval=retrieve_legacy_candidates(conn, context=context, config=config),
        config=config,
    )
    candidate = next(item for item in ranked if item.artist_id == 2)
    assert candidate.legacy.diagnostic.state == state
    assert isfinite(candidate.relevance_score)
    if state == "eligible":
        assert candidate.features.values["calibrated_lastfm_similarity"] == 1.0


@pytest.mark.parametrize("pool_size", [8, 64])
def test_full_walk_explanation_work_is_bounded_by_selected_slate(
    stage_database, monkeypatch, pool_size
):
    from scrobble_recs.models import ranking
    from scrobble_recs.models.policy import apply_slate_policy
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    config.recommendation.scoring.engine = "walk"
    config.recommendation.calibration.enabled = True
    config.recommendation.walk.min_distinct_seeds = 1
    config.recommendation.walk.min_lift = -100.0
    config.recommendation.walk.baseline_exponent = 2.0
    conn.executemany(
        "INSERT INTO artist (artist_id, canonical_name, artist_key) VALUES (?, ?, ?)",
        [
            (10 + index, f"Candidate {index}", f"candidate-{index}")
            for index in range(pool_size - 2)
        ],
    )
    conn.executemany(
        "INSERT INTO artist_similarity_edge "
        "(source_artist_id, target_artist_id, source, normalized_score) "
        "VALUES (1, ?, 'lastfm', 0.9)",
        [(10 + index,) for index in range(pool_size - 2)],
    )
    pool = retrieve_legacy_candidates(conn, context=context, config=config)
    path_targets = []
    explanation_targets = []
    path_edge_visits = 0
    original_paths = ranking.top_paths
    original_explanation = ranking._explanation

    def count_paths(result, *, target, seed_ids, max_paths):
        nonlocal path_edge_visits
        path_targets.append(target)
        path_edge_visits += sum(len(edges) for edges in result._forward.values())
        return original_paths(result, target=target, seed_ids=seed_ids, max_paths=max_paths)

    def count_explanation(score, *args, **kwargs):
        explanation_targets.append(score.artist_id)
        return original_explanation(score, *args, **kwargs)

    monkeypatch.setattr(ranking, "top_paths", count_paths)
    monkeypatch.setattr(ranking, "_explanation", count_explanation)
    ranked = ranking.rank_candidates(conn, context=context, retrieval=pool, config=config)
    assert sum(item.legacy.score is not None for item in ranked) == pool_size
    assert ranked[0].explanation["raw_components"]["lastfm_similarity"] > 0.0
    assert path_targets == []
    assert explanation_targets == []
    decisions = apply_slate_policy(conn, context=context, ranked=ranked, limit=2, config=config)
    finalized = ranking.build_policy_explanations(decisions, pool)
    selected_ids = [item.artist_id for item in finalized if item.included]
    assert len(selected_ids) == 2
    assert path_targets == selected_ids
    assert explanation_targets == selected_ids
    graph_edges = sum(len(edges) for edges in pool.legacy.walk_result._forward.values())
    assert path_edge_visits == 2 * graph_edges
    assert all("walk" in item.legacy_score.explanation for item in finalized if item.included)


@pytest.mark.parametrize("unusable", ["missing_node", "negative_hop", "nonfinite_lift"])
def test_missing_walk_signal_is_candidate_specific(stage_database, unusable):
    from scrobble_recs.models.ranking import rank_candidates
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    config.recommendation.scoring.engine = "walk"
    config.recommendation.walk.min_distinct_seeds = 1
    config.recommendation.walk.baseline_exponent = 2.0
    pool = retrieve_legacy_candidates(conn, context=context, config=config)
    candidate_id = 4 if unusable == "missing_node" else 3
    if unusable == "negative_hop":
        pool.legacy.walk_result.nodes[3] = replace(
            pool.legacy.walk_result.nodes[3], hop_distance=-1
        )
    elif unusable == "nonfinite_lift":
        pool.legacy.walk_result.nodes[3] = replace(
            pool.legacy.walk_result.nodes[3], lift=float("-inf")
        )
    ranked = rank_candidates(conn, context=context, retrieval=pool, config=config)
    candidate = next(item for item in ranked if item.artist_id == candidate_id)
    assert candidate.legacy.diagnostic.reasons == ("walk_unreachable",)
    assert "walk" in candidate.features.missing_signals
    reachable = next(item for item in ranked if item.artist_id == 2)
    assert "walk" not in reachable.features.missing_signals
