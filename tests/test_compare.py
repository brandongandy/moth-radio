from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from duckdb import DuckDBPyConnection

from scrobble_recs.config import ScrobbleRecsConfig, load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
from scrobble_recs.models.compare import compare_scorers

_CSV = (
    "uts,utc_time,artist,artist_mbid,album,album_mbid,track,track_mbid\n"
    "1704067200,2024-01-01 00:00:00,Seed One,,Al,,T1,\n"
    "1704153600,2024-01-02 00:00:00,Seed One,,Al,,T2,\n"
    "1704067200,2024-01-01 00:00:00,Seed Two,,Be,,U1,\n"
    "1704153600,2024-01-02 00:00:00,Seed Two,,Be,,U2,\n"
)


def _setup(tmp_path: Path) -> tuple[DuckDBPyConnection, ScrobbleRecsConfig]:
    (tmp_path / "seeds.csv").write_text(_CSV, encoding="utf-8")
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
                    "discovery_evidence": {"minimum_seed_count": 2},
                    "walk": {"min_distinct_seeds": 2, "min_lift": -100.0},
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    conn = initialize_database(config)
    import_lastfm_csv(conn, tmp_path / "seeds.csv")
    _build_graph(conn)
    conn.execute("UPDATE artist_similarity_edge SET fetched_at = '2000-01-01'")
    return conn, config


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


def _build_graph(conn: DuckDBPyConnection) -> None:
    s1 = _artist_id(conn, "seed one")
    s2 = _artist_id(conn, "seed two")
    cand = _new_artist(conn, "Bridge Cand", "bridge cand")
    _edge(conn, s1, cand, 0.9)
    _edge(conn, s2, cand, 0.9)
    for index in range(12):
        leaf_a = _new_artist(conn, f"Leaf A{index}", f"leaf a{index}")
        leaf_b = _new_artist(conn, f"Leaf B{index}", f"leaf b{index}")
        _edge(conn, s1, leaf_a, 0.3)
        _edge(conn, s2, leaf_b, 0.3)


def test_compare_runs_both_engines_over_identical_split(tmp_path: Path) -> None:
    conn, config = _setup(tmp_path)
    try:
        results = compare_scorers(
            conn,
            config,
            train_until=datetime(2024, 1, 3),
            test_from=datetime(2024, 1, 3),
            test_until=datetime(2024, 4, 3),
            scorers=["legacy", "walk"],
        )
    finally:
        conn.close()

    assert [row.scorer for row in results] == ["legacy", "walk"]
    by_scorer = {row.scorer: row for row in results}
    for row in results:
        assert 0.0 <= row.hit_rate_at_10 <= 1.0
        assert 0.0 <= row.hit_rate_at_100 <= 1.0
        assert 0.0 <= row.catalog_coverage <= 1.0
        assert 0.0 <= row.knownness_mean <= 1.0
        assert row.tag_entropy >= 0.0
    # Both engines recommend the twice-seeded bridge.
    assert by_scorer["legacy"].recommendations > 0
    assert by_scorer["walk"].recommendations > 0
    # Legacy never records hop distance; walk does.
    assert by_scorer["legacy"].mean_hop_distance is None
    assert by_scorer["walk"].mean_hop_distance == 1.0


def test_compare_fusion_ablations_share_stable_state_and_do_not_contaminate_runs(tmp_path):
    from dataclasses import asdict

    conn, config = _setup(tmp_path)
    try:
        config.recommendation.profile_window_months = 120
        arguments = dict(
            train_until=datetime(2030, 1, 3),
            test_from=datetime(2030, 1, 3),
            test_until=datetime(2030, 4, 3),
        )
        before = conn.execute("SELECT count(*) FROM recommendation_run").fetchone()
        forward = compare_scorers(
            conn,
            config,
            scorers=["legacy", "walk", "fusion"],
            ablate_sources=["lastfm_walk"],
            **arguments,
        )
        reverse = compare_scorers(
            conn,
            config,
            scorers=["fusion", "walk", "legacy"],
            ablate_sources=["lastfm_walk"],
            **arguments,
        )
        assert conn.execute("SELECT count(*) FROM recommendation_run").fetchone() == before
        assert len({r.evidence_snapshot_id for r in forward + reverse}) == 1
        assert len({r.stable_state_id for r in forward + reverse}) == 1

        def comparable(rows):
            return {
                r.scorer: {k: v for k, v in asdict(r).items() if k != "runtime_seconds"}
                for r in rows
            }

        assert comparable(forward) == comparable(reverse)
        assert {r.scorer for r in forward} == {
            "legacy",
            "walk",
            "fusion",
            "fusion-without-lastfm_walk",
        }
        fusion = next(r for r in forward if r.scorer == "fusion")
        assert fusion.recommendations > 0
        assert (
            fusion.pool_candidate_count >= fusion.eligible_candidate_count >= fusion.recommendations
        )
        assert 0 <= fusion.provider_concentration <= 1
        assert 0 <= fusion.multi_family_share <= 1
        assert fusion.runtime_seconds >= 0
        assert fusion.promotion_status == "not_promoted"
        assert "missing_blinded_evidence" in fusion.missing_promotion_evidence
        assert config.recommendation.fusion.enabled is False
    finally:
        conn.close()


def test_provider_concentration_counts_lastfm_direct_and_tags_as_one_provider(tmp_path):
    from scrobble_recs.models.compare import run_quality_metrics

    conn, config = _setup(tmp_path)
    try:
        _new_artist(conn, "Catalogue Only", "catalogue only")
        conn.execute(
            "INSERT INTO artist_tag (artist_id, tag, source, normalized_weight, fetched_at) "
            "SELECT artist_id, 'facet', 'lastfm', 1, '2000-01-01' FROM artist"
        )
        conn.execute(
            "INSERT INTO tag_top_artist (artist_id, tag, source, tag_rank, fetched_at) "
            "SELECT artist_id, 'facet', 'lastfm', 10, '2000-01-01' FROM artist"
        )
        from scrobble_recs.models.profile_window import ListeningWindow
        from scrobble_recs.models.recommend import generate_recommendations

        stats = generate_recommendations(
            conn,
            config,
            limit=10,
            as_of=datetime(2024, 1, 3),
            history_window=ListeningWindow(datetime(2023, 1, 1), datetime(2024, 1, 3)),
            historical_cutoff=datetime(2024, 1, 3),
            local_history_end=datetime(2024, 1, 3),
        )
        metrics = run_quality_metrics(conn, stats.run_id, set(), cutoff=datetime(2024, 1, 3))
        assert metrics["provider_concentration"] == 1
        assert (
            metrics["pool_coverage"]
            == metrics["pool_candidate_count"]
            / conn.execute("SELECT count(*) FROM artist").fetchone()[0]
        )
    finally:
        conn.close()


def test_walk_only_fusion_attribution_uses_persisted_retriever_membership(tmp_path):
    conn, config = _setup(tmp_path)
    try:
        config.recommendation.obviousness.enabled = False
        results = compare_scorers(
            conn,
            config,
            train_until=datetime(2024, 1, 3),
            test_from=datetime(2024, 1, 3),
            test_until=datetime(2024, 4, 3),
            scorers=["fusion"],
            ablate_sources=["lastfm_similarity"],
        )
        ablated = results[1]
        assert ablated.recommendations > 0
        assert ablated.provider_concentration == 1
        assert ablated.candidate_count_by_source["lastfm_walk"] > 0
        assert ablated.candidate_count_by_source.get("lastfm_similarity", 0) == 0
    finally:
        conn.close()
