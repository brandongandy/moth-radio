import asyncio
import json
from pathlib import Path

from textual.widgets import DataTable, Input, Link

from scrobble_recs.config import load_config
from scrobble_recs.db import initialize_database
from scrobble_recs.tui.app import ScrobbleRecsTui


def test_blinded_tui_saves_resumes_and_never_calls_network_or_ranking(tmp_path, monkeypatch):
    import httpx
    from test_experiments import create, seed_runs

    import scrobble_recs.models.recommend as recommend_module
    import scrobble_recs.tui.app as app_module

    def forbidden(*args, **kwargs):
        raise AssertionError("network/ranking forbidden while judging")

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(app_module, "record_feedback_event", forbidden)
    monkeypatch.setattr(app_module, "rerank_recommendation_run", forbidden)
    monkeypatch.setattr(recommend_module, "generate_recommendations", forbidden)
    from scrobble_recs.apis.lastfm import LastFmClient
    from scrobble_recs.apis.listenbrainz import ListenBrainzClient
    from scrobble_recs.apis.musicbrainz import MusicBrainzClient

    for client in (LastFmClient, MusicBrainzClient, ListenBrainzClient):
        monkeypatch.setattr(client, "__init__", forbidden)
    conn = initialize_database(load_config(write_config(tmp_path)))
    seed_runs(conn)
    create(conn)

    async def exercise():
        first_order = None
        for reopening in (False, True):
            app = ScrobbleRecsTui(conn)
            async with app.run_test(size=(110, 42)) as pilot:
                await pilot.press("x")
                await pilot.pause()
                screen = app.screen
                assert screen.__class__.__name__ == "ExperimentScreen"
                table = screen.query_one("#experiment-items", DataTable)
                rows = [table.get_row_at(i)[:3] for i in range(table.row_count)]
                assert "PRIVATE" not in str(rows)
                assert first_order is None or rows == first_order
                first_order = rows
                if reopening:
                    assert screen.judgment.labels
                    await pilot.press("c", "n")
                    await pilot.pause()
                    assert (
                        "exhausted"
                        in screen.query_one("#experiment-status").renderable.plain.lower()
                    )
                else:
                    await pilot.press("1", "b", "s")
                    await pilot.pause()
                    assert screen.judgment.labels
                    # Inherited actions must neither rerank nor reveal debug provenance.
                    await pilot.press("r", "a", "e", "R", "m", "h", "?")
                    assert app.screen is screen
                await pilot.press("escape")
                await pilot.pause()
                assert app.screen.__class__.__name__ != "ExperimentScreen"

    try:
        asyncio.run(exercise())
    finally:
        conn.close()


def test_blinded_tui_keyboard_inputs_save_resume_and_preserve_action_isolation(
    tmp_path, monkeypatch
):
    from test_experiments import create, seed_runs

    import scrobble_recs.tui.app as app_module

    def forbidden(*args, **kwargs):
        raise AssertionError("ordinary feedback/reranking forbidden while judging")

    monkeypatch.setattr(app_module, "record_feedback_event", forbidden)
    monkeypatch.setattr(app_module, "rerank_recommendation_run", forbidden)
    conn = initialize_database(load_config(write_config(tmp_path)))
    seed_runs(conn)
    experiment = create(conn)

    async def exercise():
        for reopening in (False, True):
            app = ScrobbleRecsTui(conn)
            async with app.run_test(size=(110, 42)) as pilot:
                run_id = app.run_id
                await pilot.press("x")
                screen = app.screen
                table = screen.query_one("#experiment-items", DataTable)
                confidence = screen.query_one("#experiment-confidence", Input)
                note = screen.query_one("#experiment-note", Input)
                assert screen.exposure.experiment_id == experiment
                assert app.focused is table
                if not reopening:
                    await pilot.press("tab")
                    assert app.focused is confidence
                    await pilot.press("0", ".", "8", "tab")
                    assert app.focused is note
                    await pilot.press("T", "r", "y", "space", "l", "a", "t", "e", "r")
                    assert confidence.value == "0.8" and note.value == "Try later"
                    await pilot.press("shift+tab")
                    assert app.focused is confidence
                    await pilot.press("shift+tab")
                    assert app.focused is table
                    await pilot.press("tab", "tab", "tab")
                    assert app.focused is table
                    await pilot.press("s")
                    assert screen.judgment.confidence == 0.8
                    assert screen.judgment.note == "Try later"
                else:
                    assert confidence.value == "0.8" and note.value == "Try later"
                    assert screen.judgment.confidence == 0.8
                    assert screen.judgment.note == "Try later"
                    await pilot.press("c", "n")
                    assert screen.exposure is None
                await pilot.press("r", "a", "o", "w", "m", "l", "h", "e", "R", "?")
                assert app.screen is screen
                assert app.run_id == run_id
                assert not app.show_debug_scores
                await pilot.press("escape")
                assert app.screen is not screen

    try:
        asyncio.run(exercise())
    finally:
        conn.close()


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
            table = app.query_one("#recommendations", DataTable)
            assert table.row_count == 1
            assert [column.label.plain for column in table.ordered_columns] == [
                "#",
                "Artist",
                "Score",
                "Tags",
                "Class",
            ]
            assert "Bauhaus" in _rendered_plain(app, "#recommendations")
            assert "Similar to post-punk seeds." in _rendered_plain(app, "#detail")
            assert "Final" in _rendered_plain(app, "#score-breakdown")
            assert "0.840" in _rendered_plain(app, "#score-breakdown")
            assert "[" in _rendered_plain(app, "#score-breakdown")
    finally:
        conn.close()


