from __future__ import annotations

from dataclasses import dataclass

from duckdb import DuckDBPyConnection


@dataclass(frozen=True)
class SeedArtist:
    artist_id: int
    name: str
    artist_key: str
    lastfm_name: str
    play_count: int
    seed_weight: float


def select_seed_artists(
    conn: DuckDBPyConnection,
    *,
    limit: int,
    min_scrobbles: int,
) -> list[SeedArtist]:
    rows = conn.execute(
        """
        SELECT
            a.artist_id,
            a.canonical_name,
            a.artist_key,
            coalesce(nullif(a.lastfm_name, ''), a.canonical_name) AS lastfm_name,
            f.play_count,
            coalesce(f.recent_weight, f.lifetime_weight, ln(1 + f.play_count)) AS seed_weight
        FROM artist a
        JOIN artist_listening_fact f ON f.artist_id = a.artist_id
        WHERE f.play_count >= ?
        ORDER BY seed_weight DESC, a.canonical_name ASC
        LIMIT ?
        """,
        [min_scrobbles, limit],
    ).fetchall()
    return [
        SeedArtist(
            artist_id=int(row[0]),
            name=str(row[1]),
            artist_key=str(row[2]),
            lastfm_name=str(row[3]),
            play_count=int(row[4]),
            seed_weight=float(row[5]),
        )
        for row in rows
    ]
