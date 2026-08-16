"""Tests for coalesced Markdown streaming in the TUI transcript widgets.

These tests prove that `StreamingTranscriptMessageWidget` batches streamed
fragments into a small number of `MarkdownStream` writes, so the provider's
per-token delta rate no longer drives one re-parse/repaint of the Markdown
block per fragment, while the canonical `item.text` and `selection_text`
remain complete after every fragment.
"""

import asyncio

import pytest

from tau_coding.tui import widgets as tui_widgets
from tau_coding.tui.config import TAU_DARK_THEME
from tau_coding.tui.state import ChatItem
from tau_coding.tui.widgets import StreamingTranscriptMessageWidget

_RAPID_FRAGMENTS = "Hello world"


class FakeMarkdownStream:
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


def _streaming_widget_with_fake_stream(
    monkeypatch: pytest.MonkeyPatch, *, flush_interval: float
) -> tuple[StreamingTranscriptMessageWidget, FakeMarkdownStream]:
    """Build an unmounted streaming widget whose render stream is a fake."""
    monkeypatch.setattr(tui_widgets, "_STREAM_FLUSH_INTERVAL", flush_interval)
    widget = StreamingTranscriptMessageWidget(
        ChatItem(role="assistant", text=""), theme=TAU_DARK_THEME
    )
    fake = FakeMarkdownStream()
    widget._stream = fake
    return widget, fake


@pytest.mark.anyio
async def test_rapid_fragments_coalesce_into_a_single_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rapidly appended fragments produce one stream write without dropping text.

    Proves the core Phase 4 property: the widget absorbs the provider's
    per-token delta rate and re-renders the Markdown block at most once per
    flush window. It also proves no fragment is lost in the meantime: the
    canonical item.text and selection_text are complete immediately, and the
    flattened stream writes equal the full message.
    """
    widget, fake = _streaming_widget_with_fake_stream(monkeypatch, flush_interval=0.5)

    for character in _RAPID_FRAGMENTS:
        await widget.append_fragment(character)

    assert widget.selection_text == _RAPID_FRAGMENTS
    assert widget.item.text == _RAPID_FRAGMENTS
    # The 0.5s flush window has not elapsed yet, so nothing reached the stream.
    assert len(fake.writes) < len(_RAPID_FRAGMENTS)

    # Force the scheduled flush and prove exactly one write carried everything.
    await widget._flush_now()
    assert fake.writes == [_RAPID_FRAGMENTS]
    assert widget._flush_task is None


@pytest.mark.anyio
async def test_finalize_flushes_pending_fragments_before_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalizing immediately after fragments flushes them before the stream stops.

    Proves the finalize path cannot drop streamed text: pending fragments are
    written to the stream and the finalized marker state is set even when the
    flush window never elapses.
    """
    widget, fake = _streaming_widget_with_fake_stream(monkeypatch, flush_interval=0.5)

    await widget.append_fragment("Hello")
    await widget.finalize()

    assert widget.selection_text == "Hello"
    assert widget.item.text == "Hello"
    assert fake.writes == ["Hello"]
    assert fake.stopped is True
    assert widget._is_streaming is False
    assert not widget.has_class("-streaming")
    assert widget.has_class("-finalized")
    assert widget._flush_task is None


@pytest.mark.anyio
async def test_on_unmount_cancels_pending_flush_and_stops_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing a widget mid-stream leaves no scheduled flush and stops the stream.

    Widgets can be removed while fragments are pending (transcript window
    redraws and structured finalization replace them from canonical state), so
    on_unmount must not crash, must not leak the scheduled flush task, and must
    stop the Textual stream so its background task cannot outlive the widget.
    """
    widget, fake = _streaming_widget_with_fake_stream(monkeypatch, flush_interval=0.5)
    tasks_before = {task for task in asyncio.all_tasks()}

    await widget.append_fragment("Hel")
    await widget.append_fragment("lo")
    assert widget._flush_task is not None

    await widget.on_unmount()

    assert widget._flush_task is None
    assert fake.stopped is True
    # The unmount path deliberately skips the flush (the block is rebuilt from
    # canonical item.text, and writing to a detached widget can raise).
    assert fake.writes == []
    # No new live task may remain: the scheduled flush was cancelled and
    # awaited, so any task other than the pre-existing anyio bookkeeping would
    # indicate a leaked background task.
    tasks_after = {task for task in asyncio.all_tasks() if not task.done()}
    assert tasks_after <= tasks_before


@pytest.mark.anyio
async def test_replace_text_flushes_pending_before_replacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending fragments reach the stream before replace_text swaps the text.

    Proves a corrected-final-text replacement cannot strand unflushed fragments
    in the widget: the flush runs inside the stop path before the replacement
    text is rendered, and the canonical text becomes the replacement.
    """
    widget, fake = _streaming_widget_with_fake_stream(monkeypatch, flush_interval=0.5)
    replaced_texts: list[str] = []

    async def tracking_update(self: StreamingTranscriptMessageWidget, text: str) -> None:
        replaced_texts.append(text)

    monkeypatch.setattr(StreamingTranscriptMessageWidget, "update", tracking_update)

    await widget.append_fragment("Hello")
    await widget.replace_text("final")

    assert fake.writes == ["Hello"]
    assert fake.stopped is True
    assert replaced_texts == ["final"]
    assert widget.item.text == "final"
    assert widget.selection_text == "final"
    assert widget._flush_task is None


@pytest.mark.anyio
async def test_fragments_across_flush_windows_produce_ordered_writes() -> None:
    """Fragments spanning real flush-window boundaries yield ordered batches.

    Proves the batch cadence is real rather than deferred forever: with the
    actual flush window, fragments grouped by sleeps longer than the window
    produce several writes whose concatenation still equals the canonical text.
    """
    widget = StreamingTranscriptMessageWidget(
        ChatItem(role="assistant", text=""), theme=TAU_DARK_THEME
    )
    fake = FakeMarkdownStream()
    widget._stream = fake

    for group in ("alpha", "beta", "gamma"):
        for character in group:
            await widget.append_fragment(character)
        # A sleep longer than the flush window lets each group's flush fire.
        await asyncio.sleep(0.03)

    # Finalize flushes anything still pending so the stream text is complete.
    await widget.finalize()

    assert widget.selection_text == "alphabetagamma"
    assert fake.writes == ["alpha", "beta", "gamma"]
    assert len(fake.writes) >= 2
    assert "".join(fake.writes) == "alphabetagamma"
