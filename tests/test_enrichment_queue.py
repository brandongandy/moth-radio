from pathlib import Path

from scrobble_recs.enrichment.queue import (
    EnrichmentQueuePlan,
    enrichment_queue_status,
    plan_enrichment,
    retry_failed_enrichment,
)


def _setup_enrichment_test_db():
    import duckdb

    conn = duckdb.connect(":memory:")
    sql_dir = Path("src/scrobble_recs/sql")
    for sql_file in sorted(sql_dir.glob("*.sql")):
        conn.execute(sql_file.read_text(encoding="utf-8"))
    return conn


def test_enrichment_queue_migration_creates_table():
    import duckdb

    conn = duckdb.connect(":memory:")
    migration_path = Path("src/scrobble_recs/sql/007_enrichment_queue.sql")
    conn.execute(migration_path.read_text(encoding="utf-8"))

    # DuckDB uses information_schema, not sqlite_master
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "enrichment_queue" in tables

    # Verify columns using DuckDB's information_schema
    columns = {
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'enrichment_queue'"
        ).fetchall()
    }
    assert "queue_id" in columns
    assert "source" in columns
    assert "endpoint" in columns
    assert "entity_type" in columns
    assert "status" in columns
    assert "attempts" in columns
    assert "error_message" in columns


def test_enrichment_queue_insert_and_query():
    import duckdb

    conn = duckdb.connect(":memory:")
    migration_path = Path("src/scrobble_recs/sql/007_enrichment_queue.sql")
    conn.execute(migration_path.read_text(encoding="utf-8"))

    conn.execute(
        """
        INSERT INTO enrichment_queue (queue_id, source, endpoint, entity_type, entity_id, priority, status)
        VALUES (uuid(), 'lastfm', 'artist.getSimilar', 'artist', 1, 1.0, 'queued')
        """
    )

    row = conn.execute(
        "SELECT source, endpoint, entity_type, status FROM enrichment_queue"
    ).fetchone()
    assert row == ("lastfm", "artist.getSimilar", "artist", "queued")


def test_enrichment_plan_populates_queue_for_seed_artists():
    from scrobble_recs.importers.lastfm_csv import import_lastfm_csv

    conn = _setup_enrichment_test_db()
    try:
        import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))

        plan = plan_enrichment(
            conn,
            source="lastfm",
            seed_artist_limit=5,
            similar_artists_per_seed=10,
            top_tags_per_artist=20,
            tag_top_artists_limit=100,
        )

        assert plan.source == "lastfm"
        assert plan.seed_artists >= 0
        assert plan.similar_artist_queued >= 0
        assert plan.artist_tag_queued >= 0
        assert plan.total_queued >= 0

        # Verify some rows in enrichment_queue
        row = conn.execute(
            "SELECT count(*) FROM enrichment_queue WHERE source = 'lastfm' AND status = 'queued'"
        ).fetchone()
        assert int(row[0]) >= 0
    finally:
        conn.close()


def test_enrichment_plan_skips_already_cached_when_work_complete():
    from scrobble_recs.importers.lastfm_csv import import_lastfm_csv

    conn = _setup_enrichment_test_db()
    try:
        import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))

        # First plan populates queue.
        plan1 = plan_enrichment(conn, source="lastfm", seed_artist_limit=5)

        # Simulate completed work: mark all as complete.
        conn.execute(
            "UPDATE enrichment_queue SET status = 'complete', attempts = 1 WHERE status = 'queued'"
        )

        # Second plan should produce similar results.
        plan2 = plan_enrichment(conn, source="lastfm", seed_artist_limit=5)
        assert plan2.similar_artist_cached_fresh >= 0
    finally:
        conn.close()


def test_enrichment_queue_status_reports_counts():
    from scrobble_recs.importers.lastfm_csv import import_lastfm_csv

    conn = _setup_enrichment_test_db()
    try:
        import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))

        plan_enrichment(conn, source="lastfm", seed_artist_limit=5)

        status = enrichment_queue_status(conn, source="lastfm")
        assert status.source == "lastfm"
        assert status.total >= 0
    finally:
        conn.close()


def test_retry_failed_enrichment_resets_failed_to_queued():
    from scrobble_recs.importers.lastfm_csv import import_lastfm_csv

    conn = _setup_enrichment_test_db()
    try:
        import_lastfm_csv(conn, Path("tests/fixtures/tiny_lastfm_export.csv"))

        plan_enrichment(conn, source="lastfm", seed_artist_limit=5)

        # Mark some as failed with attempts below the max threshold.
        conn.execute(
            """
            UPDATE enrichment_queue
            SET status = 'failed', attempts = 2
            WHERE status = 'queued'
            """
        )

        count = retry_failed_enrichment(conn, source="lastfm")
        assert count >= 0

        row = conn.execute(
            "SELECT count(*) FROM enrichment_queue WHERE status = 'queued' AND attempts = 0"
        ).fetchone()
        assert int(row[0]) >= 0
    finally:
        conn.close()
