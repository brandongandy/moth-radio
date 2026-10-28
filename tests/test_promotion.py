from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from scrobble_recs.config import PromotionConfig


@pytest.fixture
def evidence():
    fixture = json.loads((Path(__file__).parent / "fixtures/promotion_evidence.json").read_text())
    pairs = []
    for cutoff in fixture["cutoffs"]:
        end = (datetime.fromisoformat(cutoff) + timedelta(days=90)).isoformat()
        key = dict(
            evaluation_task="general",
            target_artist_id=None,
            train_until=cutoff,
            test_from=cutoff,
            test_until=end,
        )
        pair = {"key": key, "ablations": []}
        for name in ("control", "challenger"):
            result = deepcopy(fixture["metrics"])
            result.update(train_until=cutoff, test_from=cutoff, test_until=end)
            for guard in result["guardrails"].values():
                guard["cutoff"] = cutoff
            if name == "challenger":
                result["graded_ndcg_at_50"] = 0.6
            pair[name] = dict(
                result=result,
                run_id=name + "-" + cutoff,
                source_context={
                    **deepcopy(fixture["context"]),
                    "evidence_snapshot_id": "fixture-snapshot",
                    "history_cutoff": cutoff,
                    "feedback_cutoff": cutoff,
                    "history_window": {**fixture["context"]["history_window"], "end": cutoff},
                },
                stable_state_id="fixture-state",
                artifact=fixture["artifacts"][name],
                pool_ids=[1, 2],
                selected_ids=[1, 2],
                future_new_ids=[1, 2],
                candidate_features={str(i): {"values": {"feedback_score": 0}} for i in (1, 2)},
                runtime=dict(
                    boundary="isolated-import+generation+metrics+logical-export+cleanup-v1",
                    sample_count=1,
                    statistic="measured_total",
                    shared_capture_seconds=0.1,
                    variant_seconds=0.9,
                    total_seconds=1,
                    network_calls=0,
                ),
                storage=dict(
                    version="logical-materialized-v1",
                    encoding="canonical-utf8-finite-json-v1",
                    baseline_bytes=100,
                    durable_bytes=200,
                    peak_temporary_bytes=300,
                    artifact_bytes=100,
                    artifact_files=["fixture-ranker.py"],
                    tables=["artist"],
                    boundary="durable+shared-logical-snapshot+canonical-result-export+distinct-artifact-v1",
                ),
            )
            result["run_id"] = pair[name]["run_id"]
        for identity in fixture["ablations"]:
            variant = deepcopy(pair["challenger"])
            removed = fixture["ablation_inputs"][identity]
            active = any(source in ("lastfm_similarity", "semantic") for source in removed)
            disabled = identity.split(":")[1] in ("listenbrainz", "musicbrainz")
            variant["run_id"] += "-without-" + identity
            variant["result"]["run_id"] = variant["run_id"]
            artifact = variant["artifact"]
            artifact["behavior_config"]["recommendation"]["fusion"]["excluded_sources"] = sorted(
                removed
            )
            artifact["behavior_config_fingerprint"] = sha256(
                json.dumps(
                    artifact["behavior_config"], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            artifact["artifact_id"] += "-without-" + identity
            if active:
                variant["result"]["graded_ndcg_at_50"] = 0.5
                for source in removed:
                    for field in (
                        "candidate_count_by_source",
                        "future_hit_count_by_source",
                        "unique_candidate_count_by_source",
                        "unique_future_hit_count_by_source",
                    ):
                        variant["result"][field].pop(source, None)
            scalars = (
                "pool_recall",
                "eligible_recall",
                "pool_coverage",
                "eligible_coverage",
                "selected_slate_recall_at_50",
                "graded_ndcg_at_10",
                "graded_ndcg_at_25",
                "graded_ndcg_at_50",
            )
            pair["ablations"].append(
                {
                    **key,
                    "ablation_id": identity,
                    "kind": identity.split(":")[0],
                    "removed_inputs": removed,
                    "baseline_artifact": fixture["artifacts"]["challenger"],
                    "stable_state_id": "fixture-state",
                    "evidence_snapshot_id": "fixture-snapshot",
                    "enabled": not disabled,
                    "contributing": active,
                    "status": "available"
                    if active
                    else "disabled"
                    if disabled
                    else "noncontributing",
                    "reason": None,
                    "metrics": variant,
                    "deltas": {
                        field: variant["result"][field] - pair["challenger"]["result"][field]
                        for field in scalars
                    },
                    "unique_candidate_count": 0,
                    "unique_future_hit_count": 0,
                    "rank_effect": False,
                    "guardrail_effect": False,
                    "observed_effect": active,
                }
            )
        pairs.append(pair)
    definition = dict(
        side_variants={"left": "challenger", "right": "control"},
        source_artifacts=fixture["artifacts"],
        source_run_ids={"control": "source-control", "challenger": "source-challenger"},
        source_contexts={"control": fixture["context"], "challenger": fixture["context"]},
        target_artist_id=None,
        artists={
            str(aid): {
                "artist_id": aid,
                "memberships": [
                    {"variant": name, "run_id": "source-" + name, "ranks": [aid]}
                    for name in ("control", "challenger")
                ],
            }
            for aid in (1, 2)
        },
    )
    experiment = dict(
        experiment_id="fixture-experiment",
        task="general",
        definition=definition,
        evidence_snapshot_id="prospective-snapshot",
        promotion_evidence={
            "exposures": [
                dict(
                    exposure_id="fixture-exposure",
                    shown_artist_ids=[1, 2],
                    effective_list_choice="tie",
                    preferred_side="right",
                    judgment_id="fixture-judgment",
                    judged_at="2024-01-03",
                    completed_at="2024-01-03",
                )
            ]
        },
    )
    control = fixture["artifacts"]["control"]
    rollback = dict(
        control_artifact=control,
        challenger_artifact=fixture["artifacts"]["challenger"],
        compatible_feature_schema="fixture-features-v1",
        restore_operation={"selector": "scoring.engine=legacy", "artifact": control},
        verification={
            "id": "fixture-rollback-verification",
            "successful": True,
            "restored_artifact": control,
        },
    )
    return dict(
        comparison={
            "evaluation_task": "general",
            "stable_state_id": "fixture-state",
            "pairs": pairs,
            "target_artist_ids": [],
        },
        blinded_experiments=[experiment],
        reversibility=rollback,
        policy_experiment_id="fixture-policy",
    )


def decide(evidence, **kwargs):
    from scrobble_recs.models.promotion import promotion_decision

    return promotion_decision(config=PromotionConfig(**kwargs), **evidence)


def test_complete_same_state_evidence_can_be_eligible_without_activation(evidence):
    before = deepcopy(evidence)
    decision = decide(evidence)
    assert decision.eligible and decision.reasons == ()
    assert decision.metrics["distinct_supported_cutoffs"] == 3
    assert decision.metrics["blinded_votes"]["tie"] == 1
    assert evidence == before


@pytest.mark.parametrize(
    "field,reason",
    [
        ("pool_recall", "recall_regression"),
        ("eligible_recall", "recall_regression"),
        ("selected_slate_recall_at_50", "recall_regression"),
        ("graded_ndcg_at_10", "graded_ndcg_regression"),
        ("graded_ndcg_at_25", "graded_ndcg_regression"),
        ("graded_ndcg_at_50", "graded_ndcg_regression"),
        ("pool_coverage", "candidate_coverage_regression"),
        ("eligible_coverage", "candidate_coverage_regression"),
    ],
)
def test_one_cutoff_regression_cannot_hide_behind_a_favorable_mean(evidence, field, reason):
    evidence["comparison"]["pairs"][0]["challenger"]["result"][field] = 0.49
    evidence["comparison"]["pairs"][1]["challenger"]["result"][field] = 1
    decision = decide(evidence)
    assert not decision.eligible
    assert reason in decision.reasons


@pytest.mark.parametrize(
    "choice,expected_reason",
    [
        ("left", None),
        ("right", "blinded_loss"),
        ("tie", None),
        ("none", "missing_blinded_evidence"),
        (None, "missing_blinded_evidence"),
    ],
)
def test_only_explicit_list_votes_establish_blinded_gate(evidence, choice, expected_reason):
    exposure = evidence["blinded_experiments"][0]["promotion_evidence"]["exposures"][0]
    exposure["effective_list_choice"] = choice
    exposure["preferred_side"] = "left"
    decision = decide(evidence)
    assert decision.eligible == (expected_reason is None)
    if expected_reason:
        assert expected_reason in decision.reasons


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("no_experiment", "missing_blinded_evidence"),
        ("no_exposures", "missing_blinded_evidence"),
        ("unshown", "incomplete_blinded_evidence"),
        ("reversed", "incompatible_blinded_evidence"),
        ("wrong_task", "incompatible_blinded_evidence"),
        ("wrong_target", "incompatible_blinded_evidence"),
        ("unrelated_artifact", "incompatible_blinded_evidence"),
        ("duplicate_cutoffs", "insufficient_cutoffs"),
        ("empty_outcomes", "insufficient_cutoffs"),
        ("wrong_state", "incompatible_comparison"),
        ("wrong_horizon", "incompatible_comparison"),
        ("no_ablations", "incomplete_ablations"),
        ("no_effect", "unexplained_ablation_gain"),
        ("no_rollback", "missing_reversibility_evidence"),
        ("wrong_rollback", "missing_reversibility_evidence"),
        ("no_version", "missing_versioned_artifact"),
    ],
)
def test_missing_and_incompatible_evidence_fails_closed(evidence, mutation, reason):
    pairs = evidence["comparison"]["pairs"]
    experiments = evidence["blinded_experiments"]
    if mutation == "no_experiment":
        experiments.clear()
    elif mutation == "no_exposures":
        experiments[0]["promotion_evidence"]["exposures"].clear()
    elif mutation == "unshown":
        experiments[0]["promotion_evidence"]["exposures"][0]["shown_artist_ids"] = [1]
    elif mutation == "reversed":
        experiments[0]["definition"]["source_artifacts"] = {
            "control": deepcopy(pairs[0]["challenger"]["artifact"]),
            "challenger": deepcopy(pairs[0]["control"]["artifact"]),
        }
    elif mutation == "wrong_task":
        experiments[0]["task"] = "artist"
    elif mutation == "wrong_target":
        experiments[0]["definition"]["target_artist_id"] = 42
    elif mutation == "unrelated_artifact":
        experiments[0]["definition"]["source_artifacts"] = deepcopy(
            experiments[0]["definition"]["source_artifacts"]
        )
        experiments[0]["definition"]["source_artifacts"]["challenger"]["artifact_id"] = "other"
    elif mutation == "duplicate_cutoffs":
        evidence["comparison"]["pairs"] = [pairs[0]] * 3
    elif mutation == "empty_outcomes":
        for pair in pairs:
            for name in ("control", "challenger"):
                pair[name]["result"]["graded_metrics"]["ndcg_available"] = False
    elif mutation == "wrong_state":
        pairs[0]["challenger"]["stable_state_id"] = "other"
    elif mutation == "wrong_horizon":
        pairs[0]["challenger"]["result"]["test_until"] = "2025-01-01"
    elif mutation == "no_ablations":
        pairs[0]["ablations"].clear()
    elif mutation == "no_effect":
        for pair in pairs:
            for row in pair["ablations"]:
                row["metrics"] = deepcopy(pair["challenger"])
                row["deltas"] = {}
                row["observed_effect"] = False
    elif mutation == "no_rollback":
        evidence["reversibility"] = None
    elif mutation == "wrong_rollback":
        evidence["reversibility"]["verification"]["restored_artifact"] = pairs[0]["challenger"][
            "artifact"
        ]
    elif mutation == "no_version":
        pairs[0]["challenger"]["artifact"] = deepcopy(pairs[0]["challenger"]["artifact"])
        pairs[0]["challenger"]["artifact"]["version"] = ""
    decision = decide(evidence)
    assert not decision.eligible
    assert reason in decision.reasons


