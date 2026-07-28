from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from duckdb import DuckDBPyConnection

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
from scrobble_recs.models.profile_window import ListeningWindow
from scrobble_recs.models.recommend import generate_recommendations

_WINDOW = ListeningWindow(datetime(2023, 1, 1), datetime(2025, 1, 1))

_CSV = (
    "uts,utc_time,artist,artist_mbid,album,album_mbid,track,track_mbid\n"
    "1704067200,2024-01-01 00:00:00,Seed One,,Al,,T1,\n"
    "1704153600,2024-01-02 00:00:00,Seed One,,Al,,T2,\n"
    "1704067200,2024-01-01 00:00:00,Seed Two,,Be,,U1,\n"
    "1704153600,2024-01-02 00:00:00,Seed Two,,Be,,U2,\n"
)


def _write_config(tmp_path: Path) -> Path:
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
                    "calibration": {"enabled": False},
                    "diversity": {"enabled": False},
                    "obviousness": {"enabled": False},
                    "scoring": {"engine": "walk"},
                    "walk": {"max_hops": 2, "min_lift": -100.0, "min_distinct_seeds": 2},
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


def _new_artist(conn: DuckDBPyConnection, name: str, key: str) -> int:
    artist_id = int(
        conn.execute("SELECT coalesce(max(artist_id), 0) + 1 FROM artist").fetchone()[0]
    )
    conn.execute(
        "INSERT INTO artist (artist_id, canonical_name, artist_key) VALUES (?, ?, ?)",
        [artist_id, name, key],
    )
    return artist_id


def _edge(conn: DuckDBPyConnection, source: int, target: int, weight: float) -> None:
    conn.execute(
        """
        INSERT INTO artist_similarity_edge
            (source_artist_id, target_artist_id, source, source_score, normalized_score)
        VALUES (?, ?, 'lastfm', ?, ?)
        """,
        [source, target, weight, weight],
    )


def _build(conn: DuckDBPyConnection) -> dict[str, int]:
    s1 = _artist_id(conn, "seed one")
    s2 = _artist_id(conn, "seed two")
    cand = _new_artist(conn, "Bridge Cand", "bridge cand")
    hub = _new_artist(conn, "Hub", "hub")
    tag_only = _new_artist(conn, "Tag Only", "tag only")

    # Bridge candidate is reachable strongly from BOTH seeds.
    _edge(conn, s1, cand, 0.9)
    _edge(conn, s2, cand, 0.9)
    # Hub is reachable from one seed only (fails min_distinct_seeds=2).
    _edge(conn, s1, hub, 0.9)
    # Dilution leaves: unique per seed, each reachable from a single seed. They
    # dilute the uniform baseline so the twice-seeded bridge earns positive lift,
    # and are themselves excluded by the min-distinct-seeds gate.
    for index in range(12):
        leaf_a = _new_artist(conn, f"Leaf A{index}", f"leaf a{index}")
        leaf_b = _new_artist(conn, f"Leaf B{index}", f"leaf b{index}")
        _edge(conn, s1, leaf_a, 0.3)
        _edge(conn, s2, leaf_b, 0.3)
    # Tag-only artist: appears in tag_top_artist but has NO similarity edges.
    conn.execute(
        """
        INSERT INTO tag_top_artist (tag, artist_id, source, tag_rank, tag_count)
        VALUES ('post-punk', ?, 'lastfm', 1, 500.0)
        """,
        [tag_only],
    )
    return {"s1": s1, "s2": s2, "cand": cand, "hub": hub, "tag_only": tag_only}


def _run(tmp_path: Path) -> tuple[DuckDBPyConnection, str, dict[str, int]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "seeds.csv").write_text(_CSV, encoding="utf-8")
    config = load_config(_write_config(tmp_path))
    conn = initialize_database(config)
    import_lastfm_csv(conn, tmp_path / "seeds.csv")
    ids = _build(conn)
    stats = generate_recommendations(
        conn,
        config,
        limit=10,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
        history_window=_WINDOW,
    )
    return conn, stats.run_id, ids


def test_walk_engine_recommends_bridge_and_records_engine(tmp_path: Path) -> None:
    conn, run_id, ids = _run(tmp_path)
    try:
        engine = conn.execute(
            "SELECT scoring_engine FROM recommendation_run WHERE run_id = ?", [run_id]
        ).fetchone()[0]
        assert engine == "walk"

        recommended = {
            int(row[0])
            for row in conn.execute(
                """
                SELECT artist_id FROM recommendation
                WHERE run_id = ? AND score_band != 'candidate_only'
                """,
                [run_id],
            ).fetchall()
        }
        # Only the two-seed bridge passes min_distinct_seeds=2.
        assert ids["cand"] in recommended
        assert ids["hub"] not in recommended
    finally:
        conn.close()


def test_walk_engine_excludes_tag_only_candidate(tmp_path: Path) -> None:
    conn, run_id, ids = _run(tmp_path)
    try:
        row = conn.execute(
            """
            SELECT eligibility_state, eligibility_reasons
            FROM recommendation_candidate
            WHERE run_id = ? AND artist_id = ?
            """,
            [run_id, ids["tag_only"]],
        ).fetchone()
        assert row is not None
        assert row[0] == "excluded"
        assert "walk_unreachable" in json.loads(row[1])
        # And it never made it into the ranked recommendations.
        present = conn.execute(
            "SELECT count(*) FROM recommendation WHERE run_id = ? AND artist_id = ?",
            [run_id, ids["tag_only"]],
        ).fetchone()[0]
        assert present == 0
    finally:
        conn.close()


def test_walk_explanation_and_hop_distance_persisted(tmp_path: Path) -> None:
    conn, run_id, ids = _run(tmp_path)
    try:
        explanation_json, hop_distance = conn.execute(
            """
            SELECT explanation, hop_distance FROM recommendation
            WHERE run_id = ? AND artist_id = ?
            """,
            [run_id, ids["cand"]],
        ).fetchone()
        walk = json.loads(explanation_json)["walk"]
        assert hop_distance == 1
        assert walk["hop_distance"] == 1
        assert walk["distinct_seed_count"] == 2
        assert walk["lift"] > 0.0
        assert "personalized_mass" in walk
        assert "baseline_mass" in walk
        # Top paths run seed -> candidate (direct edges here).
        assert walk["top_paths"]
        for path in walk["top_paths"]:
            assert path["artist_ids"][0] in {ids["s1"], ids["s2"]}
            assert path["artist_ids"][-1] == ids["cand"]
        contributing = {seed["artist_id"] for seed in walk["contributing_seeds"]}
        assert contributing == {ids["s1"], ids["s2"]}
    finally:
        conn.close()


def _run_fingerprint(conn: DuckDBPyConnection, run_id: str) -> list[tuple[object, ...]]:
    return conn.execute(
        """
        SELECT rank, artist_id, final_score, raw_score, lastfm_similarity_score,
               tag_similarity_score, hop_distance, explanation
        FROM recommendation
        WHERE run_id = ?
        ORDER BY rank, artist_id
        """,
        [run_id],
    ).fetchall()


def test_walk_run_is_byte_identical_for_same_config_and_db(tmp_path: Path) -> None:
    conn_a, run_a, _ = _run(tmp_path / "a")
    conn_b, run_b, _ = _run(tmp_path / "b")
    try:
        assert _run_fingerprint(conn_a, run_a) == _run_fingerprint(conn_b, run_b)
    finally:
        conn_a.close()
        conn_b.close()
