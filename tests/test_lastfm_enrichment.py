import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from duckdb import ConversionException

from scrobble_recs.apis.cache import ApiCache
from scrobble_recs.apis.lastfm import LastFmArtistTag, LastFmSimilarArtist, LastFmTagTopArtist
from scrobble_recs.config import ScrobbleRecsConfig, load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.enrichment.lastfm import run_lastfm_enrichment
from scrobble_recs.importers.lastfm_csv import import_lastfm_csv


class FakeLastFmClient:
    def __init__(self) -> None:
        self.similar_calls: list[tuple[str, int]] = []
        self.tag_calls: list[str] = []
        self.top_artist_calls: list[tuple[str, int]] = []

    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        self.similar_calls.append((artist, limit))
        return [
            LastFmSimilarArtist("Siouxsie and the Banshees", "mbid-siouxsie", 0.91),
            LastFmSimilarArtist("Bauhaus", None, 0.72),
        ]

    def get_artist_tags(self, artist: str) -> list[LastFmArtistTag]:
        self.tag_calls.append(artist)
        return [
            LastFmArtistTag("post-punk", 100.0),
            LastFmArtistTag("gothic rock", 75.0),
        ]

    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        self.top_artist_calls.append((tag, limit))
        return [
            LastFmTagTopArtist(1, "The Cure", "mbid-cure", 1000.0),
            LastFmTagTopArtist(2, "Joy Division", None, 800.0),
        ]


class SparseLastFmClient:
    def __init__(self) -> None:
        self.similar_calls: list[tuple[str, int]] = []
        self.tag_calls: list[str] = []
        self.top_artist_calls: list[tuple[str, int]] = []

    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        self.similar_calls.append((artist, limit))
        return [LastFmSimilarArtist("Cocteau Twins", None, 0.61)]

    def get_artist_tags(self, artist: str) -> list[LastFmArtistTag]:
        self.tag_calls.append(artist)
        return [LastFmArtistTag("dream pop", 12.0)]

    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        self.top_artist_calls.append((tag, limit))
        return []


class LimitHonoringLastFmClient(FakeLastFmClient):
    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        return super().get_similar_artists(artist, limit=limit)[:limit]

    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        return super().get_tag_top_artists(tag, limit=limit)[:limit]


class FailingSimilarLastFmClient(LimitHonoringLastFmClient):
    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        self.similar_calls.append((artist, limit))
        raise RuntimeError("similar request failed")


class InvalidSimilarityMatchLastFmClient(LimitHonoringLastFmClient):
    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        self.similar_calls.append((artist, limit))
        return [
            LastFmSimilarArtist(
                "The Jesus and Mary Chain",
                None,
                "not-a-float",  # type: ignore[arg-type]
            )
        ]


class PartiallyFailingSimilarLastFmClient(LimitHonoringLastFmClient):
    def get_similar_artists(self, artist: str, *, limit: int) -> list[LastFmSimilarArtist]:
        self.similar_calls.append((artist, limit))
        return [
            LastFmSimilarArtist("Siouxsie and the Banshees", "mbid-siouxsie", 0.91),
            LastFmSimilarArtist("Bauhaus", None, 0.72),
            LastFmSimilarArtist(
                "The Jesus and Mary Chain",
                None,
                "not-a-float",  # type: ignore[arg-type]
            ),
        ][:limit]


class InvalidTagTopArtistCountLastFmClient(LimitHonoringLastFmClient):
    def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
        self.top_artist_calls.append((tag, limit))
        return [
            LastFmTagTopArtist(1, "The Cure", "mbid-cure", 1000.0),
            LastFmTagTopArtist(
                2,
                "Joy Division",
                None,
                "not-a-float",  # type: ignore[arg-type]
            ),
        ][:limit]


def _config(
    tmp_path: Path,
    *,
    lastfm_enabled: bool = True,
    lastfm_api_key: str | None = "test-key",
    similar_artists_per_seed: int = 2,
    tag_top_artists_limit: int = 2,
) -> ScrobbleRecsConfig:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "db.duckdb"),
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
                "apis": {
                    "lastfm": {
                        "enabled": lastfm_enabled,
                        "api_key": lastfm_api_key,
                    }
                },
                "recommendation": {
                    "seed_artist_limit": 1,
                    "similar_artists_per_seed": similar_artists_per_seed,
                    "min_seed_scrobbles": 2,
                    "obviousness": {"tag_top_artists_limit": tag_top_artists_limit},
                },
            }
        ),
        encoding="utf-8",
    )
    return load_config(path)


