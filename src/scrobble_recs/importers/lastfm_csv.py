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
            _replace_import_tables(conn)
            _insert_raw_scrobbles(conn)
            _insert_scrobbles(conn)
            _insert_import_warnings(conn)
            _insert_provisional_artists(conn)
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
            _warn_on_utc_time_mismatch(
                warning_writer,
                import_row_number=row_index - 1,
                uts=uts,
                utc_time=cleaned["utc_time"],
                played_at=played_at,
            )
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


def _warn_on_utc_time_mismatch(
    warning_writer: csv.DictWriter[str],
    *,
    import_row_number: int,
    uts: int,
    utc_time: str,
    played_at: datetime,
) -> None:
    if utc_time == "":
        return

    try:
        parsed_utc_time = datetime.strptime(utc_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        _write_warning(
            warning_writer,
            import_row_number=import_row_number,
            warning_code="invalid_utc_time",
            message="utc_time is not formatted as YYYY-MM-DD HH:MM:SS",
            evidence={"uts": uts, "utc_time": utc_time},
        )
        return

    if parsed_utc_time != played_at:
        _write_warning(
            warning_writer,
            import_row_number=import_row_number,
            warning_code="timestamp_mismatch",
            message="utc_time does not match uts",
            evidence={
                "uts": uts,
                "utc_time": utc_time,
                "played_at": played_at.isoformat(sep=" "),
            },
        )


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


def _insert_import_warnings(conn: DuckDBPyConnection) -> None:
    next_id = _next_id(conn, "import_warning", "warning_id")
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
        SELECT
            ? + row_number() OVER (ORDER BY import_row_number, warning_code, message) - 1
                AS warning_id,
            import_row_number,
            warning_code,
            severity,
            message,
            CAST(evidence AS JSON) AS evidence
        FROM _scrobble_recs_import_warning_stage
        ORDER BY import_row_number, warning_code, message
        """,
        [next_id],
    )


def _insert_provisional_artists(conn: DuckDBPyConnection) -> None:
    next_id = _next_id(conn, "artist", "artist_id")
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
        SELECT
            ? + row_number() OVER (ORDER BY artist_key) - 1 AS artist_id,
            canonical_name,
            artist_key,
            artist_mbid,
            canonical_name AS lastfm_name,
            'provisional' AS resolution_status,
            0.0 AS resolution_confidence
        FROM (
            SELECT
                artist_key,
                arg_min(artist_raw, scrobble_id) AS canonical_name,
                arg_min(NULLIF(trim(artist_mbid_raw), ''), scrobble_id) AS artist_mbid
            FROM scrobble
            WHERE artist_key <> ''
            GROUP BY artist_key
        ) candidates
        WHERE NOT EXISTS (
            SELECT 1 FROM artist existing WHERE existing.artist_key = candidates.artist_key
        )
        """,
        [next_id],
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
