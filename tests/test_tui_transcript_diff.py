"""Transcript render fingerprints, the diff redraw route, and row bookkeeping.

These tests prove the per-row bookkeeping of the transcript render pipeline:
every message row records a fingerprint over its render inputs plus its
derived selection and markdown text, streaming blocks recompute their
fingerprint at finalization and at the item swap, hidden-thinking placeholder
rows share one sentinel item so their render inputs are stable across
redraws, and boundary markers record no fingerprint because they are not
item-backed rows. The diff-route tests prove the display-state refresh reuses
mounted rows whose render inputs still match, remounts nothing over unchanged
state, touches only changed rows on small changes, and keeps the streaming,
boundary, and full-rebuild behavior of the baseline pipeline.
"""

import asyncio
import contextlib
import time
from collections.abc import Iterator
from typing import Any

import pytest
from markdown_it import MarkdownIt
from textual.widget import Widget

from tau_agent import (
    AgentEndEvent,
    AgentStartEvent,
    AgentToolResult,
    AssistantMessage,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolCall,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    UserMessage,
)
from tau_agent.provider_events import TextDeltaEvent, ThinkingDeltaEvent
from tau_coding.tui import widgets as tui_widgets
from tau_coding.tui.app import TauTuiApp
from tau_coding.tui.config import TAU_DARK_THEME, TAU_LIGHT_THEME, TuiTheme
from tau_coding.tui.state import ChatItem, TuiState
from tau_coding.tui.widgets import (
    _HIDDEN_THINKING_PLACEHOLDER_ITEM,
    TRANSCRIPT_WINDOW_ITEMS,
    StreamingTranscriptMessageWidget,
    ThemedMarkdownWidget,
    TranscriptMessageWidget,
    TranscriptView,
    TranscriptWindowBoundary,
)
from test_tui_app import FakeSession  # noqa: E402 - sibling test module


class _FakeMarkdownStream:
    """Stand-in for Textual's `MarkdownStream` that records writes only."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.stopped = False

    async def write(self, text: str) -> None:
        """Record a coalesced write without touching any real DOM."""
        self.writes.append(text)

    async def stop(self) -> None:
        """Record the stream being stopped."""
        self.stopped = True


class _DoubleStreamWidget(StreamingTranscriptMessageWidget):
    """Streaming widget that serves the recording double through `get_stream`."""

    def __init__(self, item: ChatItem, *, theme: TuiTheme, stream: _FakeMarkdownStream) -> None:
        self._stream_double = stream
        super().__init__(item, theme=theme)

    def get_stream(self, widget: Any) -> Any:
        """Return the injected double instead of a real Textual stream."""
        return self._stream_double


def _message_widget(
    item: ChatItem | None = None,
    *,
    theme: TuiTheme = TAU_DARK_THEME,
    show_tool_results: bool = True,
    custom_markup: str | None = None,
    invocation: str | None = "ls -la",
    result_markup: str | None = "file.txt",
) -> TranscriptMessageWidget:
    """Build a message widget whose defaults describe one expanded tool row."""
    if item is None:
        item = ChatItem(role="tool", text="ls -la", tool_name="bash", tool_result_text="file.txt")
    return TranscriptMessageWidget(
        item,
        theme=theme,
        show_tool_results=show_tool_results,
        custom_markup=custom_markup,
        invocation=invocation,
        result_markup=result_markup,
    )


def _tool_item(**overrides: Any) -> ChatItem:
    """Return the base expanded tool row item with per-case field overrides."""
    fields: dict[str, Any] = {
        "role": "tool",
        "text": "ls -la",
        "tool_name": "bash",
        "tool_result_text": "file.txt",
    }
    fields.update(overrides)
    return ChatItem(**fields)


@pytest.mark.parametrize(
    ("input_name", "base_kwargs", "variant_kwargs"),
    [
        ("theme", {}, {"theme": TAU_LIGHT_THEME}),
        (
            "role",
            {"item": ChatItem(role="status", text="x")},
            {"item": ChatItem(role="compaction_summary", text="x")},
        ),
        ("text", {}, {"item": _tool_item(text="ls -l")}),
        ("tool result text", {}, {"item": _tool_item(tool_result_text="other.txt")}),
        ("update text", {}, {"item": _tool_item(update_text="listing…")}),
        ("highlight", {}, {"item": _tool_item(highlight="alert")}),
        ("tool-result visibility", {}, {"show_tool_results": False}),
        ("invocation", {}, {"invocation": "ls -la (cached)"}),
        ("result markup", {}, {"result_markup": "other.txt"}),
        (
            "custom markup",
            {"item": ChatItem(role="custom", text="todo body", custom_type="todo")},
            {
                "item": ChatItem(role="custom", text="todo body", custom_type="todo"),
                "custom_markup": "[red]colored body[/red]",
            },
        ),
        ("plain-body flag", {}, {"item": _tool_item(plain_text=True)}),
    ],
)
def test_fingerprint_changes_per_render_input(
    input_name: str,
    base_kwargs: dict[str, Any],
    variant_kwargs: dict[str, Any],
) -> None:
    """Changing one render input changes the message row's fingerprint.

    Each case builds two widgets that differ in exactly one render input, at
    the site where the widget stores it: invocation and result markup vary on
    tool rows, custom markup varies on custom rows, and the remaining inputs
    vary directly through the item or the constructor arguments. The role case
    compares two roles that store none of the role-specific inputs (invocation,
    result markup, and custom markup are all ``None`` for both) and derive
    identical selection and markdown text, so the role alone differs.
    """
    base_widget = _message_widget(**base_kwargs)
    variant_widget = _message_widget(**variant_kwargs)

    assert variant_widget.render_fingerprint != base_widget.render_fingerprint, input_name


def test_fingerprint_covers_batch_member_content() -> None:
    """A batch member's update text changes the batch row's fingerprint.

    The batch parent's own text fields stay unchanged, so the member content
    only reaches the row through the derived selection text; the fingerprint
    must include that derived content to see the change. The in-place update
    route is what recomputes the row's derived content after the mutation.
    """
    member = ChatItem(
        role="tool", text="make check", tool_name="bash", tool_arguments={"command": "make check"}
    )
    batch = ChatItem(role="tool", text="2 batched commands", tool_batch_items=[member])
    batch_invocation = "make check"
    widget = _message_widget(item=batch, invocation=batch_invocation)
    before = widget.render_fingerprint

    member.update_text = "tests running…"
    widget.refresh_invocation(show_tool_results=True, invocation=batch_invocation)

    assert widget.render_fingerprint != before


def test_fingerprint_pure_across_unchanged_render_early_return() -> None:
    """The early-return update path keeps the fingerprint a pure input function.

    The unchanged-render early return stores a new ``show_tool_results`` flag
    without recomputing the cached markdown text, so the fingerprint includes
    markdown text only for markdown-body rows. A plain-body tool row that
    early-returns must therefore produce the same fingerprint as a freshly
    constructed widget with the same current inputs.
    """
    widget = _message_widget(
        item=_tool_item(tool_result_text="file.txt"),
        show_tool_results=True,
        invocation="ls -la",
        result_markup="FILE MARKUP",
    )
    widget.refresh_invocation(
        show_tool_results=False,
        invocation="ls -la",
        result_markup="FILE MARKUP",
    )

    fresh = _message_widget(
        item=_tool_item(tool_result_text="file.txt"),
        show_tool_results=False,
        invocation="ls -la",
        result_markup="FILE MARKUP",
    )

    assert widget.render_fingerprint == fresh.render_fingerprint


def test_fingerprint_stable_for_equal_inputs() -> None:
    """Two widgets built from equal render inputs produce equal fingerprints.

    The two items are distinct objects with equal fields, so equal
    fingerprints prove the fingerprint is value-based rather than
    identity-based.
    """
    first = _message_widget()
    second = _message_widget()

    assert first.item is not second.item
    assert first.item == second.item
    assert first.render_fingerprint == second.render_fingerprint


@pytest.mark.anyio
async def test_streaming_fingerprint_recomputed_on_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """finalize() recomputes the streaming fingerprint over the full text."""
    monkeypatch.setattr(tui_widgets, "_STREAM_FLUSH_INTERVAL", 0.5)
    widget = _DoubleStreamWidget(
        ChatItem(role="assistant", text=""),
        theme=TAU_DARK_THEME,
        stream=_FakeMarkdownStream(),
    )
    before = widget.render_fingerprint

    await widget.append_fragment("Hello")
    await widget.append_fragment(" world")
    # Streaming appends are not a recompute point, so the fingerprint still
    # holds the construction-time text until finalization.
    assert widget.render_fingerprint == before

    await widget.finalize()

    assert widget.render_fingerprint == (TAU_DARK_THEME.name, "assistant", "Hello world")


def test_streaming_fingerprint_recomputed_on_item_swap() -> None:
    """refresh_render_fingerprint() picks up the swapped item's text."""
    widget = StreamingTranscriptMessageWidget(
        ChatItem(role="assistant", text="draft"), theme=TAU_DARK_THEME
    )
    before = widget.render_fingerprint

    widget.item = ChatItem(role="assistant", text="canonical answer")
    widget.refresh_render_fingerprint()

    assert widget.render_fingerprint == (TAU_DARK_THEME.name, "assistant", "canonical answer")
    assert widget.render_fingerprint != before


