from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from duckdb import DuckDBPyConnection

from scrobble_recs.config import EXPECTED_CSV_COLUMNS
from scrobble_recs.normalize.strings import normalize_key
from scrobble_recs.normalize.timestamps import parse_uts

EXPECTED_COLUMNS = EXPECTED_CSV_COLUMNS
_REQUIRED_COLUMNS = ("uts", "artist", "track")


@dataclass(frozen=True)
class ImportStats:
    raw_scrobbles: int
    scrobbles: int
    duplicate_scrobbles: int
    distinct_artists: int
    distinct_tracks: int
    null_artist_mbid_scrobbles: int


@dataclass(frozen=True)
class _ValidatedScrobble:
    raw_utc_time: str
    raw_artist: str
    raw_artist_mbid: str
    raw_album: str
    raw_album_mbid: str
    raw_track: str
    raw_track_mbid: str
    uts: int
    utc_time: str
    artist: str
    artist_mbid: str
    album: str
    album_mbid: str
    track: str
    track_mbid: str
    played_at: datetime
    artist_key: str
    album_key: str
    track_key: str
    is_duplicate: bool


def import_lastfm_csv(conn: DuckDBPyConnection, csv_path: Path) -> ImportStats:
    rows = _read_and_validate_csv(csv_path)
    conn.execute("BEGIN TRANSACTION")
    try:
        _replace_import_tables(conn)
        _insert_raw_scrobbles(conn, rows)
        _insert_scrobbles(conn, rows)
        _insert_provisional_artists(conn)
        _rebuild_artist_listening_facts(conn)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return get_import_stats(conn)


def get_import_stats(conn: DuckDBPyConnection) -> ImportStats:
    return ImportStats(
        raw_scrobbles=_query_int(conn, "SELECT count(*) FROM raw_scrobble"),
        scrobbles=_query_int(conn, "SELECT count(*) FROM scrobble"),
        duplicate_scrobbles=_query_int(
            conn,
            "SELECT count(*) FROM scrobble WHERE is_duplicate",
        ),
        distinct_artists=_query_int(
            conn,
            "SELECT count(DISTINCT artist_key) FROM scrobble WHERE NOT is_duplicate",
        ),
        distinct_tracks=_query_int(
            conn,
            "SELECT count(DISTINCT track_key) FROM scrobble WHERE NOT is_duplicate",
        ),
        null_artist_mbid_scrobbles=_query_int(
            conn,
            """
            SELECT count(*)
            FROM scrobble
            WHERE artist_mbid_raw IS NULL OR trim(artist_mbid_raw) = ''
            """,
        ),
    )


def _read_and_validate_csv(csv_path: Path) -> list[_ValidatedScrobble]:
    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file, strict=True)
        try:
            if reader.fieldnames != list(EXPECTED_COLUMNS):
                raise ValueError(
                    "CSV columns must exactly match "
                    f"{list(EXPECTED_COLUMNS)}; got {reader.fieldnames or []}"
                )

            rows: list[_ValidatedScrobble] = []
            seen_scrobbles: set[tuple[int, str, str, str]] = set()
            for row_index, row in enumerate(reader, start=2):
                if None in row:
                    raise ValueError(f"CSV row {row_index} has the wrong number of columns")

                raw_values: dict[str, str] = {}
                for column in EXPECTED_COLUMNS:
                    raw_value = row[column]
                    if raw_value is None:
                        raise ValueError(f"CSV row {row_index} has the wrong number of columns")
                    raw_values[column] = raw_value

                cleaned_values = [_clean_field(raw_values[column]) for column in EXPECTED_COLUMNS]
                if cleaned_values == list(EXPECTED_COLUMNS):
                    raise ValueError(f"CSV contains a duplicate header row at line {row_index}")

                cleaned = dict(zip(EXPECTED_COLUMNS, cleaned_values, strict=True))
                _validate_required_fields(cleaned, row_index)

                played_at = _parse_uts(cleaned["uts"], row_index)
                uts = int(cleaned["uts"])
                artist_key = normalize_key(cleaned["artist"])
                album_key = normalize_key(cleaned["album"])
                track_key = normalize_key(cleaned["track"])
                duplicate_key = (uts, artist_key, track_key, album_key)
                is_duplicate = duplicate_key in seen_scrobbles
                seen_scrobbles.add(duplicate_key)

                rows.append(
                    _ValidatedScrobble(
                        raw_utc_time=raw_values["utc_time"],
                        raw_artist=raw_values["artist"],
                        raw_artist_mbid=raw_values["artist_mbid"],
                        raw_album=raw_values["album"],
                        raw_album_mbid=raw_values["album_mbid"],
                        raw_track=raw_values["track"],
                        raw_track_mbid=raw_values["track_mbid"],
                        uts=uts,
                        utc_time=cleaned["utc_time"],
                        artist=cleaned["artist"],
                        artist_mbid=cleaned["artist_mbid"],
                        album=cleaned["album"],
                        album_mbid=cleaned["album_mbid"],
                        track=cleaned["track"],
                        track_mbid=cleaned["track_mbid"],
                        played_at=played_at,
                        artist_key=artist_key,
                        album_key=album_key,
                        track_key=track_key,
                        is_duplicate=is_duplicate,
                    )
                )
        except csv.Error as exc:
            line_number = max(reader.line_num + 1, 1)
            raise ValueError(f"Malformed CSV at line {line_number}: {exc}") from exc

    return rows


