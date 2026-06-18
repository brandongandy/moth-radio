import json
from pathlib import Path

import pytest

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.tui.data import (
    RecommendationFilters,
    TuiRecommendationStore,
)


def write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "scrobbles.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": "tests/fixtures/tiny_lastfm_export.csv",
                    "timezone": "UTC",
                    "timestamp_column": "uts",
                    "csv_columns": [
                        "uts",
                        "utc_time",
                        "artist",
                        "artist_mbid",
                        "album",
                        "album_mbid",
                        "track",
                        "track_mbid",
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_store_loads_latest_run_recommendations_with_compact_columns(tmp_path: Path) -> None:
    conn = initialize_database(load_config(write_config(tmp_path)))
    older_run_id = "00000000-0000-0000-0000-000000000001"
    latest_run_id = "00000000-0000-0000-0000-000000000004"
    _insert_run(
        conn,
        run_id=older_run_id,
        mode="explore",
        created_at="2026-06-04 09:00:00",
    )
    _insert_run(
        conn,
        run_id=latest_run_id,
        mode="balanced",
        created_at="2026-06-04 10:00:00",
    )
    _insert_artist(conn, artist_id=10, name="Bauhaus")
    _insert_artist(conn, artist_id=11, name="Older Artist")
    _insert_recommendation(
        conn,
        run_id=latest_run_id,
        artist_id=10,
        rank=1,
        final_score=0.842,
        knownness=0.12,
        obviousness=0.04,
        explanation={
            "summary": "Fits post-punk tags.",
            "tags": [{"tag": "post-punk", "candidate_weight": 0.8}],
            "classification": {"is_rediscovery": False, "is_obvious": False},
        },
    )
    _insert_recommendation(
        conn,
        run_id=older_run_id,
        artist_id=11,
        rank=1,
        final_score=0.700,
        knownness=0.20,
        obviousness=0.10,
        explanation={"summary": "Older run recommendation.", "tags": [{"tag": "goth"}]},
    )

    store = TuiRecommendationStore(conn)
    rows = store.list_recommendations()

    assert [run.run_id for run in store.list_runs()] == [latest_run_id, older_run_id]
    assert [row.artist for row in rows] == ["Bauhaus"]
    assert rows[0].run_id == latest_run_id
    assert rows[0].rank == 1
    assert rows[0].score == pytest.approx(1.0)
    assert rows[0].primary_tags == ("post-punk",)
    assert rows[0].why_short == "Fits post-punk tags."
    assert rows[0].feedback_state == ""
    assert rows[0].score_band == ""
    assert rows[0].identity_confidence == 0.50
    assert rows[0].identity_warnings == ()
    assert rows[0].trial_penalty == 0.0
    conn.close()


def test_store_filters_by_search_tag_knownness_and_obviousness(tmp_path: Path) -> None:
    conn = initialize_database(load_config(write_config(tmp_path)))
    run_id = "00000000-0000-0000-0000-000000000002"
    _insert_run(conn, run_id=run_id, mode="explore")
    _insert_artist(conn, artist_id=20, name="Cold Cave")
    _insert_artist(conn, artist_id=21, name="Warm Cave")
    _insert_artist(conn, artist_id=22, name="Cold Archive")
    _insert_artist(conn, artist_id=23, name="Cold Familiar")
    _insert_artist(conn, artist_id=24, name="Cold Obvious")
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=20,
        rank=1,
        final_score=0.70,
        knownness=0.20,
        obviousness=0.10,
        explanation={"summary": "Coldwave fit.", "tags": [{"tag": "coldwave"}]},
    )
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=21,
        rank=2,
        final_score=0.69,
        knownness=0.20,
        obviousness=0.10,
        explanation={"summary": "Darkwave fit.", "tags": [{"tag": "coldwave"}]},
    )
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=22,
        rank=3,
        final_score=0.68,
        knownness=0.20,
        obviousness=0.10,
        explanation={"summary": "Cold but wrong tag.", "tags": [{"tag": "darkwave"}]},
    )
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=23,
        rank=4,
        final_score=0.67,
        knownness=0.80,
        obviousness=0.10,
        explanation={"summary": "Cold known fit.", "tags": [{"tag": "coldwave"}]},
    )
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=24,
        rank=5,
        final_score=0.66,
        knownness=0.20,
        obviousness=0.95,
        explanation={"summary": "Cold obvious fit.", "tags": [{"tag": "coldwave"}]},
    )

    store = TuiRecommendationStore(conn)
    rows = store.list_recommendations(
        RecommendationFilters(
            search="cold",
            tag="coldwave",
            max_knownness=0.50,
            max_obviousness=0.50,
        )
    )

    assert [row.artist for row in rows] == ["Cold Cave"]
    conn.close()