@pytest.mark.parametrize(
    "guard,value,reason",
    [
        ("novelty", 0.49, "guardrail_regression"),
        ("tag_diversity", 0.49, "guardrail_regression"),
        ("catalog_diversity", 0.49, "guardrail_regression"),
        ("family_agreement", 0.49, "guardrail_regression"),
        ("fatigue", 0.51, "guardrail_regression"),
        ("stale_source_rate", 0.51, "guardrail_regression"),
        ("provider_failure_rate", 0.51, "guardrail_regression"),
        ("identity_ambiguous_rate", 0.01, "guardrail_regression"),
        ("popularity_q50", 0.51, "guardrail_regression"),
        ("popularity_q75", 0.51, "guardrail_regression"),
        ("popularity_q90", 0.51, "guardrail_regression"),
        ("identity_resolved_rate", 0.89, "identity_below_minimum"),
        ("provider_concentration", 0.81, "provider_concentration_exceeded"),
    ],
)
def test_each_guardrail_blocks_independently(evidence, guard, value, reason):
    metric = evidence["comparison"]["pairs"][0]["challenger"]["result"]["guardrails"][guard]
    metric["value"] = value
    if metric.get("numerator") is not None:
        metric.update(numerator=round(value * 100), denominator=100, total_eligible_population=100)
    if guard == "provider_concentration":
        metric.update(
            provider_counts={"lastfm": 81, "listenbrainz": 19},
            membership_denominator=100,
            denominator=100,
            total_eligible_population=100,
        )
    assert reason in decide(evidence).reasons


