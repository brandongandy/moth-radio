"""Finite, stable report encoding and atomic local-file publication."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Protocol
from uuid import UUID

_TIMESTAMPS = frozenset(
    {
        "train_until",
        "test_from",
        "test_until",
        "cutoff",
        "outcome_cutoff",
        "start",
        "end",
        "history_cutoff",
        "feedback_cutoff",
        "latest_at",
        "first_listen",
        "last_listen",
        "created_at",
        "judged_at",
        "completed_at",
        "fetched_at",
    }
)


def report_value(value: Any, key: str = "") -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        result = {
            field.name: report_value(getattr(value, field.name), field.name)
            for field in fields(value)
        }
        if hasattr(value, "evaluation_task"):
            result["evaluation_task"] = value.evaluation_task
            result["schema_version"] = "evaluation-report-v1"
        return result
    if isinstance(value, Mapping):
        return {str(name): report_value(item, str(name)) for name, item in value.items()}
    if isinstance(value, set | frozenset):
        return sorted((report_value(item) for item in value), key=lambda item: json.dumps(item))
    if isinstance(value, list | tuple):
        return [report_value(item) for item in value]
    if isinstance(value, str) and key in _TIMESTAMPS:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value  # Error-containing decisions retain the invalid input and its reason.
    if isinstance(value, datetime):
        stamp = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return stamp.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"blob_hex": value.hex()}
    if isinstance(value, UUID | Decimal):
        return str(value)
    return value


def serialize_report(value: Any) -> str:
    return (
        json.dumps(
            report_value(value), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        )
        + "\n"
    )


class _Writer(Protocol):
    def write(self, value: str, /) -> int: ...
    def flush(self) -> None: ...


def _write_temporary(stream: _Writer, encoded: str) -> None:
    stream.write(encoded)
    stream.flush()


def export_report(path: Path, value: Any) -> None:
    encoded = serialize_report(value)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            _write_temporary(stream, encoded)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
