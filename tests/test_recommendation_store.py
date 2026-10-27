"""Persistence is exposed through a transaction-owning run payload."""

from copy import deepcopy
from dataclasses import replace
from importlib import import_module, util
from pathlib import Path

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


def test_store_bulk_loads_candidates_and_removes_staging_file(stage_database):
    store = import_module("scrobble_recs.models.recommendation_store")
    conn, payload = make_payload(stage_database)

    class BulkLoadStatementGuard:
        candidate_bulk_loads = 0
        candidate_stage_paths: list[Path] = []
        recommendation_inserts = 0

        def execute(self, query, parameters=None):
            normalized = " ".join(query.split())
            if normalized.startswith("INSERT INTO recommendation_candidate "):
                if "FROM read_csv(" not in normalized:
                    raise AssertionError("candidate persistence must use the CSV bulk scanner")
                self.candidate_bulk_loads += 1
                assert parameters is not None
                stage_path = Path(parameters[0])
                assert stage_path.is_file()
                self.candidate_stage_paths.append(stage_path)
            elif normalized.startswith("INSERT INTO recommendation ("):
                self.recommendation_inserts += 1
            return (
                conn.execute(query, parameters) if parameters is not None else conn.execute(query)
            )

        def executemany(self, query, parameters):
            raise AssertionError("recommendation persistence must not use executemany")

    guarded = BulkLoadStatementGuard()
    stats = store.persist_recommendation_run(guarded, payload=payload)

    assert stats.recommendations == 2
    assert guarded.candidate_bulk_loads == 1
    assert len(guarded.candidate_stage_paths) == 1
    assert not guarded.candidate_stage_paths[0].exists()
    assert guarded.recommendation_inserts == 1
    assert conn.execute("SELECT count(*) FROM recommendation_candidate").fetchone() == (3,)
    assert conn.execute("SELECT count(*) FROM recommendation").fetchone() == (2,)


def test_candidate_bulk_load_failure_removes_staging_file_and_rolls_back(stage_database):
    store = import_module("scrobble_recs.models.recommendation_store")
    conn, payload = make_payload(stage_database)

    class FailCandidateBulkLoad:
        stage_path: Path | None = None

        def execute(self, query, parameters=None):
            normalized = " ".join(query.split())
            if normalized.startswith("INSERT INTO recommendation_candidate ") and (
                "FROM read_csv(" in normalized
            ):
                assert parameters is not None
                self.stage_path = Path(parameters[0])
                assert self.stage_path.is_file()
                raise RuntimeError("forced candidate bulk load failure")
            return (
                conn.execute(query, parameters) if parameters is not None else conn.execute(query)
            )

        def executemany(self, query, parameters):
            raise AssertionError("recommendation persistence must not use executemany")

    guarded = FailCandidateBulkLoad()
    with pytest.raises(RuntimeError, match="forced candidate bulk load failure"):
        store.persist_recommendation_run(guarded, payload=payload)

    assert guarded.stage_path is not None
    assert not guarded.stage_path.exists()
    for table in ("recommendation_run", "recommendation_candidate", "recommendation"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_candidate_staging_reports_incremental_progress(stage_database, monkeypatch):
    store = import_module("scrobble_recs.models.recommendation_store")
    conn, payload = make_payload(stage_database)
    events = []
    monkeypatch.setattr(store, "_CANDIDATE_STAGING_PROGRESS_INTERVAL", 2, raising=False)

    store.persist_recommendation_run(conn, payload=payload, progress_callback=events.append)

    assert [
        (event.phase, event.current, event.total, event.subject, event.status) for event in events
    ] == [
        ("persist_candidates", 0, 3, "candidate rows", "processing"),
        ("persist_candidates", 2, 3, "candidate rows", "processing"),
        ("persist_candidates", 3, 3, "candidate rows", "completed"),
    ]


def test_store_preserves_last_duplicate_candidate_diagnostic(stage_database):
    store = import_module("scrobble_recs.models.recommendation_store")
    conn, payload = make_payload(stage_database)
    original = payload.scorer_diagnostics[0]
    later = replace(original, state="rejected", reasons=("later_duplicate",))
    duplicate_payload = replace(payload, scorer_diagnostics=(original, later))

    store.persist_recommendation_run(conn, payload=duplicate_payload)

    assert conn.execute(
        "SELECT eligibility_state, eligibility_reasons "
        "FROM recommendation_candidate WHERE artist_id = ?",
        [original.artist_id],
    ).fetchone() == ("rejected", '["later_duplicate"]')


def test_store_skips_set_inserts_for_empty_collections(stage_database):
    store = import_module("scrobble_recs.models.recommendation_store")
    conn, payload = make_payload(stage_database)
    empty_payload = replace(
        payload,
        retrieval=replace(payload.retrieval, candidates=(), evidence={}, admissions={}),
        ranked=(),
        decisions=(),
        scorer_diagnostics=(),
    )
    events = []

    class EmptyInsertGuard:
        def execute(self, query, parameters=None):
            normalized = " ".join(query.split())
            assert not normalized.startswith("INSERT INTO recommendation_candidate ")
            assert not normalized.startswith("INSERT INTO recommendation (")
            return (
                conn.execute(query, parameters) if parameters is not None else conn.execute(query)
            )

        def executemany(self, query, parameters):
            raise AssertionError("empty recommendation persistence must not use executemany")

    stats = store.persist_recommendation_run(
        EmptyInsertGuard(), payload=empty_payload, progress_callback=events.append
    )

    assert stats.recommendations == 0
    assert conn.execute("SELECT count(*) FROM recommendation_run").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM recommendation_candidate").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM recommendation").fetchone() == (0,)
    assert [(event.phase, event.current, event.total, event.status) for event in events] == [
        ("persist_candidates", 0, 0, "processing"),
        ("persist_candidates", 0, 0, "completed"),
    ]


def test_result_set_insert_failure_rolls_back_all_three_tables(stage_database):
    store = import_module("scrobble_recs.models.recommendation_store")
    conn, payload = make_payload(stage_database)

    class FailResultSet:
        def execute(self, query, parameters=None):
            if "INSERT INTO recommendation (" in query:
                assert conn.execute("SELECT count(*) FROM recommendation_candidate").fetchone() == (
                    3,
                )
                raise RuntimeError("forced result set failure")
            return (
                conn.execute(query, parameters) if parameters is not None else conn.execute(query)
            )

        def executemany(self, query, parameters):
            return conn.executemany(query, parameters)

    with pytest.raises(RuntimeError, match="forced result set failure"):
        store.persist_recommendation_run(FailResultSet(), payload=payload)
    for table in ("recommendation_run", "recommendation_candidate", "recommendation"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_store_exposes_payload_and_transaction_entrypoint() -> None:
    assert util.find_spec("scrobble_recs.models.recommendation_store") is not None
    module = import_module("scrobble_recs.models.recommendation_store")
    assert module.RecommendationRunPayload.__dataclass_params__.frozen
    assert callable(module.persist_recommendation_run)
