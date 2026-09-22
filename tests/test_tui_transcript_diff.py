"""Render-fingerprint and hidden-thinking placeholder sentinel bookkeeping.

These tests prove the per-row bookkeeping of the transcript render pipeline:
every message row records a fingerprint over its render inputs plus its
derived selection and markdown text, streaming blocks recompute their
fingerprint at finalization and at the item swap, hidden-thinking placeholder
rows share one sentinel item so their render inputs are stable across
redraws, and boundary markers record no fingerprint because they are not
item-backed rows.
"""

from typing import Any

import pytest

from tau_coding.tui import widgets as tui_widgets
from tau_coding.tui.app import TauTuiApp
from tau_coding.tui.config import TAU_DARK_THEME, TAU_LIGHT_THEME, TuiTheme
from tau_coding.tui.state import ChatItem
from tau_coding.tui.widgets import (
    StreamingTranscriptMessageWidget,
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
    without recomputing the cached markdown text, so the fingerprint may only
    include markdown text for markdown-body rows. A plain-body tool row that
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