def test_tui_header_uses_moth_radio_title(tmp_path: Path) -> None:
    asyncio.run(_test_tui_header_uses_moth_radio_title(tmp_path))


async def _test_tui_header_uses_moth_radio_title(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        _insert_single_recommendation(conn, artist_id=11, artist="Title Artist")

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.title == "Moth Radio"
    finally:
        conn.close()


def test_tui_opens_selected_recommendation_lastfm_url_in_browser(
    tmp_path: Path,
    monkeypatch,
) -> None:
    asyncio.run(
        _test_tui_opens_selected_recommendation_lastfm_url_in_browser(tmp_path, monkeypatch)
    )


async def _test_tui_opens_selected_recommendation_lastfm_url_in_browser(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        run_id = "00000000-0000-0000-0000-000000000114"
        lastfm_url = "https://www.last.fm/music/Bauhaus"
        _insert_run(conn, run_id=run_id, mode="balanced")
        _insert_artist(conn, artist_id=114, name="Bauhaus")
        _insert_recommendation(
            conn,
            run_id=run_id,
            artist_id=114,
            rank=1,
            final_score=0.84,
            explanation={"summary": "Gothic rock pioneers."},
            lastfm_url=lastfm_url,
        )
        opened_urls: list[str] = []
        monkeypatch.setattr(ScrobbleRecsTui, "open_url", lambda _app, url: opened_urls.append(url))

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            link = app.query_one("#lastfm-link", Link)
            assert link.display is True
            assert link.url == lastfm_url

            await pilot.click(link)

        assert opened_urls == [lastfm_url]
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


def test_tui_arrow_keys_move_selection_and_update_detail(tmp_path: Path) -> None:
    asyncio.run(_test_tui_arrow_keys_move_selection_and_update_detail(tmp_path))


async def _test_tui_arrow_keys_move_selection_and_update_detail(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        run_id = "00000000-0000-0000-0000-000000000113"
        _insert_run(conn, run_id=run_id, mode="balanced")
        _insert_artist(conn, artist_id=120, name="Arrow First")
        _insert_artist(conn, artist_id=121, name="Arrow Second")
        _insert_recommendation(
            conn,
            run_id=run_id,
            artist_id=120,
            rank=1,
            final_score=0.9,
            explanation={"summary": "Arrow first.", "tags": [{"tag": "ambient"}]},
        )
        _insert_recommendation(
            conn,
            run_id=run_id,
            artist_id=121,
            rank=2,
            final_score=0.8,
            explanation={"summary": "Arrow second.", "tags": [{"tag": "drone"}]},
        )

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.press("down")
            await pilot.pause()
            assert app.selected_artist_id == 121
            assert "Arrow Second" in _rendered_plain(app, "#detail")

            await pilot.press("up")
            await pilot.pause()
            assert app.selected_artist_id == 120
            assert "Arrow First" in _rendered_plain(app, "#detail")
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


def test_tui_recommendation_list_scrolls_to_follow_selection(tmp_path: Path) -> None:
    asyncio.run(_test_tui_recommendation_list_scrolls_to_follow_selection(tmp_path))


async def _test_tui_recommendation_list_scrolls_to_follow_selection(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        run_id = "00000000-0000-0000-0000-000000000111"
        _insert_run(conn, run_id=run_id, mode="balanced")
        for index in range(35):
            artist_id = 400 + index
            _insert_artist(conn, artist_id=artist_id, name=f"Scroll Artist {index:02d}")
            _insert_recommendation(
                conn,
                run_id=run_id,
                artist_id=artist_id,
                rank=index + 1,
                final_score=1.0 - (index / 100),
                explanation={
                    "summary": f"Scroll summary {index:02d}.",
                    "tags": [{"tag": "post-punk"}],
                },
            )

        app = ScrobbleRecsTui(conn)
        async with app.run_test(size=(100, 12)) as pilot:
            await pilot.pause()
            recommendations = app.query_one("#recommendations")
            assert recommendations.scroll_y == 0

            for _ in range(25):
                await pilot.press("j")
            await pilot.pause()

            assert app.selected_artist_id == 425
            assert recommendations.scroll_y > 0
    finally:
        conn.close()


def test_tui_help_key_opens_modal_with_feedback_meanings(tmp_path: Path) -> None:
    asyncio.run(_test_tui_help_key_opens_modal_with_feedback_meanings(tmp_path))


async def _test_tui_help_key_opens_modal_with_feedback_meanings(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        _insert_single_recommendation(conn, artist_id=40, artist="Help Artist")

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.press("?")
            await pilot.pause()
            help_text = _modal_plain(app)
            assert "TUI Help" in help_text
            assert "w wrong genre: this recommendation does not fit its shown tags" in help_text
            assert "r reject: do not recommend this artist in this run" in help_text
            assert "m more like this: boost similar future recommendations" in help_text
            assert "l less like this: suppress similar future recommendations" in help_text

            await pilot.press("escape")
            await pilot.pause()
            assert _modal_plain(app) == ""
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


def test_tui_feedback_key_records_local_feedback_for_selected_row(tmp_path: Path) -> None:
    asyncio.run(_test_tui_feedback_key_records_local_feedback_for_selected_row(tmp_path))


async def _test_tui_feedback_key_records_local_feedback_for_selected_row(
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
            assert "Feedback recorded: more_like_this" in _rendered_plain(app, "#status")
            actions = conn.execute("SELECT action FROM feedback_event").fetchall()
            assert actions == [("more_like_this",)]
    finally:
        conn.close()


def test_tui_filter_key_reduces_visible_recommendations(tmp_path: Path) -> None:
    asyncio.run(_test_tui_filter_key_reduces_visible_recommendations(tmp_path))


async def _test_tui_filter_key_reduces_visible_recommendations(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000103"
    _insert_run(conn, run_id=run_id, mode="balanced")
    _insert_artist(conn, artist_id=30, name="Cold Cave")
    _insert_artist(conn, artist_id=31, name="Warm Artist")
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=30,
        rank=1,
        final_score=0.9,
        explanation={"summary": "Cold summary.", "tags": [{"tag": "coldwave"}]},
    )
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=31,
        rank=2,
        final_score=0.8,
        explanation={"summary": "Warm summary.", "tags": [{"tag": "soul"}]},
    )

    app = ScrobbleRecsTui(conn)
    async with app.run_test() as pilot:
        await pilot.press("/")
        await pilot.pause()
        rendered = app.query_one("#recommendations").renderable.plain
        assert "Cold Cave" in rendered
        assert "Warm Artist" not in rendered

    conn.close()


def test_tui_tag_filter_key_reduces_visible_recommendations(tmp_path: Path) -> None:
    asyncio.run(_test_tui_tag_filter_key_reduces_visible_recommendations(tmp_path))


async def _test_tui_tag_filter_key_reduces_visible_recommendations(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000107"
    _insert_run(conn, run_id=run_id, mode="balanced")
    _insert_artist(conn, artist_id=70, name="Tag Filter First")
    _insert_artist(conn, artist_id=71, name="Tag Filter Second")
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=70,
        rank=1,
        final_score=0.9,
        explanation={"summary": "First tag match.", "tags": [{"tag": "coldwave"}]},
    )
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=71,
        rank=2,
        final_score=0.8,
        explanation={"summary": "Second tag miss.", "tags": [{"tag": "soul"}]},
    )

    app = ScrobbleRecsTui(conn)
    async with app.run_test() as pilot:
        await pilot.pause()
        rendered = app.query_one("#recommendations").renderable.plain
        assert "Tag Filter First" in rendered
        assert "Tag Filter Second" in rendered

        await pilot.press("f")
        await pilot.pause()
        rendered = app.query_one("#recommendations").renderable.plain
        assert app.filters.tag == "coldwave"
        assert "Tag Filter First" in rendered
        assert "Tag Filter Second" not in rendered

    conn.close()


def test_tui_run_key_switches_to_next_persisted_run(tmp_path: Path) -> None:
    asyncio.run(_test_tui_run_key_switches_to_next_persisted_run(tmp_path))


async def _test_tui_run_key_switches_to_next_persisted_run(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    first_run = "00000000-0000-0000-0000-000000000104"
    second_run = "00000000-0000-0000-0000-000000000105"
    _insert_run(conn, run_id=first_run, mode="balanced")
    _insert_run(conn, run_id=second_run, mode="explore")
    _insert_artist(conn, artist_id=40, name="First Run Artist")
    _insert_artist(conn, artist_id=41, name="Second Run Artist")
    _insert_recommendation(
        conn,
        run_id=first_run,
        artist_id=40,
        rank=1,
        final_score=0.9,
        explanation={"summary": "First run.", "tags": [{"tag": "post-punk"}]},
    )
    _insert_recommendation(
        conn,
        run_id=second_run,
        artist_id=41,
        rank=1,
        final_score=0.8,
        explanation={"summary": "Second run.", "tags": [{"tag": "coldwave"}]},
    )

    app = ScrobbleRecsTui(conn)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.run_id == second_run
        assert "Second Run Artist" in app.query_one("#recommendations").renderable.plain

        await pilot.press("R")
        await pilot.pause()
        rendered = app.query_one("#recommendations").renderable.plain
        assert app.run_id == first_run
        assert "First Run Artist" in rendered
        assert "Second Run Artist" not in rendered

    conn.close()


def test_tui_debug_score_toggle_shows_component_scores(tmp_path: Path) -> None:
    asyncio.run(_test_tui_debug_score_toggle_shows_component_scores(tmp_path))


async def _test_tui_debug_score_toggle_shows_component_scores(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000106"
    _insert_run(conn, run_id=run_id, mode="balanced")
    _insert_artist(conn, artist_id=50, name="Debug Artist")
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=50,
        rank=1,
        final_score=0.9,
        explanation={"summary": "Debug summary.", "tags": [{"tag": "post-punk"}]},
    )

    app = ScrobbleRecsTui(conn)
    async with app.run_test() as pilot:
        await pilot.press("e")
        await pilot.pause()
        rendered = app.query_one("#score-breakdown").renderable.plain
        assert "lastfm:" in rendered
        assert "diversity penalty:" in rendered

    conn.close()


def test_tui_feedback_key_records_event_reranks_and_reloads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    asyncio.run(_test_tui_feedback_key_records_event_reranks_and_reloads(tmp_path, monkeypatch))


async def _test_tui_feedback_key_records_event_reranks_and_reloads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000107"
    _insert_run(conn, run_id=run_id, mode="balanced")
    _insert_artist(conn, artist_id=60, name="Feedback Artist")
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=60,
        rank=1,
        final_score=0.9,
        explanation={"summary": "Feedback summary.", "tags": [{"tag": "post-punk"}]},
    )
    calls: list[tuple[str, int, str, dict[str, object]]] = []
    reranks: list[str] = []

    class FakeStats:
        recommendations = 1
        hidden = 0

    def fake_record(conn_arg, *, run_id, artist_id, action, value=1.0, context=None, note=None):
        calls.append((run_id, artist_id, action, context or {}))

    def fake_rerank(conn_arg, run_id_arg):
        reranks.append(run_id_arg)
        return FakeStats()

    monkeypatch.setattr("scrobble_recs.tui.app.record_feedback_event", fake_record)
    monkeypatch.setattr("scrobble_recs.tui.app.rerank_recommendation_run", fake_rerank)

    app = ScrobbleRecsTui(conn)
    async with app.run_test() as pilot:
        await pilot.press("m")
        await pilot.pause()
        assert calls == [
            (
                run_id,
                60,
                "more_like_this",
                {"tags": ["post-punk"]},
            )
        ]
        assert reranks == [run_id]
        assert (
            "Feedback recorded: more_like_this; reranked 1; hidden 0"
            in app.query_one("#status").renderable
        )

    conn.close()


def test_tui_feedback_key_reloads_rows_and_preserves_selected_artist_after_rerank(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _test_tui_feedback_key_reloads_rows_and_preserves_selected_artist_after_rerank(tmp_path)
    )


async def _test_tui_feedback_key_reloads_rows_and_preserves_selected_artist_after_rerank(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        run_id = "00000000-0000-0000-0000-000000000109"
        _insert_run(conn, run_id=run_id, mode="balanced")
        _insert_artist(conn, artist_id=80, name="First Rerank")
        _insert_artist(conn, artist_id=81, name="Second Rerank")
        _insert_recommendation(
            conn,
            run_id=run_id,
            artist_id=80,
            rank=1,
            final_score=0.9,
            explanation={"summary": "First.", "tags": [{"tag": "post-punk"}]},
        )
        _insert_recommendation(
            conn,
            run_id=run_id,
            artist_id=81,
            rank=2,
            final_score=0.8,
            explanation={"summary": "Second.", "tags": [{"tag": "coldwave"}]},
        )

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.press("j")
            await pilot.pause()
            assert app.selected_artist_id == 81
            assert app.rows[0].artist_id == 80

            await pilot.press("m")
            await pilot.pause()

            assert [row.artist_id for row in app.rows] == [81, 80]
            assert app.selected_index == 0
            assert app.selected_artist_id == 81
            assert "Second Rerank" in _rendered_plain(app, "#detail")
            assert "Feedback recorded: more_like_this; reranked 2; hidden 0" in _rendered_plain(
                app, "#status"
            )
            ranks = conn.execute(
                """
                SELECT artist_id, rank
                FROM recommendation
                WHERE run_id = ?
                ORDER BY rank
                """,
                [run_id],
            ).fetchall()
            assert ranks == [(81, 1), (80, 2)]
    finally:
        conn.close()


def test_tui_wrong_feedback_resets_selection_to_top_after_rerank(tmp_path: Path) -> None:
    asyncio.run(_test_tui_wrong_feedback_resets_selection_to_top_after_rerank(tmp_path))


async def _test_tui_wrong_feedback_resets_selection_to_top_after_rerank(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        run_id = "00000000-0000-0000-0000-000000000112"
        _insert_run(conn, run_id=run_id, mode="balanced")
        _insert_artist(conn, artist_id=90, name="Wrong Top")
        _insert_artist(conn, artist_id=91, name="Wrong Selected")
        _insert_recommendation(
            conn,
            run_id=run_id,
            artist_id=90,
            rank=1,
            final_score=0.9,
            explanation={"summary": "Top.", "tags": [{"tag": "post-punk"}]},
        )
        _insert_recommendation(
            conn,
            run_id=run_id,
            artist_id=91,
            rank=2,
            final_score=0.8,
            explanation={"summary": "Selected.", "tags": [{"tag": "coldwave"}]},
        )

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.press("j")
            await pilot.pause()
            assert app.selected_artist_id == 91

            await pilot.press("w")
            await pilot.pause()

            assert [row.artist_id for row in app.rows] == [90, 91]
            assert app.selected_index == 0
            assert app.selected_artist_id == 90
            assert "Wrong Top" in _rendered_plain(app, "#detail")
            assert "Feedback recorded: wrong_genre; reranked 2; hidden 0" in _rendered_plain(
                app, "#status"
            )
    finally:
        conn.close()


def test_tui_feedback_key_uses_selected_artist_after_navigation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    asyncio.run(_test_tui_feedback_key_uses_selected_artist_after_navigation(tmp_path, monkeypatch))


async def _test_tui_feedback_key_uses_selected_artist_after_navigation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    run_id = "00000000-0000-0000-0000-000000000108"
    _insert_run(conn, run_id=run_id, mode="balanced")
    _insert_artist(conn, artist_id=70, name="First Feedback")
    _insert_artist(conn, artist_id=71, name="Second Feedback")
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=70,
        rank=1,
        final_score=0.9,
        explanation={"summary": "First.", "tags": [{"tag": "post-punk"}]},
    )
    _insert_recommendation(
        conn,
        run_id=run_id,
        artist_id=71,
        rank=2,
        final_score=0.8,
        explanation={"summary": "Second.", "tags": [{"tag": "coldwave"}]},
    )
    artist_ids: list[int] = []

    def fake_record(conn_arg, *, run_id, artist_id, action, value=1.0, context=None, note=None):
        artist_ids.append(artist_id)

    def fake_rerank(conn_arg, run_id_arg):
        class FakeStats:
            recommendations = 2
            hidden = 0

        return FakeStats()

    monkeypatch.setattr("scrobble_recs.tui.app.record_feedback_event", fake_record)
    monkeypatch.setattr("scrobble_recs.tui.app.rerank_recommendation_run", fake_rerank)

    app = ScrobbleRecsTui(conn)
    async with app.run_test() as pilot:
        await pilot.press("j")
        await pilot.press("r")
        await pilot.pause()
        assert artist_ids == [71]

    conn.close()


def test_tui_feedback_key_shows_no_selection_when_run_has_no_rows(tmp_path: Path) -> None:
    asyncio.run(_test_tui_feedback_key_shows_no_selection_when_run_has_no_rows(tmp_path))


async def _test_tui_feedback_key_shows_no_selection_when_run_has_no_rows(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        _insert_run(conn, run_id="00000000-0000-0000-0000-000000000110", mode="balanced")

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.pause()
            assert "No recommendation selected" in _rendered_plain(app, "#status")
    finally:
        conn.close()


def test_tui_feedback_key_shows_no_selection_when_no_run_exists(tmp_path: Path) -> None:
    asyncio.run(_test_tui_feedback_key_shows_no_selection_when_no_run_exists(tmp_path))


async def _test_tui_feedback_key_shows_no_selection_when_no_run_exists(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.pause()
            assert "No recommendation selected" in _rendered_plain(app, "#status")
    finally:
        conn.close()


def test_tui_feedback_key_reports_rerank_error(tmp_path: Path, monkeypatch) -> None:
    asyncio.run(_test_tui_feedback_key_reports_rerank_error(tmp_path, monkeypatch))


async def _test_tui_feedback_key_reports_rerank_error(tmp_path: Path, monkeypatch) -> None:
    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        _insert_single_recommendation(conn, artist_id=82, artist="Error Artist")

        def fake_rerank(conn_arg, run_id_arg):
            raise ValueError("local rerank failed")

        monkeypatch.setattr("scrobble_recs.tui.app.rerank_recommendation_run", fake_rerank)

        app = ScrobbleRecsTui(conn)
        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.pause()
            assert "Feedback error: local rerank failed" in _rendered_plain(app, "#status")
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
    lastfm_url: str | None = None,
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
            explanation,
            lastfm_url
        )
        VALUES (?, ?, ?, ?, ?, 0.1, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ?, ?)
        """,
        [run_id, rank, artist_id, final_score, final_score, json.dumps(explanation), lastfm_url],
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


def _modal_plain(app: ScrobbleRecsTui) -> str:
    return "\n".join(widget.renderable.plain for widget in app.screen.query("#help-modal Static"))


def test_tui_browses_persisted_catalog_run_with_raising_http_clients(tmp_path, monkeypatch):
    from datetime import datetime

    import httpx

    from scrobble_recs.models.evidence import EvidenceObservation, persist_evidence_observation

    config = load_config(write_config(tmp_path))
    conn = initialize_database(config)
    try:
        run_id = "00000000-0000-0000-0000-000000000188"
        _insert_run(conn, run_id=run_id, mode="balanced")
        _insert_artist(conn, artist_id=10, name="Catalog Artist")
        evidence = {
            "relationship_type": "member of band",
            "direction": "forward",
            "supported_relationship": True,
        }
        persist_evidence_observation(
            conn,
            EvidenceObservation(
                "musicbrainz",
                "catalog",
                "catalog_relationship",
                None,
                10,
                None,
                None,
                None,
                datetime(2025, 1, 1),
                0.95,
                evidence,
            ),
            observation_key="tui-catalog",
        )
        _insert_recommendation(
            conn,
            run_id=run_id,
            artist_id=10,
            rank=1,
            final_score=0.8,
            explanation={"summary": "Catalog: member of band.", "catalog": evidence},
        )

        def raising(*args, **kwargs):
            raise AssertionError("TUI constructed HTTP client")

        monkeypatch.setattr(httpx, "Client", raising)
        monkeypatch.setattr(httpx, "AsyncClient", raising)

        async def browse():
            app = ScrobbleRecsTui(conn)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert app.query_one("#recommendations", DataTable).row_count == 1
                assert "Catalog Artist" in _rendered_plain(app, "#recommendations")
                assert "member of band" in _rendered_plain(app, "#detail")
                await pilot.press("down", "up")
                await pilot.pause()
                assert "Catalog Artist" in _rendered_plain(app, "#recommendations")

        asyncio.run(browse())
    finally:
        conn.close()
