"""Same-state local rolling replay with explicit ablation and cost manifests."""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, Literal

from duckdb import DuckDBPyConnection, connect

from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.compare import _comparison_state_id
from scrobble_recs.models.evaluate import EvaluationWindow, evaluate_recommendations
from scrobble_recs.models.profile_window import normalize_timestamp, resolve_listening_window
from scrobble_recs.models.recommendation_query import capture_evidence_snapshot
from scrobble_recs.models.report_json import report_value

ABLATIONS: dict[str, tuple[str, ...]] = {
    "provider:lastfm": ("lastfm_similarity", "lastfm_walk", "semantic"),
    "provider:listenbrainz": ("listenbrainz",),
    "provider:musicbrainz": ("musicbrainz",),
    **{
        f"signal:{name}": (name,)
        for name in (
            "lastfm_similarity",
            "lastfm_walk",
            "semantic",
            "listenbrainz",
            "musicbrainz",
            "local_feedback",
        )
    },
}
RUNTIME_BOUNDARY = "isolated-import+generation+metrics+logical-export+cleanup-v1"


@dataclass(frozen=True)
class RollingComparisonReport:
    evaluation_task: str
    stable_state_id: str
    pairs: tuple[dict[str, Any], ...]
    baseline_storage: dict[str, Any]
    shared_capture_seconds: float
    target_artist_ids: tuple[int, ...]
    qualification: str = (
        "captured present graph; retrospective screening, not leakage-free causal evidence"
    )
    promotion_status: str = "not_promoted"


