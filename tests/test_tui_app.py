import asyncio
import json
from pathlib import Path

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.tui.app import ScrobbleRecsTui


def write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {
                    "name": "scrobble-recs",
                    "db_path": str(tmp_path / "scrobbles.duckdb"),
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
    return config_path


def test_tui_renders_recommendation_list_and_selected_detail(tmp_path: Path) -> None:
    asyncio.run(_test_tui_renders_recommendation_list_and_selected_detail(tmp_path))


async def _test_tui_renders_recommendation_list_and_selected_detail(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        _insert_run(conn, run_id="00000000-0000-0000-0000-000000000101", mode="balanced")
        _insert_artist(conn, artist_id=10, name="Bauhaus")
        _insert_recommendation(
            conn,
            run_id="00000000-0000-0000-0000-000000000101",
            artist_id=10,
            rank=1,
            final_score=0.84,
            explanation={
                "summary": "Similar to post-punk seeds.",
                "seed_artists": [{"artist": "The Cure"}],
                "tags": [{"tag": "post-punk"}],
                "penalties": {"knownness": 0.01, "obviousness": 0.00, "diversity": 0.00},
                "classification": {"is_rediscovery": False, "is_obvious": False},
            },
        )

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "Bauhaus" in _rendered_plain(app, "#recommendations")
            assert "Similar to post-punk seeds." in _rendered_plain(app, "#detail")
            assert "final: 0.840" in _rendered_plain(app, "#score-breakdown")
    finally:
        conn.close()


def test_tui_moves_selection_and_updates_detail(tmp_path: Path) -> None:
    asyncio.run(_test_tui_moves_selection_and_updates_detail(tmp_path))


async def _test_tui_moves_selection_and_updates_detail(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        run_id = "00000000-0000-0000-0000-000000000102"
        _insert_run(conn, run_id=run_id, mode="balanced")
        _insert_artist(conn, artist_id=20, name="First Artist")
        _insert_artist(conn, artist_id=21, name="Second Artist")
        _insert_recommendation(
            conn,
            run_id=run_id,
            artist_id=20,
            rank=1,
            final_score=0.9,
            explanation={"summary": "First summary.", "tags": [{"tag": "post-punk"}]},
        )
        _insert_recommendation(
            conn,
            run_id=run_id,
            artist_id=21,
            rank=2,
            final_score=0.8,
            explanation={"summary": "Second summary.", "tags": [{"tag": "coldwave"}]},
        )

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.press("j")
            await pilot.pause()
            assert "Second Artist" in _rendered_plain(app, "#detail")
            assert app.selected_artist_id == 21
    finally:
        conn.close()


def test_tui_shows_status_when_no_recommendation_runs(tmp_path: Path) -> None:
    asyncio.run(_test_tui_shows_status_when_no_recommendation_runs(tmp_path))


async def _test_tui_shows_status_when_no_recommendation_runs(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "no recommendation runs exist" in _rendered_plain(app, "#status")
    finally:
        conn.close()


def test_tui_move_keys_respect_list_boundaries(tmp_path: Path) -> None:
    asyncio.run(_test_tui_move_keys_respect_list_boundaries(tmp_path))


async def _test_tui_move_keys_respect_list_boundaries(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        run_id = "00000000-0000-0000-0000-000000000103"
        _insert_run(conn, run_id=run_id, mode="balanced")
        _insert_artist(conn, artist_id=30, name="Boundary First")
        _insert_artist(conn, artist_id=31, name="Boundary Second")
        _insert_recommendation(
            conn,
            run_id=run_id,
            artist_id=30,
            rank=1,
            final_score=0.9,
            explanation={"summary": "First boundary.", "tags": [{"tag": "post-punk"}]},
        )
        _insert_recommendation(
            conn,
            run_id=run_id,
            artist_id=31,
            rank=2,
            final_score=0.8,
            explanation={"summary": "Second boundary.", "tags": [{"tag": "coldwave"}]},
        )

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.press("k")
            await pilot.pause()
            assert app.selected_artist_id == 30
            assert "Boundary First" in _rendered_plain(app, "#detail")

            await pilot.press("j")
            await pilot.press("j")
            await pilot.pause()
            assert app.selected_artist_id == 31
            assert "Boundary Second" in _rendered_plain(app, "#detail")

            await pilot.press("k")
            await pilot.pause()
            assert app.selected_artist_id == 30
            assert "Boundary First" in _rendered_plain(app, "#detail")
    finally:
        conn.close()


def test_tui_help_key_updates_status(tmp_path: Path) -> None:
    asyncio.run(_test_tui_help_key_updates_status(tmp_path))


async def _test_tui_help_key_updates_status(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        _insert_single_recommendation(conn, artist_id=40, artist="Help Artist")

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.press("?")
            await pilot.pause()
            assert "Keys: j/k move" in _rendered_plain(app, "#status")
    finally:
        conn.close()


def test_tui_undo_key_updates_status(tmp_path: Path) -> None:
    asyncio.run(_test_tui_undo_key_updates_status(tmp_path))


async def _test_tui_undo_key_updates_status(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        _insert_single_recommendation(conn, artist_id=50, artist="Undo Artist")

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.press("u")
            await pilot.pause()
            assert "Undo is outside TUI V1" in _rendered_plain(app, "#status")
    finally:
        conn.close()


def test_tui_feedback_placeholder_key_updates_status_for_selected_row(tmp_path: Path) -> None:
    asyncio.run(_test_tui_feedback_placeholder_key_updates_status_for_selected_row(tmp_path))


async def _test_tui_feedback_placeholder_key_updates_status_for_selected_row(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        _insert_single_recommendation(conn, artist_id=60, artist="Feedback Artist")

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.pause()
            assert app.selected_artist_id == 60
            assert "Feedback action reserved for TUI V1 follow-up: more_like_this" in (
                _rendered_plain(app, "#status")
            )
    finally:
        conn.close()


def _insert_run(conn, *, run_id: str, mode: str) -> None:
    conn.execute(
        """
        INSERT INTO recommendation_run (
            run_id,
            config_hash,
            mode,
            seed_summary,
            scoring_weights
        )
        VALUES (?, 'hash', ?, '{}', '{}')
        """,
        [run_id, mode],
    )


def _insert_artist(conn, *, artist_id: int, name: str) -> None:
    conn.execute(
        """
        INSERT INTO artist (artist_id, canonical_name, artist_key, lastfm_name)
        VALUES (?, ?, ?, ?)
        """,
        [artist_id, name, name.casefold().replace(" ", "-"), name],
    )


def _insert_recommendation(
    conn,
    *,
    run_id: str,
    artist_id: int,
    rank: int,
    final_score: float,
    explanation: dict[str, object],
) -> None:
    conn.execute(
        """
        INSERT INTO recommendation (
            run_id,
            rank,
            artist_id,
            final_score,
            raw_score,
            lastfm_similarity_score,
            tag_similarity_score,
            cluster_fit_score,
            rediscovery_score,
            metadata_graph_score,
            feedback_score,
            knownness,
            obviousness,
            diversity_penalty,
            explanation
        )
        VALUES (?, ?, ?, ?, ?, 0.1, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ?)
        """,
        [run_id, rank, artist_id, final_score, final_score, json.dumps(explanation)],
    )


def _insert_single_recommendation(conn, *, artist_id: int, artist: str) -> None:
    run_id = f"00000000-0000-0000-0000-{artist_id:012d}"
    _insert_run(conn, run_id=run_id, mode="balanced")
    _insert_artist(conn, artist_id=artist_id, name=artist)
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=artist_id,
        rank=1,
        final_score=0.9,
        explanation={"summary": f"{artist} summary.", "tags": [{"tag": "post-punk"}]},
    )


def _rendered_plain(app: ScrobbleRecsTui, selector: str) -> str:
    return app.query_one(selector).renderable.plain
