import json
from datetime import datetime
from pathlib import Path

import pytest
from duckdb import ConstraintException

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.models.feedback import (
    FeedbackProfile,
    apply_feedback_to_tag_profile,
    feedback_adjustment_for_candidate,
    feedback_obviousness_for_candidate,
    load_feedback_profile,
    record_feedback_event,
    rerank_recommendation_run,
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


def test_feedback_migration_creates_event_table(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main'
                """
            ).fetchall()
        }

        assert "feedback_event" in tables
        assert _columns(conn, "feedback_event") == [
            ("feedback_id", "UUID"),
            ("created_at", "TIMESTAMP"),
            ("run_id", "UUID"),
            ("artist_id", "UBIGINT"),
            ("action", "VARCHAR"),
            ("value", "DOUBLE"),
            ("context", "JSON"),
            ("note", "VARCHAR"),
        ]

        conn.execute(
            """
            INSERT INTO feedback_event (
                feedback_id,
                run_id,
                artist_id,
                action,
                value,
                context,
                note
            )
            VALUES (
                '00000000-0000-0000-0000-000000000101',
                '00000000-0000-0000-0000-000000000001',
                10,
                'more_like_this',
                1.0,
                '{"tags":["post-punk"]}',
                'good fit'
            )
            """
        )

        with pytest.raises(ConstraintException):
            conn.execute(
                """
                INSERT INTO feedback_event (
                    feedback_id,
                    run_id,
                    artist_id,
                    action,
                    value,
                    context
                )
                VALUES (
                    '00000000-0000-0000-0000-000000000101',
                    '00000000-0000-0000-0000-000000000001',
                    11,
                    'reject',
                    1.0,
                    '{}'
                )
                """
            )
    finally:
        conn.close()


def _columns(conn, table_name: str) -> list[tuple[str, str]]:
    return [
        (str(column_name), str(data_type))
        for column_name, data_type in conn.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'main'
              AND table_name = ?
            ORDER BY ordinal_position
            """,
            [table_name],
        ).fetchall()
    ]


def test_record_feedback_event_validates_action_and_persists_json_context(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_artist(conn, artist_id=10, name="Bauhaus")
    _insert_run(conn, run_id="00000000-0000-0000-0000-000000000001")

    event = record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000001",
        artist_id=10,
        action="more_like_this",
        value=1.0,
        context={
            "weight": 2,
            "tags": [" Gothic Rock ", "coldwave", "gothic rock"],
            "source": "review",
        },
        note="good lead",
        created_at=datetime(2026, 6, 4, 12, 0, 0),
        feedback_id="00000000-0000-0000-0000-000000000201",
    )

    assert event.feedback_id == "00000000-0000-0000-0000-000000000201"
    assert event.action == "more_like_this"
    row = conn.execute(
        """
        SELECT run_id::VARCHAR, artist_id, action, value, context, note, created_at
        FROM feedback_event
        WHERE feedback_id = ?
        """,
        [event.feedback_id],
    ).fetchone()
    assert row == (
        "00000000-0000-0000-0000-000000000001",
        10,
        "more_like_this",
        1.0,
        (
            '{"artist_key":"bauhaus","artist_name":"Bauhaus","lastfm_name":"Bauhaus",'
            '"source":"review","tags":["coldwave","gothic rock"],"weight":2}'
        ),
        "good lead",
        datetime(2026, 6, 4, 12, 0, 0),
    )
    conn.close()


def test_record_feedback_event_rejects_unknown_action(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)

    with pytest.raises(ValueError, match="unsupported feedback action 'favorite'"):
        record_feedback_event(
            conn,
            run_id="00000000-0000-0000-0000-000000000001",
            artist_id=10,
            action="favorite",
        )

    assert conn.execute("SELECT count(*) FROM feedback_event").fetchone() == (0,)
    conn.close()


