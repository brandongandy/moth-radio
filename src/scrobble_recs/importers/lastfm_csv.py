from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
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
    near_duplicate_scrobbles: int
    distinct_artists: int
    distinct_tracks: int
    null_artist_mbid_scrobbles: int
    import_warnings: int


_STAGING_COLUMNS = (
    "import_row_number",
    "raw_utc_time",
    "raw_artist",
    "raw_artist_mbid",
    "raw_album",
    "raw_album_mbid",
    "raw_track",
    "raw_track_mbid",
    "uts",
    "utc_time",
    "artist",
    "artist_mbid",
    "album",
    "album_mbid",
    "track",
    "track_mbid",
    "played_at",
    "played_date",
    "artist_key",
    "album_key",
    "track_key",
    "is_duplicate",
)
_STAGING_TABLE = "_scrobble_recs_import_stage"
_WARNING_COLUMNS = (
    "import_row_number",
    "warning_code",
    "severity",
    "message",
    "evidence",
)
_WARNING_STAGE_TABLE = "_scrobble_recs_import_warning_stage"


def import_lastfm_csv(
    conn: DuckDBPyConnection,
    csv_path: Path,
    *,
    near_duplicate_window_seconds: int = 0,
) -> ImportStats:
    with TemporaryDirectory(prefix="scrobble_recs_import_") as temp_dir:
        staging_csv = Path(temp_dir) / "validated_lastfm_import.csv"
        warning_csv = Path(temp_dir) / "validated_lastfm_import_warnings.csv"
        _write_validated_import_csv(
            csv_path,
            staging_csv,
            warning_csv,
            near_duplicate_window_seconds=near_duplicate_window_seconds,
        )
        conn.execute("BEGIN TRANSACTION")
        try:
            _create_import_stage(conn, staging_csv)
            _create_warning_stage(conn, warning_csv)
            next_artist_id = _next_id(conn, "artist", "artist_id")
            next_warning_id = _next_id(conn, "import_warning", "warning_id")
            _replace_import_tables(conn)
            _insert_raw_scrobbles(conn)
            _insert_scrobbles(conn)
            _insert_artist_mbid_conflict_warnings(conn, next_warning_id)
            _insert_track_mbid_conflict_warnings(conn, next_warning_id)
            _insert_import_warnings(conn, next_warning_id)
            _insert_provisional_artists(conn, next_artist_id)
            _update_identity_confidence(conn)
            _insert_provisional_tracks(conn)
            _rebuild_artist_listening_facts(conn)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        conn.execute(f"DROP TABLE IF EXISTS {_STAGING_TABLE}")
        conn.execute(f"DROP TABLE IF EXISTS {_WARNING_STAGE_TABLE}")
    return get_import_stats(conn)


def get_import_stats(conn: DuckDBPyConnection) -> ImportStats:
    return ImportStats(
        raw_scrobbles=_query_int(conn, "SELECT count(*) FROM raw_scrobble"),
        scrobbles=_query_int(conn, "SELECT count(*) FROM scrobble"),
        duplicate_scrobbles=_query_int(
            conn,
            "SELECT count(*) FROM scrobble WHERE is_duplicate",
        ),
        near_duplicate_scrobbles=_query_int(
            conn,
            """
            SELECT count(*)
            FROM import_warning
            WHERE warning_code = 'near_duplicate_scrobble'
            """,
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
        import_warnings=_query_int(conn, "SELECT count(*) FROM import_warning"),
    )


def _write_validated_import_csv(
    csv_path: Path,
    staging_csv: Path,
    warning_csv: Path,
    *,
    near_duplicate_window_seconds: int,
) -> None:
    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file, strict=True)
        staging_csv.parent.mkdir(parents=True, exist_ok=True)
        with (
            staging_csv.open("w", encoding="utf-8", newline="") as output_file,
            warning_csv.open("w", encoding="utf-8", newline="") as warning_file,
        ):
            writer = csv.DictWriter(output_file, fieldnames=list(_STAGING_COLUMNS))
            warning_writer = csv.DictWriter(warning_file, fieldnames=list(_WARNING_COLUMNS))
            writer.writeheader()
            warning_writer.writeheader()

            seen_scrobbles: set[tuple[int, str, str, str]] = set()
            seen_near_duplicates: dict[tuple[str, str], list[tuple[int, int]]] = {}
            _write_validated_rows(
                reader,
                writer,
                warning_writer,
                seen_scrobbles,
                seen_near_duplicates,
                near_duplicate_window_seconds=near_duplicate_window_seconds,
            )


