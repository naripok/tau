"""Headless benchmark of the five measured transcript render operations.

Reproduces the rows and methodology of the measurement table in
`docs/design/2026-09-21-tui-transcript-render-perf-proposal.md`: a headless
Textual pilot, a 120x40 terminal, and a populated transcript of 300 items
(200 tool rows, 100 markdown messages). One row is one measured operation.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Sequence
from pathlib import Path

from markdown_it import MarkdownIt
from textual.pilot import Pilot

from tau_coding.session_manager import CodingSessionRecord
from tau_coding.session_stats import SessionStats
from tau_coding.tui.app import TauTuiApp
from tau_coding.tui.config import TuiTheme
from tau_coding.tui.state import ChatItem
from tau_coding.tui.widgets import (
    _STREAM_FLUSH_INTERVAL,
    TranscriptView,
)

TERMINAL_SIZE = (120, 40)
PENDING_TOOL_ROWS = 100
COMPLETED_TOOL_ROWS = 100
MARKDOWN_MESSAGES = 100
REPETITIONS = 7
# A flush window of two intervals leaves room for the coalesced flush to land.
FLUSH_SETTLE_INTERVALS = 2

_STREAM_FRAGMENTS = (
    "The diff walk ",
    "compares render fingerprints, ",
    "reuses every matching row, ",
    "and mounts only the changed positions. ",
    "The parser stays memoized. ",
)


class FakeSessionManager:
    """Session index stub with the minimal surface the app reads at mount."""

    def __init__(self) -> None:
        self.records = (
            CodingSessionRecord(
                id="session-1",
                path=Path("/workspace/project/.tau/sessions/session-1.jsonl"),
                cwd=Path("/workspace/project"),
                model="fake-model",
                title=None,
                created_at=0.0,
                updated_at=0.0,
            ),
        )

    def list_sessions(self, cwd: Path | None = None) -> list[CodingSessionRecord]:
        """Return the single stub record without touching the filesystem."""
        return list(self.records)


class FakeSession:
    """Minimal session stub with the same attribute set the counting tests use."""

    def __init__(self) -> None:
        self.cwd = Path("/workspace/project")
        self.provider_name = "openai"
        self.model = "fake-model"
        self.thinking_level = "medium"
        self.session_title: str | None = None
        self.context_token_estimate = 12_034
        self.has_provider_context_usage = True
        self.auto_compact_token_threshold = 200_000
        self.context_window_tokens = 216_384
        self.session_stats = SessionStats()
        self.extension_names: Sequence[object] = ()
        self.tools: Sequence[object] = ()
        self.skills: Sequence[object] = ()
        self.prompt_templates: Sequence[object] = ()
        self.context_files: Sequence[object] = ()
        self.available_models: Sequence[object] = ()
        self.available_providers: Sequence[object] = ("openai",)
        self.available_thinking_levels: Sequence[object] = ("off", "low", "medium", "high")
        self.messages: Sequence[object] = ()
        self.session_manager = FakeSessionManager()

    async def emit_pending_session_start(self) -> None:
        """No-op stand-in for the deferred session-start emission."""
        return None


def _assistant_markdown(index: int) -> str:
    """Return one assistant message of realistic paragraph length."""
    return (
        f"Update {index + 1} of {MARKDOWN_MESSAGES}: the diff walk compares "
        f"`render_fingerprint` tuples and reuses every row whose inputs did not "
        f"change, so the mounted widget keeps its parser and its object identity "
        f"across refreshes while the turn runs."
    )


def _pending_tool_item(index: int) -> ChatItem:
    """Return one running bash tool row with no result yet."""
    command = f"pytest -q tests/test_suite_{index % 8}.py"
    return ChatItem(
        role="tool",
        text=command,
        tool_name="bash",
        tool_arguments={"command": command},
        started_at=time.monotonic(),
    )


def _completed_tool_item(index: int) -> ChatItem:
    """Return one finished bash tool row with a result block."""
    command = f"rg TODO src/ --count-matches  # pass {index + 1}"
    return ChatItem(
        role="tool",
        text=command,
        tool_name="bash",
        tool_arguments={"command": command},
        tool_result_text=(
            f"src/tau_coding/tui/widgets.py:{40 + index}\n"
            f"src/tau_coding/tui/app.py:{index + 7}\n"
            f"src/tau_coding/tui/state.py:{12 + index}\n"
        ),
    )


async def _settle(pilot: Pilot) -> None:
    """Let the pending mounts, parses, layout passes, and repaints land."""
    await pilot.pause()


async def _populate_transcript(app: TauTuiApp, transcript: TranscriptView, pilot: Pilot) -> None:
    """Append 300 items through the incremental append route.

    Appends 100 completed tool rows, 100 markdown assistant messages of
    realistic paragraph length, and 100 pending tool rows (each then updated
    through the in-place ``update_item`` route). Tool results stay collapsed,
    which is the app default. The latest 200 item window then holds the 100
    markdown messages and the 100 pending tool rows, so the measured in-place
    update targets a mounted row.
    """
    state = app.state
    theme = app.tui_settings.resolved_theme
    transcript.update_from_state(state, theme=theme)
    await _settle(pilot)

    for index in range(COMPLETED_TOOL_ROWS):
        item = _completed_tool_item(index)
        state.items.append(item)
        await transcript.append_item(
            item,
            theme=theme,
            show_tool_results=state.show_tool_results,
            invocation=state.resolve_tool_invocation(item, expanded=state.show_tool_results),
            result_markup=state.resolve_tool_result(item, expanded=state.show_tool_results),
        )
        await _settle(pilot)

    for index in range(MARKDOWN_MESSAGES):
        item = ChatItem(role="assistant", text=_assistant_markdown(index))
        state.items.append(item)
        await transcript.append_item(
            item,
            theme=theme,
            show_tool_results=state.show_tool_results,
        )
        await _settle(pilot)

    for index in range(PENDING_TOOL_ROWS):
        item = _pending_tool_item(index)
        state.items.append(item)
        await transcript.append_item(
            item,
            theme=theme,
            show_tool_results=state.show_tool_results,
            invocation=state.resolve_tool_invocation(item, expanded=state.show_tool_results),
            result_markup=state.resolve_tool_result(item, expanded=state.show_tool_results),
        )
        item.update_text = f"step {index + 1}: command running"
        await transcript.update_item(
            item,
            theme=theme,
            show_tool_results=state.show_tool_results,
            invocation=state.resolve_tool_invocation(item, expanded=state.show_tool_results),
            result_markup=state.resolve_tool_result(item, expanded=state.show_tool_results),
        )
        await _settle(pilot)


async def _measure_full_rebuild(transcript: TranscriptView, pilot: Pilot) -> float:
    """Return the median cost of one full ``_redraw`` rebuild, settle included."""
    await _settle(pilot)
    transcript._redraw(scroll_end=False)  # warm-up: pay the cold caches once
    await _settle(pilot)
    durations = []
    for _ in range(REPETITIONS):
        await _settle(pilot)
        start = time.perf_counter()
        transcript._redraw(scroll_end=False)
        await _settle(pilot)
        durations.append(time.perf_counter() - start)
    return statistics.median(durations)


async def _measure_layout_refresh(transcript: TranscriptView, pilot: Pilot) -> float:
    """Return the median cost of one ``refresh(layout=True)`` on the settled transcript."""
    durations = []
    for _ in range(REPETITIONS):
        await _settle(pilot)
        start = time.perf_counter()
        transcript.refresh(layout=True)
        await _settle(pilot)
        durations.append(time.perf_counter() - start)
    return statistics.median(durations)


async def _measure_in_place_update(
    app: TauTuiApp, transcript: TranscriptView, pilot: Pilot
) -> float:
    """Return the median cost of one ``update_item`` in-place tool row update."""
    state = app.state
    theme = app.tui_settings.resolved_theme
    item = state.items[COMPLETED_TOOL_ROWS + MARKDOWN_MESSAGES]
    durations = []
    for index in range(REPETITIONS):
        await _settle(pilot)
        item.update_text = f"step {index + 1}: {900 + index} checks scheduled"
        start = time.perf_counter()
        await transcript.update_item(
            item,
            theme=theme,
            show_tool_results=state.show_tool_results,
            invocation=state.resolve_tool_invocation(item, expanded=state.show_tool_results),
            result_markup=state.resolve_tool_result(item, expanded=state.show_tool_results),
        )
        await _settle(pilot)
        durations.append(time.perf_counter() - start)
    return statistics.median(durations)


async def _settle_idle(pilot: Pilot) -> None:
    """Run the flush cycle's wait and settle with no fragments appended."""
    await asyncio.sleep(_STREAM_FLUSH_INTERVAL * FLUSH_SETTLE_INTERVALS)
    await _settle(pilot)