def _clean_field(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()


def _validate_required_fields(row: dict[str, str], row_index: int) -> None:
    for column in _REQUIRED_COLUMNS:
        if row[column] == "":
            raise ValueError(f"CSV row {row_index} is missing required field '{column}'")


def _parse_uts(value: str, row_index: int) -> datetime:
    try:
        return parse_uts(value)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError(f"CSV row {row_index} has invalid uts: {exc}") from exc


def _insert_raw_scrobbles(conn: DuckDBPyConnection, rows: list[_ValidatedScrobble]) -> None:
    next_id = _next_id(conn, "raw_scrobble", "raw_scrobble_id")
    for offset, row in enumerate(rows):
        conn.execute(
            """
            INSERT INTO raw_scrobble (
                raw_scrobble_id,
                uts,
                utc_time,
                artist,
                artist_mbid,
                album,
                album_mbid,
                track,
                track_mbid
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                next_id + offset,
                row.uts,
                row.raw_utc_time,
                row.raw_artist,
                row.raw_artist_mbid,
                row.raw_album,
                row.raw_album_mbid,
                row.raw_track,
                row.raw_track_mbid,
            ],
        )


def _insert_scrobbles(conn: DuckDBPyConnection, rows: list[_ValidatedScrobble]) -> None:
    next_id = _next_id(conn, "scrobble", "scrobble_id")
    for offset, row in enumerate(rows):
        conn.execute(
            """
            INSERT INTO scrobble (
                scrobble_id,
                played_at,
                played_date,
                artist_raw,
                album_raw,
                track_raw,
                artist_mbid_raw,
                album_mbid_raw,
                track_mbid_raw,
                artist_key,
                album_key,
                track_key,
                is_duplicate
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                next_id + offset,
                row.played_at,
                row.played_at.date(),
                row.artist,
                row.album,
                row.track,
                row.artist_mbid,
                row.album_mbid,
                row.track_mbid,
                row.artist_key,
                row.album_key,
                row.track_key,
                row.is_duplicate,
            ],
        )


def _replace_import_tables(conn: DuckDBPyConnection) -> None:
    conn.execute("DELETE FROM enrichment_work")
    conn.execute("DELETE FROM artist_similarity_edge")
    conn.execute("DELETE FROM artist_tag")
    conn.execute("DELETE FROM tag_top_artist")
    conn.execute("DELETE FROM artist_listening_fact")
    conn.execute("DELETE FROM artist")
    conn.execute("DELETE FROM scrobble")
    conn.execute("DELETE FROM raw_scrobble")


def _insert_provisional_artists(conn: DuckDBPyConnection) -> None:
    existing_keys = {
        row[0]
        for row in conn.execute(
            "SELECT artist_key FROM artist WHERE artist_key IS NOT NULL"
        ).fetchall()
    }
    candidates = conn.execute(
        """
        SELECT
            artist_key,
            arg_min(artist_raw, scrobble_id) AS canonical_name,
            arg_min(NULLIF(trim(artist_mbid_raw), ''), scrobble_id) AS artist_mbid
        FROM scrobble
        WHERE artist_key <> ''
        GROUP BY artist_key
        ORDER BY artist_key
        """
    ).fetchall()

    next_id = _next_id(conn, "artist", "artist_id")
    inserted = 0
    for artist_key, canonical_name, artist_mbid in candidates:
        if artist_key in existing_keys:
            continue

        conn.execute(
            """
            INSERT INTO artist (
                artist_id,
                canonical_name,
                artist_key,
                musicbrainz_artist_mbid,
                lastfm_name,
                resolution_status,
                resolution_confidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                next_id + inserted,
                canonical_name,
                artist_key,
                artist_mbid,
                canonical_name,
                "provisional",
                0.0,
            ],
        )
        inserted += 1


def _rebuild_artist_listening_facts(conn: DuckDBPyConnection) -> None:
    conn.execute("DELETE FROM artist_listening_fact")
    conn.execute(
        """
        INSERT INTO artist_listening_fact (
            artist_id,
            play_count,
            track_count,
            album_count,
            first_played_at,
            last_played_at,
            active_days,
            active_months,
            session_count,
            recent_90d_plays,
            recent_365d_plays,
            lifetime_weight,
            recent_weight,
            knownness,
            rediscovery_score
        )
        SELECT
            a.artist_id,
            count(*) AS play_count,
            count(DISTINCT s.track_key) AS track_count,
            count(DISTINCT s.album_key) AS album_count,
            min(s.played_at) AS first_played_at,
            max(s.played_at) AS last_played_at,
            count(DISTINCT s.played_date) AS active_days,
            count(DISTINCT date_trunc('month', s.played_at)) AS active_months,
            NULL AS session_count,
            count(*) FILTER (WHERE s.played_at >= current_timestamp - INTERVAL 90 DAY)
                AS recent_90d_plays,
            count(*) FILTER (WHERE s.played_at >= current_timestamp - INTERVAL 365 DAY)
                AS recent_365d_plays,
            NULL AS lifetime_weight,
            NULL AS recent_weight,
            NULL AS knownness,
            NULL AS rediscovery_score
        FROM scrobble s
        JOIN artist a ON a.artist_key = s.artist_key
        WHERE NOT s.is_duplicate
        GROUP BY a.artist_id
        """
    )


def _next_id(conn: DuckDBPyConnection, table: str, column: str) -> int:
    return _query_int(conn, f"SELECT coalesce(max({column}), 0) + 1 FROM {table}")


def _query_int(conn: DuckDBPyConnection, sql: str) -> int:
    result = conn.execute(sql).fetchone()
    if result is None:
        return 0
    value: Any = result[0]
    return int(value or 0)