def test_load_feedback_profile_derives_artist_tag_and_seed_adjustments(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_artist(conn, artist_id=20, name="Candidate A")
    _insert_artist(conn, artist_id=21, name="Candidate B")
    _insert_run(conn, run_id="00000000-0000-0000-0000-000000000002")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000002",
        artist_id=20,
        action="more_like_this",
        context={"tags": ["post-punk", "coldwave"]},
        feedback_id="00000000-0000-0000-0000-000000000301",
    )
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000002",
        artist_id=21,
        action="hide_artist",
        context={"tags": ["arena rock"]},
        feedback_id="00000000-0000-0000-0000-000000000302",
    )
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000002",
        artist_id=21,
        action="boost_tag",
        context={"tags": ["coldwave"]},
        feedback_id="00000000-0000-0000-0000-000000000303",
    )

    profile = load_feedback_profile(conn)

    assert isinstance(profile, FeedbackProfile)
    assert profile.hidden_artist_ids == {21}
    assert profile.boosted_seed_artist_ids == {20}
    assert profile.artist_adjustments[20] == pytest.approx(0.15)
    assert profile.artist_adjustments[21] == pytest.approx(-1.0)
    assert profile.tag_adjustments["post-punk"] == pytest.approx(0.10)
    assert profile.tag_adjustments["coldwave"] == pytest.approx(0.25)
    conn.close()


