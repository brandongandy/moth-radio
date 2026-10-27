"""Offline contracts for the legacy retrieval boundary."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from importlib import import_module, util

import pytest
from test_recommend import write_config

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.models.profile_window import ListeningWindow
from scrobble_recs.models.recommendation_query import QueryContext, TasteRegionSeed


@pytest.fixture
def stage_database(tmp_path):
    config = load_config(write_config(tmp_path))
    config.recommendation.obviousness.enabled = False
    config.recommendation.diversity.enabled = False
    conn = initialize_database(config)
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key) VALUES "
        "(1, 'Seed', 'seed'), (2, 'Zulu', 'zulu'), (3, 'Alpha', 'alpha'), "
        "(4, 'Tag Only', 'tag only')"
    )
    conn.execute(
        "INSERT INTO artist_similarity_edge "
        "(source_artist_id, target_artist_id, source, normalized_score) "
        "VALUES (1, 2, 'lastfm', 0.9), (1, 3, 'lastfm', 0.9)"
    )
    conn.execute(
        "INSERT INTO tag_top_artist (artist_id, tag, source, tag_rank) "
        "VALUES (4, 'rock', 'lastfm', 20)"
    )
    context = QueryContext(
        query_kind="general",
        mode="balanced",
        novelty_intent="mixed",
        history_window=ListeningWindow(datetime(2023, 1, 1), datetime(2025, 1, 1)),
        taste_region_seeds=(TasteRegionSeed(1, 1.0, ("core",)),),
        evidence_snapshot_id="fixture",
        config_hash="fixture",
    )
    try:
        yield conn, config, context
    finally:
        conn.close()


def test_retrieval_keeps_direct_and_tag_only_provenance_without_scoring(stage_database):
    assert util.find_spec("scrobble_recs.models.retrieval") is not None
    module = import_module("scrobble_recs.models.retrieval")
    conn, config, context = stage_database
    result = module.retrieve_legacy_candidates(conn, context=context, config=config)
    assert [candidate.artist_id for candidate in result.candidates] == [2, 3, 4]
    assert result.candidates[0].provenance == (
        {"source": "lastfm", "seed_artist_id": 1, "score": 0.9},
    )
    assert result.candidates[2].provenance == ({"source": "lastfm_tag", "tag": "rock", "rank": 20},)
    assert result.candidates[2].source_values["lastfm_similarity"] is None


def test_retrieval_empty_evidence_returns_empty_pool(stage_database):
    assert util.find_spec("scrobble_recs.models.retrieval") is not None
    module = import_module("scrobble_recs.models.retrieval")
    conn, config, context = stage_database
    conn.execute("DELETE FROM artist_similarity_edge")
    conn.execute("DELETE FROM tag_top_artist")
    assert module.retrieve_legacy_candidates(conn, context=context, config=config).candidates == ()


def test_retrieval_records_copy_caller_owned_nested_inputs() -> None:
    assert util.find_spec("scrobble_recs.models.retrieval") is not None
    module = import_module("scrobble_recs.models.retrieval")
    provenance = ({"seed_ids": [7]},)
    values = {"direct": 0.8}
    candidate = module.RetrievalCandidate(9, "Nine", provenance, values)
    diagnostics = {"sources": ["lastfm"]}
    result = module.RetrievalResult((candidate,), diagnostics)
    before = deepcopy(result)
    provenance[0]["seed_ids"].append(8)
    values["direct"] = 0.1
    diagnostics["sources"].append("tag")
    assert result == before
    with pytest.raises(FrozenInstanceError):
        candidate.artist_id = 10


def test_retrieval_keeps_walk_reachability_and_ignores_nonfinite_edges(stage_database):
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    config.recommendation.scoring.engine = "walk"
    conn.execute(
        "UPDATE artist_similarity_edge SET normalized_score = 'NaN' WHERE target_artist_id = 3"
    )
    result = retrieve_legacy_candidates(conn, context=context, config=config)
    assert result.legacy.walk_result.nodes[2].hop_distance == 1
    assert result.legacy.walk_result.nodes[3].ppr_personalized == 0.0
    assert result.legacy.walk_result.nodes[3].lift == float("-inf")
    assert 4 not in result.legacy.walk_result.nodes


def test_retrieval_historical_listening_uses_exclusive_cutoff(stage_database):
    from scrobble_recs.models.retrieval import retrieve_legacy_candidates

    conn, config, context = stage_database
    conn.execute("""
        INSERT INTO scrobble
            (scrobble_id, artist_key, track_key, played_at, played_date, is_duplicate)
        VALUES (1, 'zulu', 'a', '2024-01-01', '2024-01-01', false),
               (2, 'zulu', 'b', '2025-01-01', '2025-01-01', false)
    """)
    result = retrieve_legacy_candidates(
        conn, context=replace(context, history_cutoff=datetime(2025, 1, 1)), config=config
    )
    assert result.legacy.scoring_data.listening_by_artist[2][2:4] == (1, 1)


def test_feedback_seed_expansion_preserves_current_candidates_and_snapshot(stage_database):
    from scrobble_recs.models.feedback import FeedbackProfile
    from scrobble_recs.models.profile import SeedArtist
    from scrobble_recs.models.recommendation_query import TasteRegionSeed
    from scrobble_recs.models.retrieval import (
        _add_feedback_seed_artists,
        retrieve_legacy_candidates,
    )

    conn, config, context = stage_database
    conn.execute("""
        INSERT INTO artist_similarity_edge
            (source_artist_id, target_artist_id, source, normalized_score)
        VALUES (4, 3, 'lastfm', 0.8), (2, 3, 'lastfm', 0.7)
    """)
    initial = [SeedArtist(1, "Seed", "seed", "Seed", 2, 1.0, ("lifetime",))]
    expanded = _add_feedback_seed_artists(
        conn, initial, FeedbackProfile(boosted_seed_artist_ids={2, 4})
    )
    assert [seed.artist_id for seed in expanded] == [1, 4]
    assert initial == [SeedArtist(1, "Seed", "seed", "Seed", 2, 1.0, ("lifetime",))]
    context = replace(
        context,
        taste_region_seeds=tuple(
            TasteRegionSeed(seed.artist_id, seed.seed_weight, seed.regions) for seed in expanded
        ),
    )
    result = retrieve_legacy_candidates(conn, context=context, config=config)
    assert [item.artist_id for item in result.candidates] == [2, 3]
    assert {edge["seed_artist_id"] for edge in result.candidates[1].provenance} == {1, 2, 4}


def test_fusion_source_lists_are_query_local_ranked_and_membership_unranked(stage_database):
    from scrobble_recs.models.evidence import EvidenceObservation, persist_evidence_observation
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    config.recommendation.scoring.engine = "fusion"
    config.recommendation.fusion.enabled = True
    config.apis.listenbrainz.enabled = True
    config.apis.listenbrainz.candidate_generation_enabled = True
    persist_evidence_observation(
        conn,
        EvidenceObservation(
            "listenbrainz",
            "collaborative",
            "artist_radio_membership",
            1,
            2,
            None,
            None,
            "easy",
            datetime(2020, 1, 1),
            0.95,
            {"raw_provider": {"artist": "Zulu"}},
        ),
        observation_key="radio-test",
    )
    result = retrieve_with_backoff(conn, context=context, config=config, limit=10)
    direct = [r for r in result.source_candidates if r.source == "lastfm_similarity"]
    assert [(r.artist_id, r.source_rank) for r in direct] == [(2, 1), (3, 2)]
    radio = [r for r in result.source_candidates if r.source == "listenbrainz"]
    assert len(radio) == 1 and radio[0].source_rank is None and not radio[0].ranked
    assert result.diagnostics["fusion_sources"]["listenbrainz"]["status"] == "stale"
    assert result.diagnostics["fusion_sources"]["musicbrainz"]["status"] == "disabled"
    assert (
        result.diagnostics["fusion_sources"]["lastfm_similarity"]["calibration"]
        == "seed-weighted-normalized-v1"
    )
    assert result.legacy.walk_result is not None


@pytest.mark.parametrize("corruption", ["raw_score = 'NaN'", "family = 'semantic'"])
def test_fusion_quarantines_malformed_observations_without_losing_valid_rows(
    stage_database, corruption
):
    from scrobble_recs.models.evidence import EvidenceObservation, persist_evidence_observation
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    config.recommendation.scoring.engine = "fusion"
    config.recommendation.fusion.enabled = True
    config.apis.listenbrainz.enabled = True
    config.apis.listenbrainz.candidate_generation_enabled = True
    for target in (2, 3):
        persist_evidence_observation(
            conn,
            EvidenceObservation(
                "listenbrainz",
                "collaborative",
                "artist_radio_membership",
                1,
                target,
                None,
                None,
                "easy",
                datetime(2020, 1, 1),
                0.95,
                {},
            ),
            observation_key=str(target),
        )
    conn.execute(f"UPDATE artist_evidence_observation SET {corruption} WHERE target_artist_id = 3")
    result = retrieve_with_backoff(conn, context=context, config=config, limit=10)
    assert [r.artist_id for r in result.source_candidates if r.source == "listenbrainz"] == [2]
    assert result.diagnostics["invalid_observations"] == [
        {
            "source": "listenbrainz",
            "source_artist_id": 1,
            "target_artist_id": 3,
            "reason": "invalid_observation",
        }
    ]
    assert any(r.source == "lastfm_similarity" for r in result.source_candidates)


def test_fusion_excludes_only_invalid_legacy_edge_and_uses_seed_weighted_order(stage_database):
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    config.recommendation.scoring.engine = "fusion"
    config.recommendation.fusion.enabled = True
    conn.execute(
        "INSERT INTO artist_similarity_edge "
        "(source_artist_id, target_artist_id, source, normalized_score) "
        "VALUES (2, 3, 'lastfm', .1), (2, 4, 'lastfm', .8), (4, 3, 'lastfm', 'NaN')"
    )
    context = replace(
        context,
        taste_region_seeds=(TasteRegionSeed(1, 1, ("core",)), TasteRegionSeed(2, 9, ("core",))),
    )
    result = retrieve_with_backoff(conn, context=context, config=config, limit=10)
    assert [
        (r.artist_id, r.source_rank)
        for r in result.source_candidates
        if r.source == "lastfm_similarity"
    ] == [(4, 1), (3, 2)]
    assert len(result.diagnostics["invalid_legacy_rows"]) == 1
    assert result.legacy.scoring_data.similarity_by_target[3] == [(1, 0.9), (2, 0.1)]


def test_fusion_source_availability_keeps_missing_disabled_and_unavailable_distinct(stage_database):
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    config.recommendation.scoring.engine = "fusion"
    config.recommendation.fusion.enabled = True
    config.recommendation.fusion.excluded_sources = ["semantic"]
    config.apis.listenbrainz.enabled = True
    config.apis.listenbrainz.candidate_generation_enabled = True
    conn.execute("DELETE FROM artist_similarity_edge")
    result = retrieve_with_backoff(conn, context=context, config=config, limit=10)
    statuses = result.diagnostics["fusion_sources"]
    assert statuses["lastfm_similarity"]["status"] == "missing"
    assert statuses["lastfm_walk"]["status"] == "unavailable"
    assert statuses["semantic"]["status"] == "disabled"
    assert statuses["listenbrainz"]["status"] == "missing"
    assert statuses["musicbrainz"]["status"] == "disabled"


def test_fusion_source_lists_ignore_legacy_insertion_order(stage_database):
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    config.recommendation.scoring.engine = "fusion"
    config.recommendation.fusion.enabled = True
    first = retrieve_with_backoff(conn, context=context, config=config, limit=10)
    conn.execute("CREATE TEMP TABLE edges AS SELECT * FROM artist_similarity_edge")
    conn.execute("DELETE FROM artist_similarity_edge")
    conn.execute(
        "INSERT INTO artist_similarity_edge SELECT * FROM edges ORDER BY target_artist_id DESC"
    )
    second = retrieve_with_backoff(conn, context=context, config=config, limit=10)
    assert first.source_candidates == second.source_candidates
    assert first.fused_candidates == second.fused_candidates
    assert first.diagnostics == second.diagnostics


def test_fusion_walk_does_not_remove_lastfm_only_compatibility_admission(stage_database):
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    legacy = retrieve_with_backoff(conn, context=context, config=config, limit=10)
    assert legacy.admissions[2].admitted is True
    config.recommendation.scoring.engine = "fusion"
    config.recommendation.fusion.enabled = True
    # The single-seed graph fails walk's two-seed gate but passes direct admission.
    fusion = retrieve_with_backoff(conn, context=context, config=config, limit=10)
    assert fusion.admissions[2] == legacy.admissions[2]


@pytest.mark.parametrize("production", [False, True])
def test_fusion_seed_tag_quarantine_precedes_profile_and_is_order_independent(
    stage_database, production
):
    import json

    from scrobble_recs.models.recommend import generate_recommendations
    from scrobble_recs.models.retrieval import retrieve_with_backoff

    conn, config, context = stage_database
    config.recommendation.scoring.engine = "fusion"
    config.recommendation.fusion.enabled = True
    config.recommendation.fatigue.enabled = False
    conn.execute(
        "INSERT INTO scrobble (scrobble_id, artist_key, track_key, "
        "played_at, played_date, is_duplicate) VALUES (900, 'seed', 'one', "
        "'2024-01-01', '2024-01-01', false), (901, 'seed', 'two', "
        "'2024-01-02', '2024-01-02', false)"
    )
    results = []
    for reverse in (False, True):
        conn.execute("DELETE FROM artist_tag")
        facts = [(1, "valid", 1.0), (1, "bad", float("nan")), (2, "valid", 1.0)]
        conn.executemany(
            "INSERT INTO artist_tag (artist_id, tag, source, "
            "normalized_weight, fetched_at) VALUES (?, ?, 'lastfm', ?, "
            "'2020-01-01')",
            list(reversed(facts)) if reverse else facts,
        )
        if production:
            stats = generate_recommendations(
                conn,
                config,
                limit=10,
                as_of=datetime(2025, 1, 1),
                history_window=context.history_window,
            )
            evidence, feature = conn.execute(
                "SELECT evidence, feature_record FROM "
                "recommendation_candidate WHERE run_id = ? AND artist_id = 2",
                [stats.run_id],
            ).fetchone()
            diagnostics = json.loads(
                conn.execute(
                    "SELECT seed_summary FROM recommendation_run WHERE run_id = ?", [stats.run_id]
                ).fetchone()[0]
            )["retrieval_diagnostics"]
            result = (json.loads(evidence)["fusion"]["source_ranks"]["semantic"], feature)
        else:
            retrieval = retrieve_with_backoff(conn, context=context, config=config, limit=10)
            assert retrieval.legacy.inputs.tag_profile == {"valid": 1.0}
            diagnostics = retrieval.diagnostics
            result = tuple(r for r in retrieval.source_candidates if r.source == "semantic")
            assert [(r.artist_id, r.source_rank) for r in result] == [(2, 1)]
        assert [r["kind"] for r in diagnostics["invalid_legacy_rows"]] == ["tag"]
        results.append(result)
    assert results[0] == results[1]