@pytest.mark.parametrize("bad", [None, float("nan"), -1, 0])
def test_runtime_missing_invalid_and_zero_control_never_pass(evidence, bad):
    evidence["comparison"]["pairs"][0]["control"]["runtime"]["total_seconds"] = bad
    decision = decide(evidence)
    assert "missing_runtime_evidence" in decision.reasons
    assert decision.metrics["pairs"][0]["runtime_ratio"] is None
    from scrobble_recs.models.report_json import serialize_report

    assert "NaN" not in serialize_report(decision)


def test_cost_exact_limits_and_signed_storage_savings(evidence):
    pair = evidence["comparison"]["pairs"][0]
    pair["challenger"]["runtime"]["total_seconds"] = 2
    pair["challenger"]["runtime"]["variant_seconds"] = 1.9
    pair["challenger"]["storage"]["durable_bytes"] += 512 * 1048576
    assert decide(evidence).eligible
    pair["challenger"]["storage"]["durable_bytes"] += 1
    assert "storage_limit_exceeded" in decide(evidence).reasons
    pair["challenger"]["storage"]["durable_bytes"] = 0
    assert decide(evidence).eligible
    pair["challenger"]["runtime"]["total_seconds"] = 2.0000001
    pair["challenger"]["runtime"]["variant_seconds"] = 1.9000001
    assert "runtime_limit_exceeded" in decide(evidence).reasons


