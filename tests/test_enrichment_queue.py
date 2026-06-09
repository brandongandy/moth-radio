from pathlib import Path


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