@pytest.mark.anyio
async def test_hidden_thinking_placeholder_uses_shared_sentinel() -> None:
    """Every mounted hidden-thinking placeholder row carries the sentinel item.

    Sharing one item by identity is what keeps a placeholder row's render
    inputs, and therefore its fingerprint, stable across redraws.
    """
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        for index in range(3):
            state.add_item("thinking", f"thought {index}")

        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        placeholders = [
            widget
            for widget in app.query(TranscriptMessageWidget)
            if widget.item.role == "thinking"
        ]
        assert len(placeholders) == 1
        assert placeholders[0].item is tui_widgets._HIDDEN_THINKING_PLACEHOLDER_ITEM


def test_boundary_has_no_fingerprint() -> None:
    """Boundary markers are not item-backed rows, so they record no fingerprint."""
    boundary = TranscriptWindowBoundary("later", 3)

    assert not hasattr(boundary, "render_fingerprint")


# ---------------------------------------------------------------------------
# Diff-based redraw on the display-state refresh route (Task 2)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _frozen_monotonic(clock: dict[str, float]) -> Iterator[None]:
    """Freeze ``time.monotonic`` across one synchronous render only.

    ``tau_coding.tui.state`` shares the stdlib ``time`` module, so patching its
    ``monotonic`` is a global patch. Scoped to the synchronous refresh call it
    advances only the tool-row elapsed suffix; left installed across an
    ``await`` it freezes the event loop's own clock and hangs the test.
    """
    real_monotonic = time.monotonic

    def fake_monotonic() -> float:
        return clock["now"]

    time.monotonic = fake_monotonic  # type: ignore[assignment]
    try:
        yield
    finally:
        time.monotonic = real_monotonic  # type: ignore[assignment]


@contextlib.contextmanager
def _counted_row_changes(
    transcript: TranscriptView,
) -> Iterator[tuple[list[Widget], list[Widget]]]:
    """Count the widgets mounted on and removed from the transcript while active.

    Counts widgets rather than calls so one batched mount or removal of several
    rows still reports each row.
    """
    mounted: list[Widget] = []
    removed: list[Widget] = []
    original_mount = transcript.mount
    original_remove_children = transcript.remove_children

    def counting_mount(*widgets: Widget, **kwargs: object) -> object:
        mounted.extend(widgets)
        return original_mount(*widgets, **kwargs)

    def counting_remove_children(children: object) -> object:
        removed.extend(list(children))  # type: ignore[arg-type]
        return original_remove_children(children)

    transcript.mount = counting_mount  # type: ignore[method-assign]
    transcript.remove_children = counting_remove_children  # type: ignore[method-assign]
    try:
        yield mounted, removed
    finally:
        transcript.mount = original_mount  # type: ignore[method-assign]
        transcript.remove_children = original_remove_children  # type: ignore[method-assign]


@contextlib.contextmanager
def _spied_render_routes(
    transcript: TranscriptView,
) -> Iterator[tuple[list[str], list[bool]]]:
    """Record the render route (rebuild or diff) and scroll_end of each refresh."""
    routes: list[str] = []
    scroll_ends: list[bool] = []
    original_redraw = transcript._redraw
    original_diff_redraw = transcript._diff_redraw

    def spy_redraw(*, scroll_end: bool, preserve_window: bool = False) -> None:
        routes.append("rebuild")
        scroll_ends.append(scroll_end)
        original_redraw(scroll_end=scroll_end, preserve_window=preserve_window)

    def spy_diff_redraw(*, scroll_end: bool) -> None:
        routes.append("diff")
        scroll_ends.append(scroll_end)
        original_diff_redraw(scroll_end=scroll_end)

    transcript._redraw = spy_redraw  # type: ignore[method-assign]
    transcript._diff_redraw = spy_diff_redraw  # type: ignore[method-assign]
    try:
        yield routes, scroll_ends
    finally:
        transcript._redraw = original_redraw  # type: ignore[method-assign]
        transcript._diff_redraw = original_diff_redraw  # type: ignore[method-assign]


