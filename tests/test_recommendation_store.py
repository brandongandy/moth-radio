"""Persistence is exposed through a transaction-owning run payload."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from importlib import import_module, util
from pathlib import Path

import pytest
from test_retrieval import stage_database as stage_database


def recording_payload(stage_database):
    from test_representative import ARTIST, radio

    from scrobble_recs.apis.cache import ApiCache
    from scrobble_recs.apis.musicbrainz import recording_params
    from scrobble_recs.models.evidence import CandidateEvidence

    conn, payload = make_payload(stage_database)
    conn.execute(
        "UPDATE artist SET musicbrainz_artist_mbid = ?, "
        "resolution_status = 'external_listenbrainz', "
        "identity_confidence = .9, identity_warnings = '[]' WHERE artist_id = 2",
        [ARTIST],
    )
    raw = json.loads(Path("tests/fixtures/musicbrainz_recording.json").read_text())
    ApiCache(conn).write_response(
        source="musicbrainz",
        endpoint=f"recording/{raw['id']}",
        request_url="https://example.test",
        request_params=recording_params(),
        response_status=200,
        response_json=raw,
        fetched_at=datetime(2024, 1, 2),
        expires_at=datetime(2024, 2, 1),
        error_message=None,
    )
    observations = (radio(fetched=datetime(2024, 1, 1)),)
    return conn, replace(
        payload,
        retrieval=replace(
            payload.retrieval,
            evidence={
                **payload.retrieval.evidence,
                2: CandidateEvidence(2, observations),
            },
        ),
    )


def test_recording_snapshot_has_dual_provenance_and_never_changes_ranking(stage_database):
    import hashlib

    from scrobble_recs.models.recommendation_store import persist_recommendation_run

    conn, payload = recording_payload(stage_database)
    persist_recommendation_run(conn, payload=payload)
    before = conn.execute(
        "SELECT artist_id, rank, final_score FROM recommendation ORDER BY rank"
    ).fetchall()
    value = conn.execute("SELECT start_here FROM recommendation WHERE artist_id = 2").fetchone()[0]
    item = json.loads(value)
    assert set(item) == {"kind", "title", "musicbrainz_id", "source", "reason", "evidence"}
    assert item["evidence"]["listenbrainz"]["cache_key"] == "radio-key"
    corroboration = item["evidence"]["musicbrainz"]
    assert corroboration["fetched_at"] == "2024-01-02T00:00:00"
    assert corroboration["expires_at"] == "2024-02-01T00:00:00"
    assert corroboration["stale_policy"] == "validated_success_allowed"
    assert corroboration["cache_key"].startswith("musicbrainz:recording/")
    raw = json.loads(Path("tests/fixtures/musicbrainz_recording.json").read_text())
    assert (
        corroboration["payload_hash"]
        == hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
    )
    assert conn.execute(
        "SELECT start_here IS NULL FROM recommendation WHERE artist_id = 3"
    ).fetchone()[0]
    refreshed = dict(raw, title="Refreshed title")
    conn.execute("UPDATE api_cache SET response_json = ?::JSON", [json.dumps(refreshed)])
    assert (
        conn.execute("SELECT start_here FROM recommendation WHERE artist_id = 2").fetchone()[0]
        == value
    )
    conn.execute("DELETE FROM api_cache")
    persist_recommendation_run(
        conn, payload=replace(payload, run_id="00000000-0000-0000-0000-000000000002")
    )
    assert (
        conn.execute(
            "SELECT artist_id, rank, final_score FROM recommendation "
            "WHERE run_id = ? ORDER BY rank",
            ["00000000-0000-0000-0000-000000000002"],
        ).fetchall()
        == before
    )
    assert (
        conn.execute(
            "SELECT start_here FROM recommendation WHERE run_id = ? AND artist_id = 2",
            [payload.run_id],
        ).fetchone()[0]
        == value
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM recommendation WHERE run_id != ? AND start_here IS NOT NULL",
            [payload.run_id],
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE artist SET identity_confidence = .89 WHERE artist_id = 2",
        "UPDATE artist SET identity_warnings = '[\"conflict\"]' WHERE artist_id = 2",
        "UPDATE artist SET identity_warnings = '{}' WHERE artist_id = 2",
        "UPDATE artist SET resolution_status = 'provisional_mbid' WHERE artist_id = 2",
        "UPDATE artist SET resolution_status = 'ambiguous_musicbrainz' WHERE artist_id = 2",
        "UPDATE artist SET musicbrainz_artist_mbid = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' "
        "WHERE artist_id = 2",
        "UPDATE artist SET musicbrainz_artist_mbid = '{AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA}' "
        "WHERE artist_id = 3",
        "UPDATE api_cache SET request_params = '{}'",
        "UPDATE api_cache SET response_json = '{}'",
        "UPDATE api_cache SET response_status = 404",
    ],
)
def test_recording_attachment_fails_closed_without_affecting_artist(stage_database, mutation):
    from scrobble_recs.models.recommendation_store import persist_recommendation_run

    conn, payload = recording_payload(stage_database)
    conn.execute(mutation)
    persist_recommendation_run(conn, payload=payload)
    assert conn.execute(
        "SELECT artist_id, start_here FROM recommendation ORDER BY rank"
    ).fetchall() == [(2, None), (3, None)]


def test_recording_attachment_honors_metadata_cutoff_and_scoped_observations(stage_database):
    from scrobble_recs.models.evidence import persist_evidence_observation
    from scrobble_recs.models.recommendation_store import persist_recommendation_run

    conn, payload = recording_payload(stage_database)
    cutoff = replace(
        payload, context=replace(payload.context, feedback_cutoff=datetime(2024, 1, 1))
    )
    persist_recommendation_run(conn, payload=cutoff)
    assert (
        conn.execute("SELECT count(*) FROM recommendation WHERE start_here IS NOT NULL").fetchone()[
            0
        ]
        == 0
    )
    persist_evidence_observation(
        conn, payload.retrieval.evidence[2].observations[0], observation_key="unscoped"
    )
    scoped = replace(
        payload,
        run_id="00000000-0000-0000-0000-000000000002",
        retrieval=replace(payload.retrieval, evidence={}),
    )
    persist_recommendation_run(conn, payload=scoped)
    assert (
        conn.execute("SELECT count(*) FROM recommendation WHERE start_here IS NOT NULL").fetchone()[
            0
        ]
        == 0
    )


def test_representative_migration_is_additive_historical_and_replay_safe(stage_database):
    from scrobble_recs.db import apply_migrations
    from scrobble_recs.models.recommendation_store import persist_recommendation_run

    conn, payload = make_payload(stage_database)
    persist_recommendation_run(conn, payload=payload)
    for (name,) in conn.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'recommendation'"
    ).fetchall():
        conn.execute(f'DROP INDEX "{name}"')
    conn.execute("ALTER TABLE recommendation DROP COLUMN IF EXISTS start_here")
    apply_migrations(conn)
    apply_migrations(conn)
    assert conn.execute("SELECT start_here FROM recommendation").fetchall() == [(None,), (None,)]


def test_present_recording_generation_export_and_tui_never_construct_clients(
    stage_database,
    monkeypatch,
    tmp_path,
):
    import httpx

    from scrobble_recs.apis.lastfm import LastFmClient
    from scrobble_recs.apis.listenbrainz import ListenBrainzClient
    from scrobble_recs.apis.musicbrainz import MusicBrainzClient
    from scrobble_recs.export import export_recommendations
    from scrobble_recs.models.evidence import persist_evidence_observation
    from scrobble_recs.models.feedback import rerank_recommendation_run
    from scrobble_recs.models.recommend import generate_recommendations
    from scrobble_recs.tui.app import _detail_text
    from scrobble_recs.tui.data import TuiRecommendationStore

    conn, payload = recording_payload(stage_database)
    config = payload.config
    config.apis.listenbrainz.enabled = config.apis.musicbrainz.enabled = True
    config.apis.listenbrainz.candidate_generation_enabled = True
    config.recommendation.min_seed_scrobbles = 1
    conn.execute(
        "INSERT INTO scrobble (scrobble_id, artist_key, track_key, played_at, played_date, "
        "is_duplicate) VALUES (1, 'seed', 'song', '2024-01-01', '2024-01-01', false)"
    )
    persist_evidence_observation(
        conn, payload.retrieval.evidence[2].observations[0], observation_key="offline-radio"
    )
    # Independent semantic evidence admits the artist under the existing source-aware policy.
    semantic = replace(
        payload.retrieval.evidence[2].observations[0],
        source="lastfm",
        family="semantic",
        relationship_kind="artist_tag",
        evidence={"tag": "rock", "strength": "moderate"},
    )
    persist_evidence_observation(conn, semantic, observation_key="offline-semantic")

    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP/provider use in offline generation or presentation")

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    for cls in (LastFmClient, ListenBrainzClient, MusicBrainzClient):
        monkeypatch.setattr(cls, "__init__", forbidden)
        for name in vars(cls):
            if name.startswith(("lookup_", "get_", "search_")):
                monkeypatch.setattr(cls, name, forbidden)
    stats = generate_recommendations(conn, config, limit=2, as_of=datetime(2025, 1, 1))
    snapshot = conn.execute(
        "SELECT artist_id, start_here FROM recommendation WHERE run_id = ? ORDER BY artist_id",
        [stats.run_id],
    ).fetchall()
    item = json.loads(next(item for artist, item in snapshot if artist == 2))
    assert item["title"] == 'A, "literal" [recording]\r\n雪'
    config.apis.listenbrainz.candidate_generation_enabled = False
    excluded = generate_recommendations(conn, config, limit=2, as_of=datetime(2025, 1, 1))
    assert (
        conn.execute(
            "SELECT count(*) FROM recommendation WHERE run_id = ? AND start_here IS NOT NULL",
            [excluded.run_id],
        ).fetchone()[0]
        == 0
    )
    conn.execute("DELETE FROM api_cache")
    conn.execute("DELETE FROM artist_evidence_observation")
    conn.execute("UPDATE artist SET resolution_status = 'ambiguous_musicbrainz'")
    rerank_recommendation_run(conn, stats.run_id)
    assert (
        conn.execute(
            "SELECT artist_id, start_here FROM recommendation WHERE run_id = ? ORDER BY artist_id",
            [stats.run_id],
        ).fetchall()
        == snapshot
    )
    path = tmp_path / "offline.jsonl"
    export_recommendations(conn, path, format_name="jsonl", run_id=stats.run_id)
    exported = next(
        row for row in map(json.loads, path.read_text().splitlines()) if row["artist"] == "Zulu"
    )
    assert exported["start_here"] == item
    detail = TuiRecommendationStore(conn).get_detail(stats.run_id, artist_id=2)
    assert detail.start_here == item
    assert "[recording]" in _detail_text(detail).plain


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
