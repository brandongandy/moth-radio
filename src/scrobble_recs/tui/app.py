from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from duckdb import DuckDBPyConnection
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Link, Static

from scrobble_recs.models.experiments import (
    LABELS,
    BlindedExposure,
    EffectiveJudgment,
    SideChoice,
)
from scrobble_recs.models.feedback import record_feedback_event, rerank_recommendation_run
from scrobble_recs.tui.data import (
    RecommendationDetail,
    RecommendationFilters,
    RecommendationListRow,
    TuiRecommendationStore,
)


class ScrobbleRecsTui(App[None]):
    """Textual browser for persisted recommendation runs."""

    TITLE = "Moth Radio"

    CSS = """
    Screen {
        background: #1a2a38;
        color: #e4e6e0;
    }

    Header {
        background: #263849;
        color: #e4e6e0;
    }

    Footer {
        background: #263849;
        color: #e4e6e0;
    }

    #main {
        height: 1fr;
        layout: horizontal;
    }

    #recommendations {
        width: 46%;
        height: 1fr;
        border: solid #4e7a98;
        background: #263849;
    }

    #right {
        width: 54%;
        height: 1fr;
        layout: vertical;
    }

    #detail {
        height: 2fr;
        border: solid #4e7a98;
        background: #263849;
        padding: 1 2;
    }

    #detail-content {
        height: 1fr;
    }

    #lastfm-link {
        margin-top: 1;
    }

    #score-breakdown {
        height: 1fr;
        border: solid #4e7a98;
        background: #263849;
        padding: 1 2;
    }

    #status {
        height: 1;
        padding: 0 1;
        background: #1a2a38;
        color: #a8b6c0;
    }

    DataTable {
        background: #263849;
        color: #e4e6e0;
    }

    DataTable > .datatable--header {
        background: #2d4354;
        color: #a8b6c0;
    }

    DataTable > .datatable--cursor {
        background: #40576b;
        color: #e4e6e0;
    }

    HelpModal {
        align: center middle;
    }

    #help-modal {
        width: 74;
        max-width: 90%;
        max-height: 90%;
        border: thick #40ABE5;
        padding: 1 2;
        background: #263849;
        color: #e4e6e0;
        overflow-y: auto;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit"),
        Binding("j", "move_down", "Down"),
        Binding("k", "move_up", "Up"),
        Binding("s", "feedback('save')", "Save"),
        Binding("r", "feedback('reject')", "Reject"),
        Binding("a", "feedback('already_know')", "Known"),
        Binding("o", "feedback('too_obvious')", "Obvious"),
        Binding("w", "feedback('wrong_genre')", "Wrong"),
        Binding("m", "feedback('more_like_this')", "More"),
        Binding("l", "feedback('less_like_this')", "Less"),
        Binding("h", "feedback('hide_artist')", "Hide"),
        Binding("/", "cycle_search_filter", "Search"),
        Binding("f", "cycle_tag_filter", "Tag"),
        Binding("e", "toggle_debug_scores", "Scores"),
        Binding("R", "next_run", "Run"),
        Binding("x", "experiments", "Experiments"),
        Binding("u", "undo_unavailable", "Undo"),
        Binding("?", "show_help", "Help"),
    ]

    def __init__(self, conn: DuckDBPyConnection) -> None:
        super().__init__()
        self.conn = conn
        self.store = TuiRecommendationStore(conn)
        self.filters = RecommendationFilters()
        self.rows: list[RecommendationListRow] = []
        self.run_id: str | None = None
        self.selected_index = 0
        self.selected_artist_id: int | None = None
        self.show_debug_scores = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield RecommendationTable(id="recommendations")
            with Vertical(id="right"):
                yield RecommendationDetailPanel(id="detail")
                yield ScoreBreakdownPanel(id="score-breakdown")
        yield _InspectableStatic("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._reload_rows()
        # Show cache status in the status bar.
        cache = self.store.cache_summary()
        if cache.total_entries > 0:
            self.query_one("#status", Static).update(
                f"Cache: {cache.fresh_entries} fresh / {cache.total_entries} total"
            )

    def action_move_down(self) -> None:
        if not self.rows:
            return
        self.selected_index = min(self.selected_index + 1, len(self.rows) - 1)
        self._render_all()

    def action_move_up(self) -> None:
        if not self.rows:
            return
        self.selected_index = max(self.selected_index - 1, 0)
        self._render_all()

    def action_toggle_debug_scores(self) -> None:
        self.show_debug_scores = not self.show_debug_scores
        self._render_all()

    def action_cycle_search_filter(self) -> None:
        next_search = "" if self.filters.search else "cold"
        self.filters = replace(self.filters, search=next_search)
        self.selected_index = 0
        self._reload_rows()

    def action_cycle_tag_filter(self) -> None:
        next_tag = "" if self.filters.tag else self._first_visible_tag()
        self.filters = replace(self.filters, tag=next_tag)
        self.selected_index = 0
        self._reload_rows()

    def action_next_run(self) -> None:
        runs = self.store.list_runs()
        if len(runs) < 2:
            self.query_one("#status", Static).update("No other recommendation runs")
            return
        run_ids = [run.run_id for run in runs]
        current_index = run_ids.index(self.run_id) if self.run_id in run_ids else 0
        self.run_id = run_ids[(current_index + 1) % len(run_ids)]
        self.selected_index = 0
        self._reload_rows()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Application actions include feedback/reranking and score detail. A judging
        # screen owns its bindings; no parent action may mutate or reveal its source.
        return False if isinstance(self.screen, ExperimentScreen) else True

    def action_experiments(self) -> None:
        experiments = self.store.list_experiment_ids(self.run_id)
        if not experiments:
            self.query_one("#status", Static).update("No experiments for this persisted run")
            return
        self.push_screen(ExperimentScreen(self.store, experiments))

    def action_feedback(self, action: str) -> None:
        detail = self._selected_detail()
        if detail is None or self.run_id is None:
            self.query_one("#status", Static).update("No recommendation selected")
            return
        tags = list(detail.tags)
        try:
            record_feedback_event(
                self.conn,
                run_id=self.run_id,
                artist_id=detail.artist_id,
                action=action,
                context={"tags": tags},
            )
            stats = rerank_recommendation_run(self.conn, self.run_id)
        except ValueError as exc:
            self.query_one("#status", Static).update(f"Feedback error: {exc}")
            return
        selected_artist_id = detail.artist_id
        self.rows = self.store.list_recommendations(self.filters, run_id=self.run_id)
        self.selected_index = (
            0 if action == "wrong_genre" else _index_for_artist(self.rows, selected_artist_id)
        )
        self._render_all()
        self.query_one("#status", Static).update(
            f"Feedback recorded: {action}; reranked {stats.recommendations}; hidden {stats.hidden}"
        )

    def action_undo_unavailable(self) -> None:
        self.query_one("#status", Static).update("Undo is outside TUI V1")

    def action_show_help(self) -> None:
        self.push_screen(HelpModal())

    def _reload_rows(self) -> None:
        try:
            if self.run_id is None:
                self.run_id = self.store.latest_run_id()
            self.rows = self.store.list_recommendations(self.filters, run_id=self.run_id)
        except ValueError as exc:
            self.rows = []
            self.selected_index = 0
            self.selected_artist_id = None
            self.query_one("#recommendations", RecommendationTable).set_rows([], 0)
            self.query_one("#detail", RecommendationDetailPanel).update_detail(None)
            self.query_one("#score-breakdown", ScoreBreakdownPanel).update_scores(
                None,
                self.show_debug_scores,
            )
            self.query_one("#status", Static).update(str(exc))
            return

        self.selected_index = min(self.selected_index, max(len(self.rows) - 1, 0))
        self._render_all()
        self.query_one("#status", Static).update(self._status_text())

    def _render_all(self) -> None:
        self.query_one("#recommendations", RecommendationTable).set_rows(
            self.rows,
            self.selected_index,
        )
        detail = self._selected_detail()
        self.query_one("#detail", RecommendationDetailPanel).update_detail(detail)
        self.query_one("#score-breakdown", ScoreBreakdownPanel).update_scores(
            detail,
            self.show_debug_scores,
        )

    def _selected_row(self) -> RecommendationListRow | None:
        if not self.rows:
            self.selected_artist_id = None
            return None
        row = self.rows[self.selected_index]
        self.selected_artist_id = row.artist_id
        return row

    def _selected_detail(self) -> RecommendationDetail | None:
        row = self._selected_row()
        if row is None or self.run_id is None:
            return None
        return self.store.get_detail(self.run_id, artist_id=row.artist_id)

    def _status_text(self) -> str:
        run_label = self.run_id or "none"
        return f"run: {run_label}; recommendations: {len(self.rows)}"

    def _first_visible_tag(self) -> str:
        for row in self.rows:
            if row.primary_tags:
                return row.primary_tags[0]
        return ""

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "recommendations":
            return
        if not self.rows or event.cursor_row == self.selected_index:
            return
        self.selected_index = event.cursor_row
        detail = self._selected_detail()
        self.query_one("#detail", RecommendationDetailPanel).update_detail(detail)
        self.query_one("#score-breakdown", ScoreBreakdownPanel).update_scores(
            detail,
            self.show_debug_scores,
        )


class RecommendationTable(DataTable[str]):
    """Compact recommendation table with test-visible plain content."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.renderable = Text("")
        self._columns_added = False
        super().__init__(*args, **kwargs)
        self.cursor_type = "row"
        self.show_cursor = True
        self.zebra_stripes = True

    def on_mount(self) -> None:
        self.border_title = "Recommendations"
        self._ensure_columns()

    def set_rows(self, rows: list[RecommendationListRow], selected_index: int) -> None:
        self._ensure_columns()
        self.clear()
        self.renderable = _recommendations_plain(rows)
        for row in rows:
            tags = _truncate(_join_or_none(row.primary_tags), 22)
            classification = row.feedback_state or row.classification
            self.add_row(
                f"{row.rank:03d}",
                _truncate(row.artist, 28),
                f"{row.score:.3f}",
                tags,
                _truncate(classification, 10),
                key=str(row.artist_id),
            )
        if rows:
            cursor_row = min(selected_index, len(rows) - 1)
            self.move_cursor(row=cursor_row, column=0, animate=False)

    def _ensure_columns(self) -> None:
        if self._columns_added:
            return
        self.add_columns("#", "Artist", "Score", "Tags", "Class")
        self._columns_added = True