def logical_storage(conn: DuckDBPyConnection, *, export_path: Path | None = None) -> dict[str, Any]:
    """Canonical UTF-8 finite JSON for every persisted table, column and sorted row."""
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' "
            "AND table_type = 'BASE TABLE' ORDER BY table_name"
        ).fetchall()
    ]
    payloads = []
    for table in tables:
        quoted = table.replace('"', '""')
        cursor = conn.execute(f'SELECT * FROM "{quoted}" ORDER BY ALL')
        payload = {
            "table": table,
            "columns": [item[0] for item in cursor.description],
            "rows": cursor.fetchall(),
        }
        payloads.append(payload)
    encoded = json.dumps(
        report_value({"tables": payloads}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if export_path is not None:
        export_path.write_bytes(encoded)
    return {
        "version": "logical-materialized-v1",
        "encoding": "canonical-utf8-finite-json-v1",
        "tables": tables,
        "durable_bytes": len(encoded),
    }


def _targets(conn: DuckDBPyConnection, config: ScrobbleRecsConfig, cutoff: datetime) -> list[int]:
    history = resolve_listening_window(
        as_of=cutoff,
        default_months=config.recommendation.profile_window_months,
        since=None,
        until=cutoff,
    )
    return [
        int(row[0])
        for row in conn.execute(
            "SELECT a.artist_id FROM artist a JOIN scrobble s USING (artist_key) "
            "WHERE NOT s.is_duplicate AND s.played_at >= ? AND s.played_at < ? "
            "GROUP BY a.artist_id HAVING count(*) >= ? ORDER BY a.artist_id",
            [history.start, cutoff, config.recommendation.min_seed_scrobbles],
        ).fetchall()
    ]


def supported_ablation(payload: dict[str, Any], identity: str) -> bool:
    return payload["recommendation"]["scoring"]["engine"] == "fusion" or ABLATIONS[identity] == (
        "listenbrainz",
    )


def ablated_config(payload: dict[str, Any], identity: str) -> dict[str, Any]:
    """One removal normalization for measured configs and their evidence validator."""
    if not supported_ablation(payload, identity):
        raise ValueError("unsupported source ablation for scorer")
    scoped = deepcopy(payload)
    removed = ABLATIONS[identity]
    recommendation = scoped["recommendation"]
    if recommendation["scoring"]["engine"] == "fusion":
        recommendation["fusion"]["excluded_sources"] = sorted(
            set(recommendation["fusion"]["excluded_sources"]) | set(removed)
        )
        if "semantic" in removed:
            recommendation["scoring"]["seed_idf_alpha"] = 0
    else:
        scoped["apis"]["listenbrainz"]["enabled"] = False
    return scoped


def _scoped_ablation(config: ScrobbleRecsConfig, ablation: str) -> ScrobbleRecsConfig:
    payload = ablated_config(config.model_dump(), ablation)
    scoped = config.model_copy(deep=True)
    if scoped.recommendation.scoring.engine == "fusion":
        scoped.recommendation.fusion = type(scoped.recommendation.fusion).model_validate(
            payload["recommendation"]["fusion"]
        )
        scoped.recommendation.scoring.seed_idf_alpha = payload["recommendation"]["scoring"][
            "seed_idf_alpha"
        ]
    else:
        scoped.apis.listenbrainz.enabled = payload["apis"]["listenbrainz"]["enabled"]
    return scoped


def _materialize_variant(
    directory: str,
    export: str,
    config: ScrobbleRecsConfig,
    window: EvaluationWindow,
    task: Literal["general", "artist"],
    target: int | None,
    stable: str,
    snapshot: str,
    base: dict[str, Any],
    allocation: float,
) -> dict[str, Any]:
    started = perf_counter()
    with TemporaryDirectory(prefix="variant-", dir=directory) as variant_dir:
        isolated = connect(str(Path(variant_dir) / "run.duckdb"))
        try:
            isolated.execute(f"IMPORT DATABASE '{export}'")
            if _comparison_state_id(isolated) != stable:
                raise ValueError("comparison state changed between variants")
            result = evaluate_recommendations(
                isolated,
                config,
                train_until=window.train_until,
                test_from=window.test_from,
                test_until=window.test_until,
                evaluation_task=task,
                target_artist_id=target,
            )
            if result.evidence_snapshot_id != snapshot:
                raise ValueError("comparison evidence snapshot changed")
            run = isolated.execute(
                "SELECT run_id, query_context, seed_summary FROM recommendation_run "
                "WHERE run_id = ?",
                [result.run_id],
            ).fetchone()
            assert run is not None
            run_id, context_json, summary_json = run
            artifact = json.loads(summary_json)["evaluation_artifact"]
            pool = [
                int(row[0])
                for row in isolated.execute(
                    "SELECT artist_id FROM recommendation_candidate "
                    "WHERE run_id = ? ORDER BY artist_id",
                    [run_id],
                ).fetchall()
            ]
            features = {
                str(artist): json.loads(payload or "{}")
                for artist, payload in isolated.execute(
                    "SELECT artist_id, feature_record FROM recommendation_candidate "
                    "WHERE run_id = ? ORDER BY artist_id",
                    [run_id],
                ).fetchall()
            }
            ranks = [
                int(row[0])
                for row in isolated.execute(
                    "SELECT artist_id FROM recommendation WHERE run_id = ? "
                    "AND coalesce(score_band, '') != 'candidate_only' ORDER BY rank, artist_id",
                    [run_id],
                ).fetchall()
            ]
            future = [
                int(artist)
                for artist, values in result.graded_metrics["relevance"].items()
                if values["future_play_count"] > 0
            ]
            final = logical_storage(isolated, export_path=Path(variant_dir) / "logical-export.json")
            frozen = report_value(result)
        finally:
            isolated.close()
    elapsed = perf_counter() - started
    return {
        "result": frozen,
        "run_id": str(run_id),
        "source_context": json.loads(context_json),
        "stable_state_id": stable,
        "artifact": artifact,
        "pool_ids": pool,
        "selected_ids": ranks,
        "future_new_ids": future,
        "candidate_features": features,
        "runtime": {
            "boundary": RUNTIME_BOUNDARY,
            "sample_count": 1,
            "statistic": "measured_total",
            "variant_seconds": elapsed,
            "shared_capture_seconds": allocation,
            "total_seconds": elapsed + allocation,
            "network_calls": 0,
        },
        "storage": {
            **final,
            "baseline_bytes": base["durable_bytes"],
            "peak_temporary_bytes": base["durable_bytes"] + final["durable_bytes"],
            "artifact_bytes": artifact["artifact_bytes"],
            "artifact_files": artifact["artifact_files"],
            "temporary_materializations": {
                "shared_snapshot_logical_bytes": base["durable_bytes"],
                "logical-export.json": final["durable_bytes"],
                "imported_database": "included in durable_bytes",
            },
            "boundary": (
                "durable+shared-logical-snapshot+canonical-result-export+distinct-artifact-v1"
            ),
        },
    }


def ablation_state(baseline: dict[str, Any], identity: str) -> dict[str, Any]:
    removed = ABLATIONS[identity]
    config = baseline["artifact"]["behavior_config"]
    fusion = config["recommendation"]["scoring"]["engine"] == "fusion"
    disabled = fusion and all(
        source in config["recommendation"]["fusion"]["excluded_sources"] for source in removed
    )
    if removed == ("listenbrainz",):
        disabled |= not (
            config["apis"]["listenbrainz"]["enabled"]
            and (not fusion or config["apis"]["listenbrainz"]["candidate_generation_enabled"])
        )
    if removed == ("musicbrainz",):
        disabled |= not (
            config["apis"]["musicbrainz"]["enabled"]
            and config["recommendation"]["catalog_evidence"]["enabled"]
        )
    supported = supported_ablation(config, identity)
    source_counts = baseline["result"]["candidate_count_by_source"]
    aliases = {"lastfm_similarity": "lastfm", "semantic": "lastfm_tag"}
    contributing = any(
        source_counts.get(source, 0) or source_counts.get(aliases.get(source, source), 0)
        for source in removed
    )
    if "local_feedback" in removed:
        contributing |= any(
            features.get("values", {}).get("feedback_score", 0) > 0
            for features in baseline.get("candidate_features", {}).values()
        )
    status = (
        "disabled"
        if disabled
        else "available"
        if supported and contributing
        else "noncontributing"
        if not contributing
        else "unsupported"
    )
    return {"enabled": not disabled, "contributing": bool(contributing), "status": status}


def _ablation_manifest(
    identity: str,
    baseline: dict[str, Any],
    variant: dict[str, Any] | None,
    key: dict[str, Any],
    stable: str,
    snapshot: str,
) -> dict[str, Any]:
    removed = ABLATIONS[identity]
    state = ablation_state(baseline, identity)
    actual = variant or baseline
    scalar_names = (
        "pool_recall",
        "eligible_recall",
        "pool_coverage",
        "eligible_coverage",
        "selected_slate_recall_at_50",
        "graded_ndcg_at_10",
        "graded_ndcg_at_25",
        "graded_ndcg_at_50",
    )
    deltas = {name: actual["result"][name] - baseline["result"][name] for name in scalar_names}
    removed_ids = set(baseline["pool_ids"]) - set(actual["pool_ids"])
    rank_effect = baseline["selected_ids"] != actual["selected_ids"]
    guard_effect = any(
        value.get("value") != actual["result"]["guardrails"].get(name, {}).get("value")
        for name, value in baseline["result"]["guardrails"].items()
    )
    return {
        **key,
        "ablation_id": identity,
        "kind": identity.split(":")[0],
        "removed_inputs": removed,
        "baseline_artifact": baseline["artifact"],
        "stable_state_id": stable,
        "evidence_snapshot_id": snapshot,
        **state,
        "reason": "nonfusion ablation unsupported" if state["status"] == "unsupported" else None,
        "boundary": (
            "optional positive preferences removed; hard reject/hide/cooldown safety retained"
        )
        if "local_feedback" in removed
        else "all derived uses of removed inputs",
        "metrics": actual,
        "deltas": deltas,
        "unique_candidate_count": len(removed_ids),
        "unique_future_hit_count": len(removed_ids & set(baseline["future_new_ids"])),
        "rank_effect": rank_effect,
        "guardrail_effect": guard_effect,
        "observed_effect": bool(removed_ids or rank_effect or guard_effect or any(deltas.values())),
    }


def guardrail_applicability(control: dict[str, Any], challenger: dict[str, Any]) -> dict[str, Any]:
    variants = (control, challenger)
    configs = [variant.get("artifact", {}).get("behavior_config", {}) for variant in variants]
    enabled = [
        bool(config.get("recommendation", {}).get("catalog_evidence", {}).get("enabled"))
        for config in configs
    ]
    contributing = [
        bool(variant.get("result", {}).get("candidate_count_by_source", {}).get("musicbrainz", 0))
        for variant in variants
    ]
    acquisition = [
        {"apis": config.get("apis", {}), "enrichment": config.get("enrichment", {})}
        for config in configs
    ]
    same_code = bool(control.get("artifact", {}).get("content_fingerprint")) and control[
        "artifact"
    ]["content_fingerprint"] == challenger.get("artifact", {}).get("content_fingerprint")
    return {
        "catalog_diversity": {
            "not_applicable": not any(enabled + contributing),
            "basis": "catalog explicitly disabled and noncontributing on both sides",
            "control_enabled": enabled[0],
            "challenger_enabled": enabled[1],
            "control_contributing": contributing[0],
            "challenger_contributing": contributing[1],
        },
        "provider_failure_rate": {
            "not_applicable": same_code and acquisition[0] == acquisition[1],
            "basis": "same code and acquisition inputs; local generation only",
            "control_inputs": acquisition[0],
            "challenger_inputs": acquisition[1],
            "same_content_fingerprint": same_code,
            "introduces_acquisition": not (same_code and acquisition[0] == acquisition[1]),
        },
    }


def compare_rolling_recommendations(
    conn: DuckDBPyConnection,
    control_config: ScrobbleRecsConfig,
    challenger_config: ScrobbleRecsConfig,
    *,
    windows: Sequence[EvaluationWindow],
    evaluation_task: Literal["general", "artist"] = "general",
    ablations: Sequence[str] = tuple(ABLATIONS),
    variant_order: tuple[str, str] = ("control", "challenger"),
) -> RollingComparisonReport:
    if not windows or evaluation_task not in ("general", "artist"):
        raise ValueError("nonempty windows and a general or artist task are required")
    if set(ablations) - ABLATIONS.keys():
        raise ValueError("unknown ablation identity")
    if set(variant_order) != {"control", "challenger"} or len(variant_order) != 2:
        raise ValueError("variant_order must contain control and challenger once")
    for window in windows:
        cutoff, start, end = map(
            normalize_timestamp, (window.train_until, window.test_from, window.test_until)
        )
        if start < cutoff or end <= start:
            raise ValueError("invalid evaluation horizon")
    started = perf_counter()
    stable = _comparison_state_id(conn)
    pairs: list[dict[str, Any]] = []
    measured_variants: list[dict[str, Any]] = []
    targets: set[int] = set()
    with TemporaryDirectory(prefix="scrobble-rolling-") as directory:
        export = str(Path(directory) / "snapshot").replace("'", "''")
        conn.execute("BEGIN TRANSACTION")
        try:
            if stable != _comparison_state_id(conn):
                raise ValueError("comparison state changed before capture")
            base = logical_storage(conn)
            conn.execute(f"EXPORT DATABASE '{export}' (FORMAT PARQUET)")
            queries: list[tuple[EvaluationWindow, int | None, str]] = []
            for window in windows:
                cutoff = normalize_timestamp(window.train_until)
                snapshot = capture_evidence_snapshot(conn, historical_cutoff=cutoff).snapshot_id
                local_targets: list[int | None] = [None]
                if evaluation_task == "artist":
                    control_targets = _targets(conn, control_config, cutoff)
                    if control_targets != _targets(conn, challenger_config, cutoff):
                        raise ValueError("artist target populations differ between configurations")
                    local_targets = list(control_targets)
                    targets.update(control_targets)
                queries.extend((window, target, snapshot) for target in local_targets)
        finally:
            conn.execute("ROLLBACK")
        capture_seconds = perf_counter() - started
        supported = [
            identity
            for identity in ablations
            if supported_ablation(challenger_config.model_dump(), identity)
        ]
        runs_per_query = 2 + len(supported)
        allocation = capture_seconds / (len(queries) * runs_per_query) if queries else 0.0
        for window, target, snapshot in queries:
            key = {
                "evaluation_task": evaluation_task,
                "target_artist_id": target,
                **{
                    name: normalize_timestamp(getattr(window, name)).isoformat()
                    for name in ("train_until", "test_from", "test_until")
                },
            }
            variants = {}
            for name in variant_order:
                variants[name] = _materialize_variant(
                    directory,
                    export,
                    control_config if name == "control" else challenger_config,
                    window,
                    evaluation_task,
                    target,
                    stable,
                    snapshot,
                    base,
                    allocation,
                )
                measured_variants.append(variants[name])
            manifests = []
            for identity in ablations:
                removed = None
                if identity in supported:
                    removed = _materialize_variant(
                        directory,
                        export,
                        _scoped_ablation(challenger_config, identity),
                        window,
                        evaluation_task,
                        target,
                        stable,
                        snapshot,
                        base,
                        allocation,
                    )
                    measured_variants.append(removed)
                manifests.append(
                    _ablation_manifest(
                        identity, variants["challenger"], removed, key, stable, snapshot
                    )
                )
            applicability = guardrail_applicability(variants["control"], variants["challenger"])
            for name, status in applicability.items():
                if status["not_applicable"]:
                    for variant in variants.values():
                        metric = variant["result"]["guardrails"][name]
                        if not metric["available"]:
                            metric.update(availability="not_applicable", reason=status["basis"])
            pairs.append(
                {
                    "key": key,
                    **variants,
                    "ablations": manifests,
                    "guardrail_applicability": applicability,
                }
            )
        cleanup_started = perf_counter()
    if stable != _comparison_state_id(conn):
        raise ValueError("source database changed during rolling comparison")
    shared_seconds = capture_seconds + perf_counter() - cleanup_started
    extra_allocation = (
        (shared_seconds - capture_seconds) / len(measured_variants) if measured_variants else 0
    )
    for variant in measured_variants:
        variant["runtime"]["shared_capture_seconds"] += extra_allocation
        variant["runtime"]["total_seconds"] += extra_allocation
    return RollingComparisonReport(
        evaluation_task, stable, tuple(pairs), base, shared_seconds, tuple(sorted(targets))
    )
