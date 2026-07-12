import csv
import json
from datetime import datetime
from pathlib import Path

import pytest

from scrobble_recs.apis.lastfm import LastFmArtistTag, LastFmSimilarArtist, LastFmTagTopArtist
from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.enrichment.lastfm import run_lastfm_enrichment
from scrobble_recs.export import export_recommendations
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv
from scrobble_recs.models.profile_window import ListeningWindow
from scrobble_recs.models.recommend import generate_recommendations

_FIXTURE_HISTORY_WINDOW = ListeningWindow(datetime(2023, 1, 1), datetime(2025, 1, 1))


class ExportFakeLastFmClient:
    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        return [LastFmSimilarArtist("Bauhaus", None, 0.72)]

    def get_artist_tags(self, artist: str) -> list[LastFmArtistTag]:
        return [LastFmArtistTag("gothic rock", 100.0)]

    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        return [LastFmTagTopArtist(4, "Bauhaus", None, 700.0)]


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


def _prepared_run(tmp_path: Path) -> tuple[object, str]:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, config, client=ExportFakeLastFmClient())
    stats = generate_recommendations(
        conn,
        config,
        limit=1,
        mode="balanced",
        as_of=datetime(2026, 6, 4),
        history_window=_FIXTURE_HISTORY_WINDOW,
    )
    return conn, stats.run_id


