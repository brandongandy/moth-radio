from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from scrobble_recs.models import evaluate


def test_report_serialization_carries_task_unicode_and_normalized_time(tmp_path):
    from scrobble_recs.models.report_json import export_report, serialize_report

    report = evaluate.RollingEvaluationReport((), 0, 0, 0, 0, 0)
    first = serialize_report(report)
    assert first == serialize_report(report)
    payload = json.loads(first)
    assert payload["evaluation_task"] == "general"
    assert payload["schema_version"] == "evaluation-report-v1"
    data = {
        "artist": "Björk",
        "ids": {3, 1},
        "time": datetime(2024, 1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    }
    assert json.loads(serialize_report(data)) == {
        "artist": "Björk",
        "ids": [1, 3],
        "time": "2024-01-01T00:00:00Z",
    }
    dest = tmp_path / "report.json"
    dest.write_text("old")
    export_report(dest, report)
    assert dest.read_bytes() == first.encode()


@pytest.mark.parametrize("failure", ["serialize", "replace", "write"])
def test_failed_export_preserves_destination_and_removes_temporary_files(
    tmp_path, monkeypatch, failure
):
    from scrobble_recs.models import report_json

    destination = tmp_path / "report.json"
    destination.write_bytes(b"original bytes\n")
    report = {"value": float("nan") if failure == "serialize" else 1}
    if failure == "replace":

        def fail(*args):
            raise OSError("replace failed")

        monkeypatch.setattr(report_json.os, "replace", fail)
    if failure == "write":

        def fail(*args, **kwargs):
            raise OSError("write failed")

        monkeypatch.setattr(report_json, "_write_temporary", fail)
    with pytest.raises((OSError, ValueError)):
        report_json.export_report(destination, report)
    assert destination.read_bytes() == b"original bytes\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.json"]


def test_artist_report_serialization_has_explicit_task():
    from scrobble_recs.models.report_json import serialize_report

    report = evaluate.ArtistRollingEvaluationReport(
        windows=(),
        window_count=1,
        mean_recall_at_50=0,
        mean_recall_at_100=0,
        mean_ndcg_at_50=0,
        mean_identity_resolved_rate=0,
        target_count=0,
        run_count=0,
        mean_graded_adoption_ndcg_at_50=0,
    )
    assert json.loads(serialize_report(report))["evaluation_task"] == "artist"


def test_timestamp_strings_and_datetime_objects_use_one_utc_encoding():
    from scrobble_recs.models.report_json import serialize_report

    payload = json.loads(
        serialize_report(
            {
                "train_until": "2024-01-01T01:00:00+01:00",
                "test_from": "2024-01-01T00:00:00",
                "test_until": datetime(2024, 1, 1, tzinfo=UTC),
            }
        )
    )
    assert set(payload.values()) == {"2024-01-01T00:00:00Z"}


@pytest.mark.parametrize("kind", ["missing_parent", "directory"])
def test_unwritable_export_paths_leave_no_temporary_artifacts(tmp_path, kind):
    from scrobble_recs.models.report_json import export_report

    path = tmp_path / "missing" / "report.json" if kind == "missing_parent" else tmp_path
    with pytest.raises(OSError):
        export_report(path, {"value": 1})
    assert not list(tmp_path.iterdir())
