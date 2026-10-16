import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import duckdb
import pytest

from scrobble_recs.models.profile_window import ListeningWindow


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query_kind": "general", "target_artist_id": 3},
        {"query_kind": "artist"},
        {"query_kind": "unknown"},
        {"query_kind": "artist", "target_artist_id": -1},
    ],
)
def test_request_rejects_invalid_query_target_combinations(kwargs) -> None:
    from scrobble_recs.models.recommendation_query import RecommendationRequest

    with pytest.raises(ValueError):
        RecommendationRequest(**kwargs)


def test_query_serializes_sorted_seeds_and_naive_utc_without_general_target_fields() -> None:
    from scrobble_recs.models.recommendation_query import (
        QueryContext,
        RecommendationVersions,
        TasteRegionSeed,
    )

    offset = timezone(timedelta(hours=-7))
    context = QueryContext(
        query_kind="general",
        history_window=ListeningWindow(datetime(2025, 1, 1), datetime(2026, 1, 1)),
        history_cutoff=datetime(2026, 1, 1, tzinfo=offset),
        mode="balanced",
        novelty_intent="mixed",
        taste_region_seeds=(
            TasteRegionSeed(9, 0.5, ("dormant", "core")),
            TasteRegionSeed(2, 1.0, ("current", "curiosity")),
        ),
        feedback_cutoff=datetime(2026, 1, 1, tzinfo=offset),
        evidence_snapshot_id="snapshot",
        config_hash="config",
    )
    payload = context.to_json_dict()
    assert payload["history_window"] == {
        "start": "2025-01-01T00:00:00",
        "end": "2026-01-01T00:00:00",
    }
    assert payload["history_cutoff"] == "2026-01-01T07:00:00"
    assert payload["feedback_cutoff"] == "2026-01-01T07:00:00"
    assert payload["taste_region_seeds"] == [
        {"artist_id": 2, "weight": 1.0, "regions": ["curiosity", "current"]},
        {"artist_id": 9, "weight": 0.5, "regions": ["core", "dormant"]},
    ]
    assert "target_artist_id" not in payload
    assert "target_relationship" not in payload
    assert payload["versions"]["ranker"] == "legacy-v1"
    assert RecommendationVersions().query_schema == "query-v1"
    with pytest.raises(FrozenInstanceError):
        context.mode = "explore"


def test_artist_context_serializes_relationship_dates() -> None:
    from scrobble_recs.models.recommendation_query import QueryContext, TargetRelationshipSnapshot

    relationship = TargetRelationshipSnapshot(
        state="sampled",
        play_count=2,
        recency_weighted_play_count=1.5,
        unique_tracks=2,
        unique_releases=1,
        active_days=1,
        active_months=1,
        first_listen=datetime(2025, 1, 1, tzinfo=timezone(timedelta(hours=2))),
        last_listen=datetime(2025, 1, 1),
        listening_span_days=0,
        recent_trend=0.0,
        replay_concentration=0.5,
        positive_feedback=0.0,
        negative_feedback=0.0,
        prior_recommendation_exposures=0,
    )
    context = QueryContext(
        query_kind="artist",
        target_artist_id=7,
        target_relationship=relationship,
        history_window=ListeningWindow(datetime(2025, 1, 1), datetime(2026, 1, 1)),
        mode="balanced",
        novelty_intent="mixed",
        taste_region_seeds=(),
        evidence_snapshot_id="snapshot",
        config_hash="config",
    )
    payload = context.to_json_dict()
    assert payload["target_artist_id"] == 7
    assert payload["target_relationship"]["first_listen"] == "2024-12-31T22:00:00"
    assert payload["target_relationship"]["play_count"] == 2
    json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize(
    ("table", "timestamp"),
    [
        ("artist_similarity_edge", "fetched_at"),
        ("artist_tag", "fetched_at"),
        ("tag_top_artist", "fetched_at"),
        ("artist_identity_resolution", "resolved_at"),
    ],
)
def test_evidence_hash_tracks_each_source_and_respects_cutoff(
    tmp_path: Path, table: str, timestamp: str
) -> None:
    from scrobble_recs.models.recommendation_query import build_evidence_snapshot_id

    conn = duckdb.connect(str(tmp_path / "evidence.duckdb"))
    try:
        cutoff = datetime(2025, 1, 1)
        empty = build_evidence_snapshot_id(conn, historical_cutoff=cutoff)
        assert len(empty) == 64
        conn.execute(f"CREATE TABLE {table} ({timestamp} TIMESTAMP)")
        assert build_evidence_snapshot_id(conn, historical_cutoff=cutoff) == empty
        conn.execute(f"INSERT INTO {table} VALUES ('2026-01-01')")
        assert build_evidence_snapshot_id(conn, historical_cutoff=cutoff) == empty
        conn.execute(f"INSERT INTO {table} VALUES ('2025-01-01')")
        bounded = build_evidence_snapshot_id(conn, historical_cutoff=cutoff)
        assert bounded != empty
        assert build_evidence_snapshot_id(conn, historical_cutoff=cutoff) == bounded
        assert build_evidence_snapshot_id(conn, historical_cutoff=None) != bounded
    finally:
        conn.close()


def test_empty_evidence_hash_uses_canonical_zero_null_summary(tmp_path: Path) -> None:
    from scrobble_recs.models.recommendation_query import build_evidence_snapshot_id

    conn = duckdb.connect(str(tmp_path / "empty.duckdb"))
    try:
        summary = {
            table: {"row_count": 0, "latest_at": None}
            for table in (
                "artist_similarity_edge",
                "artist_tag",
                "tag_top_artist",
                "artist_identity_resolution",
            )
        }
        expected = sha256(json.dumps(summary, sort_keys=True, separators=(",", ":")).encode())
        assert build_evidence_snapshot_id(conn, historical_cutoff=None) == expected.hexdigest()
    finally:
        conn.close()
