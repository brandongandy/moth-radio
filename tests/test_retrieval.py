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