class _InspectableStatic(Static):
    """Compatibility shim for Textual versions that hide Static content."""

    def __init__(
        self,
        content: Any = "",
        *,
        expand: bool = False,
        shrink: bool = False,
        markup: bool = True,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        self.renderable = _to_text(content)
        super().__init__(
            self.renderable,
            expand=expand,
            shrink=shrink,
            markup=markup,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )

    def update(self, content: Any = "", *, layout: bool = True) -> None:
        self.renderable = _to_text(content)
        super().update(self.renderable, layout=layout)


class RecommendationDetailPanel(Vertical):
    """Selected recommendation detail panel."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.renderable = Text("")
        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        yield _InspectableStatic("", id="detail-content")
        link = Link("Open on Last.fm", id="lastfm-link")
        link.display = False
        yield link

    def on_mount(self) -> None:
        self.border_title = "Selected Recommendation"

    def update_detail(self, detail: RecommendationDetail | None) -> None:
        self.renderable = _detail_text(detail)
        self.query_one("#detail-content", _InspectableStatic).update(self.renderable)
        link = self.query_one("#lastfm-link", Link)
        link.url = detail.lastfm_url if detail and detail.lastfm_url else ""
        link.display = bool(link.url)


class ScoreBreakdownPanel(_InspectableStatic):
    """Compact score breakdown panel."""

    def on_mount(self) -> None:
        self.border_title = "Score Breakdown"

    def update_scores(self, detail: RecommendationDetail | None, show_debug_scores: bool) -> None:
        self.update(_score_breakdown_text(detail, show_debug_scores))


class HelpModal(ModalScreen[None]):
    """Modal help for local TUI keybindings."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close"),
        Binding("?", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-modal"):
            yield _InspectableStatic(_help_text())

    def action_close(self) -> None:
        self.dismiss()


class ExperimentScreen(Screen[None]):
    """Neutral local slates with complete-state partial saves and explicit choices."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "back", "Back"),
        Binding("s", "save", "Save partial"),
        Binding("n", "advance", "Save / next"),
        Binding("c", "choice", "List choice"),
        Binding("p", "preferred", "Preferred side"),
        Binding("b", "best", "Best discovery"),
        Binding("x", "next_experiment", "Experiment"),
        *[
            Binding(str(index), f"label({index - 1})", label, show=False)
            for index, label in enumerate(LABELS, 1)
        ],
    ]

    def __init__(self, store: TuiRecommendationStore, experiment_ids: list[str]) -> None:
        super().__init__()
        self.store = store
        self.experiment_ids = experiment_ids
        self.experiment_index = 0
        self.exposure: BlindedExposure | None = None
        self.judgment = EffectiveJudgment()

    def compose(self) -> ComposeResult:
        yield _InspectableStatic("Blinded local evaluation", id="experiment-title")
        yield DataTable(id="experiment-items", cursor_type="row")
        yield _InspectableStatic("", id="experiment-choices")
        yield _InspectableStatic(
            "Labels on selected artist: "
            + "  ".join(f"{i} {label}" for i, label in enumerate(LABELS, 1))
        )
        yield Input(placeholder="Optional confidence, 0 to 1", id="experiment-confidence")
        yield Input(placeholder="Optional note", id="experiment-note")
        yield _InspectableStatic("", id="experiment-status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#experiment-items", DataTable)
        table.add_columns("Side", "#", "Artist", "Labels", "Best")
        self._load()
        table.focus()

    def _load(self) -> None:
        self.exposure = self.store.next_exposure(self.experiment_ids[self.experiment_index])
        self.judgment = (
            self.exposure.judgment
            if self.exposure and self.exposure.judgment
            else EffectiveJudgment()
        )
        self.query_one("#experiment-confidence", Input).value = (
            "" if self.judgment.confidence is None else str(self.judgment.confidence)
        )
        self.query_one("#experiment-note", Input).value = self.judgment.note or ""
        self._render_judgment()
        status = (
            "Exhausted: all recorded slates completed"
            if self.exposure is None
            else (
                "Resumed saved partial" if self.exposure.judgment else "Persisted slate; unanswered"
            )
        )
        self.query_one("#experiment-status", _InspectableStatic).update(status)

    def _render_judgment(self) -> None:
        table = self.query_one("#experiment-items", DataTable)
        cursor = table.cursor_row
        table.clear()
        if self.exposure:
            for side, items in (("left", self.exposure.left), ("right", self.exposure.right)):
                for item in items:
                    table.add_row(
                        side,
                        str(item.position),
                        item.artist,
                        ", ".join(self.judgment.labels.get(item.artist_id, ())),
                        "*" if item.artist_id == self.judgment.best_discovery_artist_id else "",
                        key=str(item.artist_id),
                    )
            empty = ", ".join(
                side
                for side, items in (("left", self.exposure.left), ("right", self.exposure.right))
                if not items
            )
        else:
            empty = "left, right"
        table.move_cursor(row=min(cursor, max(0, table.row_count - 1)))
        self.query_one("#experiment-choices", _InspectableStatic).update(
            f"List: {self.judgment.list_choice or 'unanswered'} | "
            f"Preferred: {self.judgment.preferred_side or 'unanswered'} | "
            f"Best discovery: {self.judgment.best_discovery_artist_id or 'unanswered'} | "
            f"Empty sides: {empty or 'none'}"
        )

    def _selected(self) -> int | None:
        table = self.query_one("#experiment-items", DataTable)
        if not table.row_count:
            return None
        return int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or "0")

    def action_label(self, index: int) -> None:
        artist = self._selected()
        if artist is None:
            return
        labels = dict(self.judgment.labels)
        selected = set(labels.get(artist, ()))
        label = LABELS[index]
        if label in selected:
            selected.remove(label)
        else:
            selected.add(label)
        if selected:
            labels[artist] = tuple(sorted(selected))
        else:
            labels.pop(artist, None)
        self.judgment = replace(self.judgment, labels=labels)
        self._render_judgment()

    def action_best(self) -> None:
        selected = self._selected()
        self.judgment = replace(
            self.judgment,
            best_discovery_artist_id=(
                None if selected == self.judgment.best_discovery_artist_id else selected
            ),
        )
        self._render_judgment()

    @staticmethod
    def _cycle(value: SideChoice | None) -> SideChoice | None:
        choices: tuple[SideChoice | None, ...] = (None, "left", "right", "tie", "none")
        return choices[(choices.index(value) + 1) % len(choices)]

    def action_choice(self) -> None:
        self.judgment = replace(self.judgment, list_choice=self._cycle(self.judgment.list_choice))
        self._render_judgment()

    def action_preferred(self) -> None:
        self.judgment = replace(
            self.judgment, preferred_side=self._cycle(self.judgment.preferred_side)
        )
        self._render_judgment()

    def _save(self) -> bool:
        if self.exposure is None:
            return False
        try:
            confidence = self.query_one("#experiment-confidence", Input).value.strip()
            self.judgment = replace(
                self.judgment,
                confidence=float(confidence) if confidence else None,
                note=self.query_one("#experiment-note", Input).value or None,
            )
            self.store.judge_exposure(
                self.exposure.exposure_id,
                preferred_side=self.judgment.preferred_side,
                list_choice=self.judgment.list_choice,
                best_discovery_artist_id=self.judgment.best_discovery_artist_id,
                labels=self.judgment.labels,
                confidence=self.judgment.confidence,
                note=self.judgment.note,
            )
        except ValueError as exc:
            self.query_one("#experiment-status", _InspectableStatic).update(f"Cannot save: {exc}")
            return False
        self.query_one("#experiment-status", _InspectableStatic).update(
            "Saved complete judgment"
            if self.judgment.list_choice is not None
            else "Saved partial judgment"
        )
        return True

    def action_save(self) -> None:
        self._save()

    def action_advance(self) -> None:
        if self._save():
            self._load()

    def action_next_experiment(self) -> None:
        self.experiment_index = (self.experiment_index + 1) % len(self.experiment_ids)
        self._load()

    def action_back(self) -> None:
        self.app.pop_screen()


def _join_or_none(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "none"


def _recommendations_plain(rows: list[RecommendationListRow]) -> Text:
    if not rows:
        return Text("No recommendations for the current run.")
    lines = ["#    Artist                         Score  Tags                   Class"]
    for row in rows:
        tags = _truncate(_join_or_none(row.primary_tags), 22)
        classification = row.feedback_state or row.classification
        lines.append(
            f"{row.rank:03d}  {_truncate(row.artist, 28):28}  "
            f"{row.score:.3f}  {tags:22}  {_truncate(classification, 10)}"
        )
    return Text("\n".join(lines))


def _detail_text(detail: RecommendationDetail | None) -> Text:
    if detail is None:
        return Text("No recommendation selected.")

    lines = [
        detail.artist,
        f"rank {detail.rank}",
        "",
        detail.summary or "No explanation summary recorded.",
        "",
        f"Seed artists: {_join_or_none(detail.seed_artists)}",
        f"Tags: {_join_or_none(detail.tags)}",
        f"Status: {_join_or_none(detail.status_labels)}",
    ]
    if detail.classification:
        labels = tuple(name for name, enabled in detail.classification.items() if enabled)
        lines.append(f"Classification: {_join_or_none(labels)}")
    if detail.feedback_history:
        lines.append(f"Feedback history: {_join_or_none(detail.feedback_history)}")
    return Text("\n".join(lines))


def _score_breakdown_text(
    detail: RecommendationDetail | None,
    show_debug_scores: bool,
) -> Text:
    if detail is None:
        return Text("")

    score = detail.score_breakdown
    rows = [
        ("Match", score.display_score),
        ("Final", score.final_score),
        ("Raw", score.raw_score),
        ("Knownness", score.knownness),
        ("Obviousness", score.obviousness),
        ("Feedback", score.feedback_score),
    ]
    lines = [f"{label:11} {value:0.3f}  {score_bar(value)}" for label, value in rows]
    if show_debug_scores:
        lines.extend(
            [
                f"lastfm: {score.lastfm_similarity_score:.3f}",
                f"tags: {score.tag_similarity_score:.3f}",
                f"cluster fit: {score.cluster_fit_score:.3f}",
                f"rediscovery: {score.rediscovery_score:.3f}",
                f"metadata graph: {score.metadata_graph_score:.3f}",
                f"diversity penalty: {score.diversity_penalty:.3f}",
            ]
        )
    return Text("\n".join(lines))


def score_bar(value: float, *, max_value: float = 1.0, width: int = 10) -> str:
    if max_value <= 0 or width <= 0:
        return "[]"
    normalized = max(0.0, min(value / max_value, 1.0))
    filled = round(normalized * width)
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def _truncate(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width == 1:
        return "."
    return value[: width - 1] + "."


def _to_text(renderable: Any) -> Text:
    if isinstance(renderable, Text):
        return renderable
    return Text(str(renderable))


def _help_text() -> Text:
    return Text(
        "\n".join(
            [
                "TUI Help",
                "",
                "Navigation",
                "j / k: move through recommendations",
                "R: switch to another persisted recommendation run",
                "q: quit",
                "",
                "Filters and detail",
                "/: toggle the simple search filter",
                "f: toggle a tag filter from visible rows",
                "e: show or hide debug score components",
                "u: undo is not available in TUI V1",
                "",
                "Feedback",
                "s save: mark this artist as worth revisiting and boost it",
                "r reject: do not recommend this artist in this run",
                "a already know: hide this artist in this run and treat it as known",
                "o too obvious: hide this artist in this run and penalize obvious tags",
                "w wrong genre: this recommendation does not fit its shown tags",
                "m more like this: boost similar future recommendations",
                "l less like this: suppress similar future recommendations",
                "h hide artist: suppress this artist across local recommendations",
                "",
                "Esc or ?: close help",
            ]
        )
    )


def _index_for_artist(rows: list[RecommendationListRow], artist_id: int) -> int:
    for index, row in enumerate(rows):
        if row.artist_id == artist_id:
            return index
    return 0