def test_run_lastfm_enrichment_requires_api_key_before_seed_selection(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, lastfm_api_key=None)
    conn = initialize_database(config)

    with pytest.raises(RuntimeError, match="Last.fm API key is required for enrichment"):
        run_lastfm_enrichment(conn, config)

    conn.close()


def test_run_lastfm_enrichment_reports_disabled_before_missing_key(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, lastfm_enabled=False, lastfm_api_key=None)
    conn = initialize_database(config)

    with pytest.raises(RuntimeError, match="Last.fm enrichment is disabled"):
        run_lastfm_enrichment(conn, config)

    conn.close()


def test_run_lastfm_enrichment_persists_edges_tags_and_top_artists(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    client = FakeLastFmClient()

    stats = run_lastfm_enrichment(conn, config, client=client)

    assert stats.seed_artists == 1
    assert stats.similar_artist_requests == 1
    assert stats.artist_tag_requests == 1
    assert stats.tag_top_artist_requests == 2
    assert stats.similarity_edges == 2
    assert stats.artist_tags == 2
    assert stats.tag_top_artists == 4
    assert client.similar_calls == [("The Cure", 2)]
    assert client.tag_calls == ["The Cure"]
    assert client.top_artist_calls == [("post-punk", 2), ("gothic rock", 2)]

    edge_rows = conn.execute(
        """
        SELECT source_artist.canonical_name, target_artist.canonical_name, edge.normalized_score
        FROM artist_similarity_edge edge
        JOIN artist source_artist ON source_artist.artist_id = edge.source_artist_id
        JOIN artist target_artist ON target_artist.artist_id = edge.target_artist_id
        ORDER BY target_artist.canonical_name
        """
    ).fetchall()
    assert edge_rows == [
        ("The Cure", "Bauhaus", 0.72),
        ("The Cure", "Siouxsie and the Banshees", 0.91),
    ]

    tag_rows = conn.execute(
        """
        SELECT tag, tag_count, normalized_weight, fetched_at
        FROM artist_tag
        ORDER BY tag
        """
    ).fetchall()
    assert [(row[0], row[1], row[2]) for row in tag_rows] == [
        ("gothic rock", 75.0, 0.75),
        ("post-punk", 100.0, 1.0),
    ]
    assert all(isinstance(row[3], datetime) and row[3].tzinfo is None for row in tag_rows)

    top_artist_rows = conn.execute(
        """
        SELECT tag, artist.canonical_name, tag_rank, tag_count, fetched_at
        FROM tag_top_artist
        JOIN artist ON artist.artist_id = tag_top_artist.artist_id
        ORDER BY tag, tag_rank
        """
    ).fetchall()
    assert [(row[0], row[1], row[2], row[3]) for row in top_artist_rows] == [
        ("gothic rock", "The Cure", 1, 1000.0),
        ("gothic rock", "Joy Division", 2, 800.0),
        ("post-punk", "The Cure", 1, 1000.0),
        ("post-punk", "Joy Division", 2, 800.0),
    ]
    assert all(isinstance(row[4], datetime) and row[4].tzinfo is None for row in top_artist_rows)

    evidence_rows = conn.execute(
        """
        SELECT CAST(evidence AS VARCHAR)
        FROM artist_similarity_edge
        ORDER BY normalized_score
        """
    ).fetchall()
    assert [json.loads(row[0]) for row in evidence_rows] == [
        {"method": "artist.getSimilar", "target_name": "Bauhaus"},
        {"method": "artist.getSimilar", "target_name": "Siouxsie and the Banshees"},
    ]
    conn.close()


def test_run_lastfm_enrichment_reports_phase_item_progress(tmp_path: Path) -> None:
    config = _config(tmp_path)
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    client = FakeLastFmClient()
    events = []

    run_lastfm_enrichment(conn, config, client=client, progress_callback=events.append)

    assert [
        (event.phase, event.current, event.total, event.subject, event.status) for event in events
    ] == [
        ("seed_similarity", 0, 1, "The Cure", "processing"),
        ("seed_similarity", 1, 1, "The Cure", "completed"),
        ("seed_tags", 0, 1, "The Cure", "processing"),
        ("seed_tags", 1, 1, "The Cure", "completed"),
        ("tag_top_artists", 0, 2, "post-punk", "processing"),
        ("tag_top_artists", 1, 2, "post-punk", "completed"),
        ("tag_top_artists", 1, 2, "gothic rock", "processing"),
        ("tag_top_artists", 2, 2, "gothic rock", "completed"),
    ]
    conn.close()


def test_run_lastfm_enrichment_skips_already_normalized_work_on_second_run(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    first_client = FakeLastFmClient()
    second_client = FakeLastFmClient()

    run_lastfm_enrichment(conn, config, client=first_client)
    stats = run_lastfm_enrichment(conn, config, client=second_client)

    assert stats.similar_artist_requests == 0
    assert stats.artist_tag_requests == 0
    assert stats.tag_top_artist_requests == 0
    assert second_client.similar_calls == []
    assert second_client.tag_calls == []
    assert second_client.top_artist_calls == []
    assert conn.execute("SELECT count(*) FROM artist_similarity_edge").fetchone() == (2,)
    assert conn.execute("SELECT count(*) FROM artist_tag").fetchone() == (2,)
    assert conn.execute("SELECT count(*) FROM tag_top_artist").fetchone() == (4,)
    conn.close()


def test_run_lastfm_enrichment_skips_completed_sparse_and_empty_responses(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, similar_artists_per_seed=3, tag_top_artists_limit=2)
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    first_client = SparseLastFmClient()
    second_client = SparseLastFmClient()

    run_lastfm_enrichment(conn, config, client=first_client)
    stats = run_lastfm_enrichment(conn, config, client=second_client)

    assert stats.similar_artist_requests == 0
    assert stats.artist_tag_requests == 0
    assert stats.tag_top_artist_requests == 0
    assert second_client.similar_calls == []
    assert second_client.tag_calls == []
    assert second_client.top_artist_calls == []
    assert conn.execute("SELECT count(*) FROM artist_similarity_edge").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM artist_tag").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM tag_top_artist").fetchone() == (0,)
    conn.close()


def test_run_lastfm_enrichment_retries_partial_artist_tags_without_completion(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    seed_artist_id = conn.execute(
        "SELECT artist_id FROM artist WHERE canonical_name = 'The Cure'"
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO artist_tag (
            artist_id,
            tag,
            source,
            tag_count,
            normalized_weight,
            fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [seed_artist_id, "post-punk", "lastfm", 100.0, 1.0, datetime(2026, 6, 3)],
    )
    client = FakeLastFmClient()

    stats = run_lastfm_enrichment(conn, config, client=client)

    assert stats.artist_tag_requests == 1
    assert stats.artist_tags == 1
    assert client.tag_calls == ["The Cure"]
    assert conn.execute("SELECT count(*) FROM artist_tag").fetchone() == (2,)
    tag_rows = conn.execute("SELECT tag FROM artist_tag ORDER BY tag").fetchall()
    assert tag_rows == [("gothic rock",), ("post-punk",)]
    conn.close()


def test_run_lastfm_enrichment_closes_default_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    closed_clients: list[bool] = []

    class ClosingClient:
        def __init__(self, **_: object) -> None:
            self.closed = False

        def get_similar_artists(
            self,
            artist: str,
            *,
            limit: int,
        ) -> list[LastFmSimilarArtist]:
            return []

        def get_artist_tags(self, artist: str) -> list[LastFmArtistTag]:
            return []

        def get_tag_top_artists(self, tag: str, *, limit: int) -> list[LastFmTagTopArtist]:
            return []

        def close(self) -> None:
            self.closed = True
            closed_clients.append(self.closed)

    monkeypatch.setattr("scrobble_recs.enrichment.lastfm.LastFmClient", ClosingClient)

    run_lastfm_enrichment(conn, config)

    assert closed_clients == [True]
    conn.close()


def test_import_replaces_profile_derived_enrichment_but_preserves_api_cache(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    conn = initialize_database(config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, config, client=FakeLastFmClient())
    ApiCache(conn).write_response(
        source="lastfm",
        endpoint="artist.getSimilar",
        request_url="https://ws.audioscrobbler.com/2.0/",
        request_params={"method": "artist.getSimilar", "artist": "The Cure", "limit": 2},
        response_status=200,
        response_json={"similarartists": {"artist": []}},
        fetched_at=datetime(2026, 6, 3, 12, 0, 0),
        expires_at=datetime(2026, 6, 3, 12, 0, 0) + timedelta(days=180),
        error_message=None,
    )
    replacement_csv = tmp_path / "replacement.csv"
    replacement_csv.write_text(
        "\n".join(
            [
                "uts,utc_time,artist,artist_mbid,album,album_mbid,track,track_mbid",
                "1704067200,2024-01-01 00:00:00,New Order,,"
                "Power Corruption and Lies,,Age of Consent,",
                "1704153600,2024-01-02 00:00:00,New Order,,"
                "Power Corruption and Lies,,Your Silent Face,",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    import_lastfm_csv(conn, replacement_csv)

    assert conn.execute("SELECT count(*) FROM api_cache").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM enrichment_work").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM artist_similarity_edge").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM artist_tag").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM tag_top_artist").fetchone() == (0,)
    assert conn.execute("SELECT canonical_name FROM artist").fetchall() == [("New Order",)]
    conn.close()


def test_run_lastfm_enrichment_replaces_limit_scoped_rows_when_limits_change(
    tmp_path: Path,
) -> None:
    initial_config = _config(tmp_path, similar_artists_per_seed=2, tag_top_artists_limit=2)
    conn = initialize_database(initial_config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, initial_config, client=LimitHonoringLastFmClient())
    lower_limit_config = _config(tmp_path, similar_artists_per_seed=1, tag_top_artists_limit=1)
    second_client = LimitHonoringLastFmClient()

    stats = run_lastfm_enrichment(conn, lower_limit_config, client=second_client)

    assert stats.similar_artist_requests == 1
    assert stats.tag_top_artist_requests == 2
    assert second_client.similar_calls == [("The Cure", 1)]
    assert second_client.top_artist_calls == [("post-punk", 1), ("gothic rock", 1)]

    edge_rows = conn.execute(
        """
        SELECT target_artist.canonical_name
        FROM artist_similarity_edge edge
        JOIN artist target_artist ON target_artist.artist_id = edge.target_artist_id
        ORDER BY target_artist.canonical_name
        """
    ).fetchall()
    assert edge_rows == [("Siouxsie and the Banshees",)]

    top_artist_rows = conn.execute(
        """
        SELECT tag, artist.canonical_name, tag_rank
        FROM tag_top_artist
        JOIN artist ON artist.artist_id = tag_top_artist.artist_id
        ORDER BY tag, tag_rank
        """
    ).fetchall()
    assert top_artist_rows == [
        ("gothic rock", "The Cure", 1),
        ("post-punk", "The Cure", 1),
    ]

    work_rows = conn.execute(
        """
        SELECT endpoint, subject_key, request_limit
        FROM enrichment_work
        WHERE endpoint IN ('artist.getSimilar', 'tag.getTopArtists')
        ORDER BY endpoint, subject_key
        """
    ).fetchall()
    assert work_rows == [
        ("artist.getSimilar", "the cure", 1),
        ("tag.getTopArtists", "gothic rock", 1),
        ("tag.getTopArtists", "post-punk", 1),
    ]
    conn.close()


def test_failed_limit_change_preserves_previous_completed_similarity_rows(
    tmp_path: Path,
) -> None:
    initial_config = _config(tmp_path, similar_artists_per_seed=2, tag_top_artists_limit=2)
    conn = initialize_database(initial_config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, initial_config, client=LimitHonoringLastFmClient())
    higher_limit_config = _config(tmp_path, similar_artists_per_seed=3, tag_top_artists_limit=2)
    failing_client = FailingSimilarLastFmClient()

    with pytest.raises(RuntimeError, match="similar request failed"):
        run_lastfm_enrichment(conn, higher_limit_config, client=failing_client)

    assert failing_client.similar_calls == [("The Cure", 3)]
    edge_rows = conn.execute(
        """
        SELECT target_artist.canonical_name
        FROM artist_similarity_edge edge
        JOIN artist target_artist ON target_artist.artist_id = edge.target_artist_id
        ORDER BY target_artist.canonical_name
        """
    ).fetchall()
    assert edge_rows == [
        ("Bauhaus",),
        ("Siouxsie and the Banshees",),
    ]
    work_rows = conn.execute(
        """
        SELECT endpoint, subject_key, request_limit
        FROM enrichment_work
        WHERE endpoint = 'artist.getSimilar'
        ORDER BY subject_key
        """
    ).fetchall()
    assert work_rows == [("artist.getSimilar", "the cure", 2)]
    conn.close()


def test_limit_change_write_failure_rolls_back_previous_similarity_rows(
    tmp_path: Path,
) -> None:
    initial_config = _config(tmp_path, similar_artists_per_seed=2, tag_top_artists_limit=2)
    conn = initialize_database(initial_config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, initial_config, client=LimitHonoringLastFmClient())
    higher_limit_config = _config(tmp_path, similar_artists_per_seed=3, tag_top_artists_limit=2)
    invalid_client = InvalidSimilarityMatchLastFmClient()

    with pytest.raises(ConversionException):
        run_lastfm_enrichment(conn, higher_limit_config, client=invalid_client)

    assert invalid_client.similar_calls == [("The Cure", 3)]
    edge_rows = conn.execute(
        """
        SELECT target_artist.canonical_name
        FROM artist_similarity_edge edge
        JOIN artist target_artist ON target_artist.artist_id = edge.target_artist_id
        ORDER BY target_artist.canonical_name
        """
    ).fetchall()
    assert edge_rows == [
        ("Bauhaus",),
        ("Siouxsie and the Banshees",),
    ]
    work_rows = conn.execute(
        """
        SELECT endpoint, subject_key, request_limit
        FROM enrichment_work
        WHERE endpoint = 'artist.getSimilar'
        ORDER BY subject_key
        """
    ).fetchall()
    assert work_rows == [("artist.getSimilar", "the cure", 2)]
    conn.close()


def test_first_run_similarity_write_failure_rolls_back_rows_and_marker(
    tmp_path: Path,
) -> None:
    failing_config = _config(tmp_path, similar_artists_per_seed=3, tag_top_artists_limit=2)
    conn = initialize_database(failing_config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    failing_client = PartiallyFailingSimilarLastFmClient()

    with pytest.raises(ConversionException):
        run_lastfm_enrichment(conn, failing_config, client=failing_client)

    assert failing_client.similar_calls == [("The Cure", 3)]
    assert conn.execute("SELECT count(*) FROM artist_similarity_edge").fetchone() == (0,)
    assert conn.execute(
        """
            SELECT count(*)
            FROM enrichment_work
            WHERE endpoint = 'artist.getSimilar'
            """
    ).fetchone() == (0,)

    lower_limit_config = _config(tmp_path, similar_artists_per_seed=1, tag_top_artists_limit=2)
    run_lastfm_enrichment(conn, lower_limit_config, client=LimitHonoringLastFmClient())

    edge_rows = conn.execute(
        """
        SELECT target_artist.canonical_name
        FROM artist_similarity_edge edge
        JOIN artist target_artist ON target_artist.artist_id = edge.target_artist_id
        ORDER BY target_artist.canonical_name
        """
    ).fetchall()
    assert edge_rows == [("Siouxsie and the Banshees",)]
    work_rows = conn.execute(
        """
        SELECT endpoint, subject_key, request_limit
        FROM enrichment_work
        WHERE endpoint = 'artist.getSimilar'
        ORDER BY subject_key
        """
    ).fetchall()
    assert work_rows == [("artist.getSimilar", "the cure", 1)]
    conn.close()


def test_tag_top_artist_replacement_write_failure_rolls_back_previous_rows(
    tmp_path: Path,
) -> None:
    initial_config = _config(tmp_path, similar_artists_per_seed=2, tag_top_artists_limit=2)
    conn = initialize_database(initial_config)
    import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))
    run_lastfm_enrichment(conn, initial_config, client=LimitHonoringLastFmClient())
    higher_limit_config = _config(tmp_path, similar_artists_per_seed=2, tag_top_artists_limit=3)
    invalid_client = InvalidTagTopArtistCountLastFmClient()

    with pytest.raises(ConversionException):
        run_lastfm_enrichment(conn, higher_limit_config, client=invalid_client)

    assert invalid_client.top_artist_calls == [("post-punk", 3)]
    top_artist_rows = conn.execute(
        """
        SELECT tag, artist.canonical_name, tag_rank, tag_count
        FROM tag_top_artist
        JOIN artist ON artist.artist_id = tag_top_artist.artist_id
        ORDER BY tag, tag_rank
        """
    ).fetchall()
    assert top_artist_rows == [
        ("gothic rock", "The Cure", 1, 1000.0),
        ("gothic rock", "Joy Division", 2, 800.0),
        ("post-punk", "The Cure", 1, 1000.0),
        ("post-punk", "Joy Division", 2, 800.0),
    ]
    work_rows = conn.execute(
        """
        SELECT endpoint, subject_key, request_limit
        FROM enrichment_work
        WHERE endpoint = 'tag.getTopArtists'
        ORDER BY subject_key
        """
    ).fetchall()
    assert work_rows == [
        ("tag.getTopArtists", "gothic rock", 2),
        ("tag.getTopArtists", "post-punk", 2),
    ]
    conn.close()