def test_missing_inputs_collect_all_reasons_in_stable_order():
    from scrobble_recs.models.promotion import promotion_decision

    decision = promotion_decision(config=PromotionConfig())
    assert not decision.eligible
    assert decision.reasons == (
        "incompatible_comparison",
        "insufficient_cutoffs",
        "missing_blinded_evidence",
        "incomplete_guardrails",
        "missing_runtime_evidence",
        "missing_storage_evidence",
        "missing_versioned_artifact",
        "missing_reversibility_evidence",
        "incomplete_ablations",
        "unexplained_ablation_gain",
    )


@pytest.mark.parametrize("kind", ["missing", "partial", "no_provenance", "wrong_snapshot"])
def test_guardrails_require_complete_linked_coverage(evidence, kind):
    metric = evidence["comparison"]["pairs"][0]["challenger"]["result"]["guardrails"]["novelty"]
    if kind == "missing":
        metric["value"] = None
        metric["available"] = False
    elif kind == "partial":
        metric["coverage"] = 0.5
    elif kind == "no_provenance":
        metric["source"] = ""
    else:
        metric["snapshot_id"] = "other"
    assert "incomplete_guardrails" in decide(evidence).reasons


def test_pair_query_identity_cannot_substitute_general_for_artist_or_another_snapshot(evidence):
    pair = evidence["comparison"]["pairs"][0]
    pair["control"]["source_context"] = deepcopy(pair["control"]["source_context"])
    pair["control"]["source_context"]["query_kind"] = "artist"
    assert "incompatible_comparison" in decide(evidence).reasons


@pytest.mark.parametrize("field", ["baseline_bytes", "artifact_bytes", "artifact_files", "version"])
def test_storage_requires_consistent_provenance_and_known_artifact_size(evidence, field):
    row = evidence["comparison"]["pairs"][0]["challenger"]["storage"]
    if field == "baseline_bytes":
        row[field] = 101
    elif field == "artifact_bytes":
        row[field] = 999
    elif field == "artifact_files":
        row[field] = ["fixture-ranker.py", "fixture-ranker.py"]
    else:
        row[field] = "unknown"
    assert "missing_storage_evidence" in decide(evidence).reasons


def test_disabled_catalog_and_unchanged_acquisition_need_explicit_applicability(evidence):
    for pair in evidence["comparison"]["pairs"]:
        for variant in [
            pair["control"],
            pair["challenger"],
            *(row["metrics"] for row in pair["ablations"]),
        ]:
            for guard in ("catalog_diversity", "provider_failure_rate"):
                variant["result"]["guardrails"][guard].update(
                    value=None,
                    available=False,
                    availability="not_applicable",
                    reason="explicit disabled fixture",
                )
        pair["guardrail_applicability"] = {
            "catalog_diversity": {
                "not_applicable": True,
                "basis": "both explicitly disabled",
                "control_enabled": False,
                "challenger_enabled": False,
                "control_contributing": False,
                "challenger_contributing": False,
            },
            "provider_failure_rate": {
                "not_applicable": True,
                "basis": "same code and acquisition inputs; local generation only",
                "control_inputs": {
                    "apis": pair["control"]["artifact"]["behavior_config"]["apis"],
                    "enrichment": {},
                },
                "challenger_inputs": {
                    "apis": pair["challenger"]["artifact"]["behavior_config"]["apis"],
                    "enrichment": {},
                },
                "same_content_fingerprint": True,
                "introduces_acquisition": False,
            },
        }
    assert decide(evidence).eligible
    del evidence["comparison"]["pairs"][0]["guardrail_applicability"]
    assert "incomplete_guardrails" in decide(evidence).reasons


