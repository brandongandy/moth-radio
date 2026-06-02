from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import duckdb
from duckdb import DuckDBPyConnection

from scrobble_recs.config import ScrobbleRecsConfig


def connect_database(db_path: Path) -> DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def apply_migrations(conn: DuckDBPyConnection) -> None:
    sql_dir = files("scrobble_recs").joinpath("sql")
    migrations = sorted(
        (path for path in sql_dir.iterdir() if path.name.endswith(".sql")),
        key=lambda path: path.name,
    )
    for migration in migrations:
        conn.execute(migration.read_text(encoding="utf-8"))


def initialize_database(config: ScrobbleRecsConfig) -> DuckDBPyConnection:
    conn = connect_database(config.app.db_path)
    apply_migrations(conn)
    return conn