def _message_rows(
    transcript: TranscriptView,
) -> list[TranscriptMessageWidget | StreamingTranscriptMessageWidget]:
    """Return the transcript's mounted message rows in DOM order.

    A row pruned earlier in the same event-loop turn stays in ``children`` until
    Textual finishes removing it, so the prune flag filters it out.
    """
    return [
        child
        for child in transcript.children
        if isinstance(child, TranscriptMessageWidget | StreamingTranscriptMessageWidget)
        and not child._pruning
    ]


def _mounted_row(transcript: TranscriptView, item: ChatItem) -> Widget:
    row = transcript._item_widgets.get(id(item))
    assert row is not None
    return row


def _read_tool_item(**overrides: Any) -> ChatItem:
    """Return a completed read tool row with no timer and per-case fields."""
    fields: dict[str, Any] = {
        "role": "tool",
        "text": "→ read README.md",
        "tool_call_id": "call-1",
        "tool_name": "read",
        "tool_result_text": "✓ read\nold output",
    }
    fields.update(overrides)
    return ChatItem(**fields)


@pytest.mark.anyio
async def test_route_selection_conditions() -> None:
    """Only the route conditions select the full rebuild; otherwise rows diff.

    A new state object, a same-state refresh with no retained rows, and a theme
    change take the full rebuild; a same-state refresh with retained rows takes
    the diff render. The new-state-object rebuild re-enables follow mode before
    the render, visible as scroll_end=True on the rebuild call.
    """
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        for index in range(12):
            state.add_item("assistant", f"message {index}\n\nsecond line {index}")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        transcript.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert transcript._follow_output is False

        with _spied_render_routes(transcript) as (routes, scroll_ends):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
            assert routes == ["diff"]
            assert scroll_ends == [False]

            transcript.update_from_state(state, theme=TAU_LIGHT_THEME)
            assert routes[-1] == "rebuild"

            transcript._item_widgets.clear()
            transcript.update_from_state(state, theme=TAU_LIGHT_THEME)
            assert routes[-1] == "rebuild"
            assert scroll_ends[-1] is True

            new_state = TuiState()
            new_state.add_item("user", "fresh session state")
            transcript._follow_output = False
            transcript.update_from_state(new_state, theme=TAU_LIGHT_THEME)
            assert routes == ["diff", "rebuild", "rebuild", "rebuild"]
            assert scroll_ends[-1] is True
            assert transcript._follow_output is True


@pytest.mark.anyio
async def test_second_identical_refresh_remounts_nothing() -> None:
    """Two identical refreshes over settled state mount and remove nothing.

    The first render over a populated state mounts every row because the
    bookkeeping starts empty; the two counted identical refreshes that follow
    take the diff render, keep every widget object, and perform no DOM work.
    """
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "hello")
        state.add_item("assistant", "hi there")
        state.items.append(_read_tool_item(tool_result_text="✓ read\ncontents"))
        state.add_item("status", "… working")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        widgets_before = list(transcript.children)

        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
            transcript.update_from_state(state, theme=TAU_DARK_THEME)

        assert mounted == []
        assert removed == []
        assert list(transcript.children) == widgets_before


@pytest.mark.anyio
async def test_fast_path_skips_layout_refresh() -> None:
    """The second identical refresh performs no refresh(layout=True) call."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "hello")
        state.add_item("assistant", "hi there")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        layout_refreshes: list[bool] = []
        original_refresh = transcript.refresh

        def counting_refresh(*args: object, **kwargs: object) -> object:
            if kwargs.get("layout"):
                layout_refreshes.append(True)
            return original_refresh(*args, **kwargs)

        transcript.refresh = counting_refresh  # type: ignore[method-assign]
        try:
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        finally:
            transcript.refresh = original_refresh  # type: ignore[method-assign]

        assert layout_refreshes == []


@pytest.mark.anyio
async def test_retry_notice_removed_by_every_diff_render() -> None:
    """Every diff render removes a displayed retry notice and nothing else."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "hello")
        state.add_item("assistant", "hi there")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        await transcript.discard_active_assistant("… Connection lost — retrying 1/3: boom")
        await pilot.pause()
        notice = transcript._retry_notice_widget
        assert notice is not None
        assert notice.parent is transcript
        widgets_before = [widget for widget in transcript.children if widget is not notice]

        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)

        assert mounted == []
        assert removed == [notice]
        assert transcript._retry_notice_widget is None
        assert [widget for widget in transcript.children if widget is not notice] == widgets_before

        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)

        assert mounted == []
        assert removed == []


@pytest.mark.anyio
async def test_tool_result_change_replaces_exactly_one_row() -> None:
    """Changing one tool result replaces exactly that row and nothing else."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.show_tool_results = True
        state.add_item("user", "list the files")
        tool_item = _read_tool_item()
        state.items.append(tool_item)
        state.add_item("assistant", "here they are")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        rows_before = _message_rows(transcript)
        tool_row_before = _mounted_row(transcript, tool_item)

        tool_item.tool_result_text = "✓ read\nnew output"
        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert len(mounted) == 1
        assert removed == [tool_row_before]
        assert _mounted_row(transcript, tool_item) is mounted[0]
        rows_after = _message_rows(transcript)
        expected = [mounted[0] if row is tool_row_before else row for row in rows_before]
        assert rows_after == expected


@pytest.mark.anyio
async def test_in_place_update_keeps_fingerprint_current() -> None:
    """A refresh after an in-place progress update keeps the updated row.

    `update_item` re-renders the row in place and recomputes its fingerprint;
    the later refresh must reuse the row instead of replacing it, which is what
    keeps live tool progress from remounting on every display-state refresh.
    """
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "run the checks")
        tool_item = ChatItem(
            role="tool",
            text="→ agent explore",
            tool_call_id="call-1",
            tool_name="agent",
        )
        state.items.append(tool_item)
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        row_before = _mounted_row(transcript, tool_item)

        tool_item.update_text = "scanning files…"
        assert await transcript.update_item(tool_item, theme=TAU_DARK_THEME) is True
        assert _mounted_row(transcript, tool_item) is row_before
        assert "scanning files…" in row_before.selection_text

        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)

        assert mounted == []
        assert removed == []
        assert _mounted_row(transcript, tool_item) is row_before


@pytest.mark.anyio
async def test_tail_append_mounts_only_new_row() -> None:
    """A tail append mounts exactly the new row below the compaction threshold."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "hello")
        state.add_item("assistant", "hi there")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        state.add_item("status", "appended tail")
        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert len(mounted) == 1
        assert removed == []
        assert mounted[0].item is state.items[-1]
        assert _mounted_row(transcript, state.items[-1]) is mounted[0]
        assert transcript._window_end == len(state.items)