def test_store_loads_detail_score_breakdown_and_missing_enrichment_statuses(
    tmp_path: Path,
) -> None:
    conn = initialize_database(load_config(write_config(tmp_path)))
    run_id = "00000000-0000-0000-0000-000000000003"
    _insert_run(conn, run_id=run_id, mode="balanced")
    _insert_artist(conn, artist_id=30, name="Sparse Artist")
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=30,
        rank=1,
        final_score=0.50,
        raw_score=0.61,
        lastfm_similarity_score=0.0,
        tag_similarity_score=0.44,
        cluster_fit_score=0.0,
        rediscovery_score=0.0,
        metadata_graph_score=0.0,
        feedback_score=-0.10,
        knownness=0.40,
        obviousness=0.70,
        diversity_penalty=0.01,
        explanation={
            "summary": "Partial evidence.",
            "tags": [],
            "seed_artists": [],
            "penalties": {"knownness": 0.02, "obviousness": 0.08, "diversity": 0.01},
            "classification": {"is_rediscovery": False, "is_obvious": True},
        },
    )

    detail = TuiRecommendationStore(conn).get_detail(run_id, artist_id=30)

    assert detail.artist == "Sparse Artist"
    assert detail.score_breakdown.final_score == pytest.approx(0.50)
    assert detail.score_breakdown.raw_score == pytest.approx(0.61)
    assert detail.score_breakdown.feedback_score == pytest.approx(-0.10)
    assert detail.status_labels == ("partial_similarity", "partial_tags")
    assert detail.score_band == ""
    assert detail.identity_confidence == 0.50
    assert detail.identity_warnings == ()
    assert detail.local_scrobble_count == 0
    assert detail.listened_day_count == 0
    assert detail.years_since_last_listen == 0.0
    assert detail.rediscovery_eligible is False
    assert detail.trial_penalty == 0.0
    assert detail.top_seed_artists == ()
    assert detail.score_breakdown.score_band == ""
    assert detail.score_breakdown.evidence_source_count == 0
    assert detail.score_breakdown.trial_penalty == 0.0
    assert detail.score_breakdown.identity_confidence == 0.50
    assert detail.score_breakdown.identity_warnings == ()
    conn.close()


def test_store_normalizes_display_scores_without_changing_raw_breakdown(
    tmp_path: Path,
) -> None:
    conn = initialize_database(load_config(write_config(tmp_path)))
    run_id = "00000000-0000-0000-0000-000000000005"
    _insert_run(conn, run_id=run_id, mode="balanced")
    _insert_artist(conn, artist_id=40, name="Low Top Score")
    _insert_artist(conn, artist_id=41, name="Lower Score")
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=40,
        rank=1,
        final_score=0.087,
        knownness=0.10,
        obviousness=0.10,
        explanation={"summary": "Top low utility.", "tags": [{"tag": "coldwave"}]},
    )
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=41,
        rank=2,
        final_score=0.0435,
        knownness=0.10,
        obviousness=0.10,
        explanation={"summary": "Half utility.", "tags": [{"tag": "coldwave"}]},
    )

    store = TuiRecommendationStore(conn)
    rows = store.list_recommendations(run_id=run_id)
    detail = store.get_detail(run_id, artist_id=40)

    assert [row.score for row in rows] == [pytest.approx(1.0), pytest.approx(0.5)]
    assert detail.score_breakdown.display_score == pytest.approx(1.0)
    assert detail.score_breakdown.final_score == pytest.approx(0.087)
    conn.close()