async def _measure_stream_flush_cycle(
    transcript: TranscriptView, theme: TuiTheme, pilot: Pilot
) -> float:
    """Return the median flush cycle cost minus the median idle settle floor.

    One cycle appends fragments to the active streaming block, waits past the
    flush interval for the coalesced flush to land, and settles. The idle
    floor runs the same wait and settle without fragments, so the difference
    is the incremental cost of one stream flush cycle.
    """
    # Warm-up: mount the streaming block and pay its first parse, untimed.
    await transcript.append_assistant_delta("Stream ready. ", theme=theme, scroll_end=False)
    await _settle_idle(pilot)

    cycle_durations = []
    for _ in range(REPETITIONS):
        await _settle_idle(pilot)
        start = time.perf_counter()
        for fragment in _STREAM_FRAGMENTS:
            await transcript.append_assistant_delta(fragment, theme=theme, scroll_end=False)
        await _settle_idle(pilot)
        cycle_durations.append(time.perf_counter() - start)

    floor_durations = []
    for _ in range(REPETITIONS):
        await _settle_idle(pilot)
        start = time.perf_counter()
        await _settle_idle(pilot)
        floor_durations.append(time.perf_counter() - start)

    return statistics.median(cycle_durations) - statistics.median(floor_durations)


def _measure_parser_construction() -> float:
    """Return the median construction cost of one ``MarkdownIt("gfm-like")``."""
    durations = []
    for _ in range(REPETITIONS):
        start = time.perf_counter()
        MarkdownIt("gfm-like")
        durations.append(time.perf_counter() - start)
    return statistics.median(durations)


async def _measure_all() -> None:
    """Run the five measurements and print one line per operation."""
    app = TauTuiApp(FakeSession())
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptView)
        await _populate_transcript(app, transcript, pilot)
        await _settle(pilot)

        theme = app.tui_settings.resolved_theme
        rebuild = await _measure_full_rebuild(transcript, pilot)
        layout_refresh = await _measure_layout_refresh(transcript, pilot)
        in_place_update = await _measure_in_place_update(app, transcript, pilot)
        stream_flush_cycle = await _measure_stream_flush_cycle(transcript, theme, pilot)
        parser_construction = _measure_parser_construction()

        rows = (
            ("_redraw full rebuild", rebuild),
            ("TranscriptView.refresh(layout=True) on the settled transcript", layout_refresh),
            ("update_item in-place update of one tool row", in_place_update),
            ("One stream flush cycle, incremental cost over the idle floor", stream_flush_cycle),
            ('MarkdownIt("gfm-like") construction', parser_construction),
        )
        for label, seconds in rows:
            print(f"{label}: {seconds * 1000:.2f} ms")


def main() -> None:
    """Run the benchmark headless and exit 0."""
    asyncio.run(_measure_all())


if __name__ == "__main__":
    main()
