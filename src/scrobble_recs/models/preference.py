"""Versioned local Bradley–Terry utilities, trained only on explicit displayed pairs.

Objective: mean(logaddexp(0, -Xw)) + lambda * ||w||² / 2. Intercept is
zero because a per-artist intercept cancels. L-BFGS-B starts at zero, never
samples, and canonical coefficients round to 12 decimals (negative zero -> zero).
The v1 transform supports fusion-features-v1 only. Provider contributions are
excluded; four already-capped family aggregates are clipped to [0,1]. Missing
families structurally mean no membership; required base features never default.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from hashlib import sha256
from math import isfinite, log1p
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from duckdb import DuckDBPyConnection
from scipy.optimize import minimize
from scipy.special import expit

from scrobble_recs.config import ScrobbleRecsConfig
from scrobble_recs.models.experiments import validate_source_context
from scrobble_recs.models.profile_window import normalize_timestamp
from scrobble_recs.models.recommendation_query import QueryContext, QueryKind

if TYPE_CHECKING:
    from scrobble_recs.models.ranking import RankedCandidate

Scope = Literal["shared", "general", "artist"]
FEATURE_SCHEMA = "preference-features-v1"
TRANSFORM = "fusion-family-cap-context-v1"
BASE_FEATURES = ("tag_similarity_score", "feedback_score", "identity_confidence", "knownness")
FAMILIES = ("collaborative", "semantic", "catalog", "personal")
FEATURE_NAMES = (
    *BASE_FEATURES,
    *FAMILIES,
    *(f"artist:{key}" for key in (*BASE_FEATURES, *FAMILIES)),
    "target_depth:tag",
    "target_trend:tag",
)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def _finite(value: Any) -> float:
    if type(value) not in (int, float) or not isfinite(value):
        raise ValueError("invalid_numeric_feature")
    return float(value)


def encode_features(record: dict[str, Any], context: dict[str, Any]) -> tuple[float, ...]:
    if record.get("schema_version") != "fusion-features-v1":
        raise ValueError("incompatible_source_schema")
    values = record["values"]
    base = [max(-1.0, min(1.0, _finite(values.get(key)))) for key in BASE_FEATURES]
    base += [max(0.0, min(1.0, _finite(values.get(f"fusion_family_{key}", 0)))) for key in FAMILIES]
    kind = context.get("query_kind")
    if kind not in ("general", "artist"):
        raise ValueError("invalid_query_kind")
    depth = trend = 0.0
    if kind == "artist":
        relationship = context.get("target_relationship") or {}
        count = _finite(relationship.get("play_count"))
        if count < 0 or type(context.get("target_artist_id")) is not int:
            raise ValueError("invalid_target_context")
        depth = min(1.0, log1p(count) / log1p(1000))
        trend = max(-1.0, min(1.0, _finite(relationship.get("recent_trend"))))
    return tuple(
        base
        + [value if kind == "artist" else 0.0 for value in base]
        + [depth * base[0], trend * base[0]]
    )


@dataclass(frozen=True)
class PairwiseExample:
    exposure_id: str
    preferred_artist_id: int
    other_artist_id: int
    feature_difference: tuple[float, ...]
    judged_at: datetime
    query_kind: QueryKind
    control_difference: float = 0.0
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreferenceModelArtifact:
    artifact_id: str
    query_scope: Scope
    feature_schema_version: str
    feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float
    l2_regularization: float
    training_cutoff: datetime
    validation_metrics: dict[str, float]
    artifact_hash: str
    training_provenance: dict[str, Any]


def _example_json(example: PairwiseExample) -> dict[str, Any]:
    return {**asdict(example), "judged_at": normalize_timestamp(example.judged_at).isoformat()}


def _ordered(examples: Sequence[PairwiseExample]) -> tuple[PairwiseExample, ...]:
    return tuple(
        sorted(
            examples,
            key=lambda x: (
                normalize_timestamp(x.judged_at),
                x.exposure_id,
                x.preferred_artist_id,
                x.other_artist_id,
            ),
        )
    )


def _content(artifact: PreferenceModelArtifact) -> dict[str, Any]:
    return {
        "scope": artifact.query_scope,
        "schema": artifact.feature_schema_version,
        "features": artifact.feature_names,
        "transform": TRANSFORM,
        "coefficients": artifact.coefficients,
        "intercept": artifact.intercept,
        "regularization": artifact.l2_regularization,
        "cutoff": normalize_timestamp(artifact.training_cutoff).isoformat(),
        "validation_metrics": artifact.validation_metrics,
        "provenance": artifact.training_provenance,
    }


def train_pairwise_ranker(
    examples: Sequence[PairwiseExample],
    *,
    feature_schema_version: str,
    feature_names: Sequence[str],
    l2_regularization: float,
    training_cutoff: datetime,
    query_scope: Scope = "shared",
    training_provenance: dict[str, Any] | None = None,
) -> PreferenceModelArtifact:
    from dataclasses import replace

    if feature_schema_version != FEATURE_SCHEMA or tuple(feature_names) != FEATURE_NAMES:
        raise ValueError("incompatible_feature_schema")
    if query_scope not in ("shared", "general", "artist"):
        raise ValueError("invalid_scope")
    if _finite(l2_regularization) <= 0 or not examples:
        raise ValueError("positive regularization and training pairs required")
    ordered = _ordered(examples)
    cutoff = normalize_timestamp(training_cutoff)
    for example in ordered:
        if (
            len(example.feature_difference) != len(feature_names)
            or normalize_timestamp(example.judged_at) > cutoff
        ):
            raise ValueError("invalid_training_example")
        for value in example.feature_difference:
            if abs(_finite(value)) > 2:
                raise ValueError("feature_difference_out_of_bounds")
    matrix = np.asarray([example.feature_difference for example in ordered], dtype=float)

    def objective(weights: Any) -> tuple[float, Any]:
        logits = matrix @ weights
        loss = float(
            np.mean(np.logaddexp(0, -logits)) + l2_regularization * (weights @ weights) / 2
        )
        gradient = -(matrix.T @ expit(-logits)) / len(matrix) + l2_regularization * weights
        return loss, gradient

    fitted = minimize(
        objective,
        np.zeros(len(feature_names)),
        jac=True,
        method="L-BFGS-B",
        options={"maxiter": 1000, "ftol": 1e-12, "gtol": 1e-10, "maxls": 50},
    )
    if not fitted.success:
        raise ValueError("optimizer_failed")
    coefficients = tuple(round(_finite(float(value)), 12) or 0.0 for value in fitted.x)
    _finite(float(fitted.fun))
    provenance = {
        "corpus_hash": _hash([_example_json(x) for x in ordered]),
        "training_count": len(ordered),
        **(training_provenance or {}),
    }
    artifact = PreferenceModelArtifact(
        "",
        query_scope,
        FEATURE_SCHEMA,
        tuple(feature_names),
        coefficients,
        0.0,
        float(l2_regularization),
        cutoff,
        {},
        "",
        provenance,
    )
    digest = _hash(_content(artifact))
    return replace(artifact, artifact_id=digest, artifact_hash=digest)


def pair_probability(artifact: PreferenceModelArtifact, difference: Sequence[float]) -> float:
    return float(expit(sum(w * x for w, x in zip(artifact.coefficients, difference, strict=True))))


def split_examples(
    examples: Sequence[PairwiseExample], *, validation_count: int
) -> tuple[tuple[PairwiseExample, ...], tuple[PairwiseExample, ...]]:
    if validation_count < 1:
        raise ValueError("positive validation_count required")
    ordered = _ordered(examples)
    if not ordered:
        return (), ()
    boundary = normalize_timestamp(ordered[max(0, len(ordered) - validation_count)].judged_at)
    validation_exposures = {
        x.exposure_id for x in ordered if normalize_timestamp(x.judged_at) >= boundary
    }
    return (
        tuple(x for x in ordered if x.exposure_id not in validation_exposures),
        tuple(x for x in ordered if x.exposure_id in validation_exposures),
    )


def validation_metrics(
    artifact: PreferenceModelArtifact, examples: Sequence[PairwiseExample]
) -> dict[str, float]:
    result: dict[str, float] = {}
    for name in ("control", "learned"):
        logits = [
            x.control_difference
            if name == "control"
            else sum(
                w * v for w, v in zip(artifact.coefficients, x.feature_difference, strict=True)
            )
            for x in _ordered(examples)
        ]
        if not logits:
            raise ValueError("validation_pairs_required")
        for value in logits:
            _finite(value)
        result[f"{name}_log_loss"] = float(np.mean(np.logaddexp(0, -np.asarray(logits))))
        result[f"{name}_accuracy"] = sum(
            1 if v > 0 else 0.5 if v == 0 else 0 for v in logits
        ) / len(logits)
    return result


def _explicit_pairs(
    shown: list[dict[str, Any]], preferred: str | None, choice: str | None, labels: dict[str, Any]
) -> set[tuple[int, int]]:
    sides = {
        side: [item["artist_id"] for item in shown if item["side"] == side]
        for side in ("left", "right")
    }
    pairs: set[tuple[int, int]] = set()
    if len(sides["left"]) == len(sides["right"]) == 1:
        votes = (preferred, choice) if preferred in (None, "left", "right") else ()
        for vote in votes:
            if vote in sides:
                pairs.add((sides[vote][0], sides["right" if vote == "left" else "left"][0]))
    positives, negatives = set(), set()
    for item in shown:
        artist = item["artist_id"]
        values = set(labels.get(str(artist), []))
        positive = bool(values & {"want_to_try", "strong_fit", "surprising_good"})
        negative = bool(values & {"wrong_direction", "reject"})
        if positive and not negative:
            positives.add(artist)
        if negative and not positive:
            negatives.add(artist)
    pairs.update((left, right) for left in positives for right in negatives)
    return {(left, right) for left, right in pairs if left != right and (right, left) not in pairs}


def _source_features(
    conn: DuckDBPyConnection,
    item: dict[str, Any],
    attribution: dict[str, Any],
    definition: dict[str, Any],
    exposure_time: datetime,
    task: str,
) -> tuple[tuple[float, ...], float, str, dict[str, Any]]:
    variant = definition["side_variants"][item["side"]]
    memberships = [m for m in attribution[str(item["artist_id"])] if m["variant"] == variant]
    if len(memberships) != 1:
        raise ValueError("ambiguous_display_source")
    run = memberships[0]["run_id"]
    if run != definition["source_run_ids"][variant]:
        raise ValueError("wrong_source_run")
    row = conn.execute(
        "SELECT r.created_at, r.query_context, c.feature_record, c.relevance_score, c.evidence "
        "FROM recommendation_run r JOIN recommendation_candidate c USING (run_id) "
        "WHERE r.run_id = ? AND c.artist_id = ?",
        [run, item["artist_id"]],
    ).fetchall()
    if len(row) != 1:
        raise ValueError("missing_or_ambiguous_source_features")
    created, raw_context, raw_features, control, evidence = row[0]
    context = json.loads(raw_context)
    validate_source_context(context)
    frozen = definition["source_contexts"][variant]
    if (
        created is None
        or created > exposure_time
        or context != frozen
        or context.get("query_kind") != task
        or context.get("target_artist_id") != definition.get("target_artist_id")
    ):
        raise ValueError("invalid_source_context_or_time")
    # Learned source runs retain the exact deterministic comparison utility.
    diagnostics = json.loads(evidence or "{}").get("preference", {})
    if diagnostics.get("used"):
        control = diagnostics["control_score"]
    record = json.loads(raw_features)
    if context.get("versions", {}).get("feature_schema") != record.get("schema_version"):
        raise ValueError("incompatible_source_schema")
    return encode_features(record, context), _finite(control), run, context


def build_pairwise_examples(
    conn: DuckDBPyConnection,
    *,
    training_cutoff: datetime,
    feature_names: Sequence[str],
    diagnostics: list[dict[str, Any]] | None = None,
) -> tuple[PairwiseExample, ...]:
    if tuple(feature_names) != FEATURE_NAMES:
        raise ValueError("incompatible_feature_order")
    rows = conn.execute(
        "WITH effective AS (SELECT *, row_number() OVER (PARTITION BY exposure_id "
        "ORDER BY created_at DESC, judgment_id DESC) AS position FROM recommendation_judgment "
        "WHERE created_at <= ?) SELECT j.exposure_id, j.judgment_id, j.created_at, "
        "j.preferred_side, j.list_choice, j.labels, e.created_at, e.shown_items, e.attribution, "
        "x.task, x.definition, e.context FROM effective j JOIN recommendation_exposure e "
        "USING (exposure_id) JOIN recommendation_experiment x USING (experiment_id) "
        "WHERE j.position = 1 ORDER BY j.created_at, j.exposure_id",
        [normalize_timestamp(training_cutoff)],
    ).fetchall()
    result = []
    for (
        eid,
        jid,
        judged,
        preferred,
        choice,
        labels,
        created,
        shown,
        attribution,
        task,
        definition,
        exposure_context,
    ) in rows:
        try:
            definition, shown, attribution = (
                json.loads(definition),
                json.loads(shown),
                json.loads(attribution),
            )
            context = json.loads(exposure_context)
            if (
                created > judged
                or task not in ("general", "artist")
                or context.get("task") != task
                or context.get("target_artist_id") != definition.get("target_artist_id")
                or len({x["artist_id"] for x in shown}) != len(shown)
            ):
                raise ValueError("invalid_exposure_context_or_time")
            pairs = _explicit_pairs(shown, preferred, choice, json.loads(labels))
            by_id = {item["artist_id"]: item for item in shown}
            if not pairs and diagnostics is not None:
                diagnostics.append(
                    {"exposure_id": str(eid), "reason": "no_unambiguous_explicit_pair"}
                )
            for left, right in sorted(pairs):
                try:
                    a, b = [
                        _source_features(conn, by_id[aid], attribution, definition, created, task)
                        for aid in (left, right)
                    ]
                    result.append(
                        PairwiseExample(
                            str(eid),
                            left,
                            right,
                            tuple(x - y for x, y in zip(a[0], b[0], strict=True)),
                            judged,
                            task,
                            a[1] - b[1],
                            {
                                "judgment_id": str(jid),
                                "source_runs": [a[2], b[2]],
                                "source_schema": "fusion-features-v1",
                                "context_hashes": [_hash(a[3]), _hash(b[3])],
                            },
                        )
                    )
                except (ValueError, KeyError, TypeError, OverflowError):
                    if diagnostics is not None:
                        diagnostics.append(
                            {
                                "exposure_id": str(eid),
                                "artists": [left, right],
                                "reason": "invalid_source_features_or_attribution",
                            }
                        )
        except (ValueError, KeyError, TypeError, OverflowError):
            if diagnostics is not None:
                diagnostics.append({"exposure_id": str(eid), "reason": "invalid_exposure"})
    return _ordered(result)


def _audit(
    conn: DuckDBPyConnection,
    action: str,
    scope: str,
    artifact_id: str | None,
    details: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO preference_lifecycle_event (action, query_scope, artifact_id, details) "
        "VALUES (?, ?, ?, ?)",
        [action, scope, artifact_id, canonical(details)],
    )


def _counts(examples: Sequence[PairwiseExample]) -> dict[str, int]:
    return {kind: sum(x.query_kind == kind for x in examples) for kind in ("general", "artist")}


def train_preference(
    conn: DuckDBPyConnection,
    *,
    config: ScrobbleRecsConfig,
    training_cutoff: datetime,
    scope: Scope = "shared",
) -> PreferenceModelArtifact:
    from dataclasses import replace

    settings = config.recommendation.preference
    skipped: list[dict[str, Any]] = []
    examples = build_pairwise_examples(
        conn, training_cutoff=training_cutoff, feature_names=FEATURE_NAMES, diagnostics=skipped
    )
    if scope not in ("shared", "general", "artist"):
        raise ValueError("invalid_scope")
    counts = {}
    for kind in ("general", "artist"):
        a, b = split_examples(
            [x for x in examples if x.query_kind == kind],
            validation_count=settings.minimum_validation_pairs,
        )
        counts[kind] = {"training": len(a), "validation": len(b)}
    if scope != "shared":
        if any(
            c["training"] < settings.minimum_training_pairs
            or c["validation"] < settings.minimum_validation_pairs
            for c in counts.values()
        ):
            raise ValueError(
                f"separate scopes require independent counts {canonical(counts)}; train shared"
            )
        examples = tuple(x for x in examples if x.query_kind == scope)
    train, valid = split_examples(examples, validation_count=settings.minimum_validation_pairs)
    if (
        len(train) < settings.minimum_training_pairs
        or len(valid) < settings.minimum_validation_pairs
    ):
        raise ValueError(f"insufficient_pairs: training={len(train)}, validation={len(valid)}")
    provenance = {
        "split_hash": _hash([[_example_json(x) for x in part] for part in (train, valid)]),
        "training_exposures": sorted({x.exposure_id for x in train}),
        "validation_exposures": sorted({x.exposure_id for x in valid}),
        "validation_boundary": min(x.judged_at for x in valid).isoformat(),
        "validation_count": len(valid),
        "per_kind_counts": counts,
        "training_per_kind": _counts(train),
        "validation_per_kind": _counts(valid),
        "scope": scope,
        "skipped": skipped,
        "minimum_training_pairs": settings.minimum_training_pairs,
        "minimum_validation_pairs": settings.minimum_validation_pairs,
    }
    artifact = train_pairwise_ranker(
        train,
        feature_schema_version=FEATURE_SCHEMA,
        feature_names=FEATURE_NAMES,
        l2_regularization=settings.l2_regularization,
        training_cutoff=training_cutoff,
        query_scope=scope,
        training_provenance=provenance,
    )
    artifact = replace(artifact, validation_metrics=validation_metrics(artifact, valid))
    digest = _hash(_content(artifact))
    artifact = replace(artifact, artifact_id=digest, artifact_hash=digest)
    decision = {"eligible": False, "reasons": ["missing_promotion_evidence"]}
    conn.execute("BEGIN TRANSACTION")
    try:
        if conn.execute(
            "SELECT 1 FROM preference_model_artifact WHERE artifact_id = ?", [artifact.artifact_id]
        ).fetchone():
            raise ValueError("artifact_already_exists: " + artifact.artifact_id)
        conn.execute(
            "INSERT INTO preference_model_artifact (artifact_id, query_scope, "
            "feature_schema_version, feature_names, training_cutoff, coefficients, intercept, "
            "regularization, training_example_count, validation_example_count, validation_metrics, "
            "promotion_decision, status, artifact_hash, training_provenance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)",
            [
                artifact.artifact_id,
                scope,
                FEATURE_SCHEMA,
                canonical(FEATURE_NAMES),
                artifact.training_cutoff,
                canonical(artifact.coefficients),
                0,
                artifact.l2_regularization,
                len(train),
                len(valid),
                canonical(artifact.validation_metrics),
                canonical(decision),
                artifact.artifact_hash,
                canonical(artifact.training_provenance),
            ],
        )
        _audit(conn, "create", scope, artifact.artifact_id, decision)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return artifact


def preference_report(conn: DuckDBPyConnection, artifact_id: str) -> dict[str, Any]:
    cursor = conn.execute(
        "SELECT * FROM preference_model_artifact WHERE artifact_id = ?", [artifact_id]
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError("artifact_not_found")
    result = dict(zip([column[0] for column in cursor.description], row, strict=True))
    for name in (
        "feature_names",
        "coefficients",
        "validation_metrics",
        "promotion_decision",
        "training_provenance",
    ):
        result[name] = json.loads(result[name])
    for name in ("created_at", "training_cutoff"):
        result[name] = result[name].isoformat()
    result["transform"] = TRANSFORM
    result["lifecycle"] = [
        {"action": action, "created_at": when.isoformat(), "details": json.loads(details)}
        for action, when, details in conn.execute(
            "SELECT action, created_at, details FROM preference_lifecycle_event "
            "WHERE artifact_id = ? OR json_contains(details->'retired', to_json(?::VARCHAR)) "
            "OR json_contains(details->'replaced', to_json(?::VARCHAR)) "
            "ORDER BY created_at, event_id",
            [artifact_id, artifact_id, artifact_id],
        ).fetchall()
    ]
    canonical(result)
    return result


def load_preference_artifact(conn: DuckDBPyConnection, artifact_id: str) -> PreferenceModelArtifact:
    try:
        row = preference_report(conn, artifact_id)
        artifact = PreferenceModelArtifact(
            row["artifact_id"],
            row["query_scope"],
            row["feature_schema_version"],
            tuple(row["feature_names"]),
            tuple(_finite(x) for x in row["coefficients"]),
            _finite(row["intercept"]),
            _finite(row["regularization"]),
            datetime.fromisoformat(row["training_cutoff"]),
            row["validation_metrics"],
            row["artifact_hash"],
            row["training_provenance"],
        )
        provenance = artifact.training_provenance
        if (
            artifact.query_scope not in ("shared", "general", "artist")
            or artifact.feature_schema_version != FEATURE_SCHEMA
            or artifact.feature_names != FEATURE_NAMES
            or len(artifact.coefficients) != len(FEATURE_NAMES)
            or artifact.intercept != 0
            or artifact.l2_regularization <= 0
            or row["status"] not in ("candidate", "active", "retired")
            or row["training_example_count"] != provenance["training_count"]
            or row["validation_example_count"] != provenance["validation_count"]
            or row["training_example_count"] < provenance["minimum_training_pairs"]
            or row["validation_example_count"] < provenance["minimum_validation_pairs"]
            or artifact.artifact_hash != _hash(_content(artifact))
            or artifact.artifact_id != artifact.artifact_hash
        ):
            raise ValueError("invalid_artifact")
        for name in ("control", "learned"):
            if not 0 <= _finite(artifact.validation_metrics[f"{name}_accuracy"]) <= 1:
                raise ValueError("invalid_validation_accuracy")
            if _finite(artifact.validation_metrics[f"{name}_log_loss"]) < 0:
                raise ValueError("invalid_validation_loss")
        return artifact
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("invalid_artifact") from exc


def preference_identity(artifact: PreferenceModelArtifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_hash": artifact.artifact_hash,
        "feature_schema_version": artifact.feature_schema_version,
        "feature_names": list(artifact.feature_names),
        "transform": TRANSFORM,
        "query_scope": artifact.query_scope,
    }


def _promotion_assessment(
    artifact: PreferenceModelArtifact, config: ScrobbleRecsConfig, evidence: dict[str, Any]
) -> dict[str, Any]:
    """Malformed local evidence is an ineligible attestation, never a runtime scorer failure."""
    try:
        return _assess_promotion(artifact, config, evidence)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return {
            "eligible": False,
            "reasons": ["invalid_promotion_evidence"],
            "artifact": preference_identity(artifact),
            "tasks": {},
        }


def _assess_promotion(
    artifact: PreferenceModelArtifact, config: ScrobbleRecsConfig, evidence: dict[str, Any]
) -> dict[str, Any]:
    from scrobble_recs.models.evaluation_identity import artifact_manifest
    from scrobble_recs.models.promotion import promotion_decision

    reasons: set[str] = set()
    settings = config.recommendation.preference
    provenance = artifact.training_provenance
    if (
        provenance["training_count"] < settings.minimum_training_pairs
        or provenance["validation_count"] < settings.minimum_validation_pairs
    ):
        reasons.add("insufficient_pairs")
    if artifact.query_scope != "shared" and any(
        counts["training"] < settings.minimum_training_pairs
        or counts["validation"] < settings.minimum_validation_pairs
        for counts in provenance["per_kind_counts"].values()
    ):
        reasons.add("insufficient_scope_pairs")
    metrics = artifact.validation_metrics
    if not (
        metrics["learned_log_loss"] < metrics["control_log_loss"]
        and metrics["learned_accuracy"] > metrics["control_accuracy"]
    ):
        reasons.add("heldout_does_not_beat_control")
    decisions = {}
    tasks = ("general", "artist") if artifact.query_scope == "shared" else (artifact.query_scope,)
    for task in tasks:
        payload = evidence.get(task, {})
        if not _linked_manifests(payload, preference_identity(artifact)):
            reasons.add("artifact_link_mismatch")
        decision = promotion_decision(
            config=config.recommendation.promotion,
            comparison=payload.get("comparison"),
            blinded_experiments=payload.get("blinded_experiments", ()),
            reversibility=payload.get("reversibility"),
            policy_experiment_id=payload.get("policy_experiment_id"),
        )
        decisions[task] = asdict(decision)
        if not payload:
            reasons.add(f"missing_{task}_promotion")
        if not decision.eligible:
            reasons.add(f"{task}_promotion_ineligible")
        comparison = payload.get("comparison", {})
        if comparison.get("evaluation_task") != task:
            reasons.add("artifact_link_mismatch")
        for pair in comparison.get("pairs", []):
            manifest = pair.get("challenger", {}).get("artifact", {})
            expected = artifact_manifest(
                config,
                {
                    "ranker": "preference-v1",
                    "feature_schema": "fusion-features-v1",
                    "slate_policy": manifest.get("slate_policy_version"),
                },
                preference=preference_identity(artifact),
            )
            if (
                manifest.get("preference") != preference_identity(artifact)
                or any(
                    manifest.get(key) != expected[key]
                    for key in ("artifact_id", "content_fingerprint", "behavior_config_fingerprint")
                )
                or manifest.get("behavior_config", {})
                .get("recommendation", {})
                .get("preference", {})
                .get("evaluation_artifact_id")
                != artifact.artifact_id
                or manifest.get("version") != "preference-v1"
                or manifest.get("feature_schema_version") != "fusion-features-v1"
            ):
                reasons.add("artifact_link_mismatch")
            control = pair.get("control", {}).get("artifact", {})
            expected_control = artifact_manifest(
                config,
                {
                    "ranker": "fusion-v1",
                    "feature_schema": "fusion-features-v1",
                    "slate_policy": control.get("slate_policy_version"),
                },
            )
            control_preference = (
                control.get("behavior_config", {}).get("recommendation", {}).get("preference", {})
            )
            if (
                control.get("preference")
                or control_preference.get("enabled")
                or control_preference.get("evaluation_artifact_id")
                or any(
                    control.get(key) != expected_control[key]
                    for key in ("artifact_id", "content_fingerprint", "behavior_config_fingerprint")
                )
            ):
                reasons.add("invalid_deterministic_control")
    return {
        "eligible": not reasons,
        "reasons": sorted(reasons),
        "tasks": decisions,
        "artifact": preference_identity(artifact),
        "validation": metrics,
    }


def _linked_manifests(value: Any, identity: dict[str, Any]) -> bool:
    """Every learned manifest, including frozen displays and ablations, names the same model."""
    if isinstance(value, list):
        return all(_linked_manifests(item, identity) for item in value)
    if isinstance(value, dict):
        if value.get("version") == "preference-v1" and "scorer" in value:
            if value.get("preference") != identity:
                return False
        return all(_linked_manifests(item, identity) for item in value.values())
    return True


def attest_preference(
    conn: DuckDBPyConnection,
    *,
    artifact_id: str,
    config: ScrobbleRecsConfig,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    encoded = canonical(evidence)
    conn.execute("BEGIN TRANSACTION")
    try:
        artifact = load_preference_artifact(conn, artifact_id)
        decision = _promotion_assessment(artifact, config, evidence)
        conn.execute(
            "INSERT INTO preference_promotion_evidence (artifact_id, evidence) VALUES (?, ?)",
            [artifact_id, encoded],
        )
        conn.execute(
            "UPDATE preference_model_artifact SET promotion_decision = ? WHERE artifact_id = ?",
            [canonical(decision), artifact_id],
        )
        _audit(conn, "attest", artifact.query_scope, artifact_id, decision)
        conn.execute("COMMIT")
        return decision
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _latest_evidence(conn: DuckDBPyConnection, artifact_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT evidence FROM preference_promotion_evidence WHERE artifact_id = ? "
        "ORDER BY created_at DESC, evidence_id DESC LIMIT 1",
        [artifact_id],
    ).fetchone()
    return json.loads(row[0]) if row else {}


def activate_preference(
    conn: DuckDBPyConnection, *, artifact_id: str, config: ScrobbleRecsConfig
) -> dict[str, Any]:
    conn.execute("BEGIN TRANSACTION")
    try:
        artifact = load_preference_artifact(conn, artifact_id)
        status = conn.execute(
            "SELECT status FROM preference_model_artifact WHERE artifact_id = ?", [artifact_id]
        ).fetchone()
        if status != ("candidate",):
            raise ValueError("activation_requires_candidate")
        decision = _promotion_assessment(artifact, config, _latest_evidence(conn, artifact_id))
        if not decision["eligible"]:
            raise ValueError("promotion_ineligible: " + ",".join(decision["reasons"]))
        active = conn.execute(
            "SELECT artifact_id FROM preference_model_artifact "
            "WHERE query_scope = ? AND status = 'active'",
            [artifact.query_scope],
        ).fetchall()
        if len(active) > 1:
            raise ValueError("conflicting_active_artifacts")
        conn.execute(
            "UPDATE preference_model_artifact SET status = 'retired' "
            "WHERE query_scope = ? AND status = 'active'",
            [artifact.query_scope],
        )
        conn.execute(
            "UPDATE preference_model_artifact SET status = 'active', promotion_decision = ? "
            "WHERE artifact_id = ?",
            [canonical(decision), artifact_id],
        )
        conn.execute(
            "INSERT INTO preference_scope_selection VALUES (?, false) "
            "ON CONFLICT (query_scope) DO UPDATE SET suppressed=false",
            [artifact.query_scope],
        )
        _audit(
            conn,
            "activate",
            artifact.query_scope,
            artifact_id,
            {"replaced": [row[0] for row in active], "decision": decision},
        )
        conn.execute("COMMIT")
        return decision
    except Exception:
        conn.execute("ROLLBACK")
        raise


def rollback_preference(conn: DuckDBPyConnection, *, scope: Scope) -> dict[str, Any]:
    if scope not in ("shared", "general", "artist"):
        raise ValueError("invalid_scope")
    conn.execute("BEGIN TRANSACTION")
    try:
        rows = conn.execute(
            "SELECT artifact_id FROM preference_model_artifact "
            "WHERE query_scope = ? AND status = 'active'",
            [scope],
        ).fetchall()
        conn.execute(
            "UPDATE preference_model_artifact SET status = 'retired' "
            "WHERE query_scope = ? AND status = 'active'",
            [scope],
        )
        conn.execute(
            "INSERT INTO preference_scope_selection VALUES (?, true) "
            "ON CONFLICT (query_scope) DO UPDATE SET suppressed=true",
            [scope],
        )
        result = {
            "retired_count": len(rows),
            "scope": scope,
            "effective_ranker": "deterministic_control",
        }
        _audit(conn, "rollback", scope, None, {**result, "retired": [row[0] for row in rows]})
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _select_artifact(
    conn: DuckDBPyConnection, config: ScrobbleRecsConfig, context: QueryContext
) -> PreferenceModelArtifact:
    settings = config.recommendation.preference
    required = {
        "preference_model_artifact",
        "preference_scope_selection",
        "preference_promotion_evidence",
    }
    present = {
        row[0]
        for row in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()
    }
    if not required <= present:
        raise ValueError("missing_preference_storage")
    identity = settings.evaluation_artifact_id
    if identity is None:
        for scope in (context.query_kind, "shared"):
            suppressed = conn.execute(
                "SELECT suppressed FROM preference_scope_selection WHERE query_scope = ?", [scope]
            ).fetchone()
            if suppressed == (True,):
                raise ValueError("scope_rolled_back")
            rows = conn.execute(
                "SELECT artifact_id, status FROM preference_model_artifact WHERE query_scope = ? "
                "AND status NOT IN ('candidate', 'retired') ORDER BY artifact_id",
                [scope],
            ).fetchall()
            if len(rows) > 1:
                raise ValueError("conflicting_active_artifacts")
            if rows:
                if rows[0][1] != "active":
                    raise ValueError("invalid_artifact_status")
                identity = rows[0][0]
                break
        if identity is None:
            raise ValueError("no_active_artifact")
    artifact = load_preference_artifact(conn, identity)
    if artifact.query_scope not in ("shared", context.query_kind):
        raise ValueError("incompatible_query_scope")
    if settings.evaluation_artifact_id is None:
        evidence = _latest_evidence(conn, identity)
        decision = _promotion_assessment(artifact, config, evidence)
        if not decision["eligible"]:
            raise ValueError("missing_or_invalid_promotion_link")
        if context.query_kind == "artist":
            targets = evidence.get("artist", {}).get("comparison", {}).get("target_artist_ids", [])
            if context.target_artist_id not in targets:
                raise ValueError("unsupported_target_context")
    return artifact


def apply_preference_ranker(
    conn: DuckDBPyConnection,
    *,
    context: QueryContext,
    ranked: tuple[RankedCandidate, ...],
    config: ScrobbleRecsConfig,
) -> tuple[tuple[RankedCandidate, ...], dict[str, Any]]:
    settings = config.recommendation.preference
    if not settings.enabled and settings.evaluation_artifact_id is None:
        return ranked, {}
    diagnostics: dict[str, Any] = {
        "used": False,
        "use": "evaluation" if settings.evaluation_artifact_id else "production",
        "attempted_artifact_id": settings.evaluation_artifact_id,
        "query_kind": context.query_kind,
        "target_artist_id": context.target_artist_id,
        "ranker_fallback_reason": None,
    }
    try:
        if config.recommendation.scoring.engine != "fusion":
            raise ValueError("unsupported_engine")
        artifact = _select_artifact(conn, config, context)
        diagnostics.update(
            attempted_artifact_id=artifact.artifact_id, artifact=preference_identity(artifact)
        )
        scores = {}
        for item in ranked:
            if item.legacy is None or item.legacy.score is None:
                continue
            vector = encode_features(asdict(item.features), context.to_json_dict())
            utility = _finite(
                sum(w * x for w, x in zip(artifact.coefficients, vector, strict=True))
            )
            scores[item.artist_id] = (utility, _finite(float(expit(utility))))
        diagnostics["used"] = True
        result = []
        for item in ranked:
            detail = {**diagnostics, "control_score": item.relevance_score}
            score = item.relevance_score
            if item.artist_id in scores:
                utility, score = scores[item.artist_id]
                detail.update(
                    learned_utility=utility,
                    learned_contribution=score - item.relevance_score,
                    personal_score=score,
                )
            result.append(
                replace(
                    item,
                    relevance_score=score,
                    explanation={**item.explanation, "preference": detail},
                )
            )
        return tuple(sorted(result, key=lambda x: (-x.relevance_score, x.artist_id))), diagnostics
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        # Never persist exception contents: they may contain a path or malformed payload.
        safe_reasons = {
            "unsupported_engine",
            "missing_preference_storage",
            "scope_rolled_back",
            "conflicting_active_artifacts",
            "invalid_artifact_status",
            "no_active_artifact",
            "incompatible_query_scope",
            "missing_or_invalid_promotion_link",
            "unsupported_target_context",
        }
        reason = str(error)
        diagnostics["ranker_fallback_reason"] = (
            reason if reason in safe_reasons else "invalid_artifact_or_features"
        )
        diagnostics["used"] = False
        return tuple(
            replace(item, explanation={**item.explanation, "preference": diagnostics})
            for item in ranked
        ), diagnostics