def _write_validated_rows(
    reader: csv.DictReader[str],
    writer: csv.DictWriter[str],
    warning_writer: csv.DictWriter[str],
    seen_scrobbles: set[tuple[int, str, str, str]],
    seen_near_duplicates: dict[tuple[str, str], list[tuple[int, int]]],
    *,
    near_duplicate_window_seconds: int,
) -> None:
    try:
        if reader.fieldnames != list(EXPECTED_COLUMNS):
            raise ValueError(
                "CSV columns must exactly match "
                f"{list(EXPECTED_COLUMNS)}; got {reader.fieldnames or []}"
            )

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
            is_exact_duplicate = duplicate_key in seen_scrobbles
            seen_scrobbles.add(duplicate_key)
            is_near_duplicate = _is_near_duplicate(
                seen_near_duplicates,
                row_number=row_index - 1,
                uts=uts,
                artist_key=artist_key,
                track_key=track_key,
                near_duplicate_window_seconds=near_duplicate_window_seconds,
            )
            if is_near_duplicate and not is_exact_duplicate:
                _write_warning(
                    warning_writer,
                    import_row_number=row_index - 1,
                    warning_code="near_duplicate_scrobble",
                    message="same artist and track within configured near-duplicate window",
                    evidence={
                        "uts": uts,
                        "artist_key": artist_key,
                        "track_key": track_key,
                        "window_seconds": near_duplicate_window_seconds,
                    },
                )
            is_duplicate = is_exact_duplicate or is_near_duplicate

            writer.writerow(
                {
                    "import_row_number": row_index - 1,
                    "raw_utc_time": raw_values["utc_time"],
                    "raw_artist": raw_values["artist"],
                    "raw_artist_mbid": raw_values["artist_mbid"],
                    "raw_album": raw_values["album"],
                    "raw_album_mbid": raw_values["album_mbid"],
                    "raw_track": raw_values["track"],
                    "raw_track_mbid": raw_values["track_mbid"],
                    "uts": str(uts),
                    "utc_time": cleaned["utc_time"],
                    "artist": cleaned["artist"],
                    "artist_mbid": cleaned["artist_mbid"],
                    "album": cleaned["album"],
                    "album_mbid": cleaned["album_mbid"],
                    "track": cleaned["track"],
                    "track_mbid": cleaned["track_mbid"],
                    "played_at": played_at.isoformat(sep=" "),
                    "played_date": played_at.date().isoformat(),
                    "artist_key": artist_key,
                    "album_key": album_key,
                    "track_key": track_key,
                    "is_duplicate": str(is_duplicate).lower(),
                }
            )
    except csv.Error as exc:
        line_number = max(reader.line_num + 1, 1)
        raise ValueError(f"Malformed CSV at line {line_number}: {exc}") from exc


def _is_near_duplicate(
    seen_near_duplicates: dict[tuple[str, str], list[tuple[int, int]]],
    *,
    row_number: int,
    uts: int,
    artist_key: str,
    track_key: str,
    near_duplicate_window_seconds: int,
) -> bool:
    if near_duplicate_window_seconds <= 0:
        return False
    if artist_key == "" or track_key == "":
        return False

    key = (artist_key, track_key)
    previous_scrobbles = seen_near_duplicates.setdefault(key, [])
    is_duplicate = any(
        abs(uts - previous_uts) <= near_duplicate_window_seconds
        for previous_uts, _previous_row_number in previous_scrobbles
    )
    previous_scrobbles.append((uts, row_number))
    return is_duplicate


def _write_warning(
    warning_writer: csv.DictWriter[str],
    *,
    import_row_number: int,
    warning_code: str,
    message: str,
    evidence: dict[str, object],
) -> None:
    warning_writer.writerow(
        {
            "import_row_number": import_row_number,
            "warning_code": warning_code,
            "severity": "warning",
            "message": message,
            "evidence": json.dumps(evidence, separators=(",", ":")),
        }
    )


