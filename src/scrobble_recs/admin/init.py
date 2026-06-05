from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scrobble_recs.config import default_config_document, load_config
from scrobble_recs.db import initialize_database


@dataclass(frozen=True)
class InitResult:
    config_path: Path
    db_path: Path
    cache_dir: Path
    log_path: Path


def initialize_project(config_path: Path, *, force: bool = False) -> InitResult:
    resolved_config_path = config_path.expanduser()
    if resolved_config_path.exists() and not force:
        raise FileExistsError(f"config already exists at {resolved_config_path}")

    document = _config_document_for_location(resolved_config_path)
    resolved_config_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_config_path.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )

    config = load_config(resolved_config_path)
    config.app.cache_dir.mkdir(parents=True, exist_ok=True)
    config.app.log_path.parent.mkdir(parents=True, exist_ok=True)
    conn = initialize_database(config)
    conn.close()
    return InitResult(
        config_path=resolved_config_path,
        db_path=config.app.db_path,
        cache_dir=config.app.cache_dir,
        log_path=config.app.log_path,
    )


def _config_document_for_location(config_path: Path) -> dict[str, Any]:
    document = default_config_document()
    document["app"]["db_path"] = str(config_path.parent / "data" / "scrobbles.duckdb")
    document["app"]["cache_dir"] = str(config_path.parent / "data" / "cache")
    document["app"]["log_path"] = str(config_path.parent / "data" / "app.log")
    return document
