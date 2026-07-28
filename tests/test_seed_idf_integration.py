from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from duckdb import DuckDBPyConnection

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
from scrobble_recs.models.profile_window import ListeningWindow
from scrobble_recs.models.recommend import generate_recommendations

_WINDOW = ListeningWindow(datetime(2023, 1, 1), datetime(2025, 1, 1))

_TWO_SEED_CSV = (
    "uts,utc_time,artist,artist_mbid,album,album_mbid,track,track_mbid\n"
    "1704067200,2024-01-01 00:00:00,Alpha Seed,,Al,,T1,\n"
    "1704153600,2024-01-02 00:00:00,Alpha Seed,,Al,,T2,\n"
    "1704067200,2024-01-01 00:00:00,Beta Seed,,Be,,U1,\n"
    "1704153600,2024-01-02 00:00:00,Beta Seed,,Be,,U2,\n"
)


def _write_config(tmp_path: Path, *, alpha: float) -> Path:
    config_path = tmp_path / f"config_{alpha}.json"
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
                    "lastfm_csv": str(tmp_path / "seeds.csv"),
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
                "recommendation": {
                    "seed_artist_limit": 10,
                    "similar_artists_per_seed": 10,
                    "min_seed_scrobbles": 2,
                    "discovery_evidence": {"minimum_seed_count": 2, "minimum_seed_edge": 0.05},
                    "calibration": {"enabled": False},
                    "diversity": {"enabled": False},
                    "obviousness": {"enabled": False},
                    "scoring": {"seed_idf_alpha": alpha},
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _artist_id(conn: DuckDBPyConnection, key: str) -> int:
    return int(
        conn.execute("SELECT artist_id FROM artist WHERE artist_key = ?", [key]).fetchone()[0]
    )


def _build_graph(conn: DuckDBPyConnection) -> None:
    alpha = _artist_id(conn, "alpha seed")
    beta = _artist_id(conn, "beta seed")
    cand = int(conn.execute("SELECT coalesce(max(artist_id), 0) + 1 FROM artist").fetchone()[0])
    conn.execute(
        """
        INSERT INTO artist (artist_id, canonical_name, artist_key)
        VALUES (?, 'Shared Cand', 'shared cand')
        """,
        [cand],
    )
    # Alpha has the strong edge to the candidate; Beta the weak one.
    conn.execute(
        """
        INSERT INTO artist_similarity_edge
            (source_artist_id, target_artist_id, source, source_score, normalized_score)
        VALUES (?, ?, 'lastfm', 0.9, 0.9), (?, ?, 'lastfm', 0.1, 0.1)
        """,
        [alpha, cand, beta, cand],
    )
    # Alpha is globally popular (rank 1 across several tags); Beta is obscure.
    conn.execute(
        """
        INSERT INTO tag_top_artist (tag, artist_id, source, tag_rank, tag_count)
        VALUES ('x1', ?, 'lastfm', 1, 900.0), ('x2', ?, 'lastfm', 1, 900.0),
               ('x3', ?, 'lastfm', 1, 900.0), ('y1', ?, 'lastfm', 90, 10.0)
        """,
        [alpha, alpha, alpha, beta],
    )


def _run(tmp_path: Path, *, alpha: float) -> tuple[DuckDBPyConnection, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "seeds.csv").write_text(_TWO_SEED_CSV, encoding="utf-8")
    config = load_config(_write_config(tmp_path, alpha=alpha))
    conn = initialize_database(config)
    import_lastfm_csv(conn, tmp_path / "seeds.csv")
    _build_graph(conn)
    stats = generate_recommendations(
        conn,
        config,
        limit=5,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
        history_window=_WINDOW,
    )
    return conn, stats.run_id


def _candidate_lastfm_score(conn: DuckDBPyConnection, run_id: str) -> float:
    row = conn.execute(
        """
        SELECT r.lastfm_similarity_score
        FROM recommendation r
        JOIN artist a ON a.artist_id = r.artist_id
        WHERE r.run_id = ? AND a.canonical_name = 'Shared Cand'
        """,
        [run_id],
    ).fetchone()
    assert row is not None, "candidate was not recommended"
    return float(row[0])


def test_full_idf_downweights_popular_seed_edge(tmp_path: Path) -> None:
    conn_zero, run_zero = _run(tmp_path / "a", alpha=0.0)
    conn_one, run_one = _run(tmp_path / "b", alpha=1.0)
    try:
        # alpha=0: plain weighted average of equal-weight seeds -> (0.9 + 0.1)/2 = 0.5.
        assert _candidate_lastfm_score(conn_zero, run_zero) == pytest.approx(0.5)
        # alpha=1: the popular Alpha seed's strong edge is zeroed out, so the
        # candidate is left carrying only Beta's weak edge -> 0.1.
        assert _candidate_lastfm_score(conn_one, run_one) == pytest.approx(0.1)
    finally:
        conn_zero.close()
        conn_one.close()


def test_explanation_and_run_summary_carry_idf(tmp_path: Path) -> None:
    conn, run_id = _run(tmp_path, alpha=1.0)
    try:
        explanation_json, seed_summary_json = conn.execute(
            """
            SELECT r.explanation, run.seed_summary
            FROM recommendation r
            JOIN artist a ON a.artist_id = r.artist_id
            JOIN recommendation_run run ON run.run_id = r.run_id
            WHERE r.run_id = ? AND a.canonical_name = 'Shared Cand'
            """,
            [run_id],
        ).fetchone()
        explanation = json.loads(explanation_json)
        idf_by_artist = {seed["artist"]: seed["idf_weight"] for seed in explanation["seed_artists"]}
        # Popular seed gets the minimal idf (0.0); obscure seed the maximal (1.0).
        assert idf_by_artist["Alpha Seed"] == 0.0
        assert idf_by_artist["Beta Seed"] == 1.0

        seed_summary = json.loads(seed_summary_json)
        assert seed_summary["idf"]["alpha"] == 1.0
        assert seed_summary["idf"]["missing_fraction"] == 0.0
    finally:
        conn.close()
