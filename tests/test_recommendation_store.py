"""Persistence is exposed through a transaction-owning run payload."""

from copy import deepcopy
from dataclasses import replace
from importlib import import_module, util

import pytest
from test_retrieval import stage_database as stage_database


def make_payload(stage_database):
    retrieval = import_module("scrobble_recs.models.retrieval")
    ranking = import_module("scrobble_recs.models.ranking")
    policy = import_module("scrobble_recs.models.policy")
    store = import_module("scrobble_recs.models.recommendation_store")
    conn, config, context = stage_database
    candidates = retrieval.retrieve_legacy_candidates(conn, context=context, config=config)
    ranked = ranking.rank_candidates(conn, context=context, retrieval=candidates, config=config)
    decisions = policy.apply_slate_policy(
        conn, context=context, ranked=ranked, limit=2, config=config
    )
    decisions = ranking.build_policy_explanations(decisions, candidates)
    diagnostics = tuple(
        item.legacy.diagnostic
        for item in sorted(ranked, key=lambda item: item.legacy.diagnostic_order)
    )
    return conn, store.RecommendationRunPayload(
        "00000000-0000-0000-0000-000000000001",
        context,
        config,
        candidates,
        ranked,
        decisions,
        diagnostics,
        "explicit",
    )


def test_store_persists_seed_ids_and_does_not_mutate_payload(stage_database):
    store = import_module("scrobble_recs.models.recommendation_store")
    conn, payload = make_payload(stage_database)
    before = deepcopy(payload)
    stats = store.persist_recommendation_run(conn, payload=payload)
    assert stats.recommendations == 2
    assert payload == before
    assert conn.execute(
        "SELECT candidate_sources FROM recommendation_candidate WHERE artist_id = 2"
    ).fetchone() == ("[1]",)
    assert conn.execute("SELECT artist_id FROM recommendation ORDER BY rank").fetchall() == [
        (2,),
        (3,),
    ]


def test_run_payload_copies_caller_sequences_and_configuration(stage_database):
    _, original = make_payload(stage_database)
    ranked = list(original.ranked)
    config = original.config.model_copy(deep=True)
    payload = replace(original, ranked=ranked, config=config)
    ranked.clear()
    config.recommendation.calibration.enabled = True
    assert len(payload.ranked) == 3
    assert payload.config.recommendation.calibration.enabled is False


def test_run_payload_owns_retrieval_evidence_after_caller_mutation(stage_database):
    from scrobble_recs.models.recommendation_store import persist_recommendation_run

    conn, original = make_payload(stage_database)
    caller_retrieval = original.retrieval
    payload = replace(original, retrieval=caller_retrieval)
    before = deepcopy(payload)
    caller_retrieval.legacy.scoring_data.similarity_by_target[2].append((99, 0.8))
    caller_retrieval.legacy.inputs.tag_profile["changed after payload"] = 1.0
    caller_retrieval.diagnostics["changed after payload"] = True
    assert payload == before
    assert payload.retrieval is not caller_retrieval
    persist_recommendation_run(conn, payload=payload)
    assert conn.execute(
        "SELECT candidate_sources FROM recommendation_candidate WHERE artist_id = 2"
    ).fetchone() == ("[1]",)
    assert payload.ranked[0].legacy.retrieval.scoring_data.similarity_by_target[2] == [(1, 0.9)]


def test_result_insert_failure_rolls_back_all_three_tables(stage_database):
    store = import_module("scrobble_recs.models.recommendation_store")
    conn, payload = make_payload(stage_database)

    class FailSecondResult:
        results = 0

        def execute(self, query, parameters=None):
            if "INSERT INTO recommendation (" in query:
                self.results += 1
                if self.results == 2:
                    assert conn.execute("SELECT count(*) FROM recommendation").fetchone() == (1,)
                    raise RuntimeError("forced second result failure")
            return (
                conn.execute(query, parameters) if parameters is not None else conn.execute(query)
            )

        def executemany(self, query, parameters):
            return conn.executemany(query, parameters)

    with pytest.raises(RuntimeError, match="forced second result failure"):
        store.persist_recommendation_run(FailSecondResult(), payload=payload)
    for table in ("recommendation_run", "recommendation_candidate", "recommendation"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_store_exposes_payload_and_transaction_entrypoint() -> None:
    assert util.find_spec("scrobble_recs.models.recommendation_store") is not None
    module = import_module("scrobble_recs.models.recommendation_store")
    assert module.RecommendationRunPayload.__dataclass_params__.frozen
    assert callable(module.persist_recommendation_run)