def test_export_recommendations_writes_csv(tmp_path: Path) -> None:
    conn, run_id = _prepared_run(tmp_path)
    output = tmp_path / "recs.csv"

    exported = export_recommendations(conn, output, format_name="csv", run_id=run_id)

    assert exported == 1
    with output.open("r", encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    assert rows[0]["rank"] == "1"
    assert rows[0]["artist"] == "Bauhaus"
    assert rows[0]["primary_tags"] == "gothic rock"
    assert rows[0]["why"].startswith("Matched tags: 1.")
    conn.close()


def test_export_recommendations_writes_jsonl(tmp_path: Path) -> None:
    conn, run_id = _prepared_run(tmp_path)
    output = tmp_path / "recs.jsonl"

    exported = export_recommendations(conn, output, format_name="jsonl", run_id=run_id)

    assert exported == 1
    row = json.loads(output.read_text(encoding="utf-8").strip())
    assert row["rank"] == 1
    assert row["artist"] == "Bauhaus"
    assert row["explanation"]["tags"][0]["tag"] == "gothic rock"
    conn.close()


def test_export_recommendations_normalizes_user_score_per_run(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000011"
    _insert_export_row(
        conn,
        run_id=run_id,
        artist_id=511,
        artist_name="Low Top Score",
        rank=1,
        created_at=datetime(2026, 6, 4, 12, 0, 0),
        final_score=0.087,
    )
    _insert_export_row(
        conn,
        run_id=run_id,
        artist_id=512,
        artist_name="Half Score",
        rank=2,
        created_at=datetime(2026, 6, 4, 12, 0, 0),
        final_score=0.0435,
        insert_run=False,
    )
    output = tmp_path / "recs.jsonl"

    export_recommendations(conn, output, format_name="jsonl", run_id=run_id)

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["score_normalized"] for row in rows] == [pytest.approx(1.0), pytest.approx(0.5)]
    assert conn.execute(
        """
        SELECT final_score
        FROM recommendation
        WHERE run_id = ?
          AND artist_id = 511
        """,
        [run_id],
    ).fetchone() == (pytest.approx(0.087),)
    conn.close()


def test_export_recommendations_rejects_invalid_format_before_run_lookup(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    output = tmp_path / "recs.txt"

    with pytest.raises(ValueError, match="format_name must be 'csv' or 'jsonl'"):
        export_recommendations(conn, output, format_name="txt")

    assert not output.exists()
    conn.close()


def test_export_recommendations_url_encodes_lastfm_artist_path(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000010"
    _insert_export_row(
        conn,
        run_id=run_id,
        artist_id=501,
        artist_name="AC/DC ?#é",
        rank=1,
        created_at=datetime(2026, 6, 4, 12, 0, 0),
        lastfm_url="https://www.last.fm/music/AC%2FDC%20%3F%23%C3%A9",
    )
    output = tmp_path / "recs.jsonl"

    export_recommendations(conn, output, format_name="jsonl", run_id=run_id)

    row = json.loads(output.read_text(encoding="utf-8").strip())
    assert row["lastfm_url"] == "https://www.last.fm/music/AC%2FDC%20%3F%23%C3%A9"
    conn.close()


def test_export_recommendations_uses_deterministic_latest_run_fallback(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    older_run_id = "00000000-0000-0000-0000-000000000001"
    latest_run_id = "00000000-0000-0000-0000-000000000002"
    shared_created_at = datetime(2026, 6, 4, 12, 0, 0)
    _insert_export_row(
        conn,
        run_id=older_run_id,
        artist_id=601,
        artist_name="Earlier Artist",
        rank=1,
        created_at=shared_created_at,
    )
    _insert_export_row(
        conn,
        run_id=latest_run_id,
        artist_id=602,
        artist_name="Tie Break Artist",
        rank=1,
        created_at=shared_created_at,
    )
    output = tmp_path / "latest.jsonl"

    export_recommendations(conn, output, format_name="jsonl")

    row = json.loads(output.read_text(encoding="utf-8").strip())
    assert row["artist"] == "Tie Break Artist"
    conn.close()


def test_csv_export_includes_quality_and_serendipity_columns(tmp_path: Path) -> None:
    conn = initialize_database(load_config(write_config(tmp_path)))
    run_id = "00000000-0000-0000-0000-000000000020"
    _insert_export_row(
        conn,
        run_id=run_id,
        artist_id=701,
        artist_name="Evidence Artist",
        rank=1,
        created_at=datetime(2026, 6, 4, 12, 0, 0),
    )
    conn.execute(
        "UPDATE recommendation_run SET seed_summary = ? WHERE run_id = ?",
        [
            json.dumps(
                {
                    "history_window": {
                        "start": "2025-06-04T00:00:00",
                        "end": "2026-06-04T00:00:00",
                    }
                }
            ),
            run_id,
        ],
    )
    conn.execute(
        "UPDATE recommendation SET explanation = ? WHERE run_id = ? AND artist_id = ?",
        [
            json.dumps(
                {
                    "quality_gate": {"reasons": ["independent_seed_support"]},
                    "serendipity": {
                        "calibrated_bonus": 0.12,
                        "cohesion": 0.40,
                        "is_bridge": True,
                    },
                }
            ),
            run_id,
            701,
        ],
    )
    output = tmp_path / "quality.csv"

    export_recommendations(conn, output, format_name="csv", run_id=run_id)

    with output.open(encoding="utf-8", newline="") as file:
        row = next(csv.DictReader(file))
    assert row["history_window_start"] == "2025-06-04T00:00:00"
    assert row["history_window_end"] == "2026-06-04T00:00:00"
    assert row["quality_gate_reasons"] == "independent_seed_support"
    assert float(row["serendipity_bonus"]) == pytest.approx(0.12)
    assert float(row["serendipity_cohesion"]) == pytest.approx(0.40)
    assert row["serendipity_bridge"] == "true"
    conn.close()


def test_export_uses_empty_quality_evidence_for_invalid_explanation(tmp_path: Path) -> None:
    conn = initialize_database(load_config(write_config(tmp_path)))
    run_id = "00000000-0000-0000-0000-000000000021"
    _insert_export_row(
        conn,
        run_id=run_id,
        artist_id=702,
        artist_name="Malformed Evidence",
        rank=1,
        created_at=datetime(2026, 6, 4, 12, 0, 0),
    )
    conn.execute(
        "UPDATE recommendation SET explanation = ? WHERE run_id = ? AND artist_id = ?",
        [json.dumps(["not", "an", "object"]), run_id, 702],
    )
    output = tmp_path / "malformed.jsonl"

    export_recommendations(conn, output, format_name="jsonl", run_id=run_id)

    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["quality_gate_reasons"] == ""
    assert row["serendipity_bonus"] == 0.0
    assert row["serendipity_cohesion"] == 0.0
    assert row["serendipity_bridge"] == "false"
    conn.close()


def test_jsonl_export_projects_non_finite_serendipity_values_as_zero(
    tmp_path: Path,
) -> None:
    conn = initialize_database(load_config(write_config(tmp_path)))
    run_id = "00000000-0000-0000-0000-000000000022"
    _insert_export_row(
        conn,
        run_id=run_id,
        artist_id=703,
        artist_name="Invalid Numeric Evidence",
        rank=1,
        created_at=datetime(2026, 6, 4, 12, 0, 0),
    )
    conn.execute(
        "UPDATE recommendation SET explanation = ? WHERE run_id = ? AND artist_id = ?",
        [
            json.dumps(
                {
                    "serendipity": {
                        "bonus": float("nan"),
                        "cohesion": float("inf"),
                    }
                }
            ),
            run_id,
            703,
        ],
    )
    output = tmp_path / "invalid-numeric-evidence.jsonl"

    export_recommendations(conn, output, format_name="jsonl", run_id=run_id)

    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["serendipity_bonus"] == 0.0
    assert row["serendipity_cohesion"] == 0.0
    conn.close()


def test_jsonl_export_replaces_nested_non_finite_explanation_values_with_null(
    tmp_path: Path,
) -> None:
    conn = initialize_database(load_config(write_config(tmp_path)))
    run_id = "00000000-0000-0000-0000-000000000024"
    _insert_export_row(
        conn,
        run_id=run_id,
        artist_id=705,
        artist_name="Nested Invalid Evidence",
        rank=1,
        created_at=datetime(2026, 6, 4, 12, 0, 0),
    )
    conn.execute(
        "UPDATE recommendation SET explanation = ? WHERE run_id = ? AND artist_id = ?",
        [
            json.dumps(
                {
                    "raw_components": {
                        "not_a_number": float("nan"),
                        "nested": [float("inf"), {"negative": float("-inf")}],
                    },
                    "serendipity": {"calibrated_bonus": 0.12},
                }
            ),
            run_id,
            705,
        ],
    )
    output = tmp_path / "nested-invalid-numeric-evidence.jsonl"

    export_recommendations(conn, output, format_name="jsonl", run_id=run_id)

    serialized = output.read_text(encoding="utf-8")

    def reject_non_finite_token(token: str) -> object:
        raise ValueError(f"non-finite JSON token: {token}")

    row = json.loads(serialized, parse_constant=reject_non_finite_token)
    assert "NaN" not in serialized
    assert "Infinity" not in serialized
    assert row["explanation"]["raw_components"] == {
        "not_a_number": None,
        "nested": [None, {"negative": None}],
    }
    assert row["explanation"]["serendipity"]["calibrated_bonus"] == pytest.approx(0.12)
    conn.close()


def test_jsonl_export_projects_boolean_serendipity_bonus_as_zero(tmp_path: Path) -> None:
    conn = initialize_database(load_config(write_config(tmp_path)))
    run_id = "00000000-0000-0000-0000-000000000023"
    _insert_export_row(
        conn,
        run_id=run_id,
        artist_id=704,
        artist_name="Boolean Numeric Evidence",
        rank=1,
        created_at=datetime(2026, 6, 4, 12, 0, 0),
    )
    conn.execute(
        "UPDATE recommendation SET explanation = ? WHERE run_id = ? AND artist_id = ?",
        [json.dumps({"serendipity": {"bonus": True}}), run_id, 704],
    )
    output = tmp_path / "boolean-numeric-evidence.jsonl"

    export_recommendations(conn, output, format_name="jsonl", run_id=run_id)

    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["serendipity_bonus"] == 0.0
    conn.close()


def _insert_export_row(
    conn,
    *,
    run_id: str,
    artist_id: int,
    artist_name: str,
    rank: int,
    created_at: datetime,
    final_score: float = 0.9,
    insert_run: bool = True,
    lastfm_url: str = "",
    score_band: str = "strong",
) -> None:
    conn.execute(
        """
        INSERT INTO artist (artist_id, canonical_name, artist_key)
        VALUES (?, ?, ?)
        """,
        [artist_id, artist_name, artist_name.casefold()],
    )
    if insert_run:
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
            VALUES (?, ?, 'hash', 'balanced', '{}', '{}')
            """,
            [run_id, created_at],
        )
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
            score_band,
            evidence_source_count,
            local_scrobble_count,
            listened_day_count,
            first_listened_at,
            last_listened_at,
            years_since_last_listen,
            rediscovery_eligible,
            trial_penalty,
            candidate_artist_name,
            canonical_artist_name,
            musicbrainz_artist_mbid,
            lastfm_url,
            musicbrainz_url,
            identity_confidence,
            identity_warnings,
            explanation
        )
        VALUES (
            ?, ?, ?, ?, ?, 0.7, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ?, ?,
            NULL, NULL, NULL, NULL, NULL, NULL, 0.0, ?, ?, ?, ?, ?, 0.5, '[]', ?
        )
        """,
        [
            run_id,
            rank,
            artist_id,
            final_score,
            final_score,
            score_band,
            1,
            artist_name,
            artist_name,
            "",
            lastfm_url,
            "",
            json.dumps(
                {
                    "summary": f"{artist_name} is similar to seed artists.",
                    "tags": [{"tag": "test tag"}],
                }
            ),
        ],
    )


def test_export_includes_candidate_only_with_flag(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000030"
    created_at = datetime(2026, 6, 4, 12, 0, 0)

    _insert_export_row(
        conn,
        run_id=run_id,
        artist_id=801,
        artist_name="Scored Artist",
        rank=1,
        created_at=created_at,
        final_score=0.9,
        score_band="strong",
    )
    _insert_export_row(
        conn,
        run_id=run_id,
        artist_id=802,
        artist_name="Candidate Only Artist",
        rank=2,
        created_at=created_at,
        final_score=0.0,
        insert_run=False,
        score_band="candidate_only",
    )

    output_without = tmp_path / "without.jsonl"
    exported = export_recommendations(conn, output_without, format_name="jsonl", run_id=run_id)
    assert exported == 1

    output_with = tmp_path / "with.jsonl"
    exported_with = export_recommendations(
        conn, output_with, format_name="jsonl", run_id=run_id, include_candidates=True
    )
    assert exported_with == 2
    conn.close()
