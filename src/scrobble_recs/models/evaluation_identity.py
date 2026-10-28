"""Portable identity for evaluation artifacts; no installation or activation state."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from scrobble_recs.config import ScrobbleRecsConfig


def behavior_config(config: ScrobbleRecsConfig) -> dict[str, Any]:
    """Omit destinations, secrets and promotion bounds; retain all behavior settings."""
    payload = config.model_dump(mode="json", by_alias=True)
    payload.pop("app", None)
    payload["input"].pop("lastfm_csv", None)
    payload["recommendation"].pop("promotion", None)
    for provider in payload["apis"].values():
        for key in ("api_key", "token", "user_agent"):
            provider.pop(key, None)
    return payload


def behavior_fingerprint(config: ScrobbleRecsConfig) -> str:
    encoded = json.dumps(
        behavior_config(config), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return sha256(encoded.encode()).hexdigest()


def artifact_manifest(
    config: ScrobbleRecsConfig,
    versions: dict[str, Any],
    *,
    preference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config.model_copy(deep=True)
    config.recommendation.preference.enabled = False
    config.recommendation.preference.evaluation_artifact_id = (
        preference["artifact_id"] if preference else None
    )
    package = Path(__file__).resolve().parents[1]
    digest = sha256()
    files = sorted(package.rglob("*.py"))
    for path in files:
        digest.update(path.relative_to(package).as_posix().encode())
        digest.update(path.read_bytes())
    if preference:
        digest.update(json.dumps(preference, sort_keys=True, allow_nan=False).encode())
    content = digest.hexdigest()
    identity = {
        "scorer": config.recommendation.scoring.engine,
        "version": versions.get("ranker"),
        "feature_schema_version": versions.get("feature_schema"),
        "slate_policy_version": versions.get("slate_policy"),
        "content_fingerprint": content,
        "behavior_config_fingerprint": behavior_fingerprint(config),
    }
    return {
        **identity,
        **({"preference": preference} if preference else {}),
        "artifact_id": sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
        "fingerprint_policy": (
            "behavior-config-v1; excludes app destinations, input CSV path, secrets, "
            "user-agent, promotion bounds"
        ),
        "behavior_config": behavior_config(config),
        "artifact_bytes": sum(path.stat().st_size for path in files),
        "artifact_files": [path.relative_to(package).as_posix() for path in files],
    }