@pytest.mark.anyio
async def test_tail_removal_removes_only_that_row() -> None:
    """Removing the last display item removes exactly that row."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "hello")
        state.add_item("assistant", "hi there")
        state.add_item("status", "tail")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        tail_row = _mounted_row(transcript, state.items[-1])

        state.items.pop()
        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert mounted == []
        assert removed == [tail_row]


@pytest.mark.anyio
async def test_positional_shift_remounts_shifted_suffix() -> None:
    """An insertion or a removal before the tail remounts the shifted suffix.

    The insertion variant proves rows before the insertion keep identity while
    rows at and after it are new. The removal variant proves the same for a
    removal and exercises the suffix removal range that runs past the end of
    the desired row sequence.
    """
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        for index in range(4):
            state.add_item("assistant", f"message {index}")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        rows_before = {id(item): _mounted_row(transcript, item) for item in state.items}
        displaced_rows = [rows_before[id(item)] for item in state.items[1:]]

        inserted = ChatItem(role="user", text="inserted")
        state.items.insert(1, inserted)
        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert _mounted_row(transcript, state.items[0]) is rows_before[id(state.items[0])]
        for item in state.items[2:]:
            row = _mounted_row(transcript, item)
            assert row.parent is transcript
            assert row is not rows_before[id(item)]
        assert _mounted_row(transcript, inserted) in mounted
        assert len(mounted) == 4
        assert len(removed) == 3
        for row in displaced_rows:
            assert row in removed

        # Removal variant: the settled state is [A, inserted, B, C, D]; removing
        # B shifts C and D one position earlier.
        rows_after_insert = {id(item): _mounted_row(transcript, item) for item in state.items}
        removed_item = state.items.pop(2)
        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert _mounted_row(transcript, state.items[0]) is rows_after_insert[id(state.items[0])]
        assert _mounted_row(transcript, state.items[1]) is rows_after_insert[id(state.items[1])]
        for item in state.items[2:]:
            assert _mounted_row(transcript, item) is not rows_after_insert[id(item)]
        assert rows_after_insert[id(removed_item)] in removed
        assert len(removed) == 3
        assert len(mounted) == 2


@pytest.mark.anyio
async def test_following_viewport_stays_pinned_on_diff_render() -> None:
    """In follow mode a diff render keeps the viewport pinned to the bottom."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        for index in range(12):
            state.add_item("assistant", f"message {index}\n\nsecond line {index}")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        assert transcript._follow_output is True

        state.add_item("assistant", "newest message\n\nwith detail")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert transcript.is_vertical_scroll_end


@pytest.mark.anyio
async def test_scrolled_up_viewport_does_not_jump_on_diff_render() -> None:
    """Scrolled away from the bottom, a diff render preserves the scroll position."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.show_tool_results = True
        state.add_item("user", "list the files")
        tool_item = _read_tool_item()
        state.items.append(tool_item)
        for index in range(10):
            state.add_item("assistant", f"message {index}\n\nsecond line {index}")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        transcript.scroll_to(y=4, animate=False)
        await pilot.pause()
        scroll_before = transcript.scroll_y
        assert transcript._follow_output is False

        tool_item.tool_result_text = "✓ read\nnew output"
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert transcript.scroll_y == scroll_before
        assert transcript.is_vertical_scroll_end is False


@pytest.mark.anyio
async def test_running_tool_row_remounts_on_elapsed_advance() -> None:
    """An advancing elapsed suffix remounts only the running tool row."""
    app = TauTuiApp(FakeSession())
    clock = {"now": 1000.0}

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "run the agent")
        tool_item = ChatItem(
            role="tool",
            text="→ agent explore",
            tool_call_id="call-1",
            tool_name="agent",
            started_at=940.0,
        )
        state.items.append(tool_item)
        with _frozen_monotonic(clock):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        tool_row_before = _mounted_row(transcript, tool_item)
        assert "(1m 0s)" in tool_row_before.selection_text

        clock["now"] = 1061.0
        with (
            _counted_row_changes(transcript) as (mounted, removed),
            _frozen_monotonic(clock),
        ):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert mounted == [_mounted_row(transcript, tool_item)]
        assert removed == [tool_row_before]
        assert "(2m 1s)" in mounted[0].selection_text


@pytest.mark.anyio
async def test_boundary_counts_update_without_remount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preserved window updates boundary counts without remounting them."""
    monkeypatch.setattr(tui_widgets, "TRANSCRIPT_WINDOW_ITEMS", 6)
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        for index in range(12):
            state.add_item("user", f"message {index}")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        assert transcript._window_start == 6

        transcript.scroll_to(y=2, animate=False)
        await pilot.pause()
        assert transcript._follow_output is False

        # The first append constructs the missing bottom boundary; the counted
        # append that follows must reuse both boundaries with count updates.
        state.add_item("user", "extra 0")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        top_boundary = transcript._top_boundary
        bottom_boundary = transcript._bottom_boundary
        assert top_boundary is not None
        assert bottom_boundary is not None

        state.add_item("user", "extra 1")
        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert mounted == []
        assert removed == []
        assert transcript._top_boundary is top_boundary
        assert transcript._bottom_boundary is bottom_boundary
        assert "Scroll for 2 later" in str(bottom_boundary.content)
        assert "Scroll for 6 earlier" in str(top_boundary.content)


