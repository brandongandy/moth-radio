"""Pure conservative promotion decisions from explicitly supplied local evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite
from typing import Any

from scrobble_recs.config import PromotionConfig
from scrobble_recs.models.experiments import ExperimentReport, _canonical
from scrobble_recs.models.profile_window import normalize_timestamp
from scrobble_recs.models.report_json import report_value
from scrobble_recs.models.rolling_compare import (
    ABLATIONS,
    RUNTIME_BOUNDARY,
    RollingComparisonReport,
    guardrail_applicability,
)

REASONS = (
    "invalid_evidence",
    "incompatible_comparison",
    "insufficient_cutoffs",
    "recall_regression",
    "graded_ndcg_regression",
    "candidate_coverage_regression",
    "missing_blinded_evidence",
    "incompatible_blinded_evidence",
    "incomplete_blinded_evidence",
    "blinded_loss",
    "incomplete_guardrails",
    "guardrail_regression",
    "identity_below_minimum",
    "provider_concentration_exceeded",
    "missing_runtime_evidence",
    "runtime_limit_exceeded",
    "missing_storage_evidence",
    "storage_limit_exceeded",
    "missing_versioned_artifact",
    "missing_reversibility_evidence",
    "incomplete_ablations",
    "unexplained_ablation_gain",
)
GUARD_DIRECTIONS = {
    "novelty": 1,
    "tag_diversity": 1,
    "catalog_diversity": 1,
    "family_agreement": 1,
    "identity_resolved_rate": 1,
    "fatigue": -1,
    "stale_source_rate": -1,
    "provider_failure_rate": -1,
    "identity_ambiguous_rate": -1,
    "popularity_q50": -1,
    "popularity_q75": -1,
    "popularity_q90": -1,
    "provider_concentration": -1,
}
ARTIFACT_KEYS = (
    "artifact_id",
    "scorer",
    "version",
    "feature_schema_version",
    "slate_policy_version",
    "content_fingerprint",
    "behavior_config_fingerprint",
)


@dataclass(frozen=True)
class PromotionDecision:
    eligible: bool
    reasons: tuple[str, ...]
    metrics: dict[str, object]

    @property
    def evaluation_task(self) -> object:
        return self.metrics.get("evaluation_task")


def _number(value: Any) -> bool:
    return type(value) in (int, float) and isfinite(value)


def _identity(value: Any) -> dict[str, Any]:
    return {key: value.get(key) for key in ARTIFACT_KEYS} if isinstance(value, dict) else {}


def _valid_artifact(value: Any) -> bool:
    if not isinstance(value, dict) or not all(
        isinstance(value.get(key), str) and value[key].strip() for key in ARTIFACT_KEYS
    ):
        return False
    behavior = value.get("behavior_config")
    if not isinstance(behavior, dict):
        return False
    encoded = json.dumps(behavior, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return bool(sha256(encoded.encode()).hexdigest() == value["behavior_config_fingerprint"])


def _finite_copy(value: Any, invalid: list[str], path: str = "") -> Any:
    if isinstance(value, float) and not isfinite(value):
        invalid.append(path)
        return None
    if isinstance(value, dict):
        return {key: _finite_copy(item, invalid, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [_finite_copy(item, invalid, f"{path}[{i}]") for i, item in enumerate(value)]
    return value


def _pair_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    task, target = value.get("evaluation_task"), value.get("target_artist_id")
    if (
        task not in ("general", "artist")
        or (task == "general" and target is not None)
        or (task == "artist" and (type(target) is not int or target < 1))
    ):
        raise ValueError("invalid task/target")
    times = tuple(
        normalize_timestamp(datetime.fromisoformat(value[name]))
        for name in ("train_until", "test_from", "test_until")
    )
    if times[1] < times[0] or times[2] <= times[1]:
        raise ValueError("invalid horizon")
    return task, target, *(stamp.isoformat() for stamp in times)


def _semantic_context(context: dict[str, Any]) -> Any:
    keys = (
        "query_kind",
        "target_artist_id",
        "history_window",
        "history_cutoff",
        "feedback_cutoff",
        "mode",
        "novelty_intent",
        "taste_region_seeds",
        "evidence_freshness",
        "target_relationship",
        "evidence_snapshot_id",
    )
    return _canonical({key: context.get(key) for key in keys})


def _check_blinded(
    experiments: list[dict[str, Any]],
    artifacts: dict[str, dict[str, Any]],
    task: str | None,
    targets: set[int | None],
    reasons: set[str],
) -> dict[str, Any]:
    totals = dict(control=0, challenger=0, tie=0, denominator=0, abstentions=0, unanswered=0)
    groups: dict[int | None, dict[str, int]] = {}
    details = []
    for experiment in experiments:
        definition = experiment.get("definition", {})
        target = definition.get("target_artist_id")
        mapping = definition.get("side_variants", {})
        linked = (
            experiment.get("task") == task
            and target in targets
            and set(mapping) == {"left", "right"}
            and set(mapping.values()) == {"control", "challenger"}
        )
        sources = definition.get("source_artifacts", {})
        contexts = definition.get("source_contexts", {})
        runs = definition.get("source_run_ids", {})
        linked &= all(
            _valid_artifact(sources.get(name))
            and _identity(sources[name]) == _identity(artifacts.get(name))
            and isinstance(runs.get(name), str)
            and bool(runs[name])
            and isinstance(contexts.get(name), dict)
            and contexts[name].get("query_kind") == task
            and contexts[name].get("target_artist_id") == target
            and contexts[name].get("evidence_snapshot_id") == experiment.get("evidence_snapshot_id")
            for name in ("control", "challenger")
        )
        linked &= runs.get("control") != runs.get("challenger")
        if all(isinstance(contexts.get(name), dict) for name in ("control", "challenger")):
            linked &= _semantic_context(contexts["control"]) == _semantic_context(
                contexts["challenger"]
            )
        frozen = definition.get("artists", {})
        for artist in frozen.values():
            memberships = artist.get("memberships", [])
            linked &= bool(memberships) and all(
                row.get("variant") in ("control", "challenger")
                and row.get("run_id") == runs.get(row.get("variant"))
                for row in memberships
            )
        if not linked:
            reasons.add("incompatible_blinded_evidence")
        exposures = experiment.get("promotion_evidence", {}).get("exposures", [])
        shown: set[int] = set()
        complete = bool(exposures) and bool(frozen)
        exposure_ids: set[str] = set()
        votes = dict(control=0, challenger=0, tie=0, denominator=0, abstentions=0, unanswered=0)
        for exposure in exposures:
            identity = exposure.get("exposure_id")
            if not identity or identity in exposure_ids:
                reasons.add("invalid_evidence")
                complete = False
            exposure_ids.add(identity)
            shown.update(exposure.get("shown_artist_ids", []))
            choice = exposure.get("effective_list_choice")
            complete &= (
                bool(
                    exposure.get("judgment_id")
                    and exposure.get("judged_at")
                    and exposure.get("completed_at")
                )
                and choice is not None
            )
            if choice is None:
                votes["unanswered"] += 1
            elif choice == "none":
                votes["abstentions"] += 1
            elif choice in ("left", "right", "tie"):
                owner = "tie" if choice == "tie" else mapping.get(choice)
                if owner in ("control", "challenger", "tie"):
                    votes[owner] += 1
                    votes["denominator"] += 1
            else:
                reasons.add("invalid_evidence")
                complete = False
        try:
            complete &= shown == {int(artist) for artist in frozen}
        except (TypeError, ValueError):
            complete = False
            reasons.add("invalid_evidence")
        if not complete:
            reasons.add("incomplete_blinded_evidence")
        if linked:
            group = groups.setdefault(target, dict.fromkeys(votes, 0))
            for name, value in votes.items():
                group[name] += value
                totals[name] += value
        details.append(
            {
                "experiment_id": experiment.get("experiment_id"),
                "target_artist_id": target,
                "linked": bool(linked),
                "complete": bool(complete),
                "votes": votes,
                "evidence": experiment,
            }
        )
    if totals["denominator"] == 0 or set(groups) != targets:
        reasons.add("missing_blinded_evidence")
    if any(group["denominator"] == 0 for group in groups.values()):
        reasons.add("missing_blinded_evidence")
    if any(group["challenger"] < group["control"] for group in groups.values()):
        reasons.add("blinded_loss")
    return {
        **totals,
        "target_groups": {str(key): value for key, value in groups.items()},
        "experiments": details,
    }


def _guard_value(metric: Any) -> float | None:
    if not isinstance(metric, dict) or not metric.get("available") or metric.get("coverage") != 1:
        return None
    if not all(metric.get(key) for key in ("source", "basis", "version", "snapshot_id", "cutoff")):
        return None
    value = metric.get("value")
    if not _number(value) or not _number(metric.get("denominator")) or metric["denominator"] <= 0:
        return None
    assert isinstance(value, int | float)
    return float(value)


def _check_guardrails(
    pair: dict[str, Any],
    config: PromotionConfig,
    reasons: set[str],
) -> dict[str, Any]:
    checks = {}
    applicability = guardrail_applicability(pair["control"], pair["challenger"])
    for name, direction in GUARD_DIRECTIONS.items():
        control = pair["control"].get("result", {}).get("guardrails", {}).get(name, {})
        challenger = pair["challenger"].get("result", {}).get("guardrails", {}).get(name, {})
        recorded = pair.get("guardrail_applicability", {}).get(name, {})
        if (
            name in applicability
            and applicability[name]["not_applicable"]
            and recorded.get("not_applicable") is True
            and recorded.get("basis")
            and all(
                recorded.get(key) == value
                for key, value in applicability[name].items()
                if key != "basis"
            )
            and all(
                metric.get("availability") == "not_applicable" for metric in (control, challenger)
            )
        ):
            checks[name] = {
                "control": control,
                "challenger": challenger,
                "availability": "not_applicable",
                "evidence": recorded,
            }
            continue
        a, b = _guard_value(control), _guard_value(challenger)
        definitions = ("source", "basis", "version")
        comparable = (
            a is not None
            and b is not None
            and all(control.get(key) == challenger.get(key) for key in definitions)
        )
        comparable &= (
            all(
                metric.get("snapshot_id")
                == pair[variant].get("result", {}).get("evidence_snapshot_id")
                and normalize_timestamp(
                    datetime.fromisoformat(metric.get("cutoff", ""))
                ).isoformat()
                == _pair_key(pair["key"])[2]
                for variant, metric in (("control", control), ("challenger", challenger))
            )
            if comparable
            else False
        )
        if not comparable:
            reasons.add("incomplete_guardrails")
        elif a is not None and b is not None and direction * (b - a) < 0:
            reasons.add("guardrail_regression")
        if (
            b is not None
            and name == "identity_resolved_rate"
            and b < config.minimum_identity_resolved_rate
        ):
            reasons.add("identity_below_minimum")
        if (
            b is not None
            and name == "provider_concentration"
            and b > config.max_single_provider_share
        ):
            reasons.add("provider_concentration_exceeded")
        checks[name] = {
            "control": control,
            "challenger": challenger,
            "comparable": comparable,
            "delta": b - a if comparable and a is not None and b is not None else None,
            "direction": direction,
        }
    return checks


def _check_costs(
    pair: dict[str, Any], config: PromotionConfig, reasons: set[str]
) -> dict[str, Any]:
    control, challenger = pair["control"], pair["challenger"]
    runtimes = [row.get("runtime", {}) for row in (control, challenger)]
    a, b = [row.get("total_seconds") for row in runtimes]
    runtime_ok = (
        _number(a)
        and a > 0
        and _number(b)
        and b >= 0
        and all(
            row.get("boundary") == RUNTIME_BOUNDARY
            and row.get("sample_count", 0) >= 1
            and row.get("statistic")
            and row.get("network_calls") == 0
            and _number(row.get("shared_capture_seconds"))
            and row["shared_capture_seconds"] >= 0
            for row in runtimes
        )
        and runtimes[0].get("shared_capture_seconds") == runtimes[1].get("shared_capture_seconds")
    )
    ratio = b / a if runtime_ok else None
    if ratio is not None and not isfinite(ratio):
        ratio = None
        runtime_ok = False
        reasons.add("invalid_evidence")
    if not runtime_ok:
        reasons.add("missing_runtime_evidence")
    elif b > a * config.max_runtime_multiplier:
        reasons.add("runtime_limit_exceeded")
    storages = [row.get("storage", {}) for row in (control, challenger)]
    storage_ok = all(
        row.get("version") == "logical-materialized-v1"
        and row.get("encoding") == "canonical-utf8-finite-json-v1"
        and row.get("boundary")
        and isinstance(row.get("tables"), list)
        and bool(row["tables"])
        and isinstance(row.get("artifact_files"), list)
        and bool(row["artifact_files"])
        and len(row["artifact_files"]) == len(set(row["artifact_files"]))
        and all(
            type(row.get(key)) is int and row[key] >= 0
            for key in ("baseline_bytes", "durable_bytes", "peak_temporary_bytes", "artifact_bytes")
        )
        for row in storages
    ) and all(
        storages[0].get(key) == storages[1].get(key)
        for key in ("baseline_bytes", "boundary", "encoding", "tables")
    )
    storage_ok &= all(
        storage.get("artifact_bytes") == variant.get("artifact", {}).get("artifact_bytes")
        and storage.get("artifact_files") == variant.get("artifact", {}).get("artifact_files")
        for storage, variant in zip(storages, (control, challenger), strict=True)
    )
    growth = None
    if storage_ok:
        footprints = [
            sum(row[key] for key in ("durable_bytes", "peak_temporary_bytes", "artifact_bytes"))
            for row in storages
        ]
        growth = (footprints[1] - footprints[0]) / 1_048_576
        if growth > config.max_storage_growth_mb:
            reasons.add("storage_limit_exceeded")
    else:
        reasons.add("missing_storage_evidence")
    return {
        "runtime_ratio": ratio,
        "runtime_reason": None if runtime_ok else "invalid_or_missing_cost_boundary",
        "storage_growth_mib": growth,
        "runtime_evidence": runtimes,
        "storage_evidence": storages,
    }


def _check_ablations(pair: dict[str, Any], reasons: set[str]) -> bool:
    rows = pair.get("ablations", [])
    identities = [row.get("ablation_id") for row in rows]
    if set(identities) != set(ABLATIONS) or len(identities) != len(set(identities)):
        reasons.add("incomplete_ablations")
    baseline = pair["challenger"]
    control = pair["control"]
    explained = False
    for row in rows:
        linked = (
            _identity(row.get("baseline_artifact")) == _identity(baseline.get("artifact"))
            and row.get("stable_state_id") == baseline.get("stable_state_id")
            and row.get("evidence_snapshot_id")
            == baseline.get("result", {}).get("evidence_snapshot_id")
            and all(row.get(key) == value for key, value in pair.get("key", {}).items())
            and row.get("status") in ("available", "disabled", "noncontributing")
        )
        if not linked:
            reasons.add("incomplete_ablations")
        actual = row.get("metrics", {}).get("result", {})
        for name in (
            "pool_recall",
            "eligible_recall",
            "pool_coverage",
            "eligible_coverage",
            "selected_slate_recall_at_50",
            "graded_ndcg_at_10",
            "graded_ndcg_at_25",
            "graded_ndcg_at_50",
        ):
            base_value = baseline.get("result", {}).get(name)
            original = control.get("result", {}).get(name)
            removed = actual.get(name)
            if not all(_number(value) for value in (base_value, original, removed)):
                continue
            gain = base_value - original
            effect = base_value - removed
            if (
                linked
                and row.get("enabled")
                and row.get("contributing")
                and gain > 0
                and effect > 0
            ):
                explained = True
        if linked and row.get("enabled") and row.get("contributing"):
            for name, direction in GUARD_DIRECTIONS.items():
                values = [
                    _guard_value(result.get("guardrails", {}).get(name))
                    for result in (control.get("result", {}), baseline.get("result", {}), actual)
                ]
                if all(value is not None for value in values):
                    a, b, c = values
                    assert a is not None and b is not None and c is not None
                    explained |= direction * (b - a) > 0 and direction * (b - c) > 0
    return explained


def _duplicate_facts(pair: dict[str, Any]) -> dict[str, Any]:
    """Repeated measurements may vary in timing/IDs; semantic contradictions may not."""
    return {
        name: {
            "artifact": _identity(pair[name].get("artifact")),
            "stable_state_id": pair[name].get("stable_state_id"),
            "context": pair[name].get("source_context"),
            "result": {
                key: value
                for key, value in pair[name].get("result", {}).items()
                if key not in ("runtime_seconds", "run_id")
            },
            "pool_ids": pair[name].get("pool_ids"),
            "selected_ids": pair[name].get("selected_ids"),
        }
        for name in ("control", "challenger")
    }


def _check_reversibility(
    evidence: dict[str, Any],
    artifacts: dict[str, Any],
    reasons: set[str],
) -> None:
    control = _identity(artifacts.get("control"))
    challenger = _identity(artifacts.get("challenger"))
    restore, verification = evidence.get("restore_operation", {}), evidence.get("verification", {})
    valid = (
        bool(control)
        and bool(challenger)
        and _identity(evidence.get("control_artifact")) == control
        and _identity(evidence.get("challenger_artifact")) == challenger
        and evidence.get("compatible_feature_schema") == challenger.get("feature_schema_version")
        and bool(restore.get("selector"))
        and _identity(restore.get("artifact")) == control
        and bool(verification.get("id"))
        and verification.get("successful") is True
        and _identity(verification.get("restored_artifact")) == control
    )
    if not valid:
        reasons.add("missing_reversibility_evidence")


def promotion_decision(
    *,
    config: PromotionConfig,
    comparison: RollingComparisonReport | Mapping[str, Any] | None = None,
    blinded_experiments: Sequence[ExperimentReport | Mapping[str, Any]] = (),
    reversibility: Mapping[str, Any] | None = None,
    policy_experiment_id: str | None = None,
) -> PromotionDecision:
    """Assess explicit evidence only. Never mutate configuration, files or active state."""
    config = PromotionConfig.model_validate(config.model_dump())
    reasons: set[str] = set()
    invalid: list[str] = []
    captured = _finite_copy(report_value(comparison or {}), invalid)
    experiments = _finite_copy(report_value(blinded_experiments), invalid)
    rollback = _finite_copy(report_value(reversibility or {}), invalid)
    if invalid:
        reasons.add("invalid_evidence")
    if config != PromotionConfig() and not policy_experiment_id:
        reasons.add("invalid_evidence")
    pairs = captured.get("pairs", [])
    task = captured.get("evaluation_task")
    if not pairs or task not in ("general", "artist"):
        reasons.update(
            (
                "incompatible_comparison",
                "incomplete_guardrails",
                "missing_runtime_evidence",
                "missing_storage_evidence",
                "incomplete_ablations",
            )
        )
    cutoffs: set[str] = set()
    targets: set[int | None] = set()
    artifacts: dict[str, Any] = {}
    seen: dict[tuple[Any, ...], Any] = {}
    checks = []
    explained = False
    for index, pair in enumerate(pairs):
        try:
            key = _pair_key(pair.get("key", {}))
            targets.add(key[1])
            compatible = key[0] == task
            facts = _duplicate_facts(pair)
            if key in seen and seen[key] != facts:
                reasons.add("invalid_evidence")
            seen[key] = facts
            for name in ("control", "challenger"):
                variant = pair[name]
                result = variant.get("result", {})
                context = variant.get("source_context", {})
                compatible &= context.get("query_kind") == key[0]
                compatible &= context.get("target_artist_id") == key[1]
                compatible &= context.get("evidence_snapshot_id") == result.get(
                    "evidence_snapshot_id"
                )
                compatible &= (
                    _pair_key(
                        {**result, "target_artist_id": result.get("target_artist_id", key[1])}
                    )
                    == key
                )
                compatible &= bool(variant.get("stable_state_id")) and variant[
                    "stable_state_id"
                ] == captured.get("stable_state_id")
                if name not in artifacts:
                    artifacts[name] = variant.get("artifact", {})
                compatible &= _identity(variant.get("artifact")) == _identity(artifacts[name])
                if not _valid_artifact(variant.get("artifact")):
                    reasons.add("missing_versioned_artifact")
            a, b = pair["control"].get("result", {}), pair["challenger"].get("result", {})
            compatible &= bool(a.get("evidence_snapshot_id")) and a.get(
                "evidence_snapshot_id"
            ) == b.get("evidence_snapshot_id")
            compatible &= _semantic_context(
                pair["control"].get("source_context", {})
            ) == _semantic_context(pair["challenger"].get("source_context", {}))
            if not compatible:
                reasons.add("incompatible_comparison")
            supported = all(
                row.get("graded_metrics", {}).get("ndcg_available") is True
                and row.get("graded_metrics", {}).get("positive_relevance_count", 0) > 0
                for row in (a, b)
            )
            if compatible and supported:
                cutoffs.add(key[2])
            regressions = {}
            for name in (
                "pool_recall",
                "eligible_recall",
                "selected_slate_recall_at_50",
                "recall_at_50",
                "recall_at_100",
                "graded_ndcg_at_10",
                "graded_ndcg_at_25",
                "graded_ndcg_at_50",
                "pool_coverage",
                "eligible_coverage",
            ):
                values = a.get(name), b.get(name)
                available = all(_number(value) for value in values)
                if not available:
                    reasons.add("incompatible_comparison")
                elif values[1] < values[0]:
                    reasons.add(
                        "graded_ndcg_regression"
                        if name.startswith("graded_ndcg")
                        else "candidate_coverage_regression"
                        if name.endswith("coverage")
                        else "recall_regression"
                    )
                regressions[name] = {
                    "control": values[0],
                    "challenger": values[1],
                    "delta": values[1] - values[0] if available else None,
                }
            guardrails = _check_guardrails(pair, config, reasons)
            costs = _check_costs(pair, config, reasons)
            explained |= _check_ablations(pair, reasons)
            checks.append(
                {
                    "key": pair["key"],
                    "compatible": bool(compatible),
                    "supported": bool(supported),
                    "metrics": regressions,
                    "guardrails": guardrails,
                    **costs,
                    "control_artifact": pair["control"].get("artifact"),
                    "challenger_artifact": pair["challenger"].get("artifact"),
                    "ablations": pair.get("ablations", []),
                }
            )
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            reasons.update(("invalid_evidence", "incompatible_comparison"))
            checks.append({"index": index, "error": "malformed_pair", "evidence": pair})
    if len(cutoffs) < config.minimum_cutoffs:
        reasons.add("insufficient_cutoffs")
    if not artifacts or any(not _valid_artifact(value) for value in artifacts.values()):
        reasons.add("missing_versioned_artifact")
    if task == "general":
        targets = {None}
    elif targets != set(captured.get("target_artist_ids", [])):
        reasons.add("incompatible_comparison")
    votes = _check_blinded(experiments, artifacts, task, targets, reasons)
    _check_reversibility(rollback, artifacts, reasons)
    if not explained:
        reasons.add("unexplained_ablation_gain")
    metrics: dict[str, object] = {
        "policy": "promotion-guardrails-v1",
        "evaluation_task": task,
        "target_artist_ids": sorted(target for target in targets if target is not None),
        "thresholds": config.model_dump(),
        "policy_experiment_id": policy_experiment_id,
        "distinct_supported_cutoffs": len(cutoffs),
        "supported_cutoffs": sorted(cutoffs),
        "artifacts": artifacts,
        "pairs": checks,
        "blinded_votes": votes,
        "reversibility": rollback,
        "invalid_numeric_paths": sorted(invalid),
        "activation_status": "not_promoted",
    }
    metrics = _finite_copy(metrics, invalid)
    if invalid:
        reasons.add("invalid_evidence")
        metrics["invalid_numeric_paths"] = sorted(set(invalid))
    ordered = tuple(reason for reason in REASONS if reason in reasons)
    return PromotionDecision(not ordered, ordered, metrics)