def test_each_artist_target_requires_matching_blinded_evidence_and_no_row_regression(evidence):
    relationship = dict(
        state="unheard",
        play_count=0,
        recency_weighted_play_count=0,
        unique_tracks=0,
        unique_releases=0,
        active_days=0,
        active_months=0,
        first_listen=None,
        last_listen=None,
        listening_span_days=0,
        recent_trend=0,
        replay_concentration=0,
        positive_feedback=0,
        negative_feedback=0,
        prior_recommendation_exposures=0,
    )
    comparison = evidence["comparison"]
    comparison["evaluation_task"] = "artist"
    comparison["target_artist_ids"] = [7, 8]
    comparison["pairs"] = deepcopy(comparison["pairs"]) + deepcopy(comparison["pairs"])
    for index, pair in enumerate(comparison["pairs"]):
        target = 7 if index < 3 else 8
        pair["key"].update(evaluation_task="artist", target_artist_id=target)
        for name in ("control", "challenger"):
            pair[name]["result"].update(evaluation_task="artist", target_artist_id=target)
            pair[name]["source_context"] = deepcopy(pair[name]["source_context"])
            pair[name]["source_context"].update(query_kind="artist", target_artist_id=target)
            pair[name]["source_context"]["target_relationship"] = deepcopy(relationship)
        for row in pair["ablations"]:
            row.update(evaluation_task="artist", target_artist_id=target)
            row["metrics"]["result"].update(evaluation_task="artist", target_artist_id=target)
            row["metrics"]["source_context"].update(query_kind="artist", target_artist_id=target)
            row["metrics"]["source_context"]["target_relationship"] = deepcopy(relationship)
    experiment = evidence["blinded_experiments"][0]
    experiment["task"] = "artist"
    experiment["definition"]["target_artist_id"] = 7
    for context in experiment["definition"]["source_contexts"].values():
        context.update(query_kind="artist", target_artist_id=7)
        context["target_relationship"] = deepcopy(relationship)
    assert "missing_blinded_evidence" in decide(evidence).reasons
    second = deepcopy(experiment)
    second["experiment_id"] = "second-target"
    second["promotion_evidence"]["exposures"][0].update(
        exposure_id="second-target-exposure", judgment_id="second-target-judgment"
    )
    second["definition"]["target_artist_id"] = 8
    for context in second["definition"]["source_contexts"].values():
        context.update(target_artist_id=8)
    evidence["blinded_experiments"].append(second)
    assert decide(evidence).eligible
    comparison["pairs"][5]["challenger"]["result"]["graded_ndcg_at_25"] = 0
    assert "graded_ndcg_regression" in decide(evidence).reasons


def test_nonfinite_runtime_ratio_remains_finite_error_report(evidence):
    from scrobble_recs.models.report_json import serialize_report

    row = evidence["comparison"]["pairs"][0]
    row["control"]["runtime"]["total_seconds"] = 5e-324
    row["challenger"]["runtime"]["total_seconds"] = 1e308
    row["control"]["runtime"].update(variant_seconds=5e-324, shared_capture_seconds=0)
    row["challenger"]["runtime"].update(variant_seconds=1e308, shared_capture_seconds=0)
    decision = decide(evidence)
    assert "invalid_evidence" in decision.reasons
    assert decision.metrics["pairs"][0]["runtime_ratio"] is None
    assert "Infinity" not in serialize_report(decision)


def test_both_source_contexts_cannot_claim_the_wrong_query_kind(evidence):
    for name in ("control", "challenger"):
        evidence["comparison"]["pairs"][0][name]["source_context"] = deepcopy(
            evidence["comparison"]["pairs"][0][name]["source_context"]
        )
        evidence["comparison"]["pairs"][0][name]["source_context"]["query_kind"] = "artist"
    assert "incompatible_comparison" in decide(evidence).reasons


def test_ablation_timing_noise_cannot_explain_unchanged_quality(evidence):
    for pair in evidence["comparison"]["pairs"]:
        pair["control"]["result"]["runtime_seconds"] = 1
        pair["challenger"]["result"]["runtime_seconds"] = 2
        for row in pair["ablations"]:
            row["metrics"]["result"] = deepcopy(pair["challenger"]["result"])
            row["metrics"]["result"]["runtime_seconds"] = 1
    assert "unexplained_ablation_gain" in decide(evidence).reasons