@pytest.mark.anyio
async def test_boundary_removed_when_window_extends_to_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A diff render that extends the window to the latest item removes the boundary."""
    monkeypatch.setattr(tui_widgets, "TRANSCRIPT_WINDOW_ITEMS", 6)
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        for index in range(12):
            state.add_item("user", f"message {index}")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        transcript.scroll_to(y=2, animate=False)
        await pilot.pause()
        state.add_item("user", "extra 0")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        bottom_boundary = transcript._bottom_boundary
        assert bottom_boundary is not None

        transcript.follow_output()
        # No pause before the counted refresh: the follow scroll crosses
        # the bottom edge and schedules the baseline window shift, whose full
        # rebuild extends the window before the diff render runs.
        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert transcript._bottom_boundary is None
        assert bottom_boundary in removed
        assert not any(
            isinstance(child, TranscriptWindowBoundary) and child.direction == "later"
            for child in _message_rows(transcript)
        )


@pytest.mark.anyio
async def test_hidden_thinking_run_collapses_to_one_row() -> None:
    """A run of hidden thinking items renders one shared placeholder row."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "plan")
        for index in range(3):
            state.add_item("thinking", f"thought {index}")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        placeholders = [
            row
            for row in _message_rows(transcript)
            if row.item is _HIDDEN_THINKING_PLACEHOLDER_ITEM
        ]
        assert len(placeholders) == 1
        placeholder = placeholders[0]
        thinking_items = [item for item in state.items if item.role == "thinking"]
        assert {id(_mounted_row(transcript, item)) for item in thinking_items} == {id(placeholder)}

        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)

        assert mounted == []
        assert removed == []
        assert _mounted_row(transcript, thinking_items[0]) is placeholder


@pytest.mark.anyio
async def test_placeholder_flag_matches_rebuild_rule_after_refresh() -> None:
    """A diff render adopts the delta-mounted placeholder row for the run.

    The placeholder that a hidden-thinking delta mounts is untracked and stores
    the delta site's visibility flag, so the diff render must adopt it through
    the row-kind-and-fingerprint disjunct, register it for every item of the
    run, and keep the placeholder flag true so the next hidden delta adds no
    second placeholder row.
    """
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "plan")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        state.add_thinking_delta("hidden thought")
        await transcript.append_thinking_delta(
            "hidden thought", theme=TAU_DARK_THEME, show_thinking=False
        )
        await pilot.pause()
        placeholder = next(
            row
            for row in _message_rows(transcript)
            if row.item is _HIDDEN_THINKING_PLACEHOLDER_ITEM
        )
        thinking_item = state.items[-1]
        assert transcript._item_widgets.get(id(thinking_item)) is not placeholder

        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert mounted == []
        assert removed == []
        assert _mounted_row(transcript, thinking_item) is placeholder
        assert transcript._hidden_thinking_placeholder_visible is True

        state.add_thinking_delta(" more")
        await transcript.append_thinking_delta(" more", theme=TAU_DARK_THEME, show_thinking=False)
        await pilot.pause()

        placeholders = [
            row
            for row in _message_rows(transcript)
            if row.item is _HIDDEN_THINKING_PLACEHOLDER_ITEM
        ]
        assert placeholders == [placeholder]


@pytest.mark.anyio
async def test_assistant_stream_survives_refresh() -> None:
    """A refresh during an assistant stream keeps the same streaming widget."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "hello")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        for delta in ("Hello ", "world"):
            state.assistant_buffer += delta
            await transcript.append_assistant_delta(delta, theme=TAU_DARK_THEME)
        await pilot.pause()
        streamed = transcript._active_assistant_widget
        assert streamed is not None

        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert transcript._active_assistant_widget is streamed
        assert streamed.parent is transcript
        assert streamed.selection_text == "Hello world"

        await streamed.finalize()
        assert streamed.selection_text == "Hello world"
        assert streamed.has_class("-finalized")


@pytest.mark.anyio
async def test_no_bottom_marker_while_stream_shows() -> None:
    """The active assistant stream at the latest window shows no bottom marker."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "hello")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        state.assistant_buffer += "streaming answer"
        await transcript.append_assistant_delta("streaming answer", theme=TAU_DARK_THEME)
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert transcript._active_assistant_widget is not None
        assert transcript._bottom_boundary is None
        assert not [
            child
            for child in _message_rows(transcript)
            if isinstance(child, TranscriptWindowBoundary) and child.direction == "later"
        ]


@pytest.mark.anyio
async def test_constructed_from_buffer_block_registers_bookkeeping() -> None:
    """A block constructed from the buffer registers the active bookkeeping."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "hello")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        state.assistant_buffer = "partial answer"
        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert removed == []
        assert len(mounted) == 1
        block = mounted[0]
        assert isinstance(block, StreamingTranscriptMessageWidget)
        assert transcript._active_assistant_widget is block
        assert block in transcript._active_message_widgets
        assert block.selection_text == "partial answer"

        state.assistant_buffer += " more"
        await transcript.append_assistant_delta(" more", theme=TAU_DARK_THEME)
        await pilot.pause()

        assert transcript._active_assistant_widget is block
        assert block.selection_text == "partial answer more"


@pytest.mark.anyio
async def test_thinking_stream_refresh_keeps_baseline() -> None:
    """A refresh during a thinking stream re-renders from state (baseline)."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.show_thinking = True
        state.add_item("user", "hello")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        state.add_thinking_delta("thinking out loud")
        await transcript.append_thinking_delta(
            "thinking out loud", theme=TAU_DARK_THEME, show_thinking=True
        )
        await pilot.pause()
        streaming_block = transcript._active_thinking_widget
        assert streaming_block is not None

        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert removed == [streaming_block]
        assert transcript._active_thinking_widget is None
        thinking_item = state.items[-1]
        rerendered = _mounted_row(transcript, thinking_item)
        assert isinstance(rerendered, TranscriptMessageWidget)
        assert rerendered.selection_text == "thinking out loud"

        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)

        assert mounted == []
        assert removed == []
        assert _mounted_row(transcript, thinking_item) is rerendered

        state.add_thinking_delta(" and more")
        await transcript.append_thinking_delta(
            " and more", theme=TAU_DARK_THEME, show_thinking=True
        )
        await pilot.pause()

        new_block = transcript._active_thinking_widget
        assert isinstance(new_block, StreamingTranscriptMessageWidget)
        assert new_block is not streaming_block


