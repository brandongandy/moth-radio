from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from duckdb import DuckDBPyConnection

from scrobble_recs.models.knownness import (
    ArtistListeningInput,
    compute_artist_listening_scores,
)


@dataclass(frozen=True)
class ListeningRefreshStats:
    artists_updated: int


def refresh_artist_listening_scores(
    conn: DuckDBPyConnection,
    *,
    as_of: datetime,
) -> ListeningRefreshStats:
    rows = conn.execute(
        """
        SELECT
            artist_id,
            play_count,
            track_count,
            active_months,
            first_played_at,
            last_played_at
        FROM artist_listening_fact
        WHERE play_count IS NOT NULL
          AND track_count IS NOT NULL
          AND active_months IS NOT NULL
          AND first_played_at IS NOT NULL
          AND last_played_at IS NOT NULL
        """
    ).fetchall()

    updated = 0
    for row in rows:
        scores = compute_artist_listening_scores(
            ArtistListeningInput(
                play_count=int(row[1]),
                track_count=int(row[2]),
                active_months=int(row[3]),
                first_played_at=row[4],
                last_played_at=row[5],
            ),
            as_of=as_of,
        )
        conn.execute(
            """
            UPDATE artist_listening_fact
            SET lifetime_weight = ?,
                recent_weight = ?,
                knownness = ?,
                rediscovery_score = ?,
                updated_at = current_timestamp
            WHERE artist_id = ?
            """,
            [
                scores.lifetime_weight,
                scores.recent_weight,
                scores.knownness,
                scores.rediscovery_score,
                int(row[0]),
            ],
        )
        updated += 1
    return ListeningRefreshStats(artists_updated=updated)