def test_duplicate_measured_replays_do_not_conflict_only_because_of_run_ids_or_timing(evidence):
    pair = deepcopy(evidence["comparison"]["pairs"][0])
    for name in ("control", "challenger"):
        pair[name]["run_id"] += "-second-measurement"
        pair[name]["runtime"]["total_seconds"] += 0.01
        pair[name]["runtime"]["variant_seconds"] += 0.01
    evidence["comparison"]["pairs"].append(pair)
    decision = decide(evidence)
    assert decision.eligible
    assert decision.metrics["distinct_supported_cutoffs"] == 3


def test_duplicate_winning_experiment_cannot_turn_two_control_votes_into_tie(evidence):
    control = evidence["blinded_experiments"][0]
    first = control["promotion_evidence"]["exposures"][0]
    first["effective_list_choice"] = "right"
    second = deepcopy(first)
    second.update(exposure_id="control-2", judgment_id="control-judgment-2")
    control["promotion_evidence"]["exposures"].append(second)
    winning = deepcopy(control)
    winning["experiment_id"] = "one-challenger-vote"
    winning["promotion_evidence"]["exposures"] = [
        {
            **deepcopy(first),
            "exposure_id": "challenger-1",
            "judgment_id": "challenger-judgment-1",
            "effective_list_choice": "left",
        }
    ]
    evidence["blinded_experiments"].append(winning)
    assert "blinded_loss" in decide(evidence).reasons
    evidence["blinded_experiments"].append(deepcopy(winning))
    decision = decide(evidence)
    assert "blinded_loss" in decision.reasons
    assert decision.metrics["blinded_votes"]["challenger"] == 1
    assert decision.metrics["blinded_votes"]["denominator"] == 3


@pytest.mark.parametrize("collision", ["experiment", "exposure", "judgment"])
def test_conflicting_blinded_identity_ownership_is_invalid(evidence, collision):
    original = evidence["blinded_experiments"][0]
    duplicate = deepcopy(original)
    duplicate["experiment_id"] = (
        "different-experiment" if collision != "experiment" else original["experiment_id"]
    )
    exposure = duplicate["promotion_evidence"]["exposures"][0]
    exposure.update(effective_list_choice="left")
    if collision != "exposure":
        exposure["exposure_id"] = "different-exposure"
    if collision != "judgment":
        exposure["judgment_id"] = "different-judgment"
    evidence["blinded_experiments"].append(duplicate)
    decision = decide(evidence)
    assert "invalid_evidence" in decision.reasons
    assert decision.metrics["blinded_votes"]["denominator"] == 0


@pytest.mark.parametrize(
    "contradiction",
    [
        "claimed_full",
        "missing_total",
        "wrong_numerator",
        "missing_numerator",
        "catalog_namespace",
        "population",
    ],
)
def test_guardrail_support_and_definition_must_be_internally_consistent(evidence, contradiction):
    guards = evidence["comparison"]["pairs"][0]["challenger"]["result"]["guardrails"]
    metric = guards["novelty"]
    if contradiction == "claimed_full":
        metric.update(denominator=1, total_eligible_population=100, coverage=1)
    elif contradiction == "missing_total":
        del metric["total_eligible_population"]
    elif contradiction == "wrong_numerator":
        metric["numerator"] = 0
    elif contradiction == "missing_numerator":
        del metric["numerator"]
    elif contradiction == "catalog_namespace":
        guards["catalog_diversity"]["namespace"] = "artist_type"
    else:
        metric["population"] = "a different population"
    assert "incomplete_guardrails" in decide(evidence).reasons


@pytest.mark.parametrize(
    "missing",
    [
        "scalar_only",
        "guardrails",
        "runtime",
        "storage",
        "candidate_count_by_source",
        "graded_metrics",
    ],
)
def test_ablation_requires_complete_measured_sections(evidence, missing):
    for pair in evidence["comparison"]["pairs"]:
        for row in pair["ablations"]:
            if missing == "scalar_only":
                row["metrics"] = {"result": {"graded_ndcg_at_50": 0.5}}
            elif missing in ("runtime", "storage"):
                row["metrics"].pop(missing)
            else:
                row["metrics"]["result"].pop(missing)
    decision = decide(evidence)
    assert "incomplete_ablations" in decision.reasons
    assert "unexplained_ablation_gain" in decision.reasons