def _create_import_stage(conn: DuckDBPyConnection, staging_csv: Path) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {_STAGING_TABLE}")
    conn.execute(
        f"""
        CREATE TEMP TABLE {_STAGING_TABLE} AS
        SELECT
            CAST(import_row_number AS UBIGINT) AS import_row_number,
            raw_utc_time,
            raw_artist,
            raw_artist_mbid,
            raw_album,
            raw_album_mbid,
            raw_track,
            raw_track_mbid,
            CAST(uts AS BIGINT) AS uts,
            utc_time,
            artist,
            artist_mbid,
            album,
            album_mbid,
            track,
            track_mbid,
            CAST(played_at AS TIMESTAMP) AS played_at,
            CAST(played_date AS DATE) AS played_date,
            artist_key,
            album_key,
            track_key,
            CAST(is_duplicate AS BOOLEAN) AS is_duplicate
        FROM read_csv(?, header=true, all_varchar=true, nullstr='\\0')
        """,
        [staging_csv.as_posix()],
    )


def _create_warning_stage(conn: DuckDBPyConnection, warning_csv: Path) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {_WARNING_STAGE_TABLE}")
    conn.execute(
        f"""
        CREATE TEMP TABLE {_WARNING_STAGE_TABLE} AS
        SELECT
            CAST(import_row_number AS UBIGINT) AS import_row_number,
            warning_code,
            severity,
            message,
            evidence
        FROM read_csv(?, header=true, all_varchar=true, nullstr='\\0')
        """,
        [warning_csv.as_posix()],
    )


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


def _insert_raw_scrobbles(conn: DuckDBPyConnection) -> None:
    next_id = _next_id(conn, "raw_scrobble", "raw_scrobble_id")
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
        SELECT
            ? + import_row_number - 1 AS raw_scrobble_id,
            uts,
            raw_utc_time,
            raw_artist,
            raw_artist_mbid,
            raw_album,
            raw_album_mbid,
            raw_track,
            raw_track_mbid
        FROM _scrobble_recs_import_stage
        """,
        [next_id],
    )


def _insert_scrobbles(conn: DuckDBPyConnection) -> None:
    next_id = _next_id(conn, "scrobble", "scrobble_id")
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
        SELECT
            ? + import_row_number - 1 AS scrobble_id,
            played_at,
            played_date,
            artist,
            album,
            track,
            artist_mbid,
            album_mbid,
            track_mbid,
            artist_key,
            album_key,
            track_key,
            is_duplicate
        FROM _scrobble_recs_import_stage
        """,
        [next_id],
    )


def _replace_import_tables(conn: DuckDBPyConnection) -> None:
    conn.execute(
        """
        DELETE FROM enrichment_work;
        DELETE FROM artist_similarity_edge;
        DELETE FROM artist_tag;
        DELETE FROM tag_top_artist;
        DELETE FROM artist_listening_fact;
        DELETE FROM track;
        DELETE FROM artist;
        DELETE FROM scrobble;
        DELETE FROM raw_scrobble;
        DELETE FROM import_warning;
        """
    )


def _insert_import_warnings(conn: DuckDBPyConnection, first_warning_id: int) -> None:
    conn.execute(
        """
        INSERT INTO import_warning (
            warning_id,
            import_row_number,
            warning_code,
            severity,
            message,
            evidence
        )
        WITH next_id AS (
            SELECT greatest(coalesce(max(warning_id), 0), ?) AS base_id
            FROM import_warning
        )
        SELECT
            next_id.base_id
                + row_number() OVER (ORDER BY import_row_number, warning_code, message)
                AS warning_id,
            import_row_number,
            warning_code,
            severity,
            message,
            CAST(evidence AS JSON) AS evidence
        FROM _scrobble_recs_import_warning_stage, next_id
        ORDER BY import_row_number, warning_code, message
        """,
        [first_warning_id - 1],
    )


