from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from scrobble_recs.apis.lastfm import LastFmArtistTag, LastFmSimilarArtist, LastFmTagTopArtist
from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.enrichment.lastfm import run_lastfm_enrichment
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
from scrobble_recs.models.evaluate import EvaluationResult, evaluate_recommendations
from scrobble_recs.models.feedback import record_feedback_event


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
                    "discovery_evidence": {"minimum_seed_count": 1},
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


def _latest_seed_summary(conn) -> dict[str, object]:
    (seed_summary_json,) = conn.execute(
        "SELECT seed_summary FROM recommendation_run ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return json.loads(seed_summary_json)


def _insert_historical_recommendation_run(
    conn,
    *,
    run_id: str,
    created_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO recommendation_run (
            run_id, created_at, config_hash, mode, seed_summary, scoring_weights
        )
        VALUES (?, ?, 'evaluation-test', 'balanced', '{}', '{}')
        """,
        [run_id, created_at],
    )


def test_evaluate_reports_basic_metrics_with_split_date(tmp_path: Path) -> None:
    conn, config = _setup_db(tmp_path)
    try:
        result = evaluate_recommendations(
            conn,
            config,
            train_until=datetime(2024, 1, 3),
            test_from=datetime(2024, 1, 3),
            test_until=datetime(2024, 4, 3),
        )

        assert isinstance(result, EvaluationResult)
        assert result.train_until == "2024-01-03T00:00:00"
        assert result.test_from == "2024-01-03T00:00:00"
        assert result.test_until == "2024-04-03T00:00:00"
        assert _latest_seed_summary(conn)["history_window"]["source"] == "explicit"
        assert result.recommendations_generated > 0
        assert 0.0 <= result.precision_at_50 <= 1.0
        assert 0.0 <= result.recall_at_50 <= 1.0
        assert 0.0 <= result.recall_at_100 <= 1.0
        assert result.recall_at_100 >= result.recall_at_50
        assert 0.0 <= result.ndcg_at_50 <= 1.0
        assert 0.0 <= result.knownness_median <= 1.0
        assert 0.0 <= result.obviousness_median <= 1.0
        assert 0.0 <= result.tag_diversity_at_20 <= 1.0
        assert 0.0 <= result.rediscovery_hit_rate <= 1.0
        assert 0.0 <= result.external_candidate_share <= 1.0
        assert 0.0 <= result.eligible_candidate_coverage <= 1.0
        assert 0.0 <= result.novelty_rate <= 1.0
        assert 0.0 <= result.cooldown_or_repeat_rate <= 1.0
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
            test_until=datetime(2030, 2, 1),
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
            test_until=datetime(2024, 4, 3),
        )

        assert result.recommendations_generated > 0
        assert result.tag_diversity_at_20 > 0.0, (
            "expected non-zero tag diversity when recommendations have tag explanations"
        )
        assert result.external_candidate_share >= 0.0
    finally:
        conn.close()


def test_evaluation_uses_pre_cutoff_history_without_refreshing_future_artist_fact(
    tmp_path: Path,
) -> None:
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
                first_played_at, last_played_at, lifetime_weight, recent_weight,
                knownness, rediscovery_score
            )
            VALUES (
                9001, 50, 50, 12, 50, TIMESTAMP '2024-01-04', TIMESTAMP '2028-01-14',
                0.123, 0.234, 0.345, 0.456
            )
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
            test_until=datetime(2025, 1, 1),
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
        summary = _latest_seed_summary(conn)
        top_seeds = summary["top_seeds"]
        assert isinstance(top_seeds, list)
        assert "Future Listener" not in {item["artist"] for item in top_seeds}
        assert conn.execute(
            """
            SELECT lifetime_weight, recent_weight, knownness, rediscovery_score
            FROM artist_listening_fact
            WHERE artist_id = 9001
            """
        ).fetchone() == (0.123, 0.234, 0.345, 0.456)
    finally:
        conn.close()


def test_evaluation_never_uses_future_scrobbles_as_seeds(tmp_path: Path) -> None:
    conn, config = _setup_db(tmp_path)
    try:
        conn.execute(
            """
            INSERT INTO artist (artist_id, canonical_name, artist_key, lastfm_name)
            VALUES (9002, 'Future Favorite', 'future-favorite', 'Future Favorite')
            """
        )
        conn.execute(
            """
            INSERT INTO scrobble (scrobble_id, played_at, artist_key, track_key, is_duplicate)
            SELECT
                9100 + range,
                TIMESTAMP '2025-01-02' + range * INTERVAL 1 DAY,
                'future-favorite',
                'future-track-' || range,
                false
            FROM range(50)
            """
        )

        result = evaluate_recommendations(
            conn,
            config,
            train_until=datetime(2025, 1, 1),
            test_from=datetime(2025, 1, 1),
            test_until=datetime(2025, 4, 1),
        )

        summary = _latest_seed_summary(conn)
        top_seeds = summary["top_seeds"]
        assert isinstance(top_seeds, list)
        assert "Future Favorite" not in {item["artist"] for item in top_seeds}
        assert 0.0 <= result.precision_at_50 <= 1.0
    finally:
        conn.close()


def test_evaluation_outcomes_are_limited_to_horizon(tmp_path: Path) -> None:
    conn, config = _setup_db(tmp_path)
    try:
        conn.execute(
            """
            INSERT INTO artist (artist_id, canonical_name, artist_key, lastfm_name)
            VALUES
                (9201, 'Horizon Discovery', 'horizon-discovery', 'Horizon Discovery'),
                (9202, 'Pre-cutoff Listener', 'pre-cutoff-listener', 'Pre-cutoff Listener'),
                (
                    9203,
                    'Beyond Horizon Listener',
                    'beyond-horizon-listener',
                    'Beyond Horizon Listener'
                )
            """
        )
        conn.execute(
            """
            INSERT INTO scrobble (scrobble_id, played_at, artist_key, track_key, is_duplicate)
            VALUES
                (9201, TIMESTAMP '2025-01-04', 'horizon-discovery', 'horizon-track', false),
                (9202, TIMESTAMP '2024-12-31', 'pre-cutoff-listener', 'pre-cutoff-track', false),
                (
                    9203,
                    TIMESTAMP '2025-02-01',
                    'beyond-horizon-listener',
                    'beyond-horizon-track',
                    false
                )
            """
        )

        from scrobble_recs.models.evaluate import _first_listened_artist_ids

        outcomes = _first_listened_artist_ids(
            conn,
            start=datetime(2025, 1, 1),
            end=datetime(2025, 2, 1),
            exclude_before=datetime(2025, 1, 1),
        )

        assert outcomes == {9201}
    finally:
        conn.close()


def test_evaluation_ignores_post_cutoff_feedback_and_cooldown_votes(tmp_path: Path) -> None:
    conn, config = _setup_db(tmp_path)
    try:
        post_cutoff_run_id = "00000000-0000-0000-0000-000000009301"
        _insert_historical_recommendation_run(
            conn,
            run_id=post_cutoff_run_id,
            created_at=datetime(2024, 1, 3),
        )
        record_feedback_event(
            conn,
            run_id=post_cutoff_run_id,
            artist_id=5,
            action="hide_artist",
            created_at=datetime(2024, 1, 3),
            feedback_id="00000000-0000-0000-0000-000000009302",
        )
        record_feedback_event(
            conn,
            run_id=post_cutoff_run_id,
            artist_id=6,
            action="reject",
            created_at=datetime(2024, 1, 3),
            feedback_id="00000000-0000-0000-0000-000000009303",
        )

        result = evaluate_recommendations(
            conn,
            config,
            train_until=datetime(2024, 1, 3),
            test_from=datetime(2024, 1, 3),
            test_until=datetime(2024, 4, 3),
        )
        recommendation_artist_ids = {
            int(row[0])
            for row in conn.execute(
                "SELECT artist_id FROM recommendation WHERE run_id = ?",
                [
                    conn.execute(
                        "SELECT run_id FROM recommendation_run ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()[0]
                ],
            ).fetchall()
        }
        (reasons_json,) = conn.execute(
            """
            SELECT eligibility_reasons
            FROM recommendation_candidate
            WHERE run_id = (SELECT run_id FROM recommendation_run ORDER BY created_at DESC LIMIT 1)
              AND artist_id = 6
            """
        ).fetchone()

        assert result.recommendations_generated > 0
        assert 5 in recommendation_artist_ids
        assert "feedback_cooldown" not in json.loads(reasons_json)
    finally:
        conn.close()


def test_evaluation_ignores_post_cutoff_recommendation_runs_for_fatigue(tmp_path: Path) -> None:
    conn, config = _setup_db(tmp_path)
    try:
        post_cutoff_run_id = "00000000-0000-0000-0000-000000009401"
        _insert_historical_recommendation_run(
            conn,
            run_id=post_cutoff_run_id,
            created_at=datetime(2024, 1, 3),
        )
        conn.execute(
            """
            INSERT INTO recommendation (
                run_id, rank, artist_id, final_score, raw_score, explanation
            )
            VALUES (?, 1, 5, 1.0, 1.0, '{}')
            """,
            [post_cutoff_run_id],
        )

        evaluate_recommendations(
            conn,
            config,
            train_until=datetime(2024, 1, 3),
            test_from=datetime(2024, 1, 3),
            test_until=datetime(2024, 4, 3),
        )
        (explanation_json,) = conn.execute(
            """
            SELECT r.explanation
            FROM recommendation AS r
            JOIN recommendation_run AS rr ON rr.run_id = r.run_id
            WHERE r.artist_id = 5
            ORDER BY rr.created_at DESC
            LIMIT 1
            """
        ).fetchone()

        assert json.loads(explanation_json)["fatigue"]["penalty"] == 0.0
    finally:
        conn.close()


def test_evaluation_normalizes_offset_aware_bounds_to_naive_utc(tmp_path: Path) -> None:
    conn, config = _setup_db(tmp_path)
    try:
        result = evaluate_recommendations(
            conn,
            config,
            train_until=datetime(2024, 1, 3, 8, tzinfo=timezone(timedelta(hours=8))),
            test_from=datetime(2024, 1, 3, tzinfo=UTC),
            test_until=datetime(2024, 4, 3, tzinfo=UTC),
        )

        assert result.train_until == "2024-01-03T00:00:00"
        assert result.test_from == "2024-01-03T00:00:00"
        assert result.test_until == "2024-04-03T00:00:00"
    finally:
        conn.close()
