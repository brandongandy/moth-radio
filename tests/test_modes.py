import json
from pathlib import Path

import pytest

from scrobble_recs.config import load_config
from scrobble_recs.models.modes import resolve_recommendation_mode


def _config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "db.duckdb"),
                    "cache_dir": str(tmp_path / "cache"),
                    "log_path": str(tmp_path / "app.log"),
                },
                "input": {
                    "lastfm_csv": "tests/fixtures/tiny_lastfm_export.csv",
                    "timezone": "UTC",
                    "timestamp_column": "uts",
                    "csv_columns": [
                        "uts",
                        "utc_time",
                        "artist",
                        "artist_mbid",
                        "album",
                        "album_mbid",
                        "track",
                        "track_mbid",
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_balanced_mode_uses_sensible_defaults(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))

    mode = resolve_recommendation_mode(None, config)

    assert mode.name == "balanced"
    assert mode.seed_regions == (
        ("lifetime", 150, 1.00),
        ("recent", 75, 1.00),
        ("old_obsession", 50, 0.85),
        ("underexplored", 50, 0.75),
    )
    assert mode.diversity_lambda == pytest.approx(0.75)
    assert mode.max_knownness == pytest.approx(0.65)
    assert mode.obviousness_penalty_weight == pytest.approx(0.25)
    assert mode.hard_hide_top_tag_rank == 3
    assert mode.same_primary_tag_cap == 4
    assert mode.focus_tags == ()


@pytest.mark.parametrize(
    ("mode_name", "expected_lambda", "expected_knownness", "expected_penalty"),
    [
        ("explore", 0.55, 0.45, 0.35),
        ("safe", 0.90, 0.75, 0.15),
        ("rediscovery", 0.80, 0.80, 0.20),
        ("recent", 0.75, 0.60, 0.25),
        ("old", 0.80, 0.75, 0.20),
        ("anti-obvious", 0.65, 0.55, 0.45),
    ],
)
def test_named_modes_resolve_ranking_knobs(
    tmp_path: Path,
    mode_name: str,
    expected_lambda: float,
    expected_knownness: float,
    expected_penalty: float,
) -> None:
    config = load_config(_config_path(tmp_path))

    mode = resolve_recommendation_mode(mode_name, config)

    assert mode.name == mode_name
    assert mode.diversity_lambda == pytest.approx(expected_lambda)
    assert mode.max_knownness == pytest.approx(expected_knownness)
    assert mode.obviousness_penalty_weight == pytest.approx(expected_penalty)
    assert mode.seed_regions


def test_tag_focus_mode_parses_comma_separated_tags(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))

    mode = resolve_recommendation_mode("tag:post-punk, gothic rock", config)

    assert mode.name == "tag"
    assert mode.focus_tags == ("post-punk", "gothic rock")
    assert mode.tag_focus_boost == pytest.approx(1.60)
    assert mode.seed_regions == (
        ("lifetime", 125, 0.85),
        ("recent", 75, 1.00),
        ("underexplored", 50, 0.75),
    )


def test_unknown_mode_raises_clear_value_error(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))

    with pytest.raises(ValueError, match="unsupported recommendation mode 'fresh'"):
        resolve_recommendation_mode("fresh", config)


def test_empty_tag_focus_mode_raises_clear_value_error(tmp_path: Path) -> None:
    config = load_config(_config_path(tmp_path))

    with pytest.raises(ValueError, match="tag focus mode requires at least one tag"):
        resolve_recommendation_mode("tag:", config)