def _insert_artist_mbid_conflict_warnings(
    conn: DuckDBPyConnection,
    first_warning_id: int,
) -> None:
    conn.execute(
        """
        INSERT INTO import_warning (
            warning_id,
            import_row_number,
            warning_code,
            severity,
            message,
            evidence
        )
        WITH next_id AS (
            SELECT greatest(coalesce(max(warning_id), 0), ?) AS base_id
            FROM import_warning
        ),
        conflicts AS (
            SELECT
                artist_key,
                min(scrobble_id) AS first_scrobble_id,
                list_sort(list_distinct(list(NULLIF(trim(artist_mbid_raw), '')))) AS mbids
            FROM scrobble
            WHERE artist_key <> ''
              AND NOT is_duplicate
              AND NULLIF(trim(artist_mbid_raw), '') IS NOT NULL
            GROUP BY artist_key
            HAVING count(DISTINCT NULLIF(trim(artist_mbid_raw), '')) > 1
        )
        SELECT
            next_id.base_id + row_number() OVER (ORDER BY artist_key) AS warning_id,
            first_scrobble_id AS import_row_number,
            'artist_mbid_conflict' AS warning_code,
            'warning' AS severity,
            'artist_key has multiple supplied artist MBIDs' AS message,
            json_object('artist_key', artist_key, 'artist_mbids', mbids) AS evidence
        FROM conflicts, next_id
        """,
        [first_warning_id - 1],
    )


def _insert_track_mbid_conflict_warnings(
    conn: DuckDBPyConnection,
    first_warning_id: int,
) -> None:
    conn.execute(
        """
        INSERT INTO import_warning (
            warning_id,
            import_row_number,
            warning_code,
            severity,
            message,
            evidence
        )
        WITH next_id AS (
            SELECT greatest(coalesce(max(warning_id), 0), ?) AS base_id
            FROM import_warning
        ),
        conflicts AS (
            SELECT
                artist_key,
                track_key,
                min(scrobble_id) AS first_scrobble_id,
                list_sort(list_distinct(list(NULLIF(trim(track_mbid_raw), '')))) AS mbids
            FROM scrobble
            WHERE artist_key <> ''
              AND track_key <> ''
              AND NOT is_duplicate
              AND NULLIF(trim(track_mbid_raw), '') IS NOT NULL
            GROUP BY artist_key, track_key
            HAVING count(DISTINCT NULLIF(trim(track_mbid_raw), '')) > 1
        )
        SELECT
            next_id.base_id + row_number() OVER (ORDER BY artist_key, track_key) AS warning_id,
            first_scrobble_id AS import_row_number,
            'track_mbid_conflict' AS warning_code,
            'warning' AS severity,
            'artist_key and track_key have multiple supplied track MBIDs' AS message,
            json_object(
                'artist_key', artist_key,
                'track_key', track_key,
                'track_mbids', mbids
            ) AS evidence
        FROM conflicts, next_id
        """,
        [first_warning_id - 1],
    )