def test_load_feedback_profile_resolves_stale_artist_id_from_context_identity(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_artist(conn, artist_id=102, name="Stephen Lynch")
    _insert_run(conn, run_id="00000000-0000-0000-0000-000000000102")
    conn.execute(
        """
        INSERT INTO feedback_event (
            feedback_id,
            run_id,
            artist_id,
            action,
            value,
            context,
            note
        )
        VALUES (
            '00000000-0000-0000-0000-000000000202',
            '00000000-0000-0000-0000-000000000102',
            101,
            'hide_artist',
            1.0,
            '{"artist_key":"stephen-lynch","artist_name":"Stephen Lynch","tags":["comedy"]}',
            NULL
        )
        """
    )

    profile = load_feedback_profile(conn)

    assert 102 in profile.hidden_artist_ids
    assert profile.artist_adjustments[102] == pytest.approx(-1.0)
    conn.close()


def test_load_feedback_profile_preserves_explicit_zero_value(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    _insert_artist(conn, artist_id=30, name="Candidate Zero")
    _insert_run(conn, run_id="00000000-0000-0000-0000-000000000003")
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000003",
        artist_id=30,
        action="more_like_this",
        value=0.0,
        context={"tags": ["coldwave"]},
        feedback_id="00000000-0000-0000-0000-000000000401",
    )

    profile = load_feedback_profile(conn)

    assert profile.artist_adjustments.get(30, 0.0) == pytest.approx(0.0)
    assert profile.tag_adjustments.get("coldwave", 0.0) == pytest.approx(0.0)
    conn.close()


def test_feedback_adjustment_for_candidate_combines_sources_and_clamps() -> None:
    profile = FeedbackProfile(
        artist_adjustments={1: 0.8, 2: -0.8},
        tag_adjustments={"coldwave": 0.5, "arena rock": -0.6},
    )

    assert feedback_adjustment_for_candidate(
        profile,
        artist_id=1,
        candidate_tags={"coldwave": 1.0},
        neighborhood_score=0.2,
    ) == pytest.approx(1.0)
    assert feedback_adjustment_for_candidate(
        profile,
        artist_id=2,
        candidate_tags={"arena rock": 1.0},
        neighborhood_score=-0.2,
    ) == pytest.approx(-1.0)
    assert feedback_adjustment_for_candidate(
        profile,
        artist_id=99,
        candidate_tags={"coldwave": 0.4, "unknown": 1.0},
    ) == pytest.approx(0.2)


def test_feedback_obviousness_for_candidate_weights_tags_and_clamps() -> None:
    profile = FeedbackProfile(
        tag_obviousness_adjustments={
            "coldwave": 0.8,
            "post-punk": 0.5,
            "fresh": -0.4,
        },
    )

    assert feedback_obviousness_for_candidate(
        profile,
        candidate_tags={"coldwave": 1.0, "post-punk": 1.0},
    ) == pytest.approx(1.0)
    assert feedback_obviousness_for_candidate(
        profile,
        candidate_tags={"fresh": 2.0},
    ) == pytest.approx(0.0)
    assert feedback_obviousness_for_candidate(
        profile,
        candidate_tags={"coldwave": 0.25},
    ) == pytest.approx(0.2)


def test_apply_feedback_to_tag_profile_suppresses_and_normalizes_tags() -> None:
    profile = FeedbackProfile(
        tag_adjustments={
            "coldwave": 0.25,
            "arena rock": -0.5,
            "post-punk": 0.1,
        },
    )

    adjusted = apply_feedback_to_tag_profile(
        {
            "coldwave": 0.25,
            "arena rock": 0.4,
        },
        profile,
    )

    assert adjusted == {
        "coldwave": pytest.approx(1.0),
        "post-punk": pytest.approx(0.2),
    }
    assert (
        apply_feedback_to_tag_profile(
            {"arena rock": 0.2},
            FeedbackProfile(tag_adjustments={"arena rock": -0.5}),
        )
        == {}
    )


def test_rerank_recommendation_run_demotes_rejected_artist_and_updates_explanation(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000501"
    _insert_artist(conn, artist_id=40, name="First Artist")
    _insert_artist(conn, artist_id=41, name="Second Artist")
    _insert_run(conn, run_id=run_id)
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=40,
        rank=1,
        final_score=0.80,
        explanation={
            "summary": "First",
            "tags": [{"tag": "post-punk", "candidate_weight": 1.0}],
        },
    )
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=41,
        rank=2,
        final_score=0.70,
        explanation={
            "summary": "Second",
            "tags": [{"tag": "coldwave", "candidate_weight": 1.0}],
        },
    )
    record_feedback_event(
        conn,
        run_id=run_id,
        artist_id=40,
        action="reject",
        context={"tags": ["post-punk"]},
        feedback_id="00000000-0000-0000-0000-000000000601",
    )

    stats = rerank_recommendation_run(conn, run_id)

    assert stats.run_id == run_id
    assert stats.recommendations == 2
    rows = conn.execute(
        """
        SELECT rank, artist_id, feedback_score, explanation
        FROM recommendation
        WHERE run_id = ?
        ORDER BY rank
        """,
        [run_id],
    ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [(1, 41), (2, 40)]
    assert rows[1][2] < 0.0
    explanation = json.loads(rows[1][3])
    assert explanation["feedback"]["score"] == pytest.approx(rows[1][2])
    assert "suppression" in explanation["feedback"]["effects"]
    conn.close()


def test_rerank_recommendation_run_hides_current_too_obvious_artist(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000502"
    _insert_artist(conn, artist_id=50, name="Too Obvious Artist")
    _insert_artist(conn, artist_id=51, name="Remaining Artist")
    _insert_run(conn, run_id=run_id)
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=50,
        rank=1,
        final_score=0.80,
        explanation={
            "summary": "Too obvious",
            "tags": [{"tag": "post-punk", "candidate_weight": 1.0}],
        },
    )
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=51,
        rank=2,
        final_score=0.70,
        explanation={
            "summary": "Remaining",
            "tags": [{"tag": "coldwave", "candidate_weight": 1.0}],
        },
    )
    record_feedback_event(
        conn,
        run_id=run_id,
        artist_id=50,
        action="too_obvious",
        context={"tags": ["post-punk"]},
        feedback_id="00000000-0000-0000-0000-000000000602",
    )

    stats = rerank_recommendation_run(conn, run_id)

    assert stats.recommendations == 1
    assert _feedback_recommendation_names(conn, run_id) == ["Remaining Artist"]
    conn.close()