@pytest.mark.anyio
async def test_finalized_row_keeps_widget_across_refresh() -> None:
    """A finalized streamed message keeps its widget across later refreshes."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "hello")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        for delta in ("Hello ", "world"):
            state.assistant_buffer += delta
            await transcript.append_assistant_delta(delta, theme=TAU_DARK_THEME)
        canonical = ChatItem(role="assistant", text="Hello world")
        state.items.append(canonical)
        state.assistant_buffer = ""
        await transcript.finish_assistant_message("Hello world", item=canonical)
        await pilot.pause()
        finalized = _mounted_row(transcript, canonical)

        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)

        assert mounted == []
        assert removed == []
        assert _mounted_row(transcript, canonical) is finalized
        assert finalized.selection_text == "Hello world"


@pytest.mark.anyio
async def test_removed_stream_block_clears_bookkeeping() -> None:
    """An emptied buffer drops the streaming block and its bookkeeping."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "hello")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        state.assistant_buffer += "draft text"
        await transcript.append_assistant_delta("draft text", theme=TAU_DARK_THEME)
        await pilot.pause()
        block = transcript._active_assistant_widget
        assert block is not None

        state.items.append(ChatItem(role="assistant", text="draft text"))
        state.assistant_buffer = ""
        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert removed == [block]
        assert transcript._active_assistant_widget is None
        assert transcript._active_message_widgets == []
        # The flushed canonical item is new to the window, so the diff render
        # mounts exactly its row while dropping the streaming block.
        canonical_row = _mounted_row(transcript, state.items[-1])
        assert mounted == [canonical_row]
        assert canonical_row.selection_text == "draft text"

        state.assistant_buffer += "next "
        await transcript.append_assistant_delta("next ", theme=TAU_DARK_THEME)
        await pilot.pause()

        new_block = transcript._active_assistant_widget
        assert isinstance(new_block, StreamingTranscriptMessageWidget)
        assert new_block is not block


@pytest.mark.anyio
async def test_theme_change_rebuilds_window_batched() -> None:
    """A theme change takes the batched full rebuild with all-new rows."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "hello")
        state.add_item("assistant", "hi there")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        rows_before = _message_rows(transcript)
        texts_before = [row.selection_text for row in rows_before]

        batch_entries: list[int] = []
        original_batch_update = app.batch_update

        @contextlib.contextmanager
        def counting_batch_update() -> Iterator[None]:
            batch_entries.append(1)
            with original_batch_update():
                yield

        app.batch_update = counting_batch_update  # type: ignore[method-assign]
        try:
            transcript.update_from_state(state, theme=TAU_LIGHT_THEME)
        finally:
            app.batch_update = original_batch_update  # type: ignore[method-assign]
        await pilot.pause()

        assert batch_entries == [1]
        rows_after = _message_rows(transcript)
        assert [row.selection_text for row in rows_after] == texts_before
        assert not {id(row) for row in rows_after} & {id(row) for row in rows_before}


@pytest.mark.anyio
async def test_session_switch_rebuilds_in_batch() -> None:
    """Clearing and reloading the same state object rebuilds fully, batched."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "old session user")
        state.add_item("assistant", "old session answer")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        rows_before = _message_rows(transcript)

        batch_entries: list[int] = []
        original_batch_update = app.batch_update

        @contextlib.contextmanager
        def counting_batch_update() -> Iterator[None]:
            batch_entries.append(1)
            with original_batch_update():
                yield

        app.batch_update = counting_batch_update  # type: ignore[method-assign]
        try:
            state.clear()
            state.load_messages(
                [
                    UserMessage(content="new session user"),
                    AssistantMessage(content="new session answer"),
                ]
            )
            transcript.update_from_state(state, theme=TAU_DARK_THEME)
        finally:
            app.batch_update = original_batch_update  # type: ignore[method-assign]
        await pilot.pause()

        assert batch_entries == [1]
        assert transcript._follow_output is True
        rows_after = _message_rows(transcript)
        assert [row.selection_text for row in rows_after] == [
            "new session user",
            "new session answer",
        ]
        assert not {id(row) for row in rows_after} & {id(row) for row in rows_before}


@pytest.mark.anyio
async def test_full_rebuild_wraps_batch_update_and_checks_mounted() -> None:
    """_redraw batches its DOM work and returns early before mount."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "hello")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        batch_entries: list[int] = []
        original_batch_update = app.batch_update

        @contextlib.contextmanager
        def counting_batch_update() -> Iterator[None]:
            batch_entries.append(1)
            with original_batch_update():
                yield

        app.batch_update = counting_batch_update  # type: ignore[method-assign]
        try:
            transcript._redraw(scroll_end=False)
        finally:
            app.batch_update = original_batch_update  # type: ignore[method-assign]

        assert batch_entries == [1]

    unmounted = TranscriptView()
    unmounted._render_state = TuiState()
    unmounted._render_theme = TAU_DARK_THEME
    unmounted._redraw(scroll_end=False)


def _mixed_state(*, show_thinking: bool) -> TuiState:
    """Build one transcript state covering every row kind the diff must render."""
    state = TuiState()
    state.show_thinking = show_thinking
    state.show_tool_results = True
    state.add_user_message("plan the refactor")
    state.add_assistant_message(
        AssistantMessage(content="Plan:\n\n1. Split the module\n2. Add tests")
    )
    state.add_item("thinking", "weighing the module split")
    group_batch_id = state.new_tool_batch_id()
    state.add_tool_call(
        ToolCall(id="call-a", name="read", arguments={"path": "a.py"}),
        batch_id=group_batch_id,
    )
    state.add_tool_call(
        ToolCall(id="call-b", name="read", arguments={"path": "b.py"}),
        batch_id=group_batch_id,
    )
    state.record_tool_result("call-a", "read", AgentToolResult(content="a contents"), False)
    state.record_tool_result("call-b", "read", AgentToolResult(content="b contents"), False)
    batch_id = state.new_tool_batch_id()
    state.add_tool_call(
        ToolCall(id="call-c", name="bash", arguments={"command": "make check"}),
        batch_id=batch_id,
    )
    state.add_tool_call(
        ToolCall(id="call-d", name="bash", arguments={"command": "make lint"}),
        batch_id=batch_id,
    )
    state.record_tool_result("call-c", "bash", AgentToolResult(content="ok"), False)
    state.record_tool_result("call-d", "bash", AgentToolResult(content="ok"), False)
    state.add_user_message("todo body", custom_type="todo")
    state.add_item("error", "Error: broken")
    state.add_item("status", "… working")
    return state


@pytest.mark.anyio
@pytest.mark.parametrize("show_thinking", [True, False])
async def test_diff_output_matches_full_rebuild_output(show_thinking: bool) -> None:
    """Diff-route output equals forced full-rebuild output for mixed state."""
    diff_state = _mixed_state(show_thinking=show_thinking)
    diff_app = TauTuiApp(FakeSession())
    async with diff_app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        diff_transcript = diff_app.query_one("#transcript", TranscriptView)
        diff_transcript.update_from_state(diff_state, theme=TAU_DARK_THEME)
        diff_transcript.update_from_state(diff_state, theme=TAU_DARK_THEME)
        await pilot.pause()
        diff_rows = [(type(row), row.selection_text) for row in _message_rows(diff_transcript)]

    rebuild_state = _mixed_state(show_thinking=show_thinking)
    rebuild_app = TauTuiApp(FakeSession())
    async with rebuild_app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        rebuild_transcript = rebuild_app.query_one("#transcript", TranscriptView)
        rebuild_transcript.update_from_state(rebuild_state, theme=TAU_DARK_THEME)
        rebuild_transcript._redraw(scroll_end=False)
        await pilot.pause()
        rebuild_rows = [
            (type(row), row.selection_text) for row in _message_rows(rebuild_transcript)
        ]

    assert diff_rows == rebuild_rows


@pytest.mark.anyio
async def test_window_paging_unchanged() -> None:
    """Scrolling to the top edge pages the window with a full rebuild."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        for index in range(250):
            state.add_item("user", f"message {index}")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        assert transcript._window_start == 50

        redraws: list[bool] = []
        original_redraw = transcript._redraw

        def spy_redraw(*, scroll_end: bool, preserve_window: bool = False) -> None:
            redraws.append(True)
            original_redraw(scroll_end=scroll_end, preserve_window=preserve_window)

        transcript._redraw = spy_redraw  # type: ignore[method-assign]
        try:
            transcript.scroll_to(y=0, animate=False)
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()
        finally:
            transcript._redraw = original_redraw  # type: ignore[method-assign]

        assert redraws == [True]
        assert transcript._window_start == 0
        assert transcript._window_end == TRANSCRIPT_WINDOW_ITEMS
        assert len(_message_rows(transcript)) == TRANSCRIPT_WINDOW_ITEMS
        anchor_row = _mounted_row(transcript, state.items[50])
        assert anchor_row.parent is transcript


