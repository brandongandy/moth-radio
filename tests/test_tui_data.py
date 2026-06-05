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
    assert rows[0].score == pytest.approx(0.842)
    assert rows[0].primary_tags == ("post-punk",)
    assert rows[0].why_short == "Fits post-punk tags."
    assert rows[0].feedback_state == ""
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
            explanation
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ],
    )