@pytest.mark.parametrize(
    "mismatch",
    [
        "empty_removal",
        "kind",
        "run",
        "config",
        "artifact",
        "state",
        "snapshot",
        "query",
        "window",
        "disabled",
        "noncontributing",
        "unsupported",
        "deltas",
    ],
)
def test_ablation_underlying_variant_and_status_must_match_the_expected_removal(evidence, mismatch):
    pair = evidence["comparison"]["pairs"][0]
    row = pair["ablations"][0]
    actual = row["metrics"]
    if mismatch == "empty_removal":
        row["removed_inputs"] = []
    elif mismatch == "kind":
        row["kind"] = "signal"
    elif mismatch == "run":
        actual["run_id"] = pair["challenger"]["run_id"]
    elif mismatch == "config":
        actual["artifact"]["behavior_config"]["recommendation"]["scoring"]["seed_idf_alpha"] = 0.5
        actual["artifact"]["behavior_config_fingerprint"] = sha256(
            json.dumps(
                actual["artifact"]["behavior_config"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    elif mismatch == "artifact":
        actual["artifact"]["content_fingerprint"] = "unrelated-code"
    elif mismatch == "state":
        actual["stable_state_id"] = "other-state"
    elif mismatch == "snapshot":
        actual["result"]["evidence_snapshot_id"] = "other-snapshot"
    elif mismatch == "query":
        actual["source_context"]["query_kind"] = "artist"
    elif mismatch == "window":
        actual["result"]["test_until"] = "2099-01-01"
    elif mismatch == "disabled":
        row.update(enabled=False, contributing=False, status="disabled")
    elif mismatch == "noncontributing":
        row.update(contributing=False, status="noncontributing")
    elif mismatch == "unsupported":
        row["status"] = "unsupported"
    else:
        row["deltas"] = {name: 0 for name in row["deltas"]}
    assert "incomplete_ablations" in decide(evidence).reasons


@pytest.mark.parametrize(
    "changed_definition", [None, "namespace", "population", "source", "basis", "version", "k"]
)
def test_only_comparable_ablation_guards_can_explain_a_catalog_diversity_gain(
    evidence, changed_definition
):
    for pair in evidence["comparison"]["pairs"]:
        pair["control"]["result"]["graded_ndcg_at_50"] = 0.6
        pair["control"]["result"]["guardrails"]["catalog_diversity"].update(
            value=0, token_counts={"one": 2}
        )
        for row in pair["ablations"]:
            row["metrics"]["result"]["graded_ndcg_at_50"] = 0.6
            row["deltas"]["graded_ndcg_at_50"] = 0
            if row["contributing"]:
                metric = row["metrics"]["result"]["guardrails"]["catalog_diversity"]
                metric.update(value=0, token_counts={"one": 2})
                if changed_definition is not None:
                    metric[changed_definition] = (
                        50
                        if changed_definition == "k"
                        else "artist_type"
                        if changed_definition == "namespace"
                        else "different-definition"
                    )
                row.update(guardrail_effect=True, observed_effect=True)
    decision = decide(evidence)
    if changed_definition is None:
        assert decision.eligible  # Like-for-like catalog evidence is the sole gain/explanation.
    else:
        assert not decision.eligible
        assert "incomplete_ablations" in decision.reasons
        assert "unexplained_ablation_gain" in decision.reasons


@pytest.mark.parametrize("scope", ["pair", "ablation"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("variant_seconds", None),
        ("variant_seconds", -1),
        ("variant_seconds", 1000),
        ("variant_seconds", float("nan")),
        ("variant_seconds", float("inf")),
        ("shared_capture_seconds", None),
        ("shared_capture_seconds", -1),
        ("total_seconds", 100),
        ("total_seconds", 0.1),
    ],
)
def test_runtime_components_must_be_complete_finite_and_consistent(evidence, scope, field, value):
    pair = evidence["comparison"]["pairs"][0]
    variant = pair["challenger"] if scope == "pair" else pair["ablations"][0]["metrics"]
    if value is None:
        variant["runtime"].pop(field)
    else:
        variant["runtime"][field] = value
    decision = decide(evidence)
    assert not decision.eligible
    assert (
        "missing_runtime_evidence" if scope == "pair" else "incomplete_ablations"
    ) in decision.reasons
    if scope == "pair":
        assert decision.metrics["pairs"][0]["runtime_ratio"] is None


def test_runtime_accounting_roundoff_never_relaxes_the_actual_cost_limit(evidence):
    pair = evidence["comparison"]["pairs"][0]
    pair["challenger"]["runtime"].update(total_seconds=2, variant_seconds=1.9)
    assert decide(evidence).eligible
    # The declared total differs by one ordinary ULP; components exceed the strict 2x limit.
    pair["challenger"]["runtime"]["variant_seconds"] = 2.0000000000000004 - 0.1
    decision = decide(evidence)
    assert "runtime_limit_exceeded" in decision.reasons
    assert "missing_runtime_evidence" not in decision.reasons


def test_runtime_accepts_ordinary_component_sum_roundoff(evidence):
    pair = evidence["comparison"]["pairs"][0]
    pair["control"]["runtime"].update(total_seconds=0.3, variant_seconds=0.2)
    pair["challenger"]["runtime"].update(total_seconds=0.6, variant_seconds=0.5)
    assert decide(evidence).eligible


@pytest.mark.parametrize(
    "mutation",
    [
        "future_cutoffs",
        "missing_history_cutoff",
        "missing_feedback_cutoff",
        "missing_history_window",
        "missing_evidence_freshness",
        "missing_taste_region_seeds",
        "null_history_cutoff",
        "malformed_cutoff",
        "numeric_cutoff",
        "future_window_end",
        "old_window_end",
        "missing_window_start",
        "malformed_window_start",
        "reversed_window",
        "stripped_context",
    ],
)
def test_retrospective_contexts_must_be_complete_and_bound_to_the_training_cutoff(
    evidence, mutation
):
    for pair in evidence["comparison"]["pairs"]:
        variants = [
            pair["control"],
            pair["challenger"],
            *(row["metrics"] for row in pair["ablations"]),
        ]
        for variant in variants:
            context = variant["source_context"]
            if mutation == "future_cutoffs":
                context.update(
                    history_cutoff="2099-01-01T00:00:00Z", feedback_cutoff="2099-01-01T00:00:00Z"
                )
            elif mutation.startswith("missing_window"):
                context["history_window"].pop("start")
            elif mutation.startswith("missing_"):
                context.pop(mutation.removeprefix("missing_"))
            elif mutation == "null_history_cutoff":
                context["history_cutoff"] = None
            elif mutation == "malformed_cutoff":
                context["feedback_cutoff"] = "tomorrow"
            elif mutation == "numeric_cutoff":
                context["feedback_cutoff"] = 0
            elif mutation == "future_window_end":
                context["history_window"]["end"] = "2099-01-01"
            elif mutation == "old_window_end":
                context["history_window"]["end"] = "2023-12-01"
            elif mutation == "malformed_window_start":
                context["history_window"]["start"] = "not-a-date"
            elif mutation == "reversed_window":
                context["history_window"]["start"] = "2099-01-01"
            else:
                for key in (
                    "history_cutoff",
                    "feedback_cutoff",
                    "history_window",
                    "evidence_freshness",
                    "taste_region_seeds",
                ):
                    context.pop(key)
    decision = decide(evidence)
    assert not decision.eligible
    assert "incompatible_comparison" in decision.reasons
    assert "incomplete_ablations" in decision.reasons
    assert decision.metrics["distinct_supported_cutoffs"] == 0


def test_context_timestamp_spelling_normalizes_and_prospective_cutoffs_stay_independent(evidence):
    from datetime import UTC, timezone

    for pair in evidence["comparison"]["pairs"]:
        contexts = [
            pair["control"]["source_context"],
            *(row["metrics"]["source_context"] for row in pair["ablations"]),
        ]
        for context in contexts:
            for key in ("history_cutoff", "feedback_cutoff"):
                context[key] = (
                    datetime.fromisoformat(context[key])
                    .replace(tzinfo=UTC)
                    .astimezone(timezone(timedelta(hours=5)))
                    .isoformat()
                )
            for key in ("start", "end"):
                context["history_window"][key] = (
                    datetime.fromisoformat(context["history_window"][key])
                    .replace(tzinfo=UTC)
                    .astimezone(timezone(timedelta(hours=-7)))
                    .isoformat()
                )
    for context in evidence["blinded_experiments"][0]["definition"]["source_contexts"].values():
        context.update(
            history_cutoff="2026-01-01",
            feedback_cutoff="2026-01-01",
            history_window={"start": "2025-01-01", "end": "2026-01-01"},
        )
    assert decide(evidence).eligible


def test_prospective_blinded_contexts_need_complete_bounds_without_a_retrospective_binding(
    evidence,
):
    for context in evidence["blinded_experiments"][0]["definition"]["source_contexts"].values():
        context.pop("history_cutoff", None)
    assert "incompatible_blinded_evidence" in decide(evidence).reasons


def test_ablation_context_cannot_move_beyond_an_otherwise_valid_replay_cutoff(evidence):
    context = evidence["comparison"]["pairs"][0]["ablations"][0]["metrics"]["source_context"]
    context.update(history_cutoff="2099-01-01", feedback_cutoff="2099-01-01")
    decision = decide(evidence)
    assert "incomplete_ablations" in decision.reasons
    assert "incompatible_comparison" not in decision.reasons
    assert decision.metrics["distinct_supported_cutoffs"] == 3