def test_rerank_recommendation_run_does_not_hide_current_feedback_from_other_runs(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    source_run_id = "00000000-0000-0000-0000-000000000504"
    target_run_id = "00000000-0000-0000-0000-000000000505"
    _insert_artist(conn, artist_id=70, name="Shared Artist")
    _insert_run(conn, run_id=source_run_id)
    _insert_run(conn, run_id=target_run_id)
    _insert_recommendation(
        conn,
        run_id=target_run_id,
        artist_id=70,
        rank=1,
        final_score=0.80,
        explanation={
            "summary": "Shared",
            "tags": [{"tag": "post-punk", "candidate_weight": 1.0}],
        },
    )
    record_feedback_event(
        conn,
        run_id=source_run_id,
        artist_id=70,
        action="too_obvious",
        context={"tags": ["post-punk"]},
        feedback_id="00000000-0000-0000-0000-000000000604",
    )

    stats = rerank_recommendation_run(conn, target_run_id)

    assert stats.recommendations == 1
    assert stats.hidden == 0
    assert _feedback_recommendation_names(conn, target_run_id) == ["Shared Artist"]
    conn.close()


def test_rerank_recommendation_run_does_not_compound_existing_feedback_score(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000503"
    _insert_artist(conn, artist_id=60, name="Stable Artist")
    _insert_run(conn, run_id=run_id)
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=60,
        rank=1,
        final_score=0.80,
        explanation={
            "summary": "Stable",
            "tags": [{"tag": "post-punk", "candidate_weight": 1.0}],
        },
    )
    record_feedback_event(
        conn,
        run_id=run_id,
        artist_id=60,
        action="more_like_this",
        context={"tags": ["post-punk"]},
        feedback_id="00000000-0000-0000-0000-000000000603",
    )

    rerank_recommendation_run(conn, run_id)
    first_score = _recommendation_score(conn, run_id=run_id, artist_id=60)
    rerank_recommendation_run(conn, run_id)
    second_score = _recommendation_score(conn, run_id=run_id, artist_id=60)

    assert second_score == pytest.approx(first_score)
    conn.close()


def _insert_artist(conn, *, artist_id: int, name: str) -> None:
    conn.execute(
        """
        INSERT INTO artist (artist_id, canonical_name, artist_key, lastfm_name)
        VALUES (?, ?, ?, ?)
        """,
        [artist_id, name, name.casefold().replace(" ", "-"), name],
    )


def _insert_run(conn, *, run_id: str) -> None:
    conn.execute(
        """
        INSERT INTO recommendation_run (
            run_id,
            config_hash,
            mode,
            seed_summary,
            scoring_weights
        )
        VALUES (?, 'hash', 'balanced', '{}', '{}')
        """,
        [run_id],
    )


def _insert_recommendation(
    conn,
    *,
    run_id: str,
    artist_id: int,
    rank: int,
    final_score: float,
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
        VALUES (?, ?, ?, ?, ?, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ?)
        """,
        [run_id, rank, artist_id, final_score, final_score, json.dumps(explanation)],
    )


def _feedback_recommendation_names(conn, run_id: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """
            SELECT a.canonical_name
            FROM recommendation r
            JOIN artist a ON a.artist_id = r.artist_id
            WHERE r.run_id = ?
            ORDER BY r.rank
            """,
            [run_id],
        ).fetchall()
    ]


def _recommendation_score(conn, *, run_id: str, artist_id: int) -> tuple[float, float]:
    final_score, feedback_score = conn.execute(
        """
        SELECT final_score, feedback_score
        FROM recommendation
        WHERE run_id = ?
          AND artist_id = ?
        """,
        [run_id, artist_id],
    ).fetchone()
    return (float(final_score), float(feedback_score))