def _insert_provisional_artists(conn: DuckDBPyConnection, first_artist_id: int) -> None:
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
        WITH name_counts AS (
            SELECT
                artist_key,
                artist_raw,
                count(*) AS plays,
                min(scrobble_id) AS first_scrobble_id
            FROM scrobble
            WHERE artist_key <> ''
              AND NOT is_duplicate
            GROUP BY artist_key, artist_raw
        ),
        preferred_names AS (
            SELECT artist_key, artist_raw AS canonical_name
            FROM (
                SELECT
                    artist_key,
                    artist_raw,
                    row_number() OVER (
                        PARTITION BY artist_key
                        ORDER BY plays DESC, first_scrobble_id
                    ) AS row_number
                FROM name_counts
            ) ranked_names
            WHERE row_number = 1
        ),
        mbids AS (
            SELECT
                artist_key,
                count(DISTINCT NULLIF(trim(artist_mbid_raw), '')) AS mbid_count,
                min(NULLIF(trim(artist_mbid_raw), '')) AS artist_mbid
            FROM scrobble
            WHERE artist_key <> ''
              AND NOT is_duplicate
            GROUP BY artist_key
        )
        SELECT
            ? + row_number() OVER (ORDER BY names.artist_key) - 1 AS artist_id,
            names.canonical_name,
            names.artist_key,
            CASE WHEN mbids.mbid_count = 1 THEN mbids.artist_mbid ELSE NULL END,
            names.canonical_name AS lastfm_name,
            CASE
                WHEN mbids.mbid_count = 1 THEN 'provisional_mbid'
                WHEN mbids.mbid_count > 1 THEN 'provisional_mbid_conflict'
                ELSE 'provisional_name'
            END AS resolution_status,
            CASE WHEN mbids.mbid_count = 1 THEN 0.5 ELSE 0.0 END AS resolution_confidence
        FROM preferred_names names
        JOIN mbids ON mbids.artist_key = names.artist_key
        WHERE NOT EXISTS (
            SELECT 1 FROM artist existing WHERE existing.artist_key = names.artist_key
        )
        """,
        [first_artist_id],
    )


def _insert_provisional_tracks(conn: DuckDBPyConnection) -> None:
    conn.execute(
        """
        INSERT INTO track (
            track_id,
            artist_id,
            canonical_title,
            track_key,
            musicbrainz_recording_mbid,
            lastfm_track_name,
            resolution_status,
            resolution_confidence
        )
        WITH next_id AS (
            SELECT coalesce(max(track_id), 0) AS base_id FROM track
        ),
        title_counts AS (
            SELECT
                s.artist_key,
                s.track_key,
                s.track_raw,
                count(*) AS plays,
                min(s.scrobble_id) AS first_scrobble_id
            FROM scrobble s
            WHERE s.artist_key <> ''
              AND s.track_key <> ''
              AND NOT s.is_duplicate
            GROUP BY s.artist_key, s.track_key, s.track_raw
        ),
        preferred_titles AS (
            SELECT artist_key, track_key, track_raw AS canonical_title
            FROM (
                SELECT
                    artist_key,
                    track_key,
                    track_raw,
                    row_number() OVER (
                        PARTITION BY artist_key, track_key
                        ORDER BY plays DESC, first_scrobble_id
                    ) AS row_number
                FROM title_counts
            ) ranked_titles
            WHERE row_number = 1
        ),
        mbids AS (
            SELECT
                artist_key,
                track_key,
                count(DISTINCT NULLIF(trim(track_mbid_raw), '')) AS mbid_count,
                min(NULLIF(trim(track_mbid_raw), '')) AS track_mbid
            FROM scrobble
            WHERE artist_key <> ''
              AND track_key <> ''
              AND NOT is_duplicate
            GROUP BY artist_key, track_key
        )
        SELECT
            next_id.base_id + row_number() OVER (ORDER BY a.artist_id, titles.track_key)
                AS track_id,
            a.artist_id,
            titles.canonical_title,
            titles.track_key,
            CASE WHEN mbids.mbid_count = 1 THEN mbids.track_mbid ELSE NULL END,
            titles.canonical_title AS lastfm_track_name,
            CASE
                WHEN mbids.mbid_count = 1 THEN 'provisional_mbid'
                WHEN mbids.mbid_count > 1 THEN 'provisional_mbid_conflict'
                ELSE 'provisional_name'
            END AS resolution_status,
            CASE WHEN mbids.mbid_count = 1 THEN 0.5 ELSE 0.0 END AS resolution_confidence
        FROM preferred_titles titles
        JOIN artist a ON a.artist_key = titles.artist_key
        JOIN mbids
          ON mbids.artist_key = titles.artist_key
         AND mbids.track_key = titles.track_key
        CROSS JOIN next_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM track existing
            WHERE existing.artist_id = a.artist_id
              AND existing.track_key = titles.track_key
        )
        """
    )


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


def _update_identity_confidence(conn: DuckDBPyConnection) -> None:
    rows = conn.execute(
        """
        SELECT artist_id, canonical_name, musicbrainz_artist_mbid, resolution_status
        FROM artist
        """
    ).fetchall()
    for artist_id, canonical_name, mbid, resolution_status in rows:
        name = str(canonical_name)
        mbid_val = str(mbid) if mbid else None
        status = str(resolution_status) if resolution_status else "provisional_name"

        confidence = 0.50
        warnings: list[str] = []

        if mbid_val and status == "provisional_mbid":
            confidence += 0.30
        confidence += 0.10

        name_parts = name.strip().split()
        if len(name_parts) <= 1 and not mbid_val:
            confidence -= 0.30
            warnings.append("generic_short_name_no_mbid")
        if status == "provisional_mbid_conflict":
            confidence -= 0.20
            warnings.append("multiple_possible_local_matches")
        if not mbid_val:
            warnings.append("missing_musicbrainz_mbid")

        identity_confidence = max(0.0, min(1.0, confidence))
        conn.execute(
            "UPDATE artist SET identity_confidence = ?, identity_warnings = ? WHERE artist_id = ?",
            [identity_confidence, json.dumps(warnings), int(artist_id)],
        )
