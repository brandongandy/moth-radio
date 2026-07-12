from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from scrobble_recs.apis.lastfm import LastFmArtistTag, LastFmSimilarArtist, LastFmTagTopArtist
from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.enrichment.lastfm import run_lastfm_enrichment
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
from scrobble_recs.models.evaluate import EvaluationResult, evaluate_recommendations


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
                "recommendation": {
                    "seed_artist_limit": 10,
                    "similar_artists_per_seed": 10,
                    "min_seed_scrobbles": 2,
                    "obviousness": {"tag_top_artists_limit": 100},
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


class _FakeLastFmClient:
    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        return [
            LastFmSimilarArtist("Siouxsie and the Banshees", None, 0.91),
            LastFmSimilarArtist("Bauhaus", None, 0.72),
            LastFmSimilarArtist("The Cure", "mbid-cure", 1.0),
            LastFmSimilarArtist("Joy Division", None, 0.60),
        ][:limit]

    def get_artist_tags(self, artist: str) -> list[LastFmArtistTag]:
        return [
            LastFmArtistTag("post-punk", 100.0),
            LastFmArtistTag("gothic rock", 75.0),
        ]

    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        rows = {
            "post-punk": [
                LastFmTagTopArtist(1, "The Cure", "mbid-cure", 1000.0),
                LastFmTagTopArtist(2, "Joy Division", None, 900.0),
                LastFmTagTopArtist(42, "Bauhaus", None, 250.0),
                LastFmTagTopArtist(55, "Siouxsie and the Banshees", None, 200.0),
            ],
            "gothic rock": [
                LastFmTagTopArtist(1, "The Cure", "mbid-cure", 1000.0),
                LastFmTagTopArtist(4, "Bauhaus", None, 700.0),
                LastFmTagTopArtist(30, "Siouxsie and the Banshees", None, 300.0),
            ],
        }
        return rows[tag][:limit]


def _setup_db(tmp_path: Path) -> tuple:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, config, client=_FakeLastFmClient())
    return conn, config


def test_evaluate_reports_basic_metrics_with_split_date(tmp_path: Path) -> None:
    conn, config = _setup_db(tmp_path)
    try:
        result = evaluate_recommendations(
            conn,
            config,
            train_until=datetime(2024, 1, 3),
            test_from=datetime(2024, 1, 3),
        )

        assert isinstance(result, EvaluationResult)
        assert result.train_until == "2024-01-03T00:00:00"
        assert result.test_from == "2024-01-03T00:00:00"
        (seed_summary_json,) = conn.execute(
            "SELECT seed_summary FROM recommendation_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert json.loads(seed_summary_json)["history_window"]["source"] == "explicit"
        assert result.recommendations_generated > 0
        assert 0.0 <= result.recall_at_50 <= 1.0
        assert 0.0 <= result.recall_at_100 <= 1.0
        assert result.recall_at_100 >= result.recall_at_50
        assert 0.0 <= result.ndcg_at_50 <= 1.0
        assert 0.0 <= result.knownness_median <= 1.0
        assert 0.0 <= result.obviousness_median <= 1.0
        assert 0.0 <= result.tag_diversity_at_20 <= 1.0
        assert 0.0 <= result.rediscovery_hit_rate <= 1.0
        assert 0.0 <= result.external_candidate_share <= 1.0
    finally:
        conn.close()


def test_evaluate_empty_test_period_returns_zero_recall(tmp_path: Path) -> None:
    conn, config = _setup_db(tmp_path)
    try:
        result = evaluate_recommendations(
            conn,
            config,
            train_until=datetime(2024, 1, 3),
            test_from=datetime(2030, 1, 1),
        )

        assert result.recall_at_50 == 0.0
        assert result.recall_at_100 == 0.0
        assert result.ndcg_at_50 == 0.0
    finally:
        conn.close()


def test_evaluate_reports_tag_diversity_and_external_share(tmp_path: Path) -> None:
    conn, config = _setup_db(tmp_path)
    try:
        result = evaluate_recommendations(
            conn,
            config,
            train_until=datetime(2024, 1, 3),
            test_from=datetime(2024, 1, 3),
        )

        assert result.recommendations_generated > 0
        assert result.tag_diversity_at_20 > 0.0, (
            "expected non-zero tag diversity when recommendations have tag explanations"
        )
        assert result.external_candidate_share >= 0.0
    finally:
        conn.close()


def test_evaluation_allows_candidate_first_listened_after_train_until(tmp_path: Path) -> None:
    conn, config = _setup_db(tmp_path)
    try:
        (seed_artist_id,) = conn.execute(
            "SELECT artist_id FROM artist WHERE canonical_name = 'The Cure'"
        ).fetchone()
        conn.execute(
            """
            INSERT INTO artist (artist_id, canonical_name, artist_key, lastfm_name)
            VALUES (9001, 'Future Listener', 'future-listener', 'Future Listener')
            """
        )
        conn.execute(
            """
            INSERT INTO scrobble (scrobble_id, played_at, artist_key, track_key, is_duplicate)
            SELECT
                9001 + range,
                TIMESTAMP '2024-01-04' + range * INTERVAL 30 DAY,
                'future-listener',
                'future-track-' || range,
                false
            FROM range(50)
            """
        )
        conn.execute(
            """
            INSERT INTO artist_listening_fact (
                artist_id, play_count, track_count, active_months, active_days,
                first_played_at, last_played_at
            )
            VALUES (9001, 50, 50, 12, 50, TIMESTAMP '2024-01-04', TIMESTAMP '2028-01-14')
            """
        )
        conn.execute(
            """
            INSERT INTO artist_similarity_edge (
                source_artist_id, target_artist_id, source,
                source_score, normalized_score, evidence, fetched_at
            )
            VALUES (?, 9001, 'lastfm', 0.95, 0.95, '{}', current_timestamp)
            """,
            [seed_artist_id],
        )

        result = evaluate_recommendations(
            conn,
            config,
            train_until=datetime(2024, 1, 3),
            test_from=datetime(2024, 1, 3),
        )

        assert result.recommendations_generated > 0
        row = conn.execute(
            """
            SELECT r.knownness, r.rediscovery_score, r.local_scrobble_count, r.explanation
            FROM recommendation AS r
            JOIN artist AS a ON a.artist_id = r.artist_id
            WHERE a.canonical_name = 'Future Listener'
            """
        ).fetchone()
        assert row is not None
        knownness, rediscovery_score, local_scrobble_count, explanation_json = row
        assert knownness == 0.0
        assert rediscovery_score == 0.0
        assert local_scrobble_count == 0
        assert json.loads(explanation_json)["penalties"]["knownness"] == 0.0
    finally:
        conn.close()
