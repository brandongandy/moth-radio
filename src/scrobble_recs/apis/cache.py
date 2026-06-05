from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from duckdb import DuckDBPyConnection

JsonObject = dict[str, Any]


def canonical_json_params(params: JsonObject) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_cache_key(source: str, endpoint: str, params: JsonObject) -> str:
    digest = hashlib.sha256(canonical_json_params(params).encode("utf-8")).hexdigest()
    return f"{source}:{endpoint}:{digest}"


@dataclass(frozen=True)
class ApiCacheEntry:
    cache_key: str
    source: str
    endpoint: str
    request_url: str
    request_params: JsonObject
    response_status: int
    response_json: JsonObject
    fetched_at: datetime
    expires_at: datetime
    error_message: str | None


@dataclass(frozen=True)
class ApiCacheSummary:
    total_entries: int
    fresh_successes: int
    stale_successes: int
    error_entries: int
    sources: dict[str, int]


class ApiCache:
    def __init__(self, conn: DuckDBPyConnection) -> None:
        self._conn = conn

    def get_fresh(
        self,
        source: str,
        endpoint: str,
        request_params: JsonObject,
        now: datetime,
    ) -> JsonObject | None:
        cache_key = build_cache_key(source, endpoint, request_params)
        row = self._conn.execute(
            """
            SELECT response_json
            FROM api_cache
            WHERE cache_key = ?
              AND response_status BETWEEN 200 AND 299
              AND expires_at > ?
            """,
            [cache_key, now],
        ).fetchone()
        if row is None:
            return None
        return cast(JsonObject, json.loads(str(row[0])))

    def get_stale_success(
        self,
        source: str,
        endpoint: str,
        request_params: JsonObject,
    ) -> JsonObject | None:
        cache_key = build_cache_key(source, endpoint, request_params)
        row = self._conn.execute(
            """
            SELECT response_json
            FROM api_cache
            WHERE cache_key = ?
              AND response_status BETWEEN 200 AND 299
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            [cache_key],
        ).fetchone()
        if row is None:
            return None
        return cast(JsonObject, json.loads(str(row[0])))

    def summary(self, now: datetime) -> ApiCacheSummary:
        source_rows = self._conn.execute(
            """
            SELECT source, count(*)
            FROM api_cache
            GROUP BY source
            ORDER BY source
            """
        ).fetchall()
        sources = {str(source): int(count) for source, count in source_rows}

        return ApiCacheSummary(
            total_entries=_query_int(
                self._conn,
                "SELECT count(*) FROM api_cache",
                [],
            ),
            fresh_successes=_query_int(
                self._conn,
                """
                SELECT count(*)
                FROM api_cache
                WHERE response_status BETWEEN 200 AND 299
                  AND expires_at > ?
                """,
                [now],
            ),
            stale_successes=_query_int(
                self._conn,
                """
                SELECT count(*)
                FROM api_cache
                WHERE response_status BETWEEN 200 AND 299
                  AND expires_at <= ?
                """,
                [now],
            ),
            error_entries=_query_int(
                self._conn,
                """
                SELECT count(*)
                FROM api_cache
                WHERE response_status < 200
                   OR response_status > 299
                """,
                [],
            ),
            sources=sources,
        )

    def write_response(
        self,
        *,
        source: str,
        endpoint: str,
        request_url: str,
        request_params: JsonObject,
        response_status: int,
        response_json: JsonObject,
        fetched_at: datetime,
        expires_at: datetime,
        error_message: str | None,
    ) -> None:
        cache_key = build_cache_key(source, endpoint, request_params)
        request_params_json = canonical_json_params(request_params)
        response_json_text = json.dumps(response_json, sort_keys=True)

        transaction_started = False
        try:
            self._conn.execute("BEGIN TRANSACTION")
            transaction_started = True
            self._conn.execute(
                "DELETE FROM api_cache WHERE cache_key = ?",
                [cache_key],
            )
            self._conn.execute(
                """
                INSERT INTO api_cache (
                    cache_key,
                    source,
                    endpoint,
                    request_url,
                    request_params,
                    response_status,
                    response_json,
                    fetched_at,
                    expires_at,
                    error_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    cache_key,
                    source,
                    endpoint,
                    request_url,
                    request_params_json,
                    response_status,
                    response_json_text,
                    fetched_at,
                    expires_at,
                    error_message,
                ],
            )
            self._conn.execute("COMMIT")
            transaction_started = False
        except Exception:
            if transaction_started:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
            raise


def _query_int(conn: DuckDBPyConnection, sql: str, params: list[object]) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    return int(row[0])
