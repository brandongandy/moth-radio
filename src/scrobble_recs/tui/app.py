from __future__ import annotations

from typing import Any, ClassVar

from duckdb import DuckDBPyConnection
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static

from scrobble_recs.tui.data import (
    RecommendationDetail,
    RecommendationFilters,
    RecommendationListRow,
    TuiRecommendationStore,
)


class ScrobbleRecsTui(App[None]):
    """Textual browser for persisted recommendation runs."""

    CSS = """
    #body {
        height: 1fr;
    }

    #recommendations {
        width: 42%;
        border: solid $primary;
        padding: 1;
    }

    #right-pane {
        width: 58%;
    }

    #detail {
        height: 1fr;
        border: solid $primary;
        padding: 1;
    }

    #score-breakdown {
        height: auto;
        border: solid $secondary;
        padding: 1;
    }

    #status {
        height: 1;
        padding: 0 1;
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
        Binding("u", "undo_unavailable", "Undo"),
        Binding("?", "help_status", "Help"),
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
        with Horizontal(id="body"):
            yield _InspectableStatic(Text(""), id="recommendations")
            with Vertical(id="right-pane"):
                yield _InspectableStatic(Text(""), id="detail")
                yield _InspectableStatic(Text(""), id="score-breakdown")
        yield _InspectableStatic("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._reload_rows()

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

    def action_feedback(self, action: str) -> None:
        detail = self._selected_detail()
        if detail is None:
            self.query_one("#status", Static).update("No recommendation selected")
            return
        self.query_one("#status", Static).update(
            f"Feedback action reserved for TUI V1 follow-up: {action}"
        )

    def action_undo_unavailable(self) -> None:
        self.query_one("#status", Static).update("Undo is outside TUI V1")

    def action_help_status(self) -> None:
        self.query_one("#status", Static).update(
            "Keys: j/k move, s/r/a/o/w/m/l/h feedback placeholders, u undo, q quit"
        )

    def _reload_rows(self) -> None:
        try:
            if self.run_id is None:
                self.run_id = self.store.latest_run_id()
            self.rows = self.store.list_recommendations(self.filters, run_id=self.run_id)
        except ValueError as exc:
            self.rows = []
            self.selected_index = 0
            self.selected_artist_id = None
            self.query_one("#recommendations", Static).update(Text(""))
            self.query_one("#detail", Static).update(Text(""))
            self.query_one("#score-breakdown", Static).update(Text(""))
            self.query_one("#status", Static).update(str(exc))
            return

        self.selected_index = min(self.selected_index, max(len(self.rows) - 1, 0))
        self._render_all()
        self.query_one("#status", Static).update(self._status_text())

    def _render_all(self) -> None:
        self.query_one("#recommendations", Static).update(self._recommendations_text())
        detail = self._selected_detail()
        self.query_one("#detail", Static).update(self._detail_text(detail))
        self.query_one("#score-breakdown", Static).update(self._score_breakdown_text(detail))

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

    def _recommendations_text(self) -> Text:
        if not self.rows:
            return Text("No recommendations for the current run.")

        text = Text()
        for index, row in enumerate(self.rows):
            marker = ">" if index == self.selected_index else " "
            tags = ", ".join(row.primary_tags) if row.primary_tags else "untagged"
            feedback = f" [{row.feedback_state}]" if row.feedback_state else ""
            text.append(
                f"{marker} #{row.rank:03d} {row.artist} "
                f"score: {row.score:.3f} tags: {tags}{feedback}\n"
            )
        return text

    def _detail_text(self, detail: RecommendationDetail | None) -> Text:
        if detail is None:
            return Text("No recommendation selected.")

        lines = [
            f"{detail.artist}",
            f"rank: {detail.rank}",
            "",
            detail.summary or "No explanation summary recorded.",
            "",
            f"seed artists: {_join_or_none(detail.seed_artists)}",
            f"tags: {_join_or_none(detail.tags)}",
            f"status: {_join_or_none(detail.status_labels)}",
        ]
        if detail.classification:
            labels = [name for name, enabled in detail.classification.items() if enabled]
            lines.append(f"classification: {_join_or_none(tuple(labels))}")
        if detail.feedback_history:
            lines.append(f"feedback history: {_join_or_none(detail.feedback_history)}")
        return Text("\n".join(lines))

    def _score_breakdown_text(self, detail: RecommendationDetail | None) -> Text:
        if detail is None:
            return Text("")

        score = detail.score_breakdown
        lines = [
            f"final: {score.final_score:.3f}",
            f"raw: {score.raw_score:.3f}",
            f"knownness: {score.knownness:.3f}",
            f"obviousness: {score.obviousness:.3f}",
            f"feedback: {score.feedback_score:.3f}",
        ]
        if self.show_debug_scores:
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

    def _status_text(self) -> str:
        run_label = self.run_id or "none"
        return f"run: {run_label}; recommendations: {len(self.rows)}"


def _join_or_none(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "none"


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


def _to_text(renderable: Any) -> Text:
    if isinstance(renderable, Text):
        return renderable
    return Text(str(renderable))