@pytest.mark.anyio
async def test_thinking_toggle_keeps_behavior() -> None:
    """Hiding thinking collapses runs to placeholders and keeps other rows."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.show_thinking = True
        state.add_item("user", "plan")
        for index in range(10):
            state.add_item("assistant", f"paragraph {index}\n\nsecond line {index}")
        state.add_item("thinking", "thought one")
        state.add_item("thinking", "thought two")
        tool_item = _read_tool_item(tool_result_text="✓ read\ncontents")
        state.items.append(tool_item)
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        tool_row = _mounted_row(transcript, tool_item)
        thinking_rows = [row for row in _message_rows(transcript) if row.item.role == "thinking"]
        assert len(thinking_rows) == 2

        transcript.scroll_to(y=4, animate=False)
        await pilot.pause()
        scroll_before = transcript.scroll_y

        state.toggle_thinking()
        transcript.update_thinking_visibility(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        placeholders = [
            row
            for row in _message_rows(transcript)
            if row.item is _HIDDEN_THINKING_PLACEHOLDER_ITEM
        ]
        assert len(placeholders) == 1
        thinking_items = [item for item in state.items if item.role == "thinking"]
        assert {id(_mounted_row(transcript, item)) for item in thinking_items} == {
            id(placeholders[0])
        }
        assert _mounted_row(transcript, tool_item) is tool_row
        assert transcript.scroll_y == scroll_before


@pytest.mark.anyio
async def test_tool_toggle_keeps_in_place_updates() -> None:
    """Toggling tool results updates rows in place and remounts nothing later."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "do the work")
        sensitive_item = _read_tool_item(tool_result_text="✓ read\ncontents")
        state.items.append(sensitive_item)
        insensitive_item = ChatItem(
            role="tool",
            text="→ agent explore",
            tool_call_id="call-2",
            tool_name="agent",
        )
        state.items.append(insensitive_item)
        state.add_item("user", "done")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        rows_before = {id(item): _mounted_row(transcript, item) for item in state.items}

        state.toggle_tool_results()
        with _counted_row_changes(transcript) as (mounted, removed):
            await transcript.update_tool_results_visibility(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        assert mounted == []
        assert removed == []
        sensitive_row = _mounted_row(transcript, sensitive_item)
        assert sensitive_row is rows_before[id(sensitive_item)]
        assert "✓ read\ncontents" in sensitive_row.selection_text
        assert sensitive_row._show_tool_results is True

        with _counted_row_changes(transcript) as (mounted, removed):
            transcript.update_from_state(state, theme=TAU_DARK_THEME)

        assert mounted == []
        assert removed == []
        for item in state.items:
            assert _mounted_row(transcript, item) is rows_before[id(item)]


@pytest.mark.anyio
async def test_append_item_keeps_incremental_mount() -> None:
    """append_item mounts one row incrementally without a full rebuild."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "hello")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        redraws: list[bool] = []
        original_redraw = transcript._redraw

        def spy_redraw(*, scroll_end: bool, preserve_window: bool = False) -> None:
            redraws.append(True)
            original_redraw(scroll_end=scroll_end, preserve_window=preserve_window)

        transcript._redraw = spy_redraw  # type: ignore[method-assign]
        state.add_item("status", "appended")
        with _counted_row_changes(transcript) as (mounted, removed):
            widget = await transcript.append_item(state.items[-1], theme=TAU_DARK_THEME)
        try:
            pass
        finally:
            transcript._redraw = original_redraw  # type: ignore[method-assign]

        assert redraws == []
        assert removed == []
        assert mounted == [widget]
        assert _mounted_row(transcript, state.items[-1]) is widget
        assert transcript._window_end == len(state.items)


@pytest.mark.anyio
async def test_streaming_events_apply_without_full_rebuild() -> None:
    """Streaming events apply through the incremental routes with no rebuild."""
    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "explore the repo")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()
        user_row = _mounted_row(transcript, state.items[0])

        redraws: list[bool] = []
        original_redraw = transcript._redraw

        def spy_redraw(*, scroll_end: bool, preserve_window: bool = False) -> None:
            redraws.append(True)
            original_redraw(scroll_end=scroll_end, preserve_window=preserve_window)

        transcript._redraw = spy_redraw  # type: ignore[method-assign]
        partial = AssistantMessage()

        async def stream(event: object) -> None:
            app.adapter.apply(event)
            await app._apply_streaming_transcript_event(event)

        try:
            # The tool start precedes the deltas: a start after buffered text
            # makes the adapter flush the buffer into an assistant item behind
            # the window end, which routes the append through the baseline
            # append_item repage site whose full rebuild this task keeps.
            await stream(
                ToolExecutionStartEvent(
                    tool_call_id="call-1", tool_name="bash", args={"command": "ls"}
                )
            )
            await stream(AgentStartEvent())
            await stream(MessageStartEvent(message=partial))
            await stream(
                MessageUpdateEvent(
                    message=partial,
                    assistant_message_event=TextDeltaEvent(
                        content_index=0, delta="Listing ", partial=partial
                    ),
                )
            )
            await stream(
                MessageUpdateEvent(
                    message=partial,
                    assistant_message_event=ThinkingDeltaEvent(
                        content_index=0, delta="hmm", partial=partial
                    ),
                )
            )
            await stream(
                ToolExecutionUpdateEvent(
                    tool_call_id="call-1",
                    tool_name="bash",
                    args={},
                    partial_result=AgentToolResult(content="listing files"),
                )
            )
            await pilot.pause()
        finally:
            transcript._redraw = original_redraw  # type: ignore[method-assign]

        assert redraws == []
        tool_rows = [row for row in _message_rows(transcript) if row.item.role == "tool"]
        assert len(tool_rows) == 1
        assert "listing files" in tool_rows[0].selection_text
        assert _mounted_row(transcript, state.items[0]) is user_row


@pytest.mark.anyio
async def test_terminal_error_boundary_renders_once() -> None:
    """A terminal error renders display state once at the boundary."""
    error = AssistantMessage(stop_reason="error", error_message="provider failed")
    session = FakeSession(
        events=[
            AgentStartEvent(),
            MessageStartEvent(message=error),
            MessageEndEvent(message=error),
            AgentEndEvent(),
        ]
    )
    app = TauTuiApp(session)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        state = app.state
        state.add_item("user", "continue")
        transcript.update_from_state(state, theme=TAU_DARK_THEME)
        await pilot.pause()

        renders: list[bool] = []
        rebuilds: list[bool] = []
        original_update_from_state = transcript.update_from_state
        original_redraw = transcript._redraw

        def spy_update_from_state(
            update_state: TuiState, *, theme: TuiTheme = TAU_DARK_THEME
        ) -> None:
            renders.append(True)
            original_update_from_state(update_state, theme=theme)

        def spy_redraw(*, scroll_end: bool, preserve_window: bool = False) -> None:
            rebuilds.append(True)
            original_redraw(scroll_end=scroll_end, preserve_window=preserve_window)

        transcript.update_from_state = spy_update_from_state  # type: ignore[method-assign]
        transcript._redraw = spy_redraw  # type: ignore[method-assign]
        try:
            await app._run_prompt("continue")
            await pilot.pause()
        finally:
            transcript.update_from_state = original_update_from_state  # type: ignore[method-assign]
            transcript._redraw = original_redraw  # type: ignore[method-assign]

        assert renders == [True]
        assert rebuilds == []
        error_rows = [row for row in _message_rows(transcript) if row.item.role == "error"]
        assert len(error_rows) == 1
        assert "provider failed" in error_rows[0].selection_text


# ---------------------------------------------------------------------------
# Fixed flush cadence and per-widget markdown parser (Task 3)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_flush_interval_is_0_05_and_monkeypatchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flush constant is 0.05 s and the flush schedule honors a replacement.

    Proves the staleness bound is the named constant itself: the shipped value
    is 0.05 s, and with a replaced smaller value, fragments grouped by sleeps
    longer than the replacement flush per group, in order, with nothing
    dropped.
    """
    assert tui_widgets._STREAM_FLUSH_INTERVAL == 0.05

    monkeypatch.setattr(tui_widgets, "_STREAM_FLUSH_INTERVAL", 0.01)
    fake = _FakeMarkdownStream()
    widget = _DoubleStreamWidget(
        ChatItem(role="assistant", text=""),
        theme=TAU_DARK_THEME,
        stream=fake,
    )
    for group in ("alpha", "beta"):
        for character in group:
            await widget.append_fragment(character)
        # A sleep longer than the replaced interval lets each group's flush fire.
        await asyncio.sleep(0.03)

    assert fake.writes == ["alpha", "beta"]


@pytest.mark.anyio
async def test_parser_constructed_once_per_widget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each markdown widget constructs exactly one parser in its lifetime.

    Textual invokes the parser factory on every parse call (the mount update,
    a full document update, and every streamed append), so the invocation
    count tracks every parse call while a per-widget memoized factory keeps
    the construction count at one per widget, and each widget builds its own
    parser.
    """
    constructions: list[MarkdownIt] = []

    class CountingMarkdownIt(MarkdownIt):
        """MarkdownIt double that records each construction."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            constructions.append(self)

    # raising=False: before the parser-factory change the widgets module has
    # no MarkdownIt name to patch, and the construction-count assertion below
    # carries the failure instead.
    monkeypatch.setattr(tui_widgets, "MarkdownIt", CountingMarkdownIt, raising=False)

    def build_counting_widget(
        markdown: str | None,
    ) -> tuple[ThemedMarkdownWidget, list[int]]:
        """Build a widget whose stored parser factory records each invocation."""
        widget = ThemedMarkdownWidget(markdown, theme=TAU_DARK_THEME)
        factory_calls: list[int] = []
        original_factory = widget._parser_factory
        # Before the parser-factory change the widget stores None and Textual
        # builds parsers internally, so the wrap below never installs and the
        # construction-count assertion carries the failure.
        if original_factory is not None:

            def counting_factory() -> MarkdownIt:
                factory_calls.append(1)
                return original_factory()

            widget._parser_factory = counting_factory
        return widget, factory_calls

    app = TauTuiApp(FakeSession())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        widget, factory_calls = build_counting_widget("# Heading\n")
        await app.mount(widget)
        await widget.update("## Replaced\n")
        for index in range(3):
            await widget.append(f" fragment {index}\n")
        await pilot.pause()

        # One parser covers mount, the document update, and the appends.
        assert len(constructions) == 1
        # The factory ran on every parse call: mount, update, three appends.
        assert len(factory_calls) == 5

        second_widget, second_calls = build_counting_widget("Second\n")
        await app.mount(second_widget)
        await pilot.pause()

        # The second widget performs its own separate construction.
        assert len(second_calls) == 1
        assert len(constructions) == 2
        assert constructions[0] is not constructions[1]