def _insert_run(
    conn,
    *,
    run_id: str,
    mode: str,
    created_at: str = "2026-06-04 12:00:00",
) -> None:
    conn.execute(
        """
        INSERT INTO recommendation_run (
            run_id,
            created_at,
            config_hash,
            mode,
            seed_summary,
            scoring_weights
        )
        VALUES (?, ?, 'hash', ?, '{}', '{}')
        """,
        [run_id, created_at, mode],
    )


def _insert_artist(conn, *, artist_id: int, name: str) -> None:
    conn.execute(
        """
        INSERT INTO artist (artist_id, canonical_name, artist_key, lastfm_name)
        VALUES (?, ?, ?, ?)
        """,
        [artist_id, name, name.casefold().replace(" ", "-"), name],
    )


def _insert_recommendation(
    conn,
    *,
    run_id: str,
    artist_id: int,
    rank: int,
    final_score: float,
    raw_score: float | None = None,
    lastfm_similarity_score: float = 0.1,
    tag_similarity_score: float = 0.1,
    cluster_fit_score: float = 0.0,
    rediscovery_score: float = 0.0,
    metadata_graph_score: float = 0.0,
    feedback_score: float = 0.0,
    knownness: float,
    obviousness: float,
    diversity_penalty: float = 0.0,
    explanation: dict[str, object],
    score_band: str = "",
    evidence_source_count: int = 0,
    local_scrobble_count: int = 0,
    listened_day_count: int = 0,
    years_since_last_listen: float = 0.0,
    rediscovery_eligible: bool = False,
    trial_penalty: float = 0.0,
    identity_confidence: float = 0.50,
    identity_warnings: str = "[]",
) -> None:
    conn.execute(
        """
        INSERT INTO recommendation (
            run_id,
            rank,
            artist_id,
            final_score,
            raw_score,
            lastfm_similarity_score,
            tag_similarity_score,
            cluster_fit_score,
            rediscovery_score,
            metadata_graph_score,
            feedback_score,
            knownness,
            obviousness,
            diversity_penalty,
            explanation,
            score_band,
            evidence_source_count,
            local_scrobble_count,
            listened_day_count,
            years_since_last_listen,
            rediscovery_eligible,
            trial_penalty,
            identity_confidence,
            identity_warnings
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run_id,
            rank,
            artist_id,
            final_score,
            raw_score if raw_score is not None else final_score,
            lastfm_similarity_score,
            tag_similarity_score,
            cluster_fit_score,
            rediscovery_score,
            metadata_graph_score,
            feedback_score,
            knownness,
            obviousness,
            diversity_penalty,
            json.dumps(explanation),
            score_band,
            evidence_source_count,
            local_scrobble_count,
            listened_day_count,
            years_since_last_listen,
            rediscovery_eligible,
            trial_penalty,
            identity_confidence,
            identity_warnings,
        ],
    )


def test_tui_store_includes_cache_summary(monkeypatch, tmp_path):
    import json

    from scrobble_recs.config import load_config
    from scrobble_recs.db import initialize_database
    from scrobble_recs.tui.data import TuiRecommendationStore

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "test",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": "tests/fixtures/tiny_lastfm_export.csv",
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(config_path)
    conn = initialize_database(cfg)
    try:
        store = TuiRecommendationStore(conn)
        summary = store.cache_summary()
        assert isinstance(summary.total_entries, int)
        assert isinstance(summary.fresh_entries, int)
        assert isinstance(summary.stale_entries, int)
        assert summary.total_entries >= 0
        assert summary.fresh_entries >= 0
        assert summary.stale_entries >= 0
    finally:
        conn.close()
