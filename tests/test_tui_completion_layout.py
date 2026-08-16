"""Completion-window layout-measurement optimization tests.

These tests prove three properties of the completion windowing rewrite in
``tau_coding.tui.app``:

* ``_visible_completion_state`` derives its window from a single measurement
  pass — every item's wrapped height is measured at most once per invocation
  instead of once per overlapping slice candidate.
* The optimized windowing is functionally identical to the pre-optimization
  implementation for every input shape that code path can encounter, verified
  against verbatim copies of the original functions from git HEAD.
* The per-item measurement cache is bounded, keyed by the render inputs, and
  shared across all window candidates.

The reference functions below are literal copies of the original
implementation from git HEAD (before this optimization); they call each other
and never touch the optimized code, so they stay an independent oracle.
"""

from __future__ import annotations

import random
from io import StringIO

import pytest
from rich.console import Console

from tau_coding.tui import app as tui_app
from tau_coding.tui.autocomplete import CompletionItem, CompletionState
from tau_coding.tui.config import TAU_DARK_THEME
from tau_coding.tui.widgets import render_completion_suggestions

LONG_DESCRIPTION = (
    "This completion description is deliberately long so it wraps across "
    "several lines inside the fixed-width completion table."
)


def _reference_completion_item_extra_wrapped_lines(
    item: CompletionItem,
    *,
    width: int | None,
) -> int:
    """Reference (git HEAD): extra wrapped lines used by one completion item."""
    if width is None or width <= 0 or not item.description:
        return 0
    output = StringIO()
    console = Console(
        file=output,
        width=width,
        force_terminal=False,
        color_system=None,
        legacy_windows=False,
    )
    console.print(
        render_completion_suggestions(
            CompletionState(items=(item,), selected_index=0),
            theme=TAU_DARK_THEME,
        ),
        end="",
    )
    line_count = len(output.getvalue().splitlines())
    return max(line_count - 1, 0)


def _reference_completion_selected_render_line(
    state: CompletionState,
    *,
    width: int | None = None,
) -> int:
    """Reference (git HEAD): rendered line number for the selected item."""
    line = 0
    has_rendered_text = False
    previous_category: str | None = None
    for index, item in enumerate(state.items):
        if item.category != previous_category:
            if has_rendered_text:
                line += 1
            if item.category:
                line += 1
                has_rendered_text = True
            previous_category = item.category
        elif has_rendered_text:
            line += 1
        if index == state.selected_index:
            return line
        line += _reference_completion_item_extra_wrapped_lines(item, width=width)
        has_rendered_text = True
    return line


def _reference_completion_render_line_count(
    state: CompletionState,
    *,
    width: int | None = None,
) -> int:
    """Reference (git HEAD): lines the completion state renders into."""
    if not state.items:
        return 0
    line_count = 0
    previous_category: str | None = None
    for index, item in enumerate(state.items):
        if item.category != previous_category:
            if index:
                line_count += 1
            if item.category:
                line_count += 1
            previous_category = item.category
        line_count += 1 + _reference_completion_item_extra_wrapped_lines(item, width=width)
    return line_count


def _reference_visible_completion_state(
    state: CompletionState,
    *,
    max_lines: int,
    width: int | None = None,
) -> CompletionState:
    """Reference (git HEAD): completion-state window with selection visible."""
    if not state.items or max_lines <= 0:
        return CompletionState()

    selected_line_limit = max(max_lines - 1, 1)
    reference_start = 0
    while reference_start < state.selected_index:
        candidate = CompletionState(
            items=state.items[reference_start:],
            selected_index=state.selected_index - reference_start,
        )
        if (
            _reference_completion_selected_render_line(candidate, width=width)
            < selected_line_limit
        ):
            break
        reference_start += 1

    reference_end = len(state.items)
    while reference_end > state.selected_index + 1:
        candidate = CompletionState(
            items=state.items[reference_start:reference_end],
            selected_index=state.selected_index - reference_start,
        )
        if _reference_completion_render_line_count(candidate, width=width) <= max_lines:
            break
        reference_end -= 1

    while reference_start < state.selected_index:
        candidate = CompletionState(
            items=state.items[reference_start:reference_end],
            selected_index=state.selected_index - reference_start,
        )
        if _reference_completion_render_line_count(candidate, width=width) <= max_lines:
            break
        reference_start += 1

    return CompletionState(
        items=state.items[reference_start:reference_end],
        selected_index=state.selected_index - reference_start,
    )


def test_visible_completion_state_measures_each_item_at_most_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the window is derived from a single measurement pass per invocation.

    The three original windowing loops rebuilt candidate states over
    overlapping slices, so the same item could be Rich-rendered once per
    candidate. Counting actual calls into the measured helper across several
    max_lines/selected combos proves the rewrite keeps the window math on top of
    one precomputed height list instead of re-measuring inside the loops.
    """
    rng = random.Random(20260815)
    items = tuple(
        CompletionItem(
            display=f"/prompt-{index:03d}",
            replacement=f"/prompt-{index:03d}",
            start=0,
            end=1,
            description=(
                " ".join(
                    rng.choice(["alpha", "beta", "gamma", "delta", "omega"])
                    for _ in range(rng.randint(20, 35))
                )
                if index % 9
                else None
            ),
            category=("Custom prompts" if index % 3 == 0 else None),
        )
        for index in range(64)
    )
    described_count = sum(1 for item in items if item.description)

    real_measure = tui_app._measure_completion_item_wrapped_lines

    def counting_measure(
        display: str,
        description: str,
        category: str | None,
        width: int | None,
    ) -> int:
        key = (display, description, category, width)
        counts[key] = counts.get(key, 0) + 1
        return real_measure(display, description, category, width)

    monkeypatch.setattr(tui_app, "_measure_completion_item_wrapped_lines", counting_measure)

    for max_lines in (3, 8, 20, 64):
        for selected_index in (0, 20, 43, 63):
            counts: dict[tuple[str, str | None, str | None, int | None], int] = {}
            window = tui_app._visible_completion_state(
                CompletionState(items=items, selected_index=selected_index),
                max_lines=max_lines,
                width=40,
            )
            assert window.selected is not None
            assert window.selected.display == f"/prompt-{selected_index:03d}"
            assert all(count <= 1 for count in counts.values()), (
                f"an item was measured more than once in one invocation "
                f"(max_lines={max_lines}, selected_index={selected_index})"
            )
            assert len(counts) <= described_count
            # Every described item is measured exactly once per invocation; the
            # items whose description is None must never reach the renderer.
            assert len(counts) == described_count


def test_visible_completion_state_matches_reference_implementation() -> None:
    """Prove the optimized windowing is functionally identical to the original.

    The windowing edge cases — headers on slice starts, category-change blank
    rows, the selected line at the trim limit, and selected indexes past the
    last item — are subtle enough that a rewrite could easily drift. Comparing
    every generated window against a verbatim copy of the pre-optimization
    functions over a deterministic seeded corpus pins the equivalence.
    """
    rng = random.Random(0xC0FFEE)
    categories = (None, "Commands", "Custom prompts", "Files")
    for _ in range(60):
        item_count = rng.randint(0, 14)
        items = tuple(
            CompletionItem(
                display=f"/prompt-{index:02d}",
                replacement=f"/prompt-{index:02d}",
                start=0,
                end=1,
                description=LONG_DESCRIPTION if rng.random() < 0.6 else "short",
                category=(rng.choice(categories) if rng.random() < 0.7 else None),
            )
            for index in range(item_count)
        )
        selected_index = rng.randrange(item_count) if item_count else 0
        if rng.random() < 0.25:
            # Pin the walk behavior for selected indexes past the last item.
            selected_index = rng.randint(item_count, item_count + 2)
        state = CompletionState(items=items, selected_index=selected_index)
        max_lines = rng.choice((1, 2, 3, 5, 8, 20, 50))
        width = rng.choice((None, 20, 40, 80))

        expected = _reference_visible_completion_state(state, max_lines=max_lines, width=width)
        actual = tui_app._visible_completion_state(state, max_lines=max_lines, width=width)

        assert actual.items == expected.items
        assert actual.selected_index == expected.selected_index


def test_completion_item_measurement_is_memoized() -> None:
    """Prove identical render inputs hit the cache instead of re-rendering.

    Repeatedly re-rendering the same item shape was the per-candidate cost this
    optimization removes; a cache that misses on identical inputs would leave
    the hot path unchanged. cache_info misses/hits deltas prove the memoization
    without wall-clock timing, and a second width proves the key covers the
    resize case.
    """
    measure = tui_app._measure_completion_item_wrapped_lines
    measure.cache_clear()

    display = "/prompt-memoized-001"
    description = LONG_DESCRIPTION
    category = "Commands"
    before = measure.cache_info()
    first = measure(display, description, category, 40)
    after_first = measure.cache_info()
    second = measure(display, description, category, 40)
    after_second = measure.cache_info()
    assert first == second
    assert after_first.misses == before.misses + 1
    assert after_first.currsize == before.currsize + 1
    assert after_second.misses == after_first.misses
    assert after_second.hits == after_first.hits + 1
    assert after_second.currsize == after_first.currsize

    # A different width is a different cache key and must re-measure.
    measure(display, description, category, 41)
    after_resize = measure.cache_info()
    assert after_resize.misses == after_second.misses + 1
    assert after_resize.currsize == after_second.currsize + 1


def test_completion_item_measurement_cache_is_bounded() -> None:
    """Prove the measurement cache cannot grow without bound.

    Completion lists are rebuilt as the user types, so an unbounded cache would
    leak render artifacts forever. Generating more distinct keys than the LRU
    maxsize must evict entries back down to the configured bound, keeping the
    cached memory proportional to maxsize regardless of session length.
    """
    measure = tui_app._measure_completion_item_wrapped_lines
    measure.cache_clear()

    for index in range(320):
        measure(
            f"/prompt-{index:03d}",
            f"{LONG_DESCRIPTION} variant {index}",
            ("Custom prompts" if index % 2 else None),
            40,
        )
    info = measure.cache_info()
    assert info.currsize <= 256
    assert info.misses == 320


def test_visible_completion_state_keeps_selected_item_visible() -> None:
    """Prove the returned window always keeps the selected item on screen.

    The windowing contract is that the selected item stays inside the returned
    window and lands on a line below the trim limit; these hand-picked cases
    cover uncategorized lists, category headers, wrapped descriptions, the last
    item, and a single over-tall item (which cannot shrink below one row).
    """
    cases = [
        (
            tuple(
                CompletionItem(
                    display=f"/prompt-{index:02d}",
                    replacement=f"/prompt-{index:02d}",
                    start=0,
                    end=1,
                    category="Custom prompts",
                )
                for index in range(30)
            ),
            24,
            8,
            None,
            "/prompt-24",
            True,
        ),
        (
            tuple(
                CompletionItem(
                    display=f"/prompt-{index:02d}",
                    replacement=f"/prompt-{index:02d}",
                    start=0,
                    end=1,
                    description=LONG_DESCRIPTION,
                    category="Custom prompts",
                )
                for index in range(12)
            ),
            8,
            8,
            48,
            "/prompt-08",
            True,
        ),
        (
            tuple(
                CompletionItem(
                    display=f"/prompt-{index:02d}",
                    replacement=f"/prompt-{index:02d}",
                    start=0,
                    end=1,
                )
                for index in range(30)
            ),
            15,
            8,
            None,
            "/prompt-15",
            True,
        ),
        (
            tuple(
                CompletionItem(
                    display=f"/prompt-{index:02d}",
                    replacement=f"/prompt-{index:02d}",
                    start=0,
                    end=1,
                )
                for index in range(30)
            ),
            29,
            8,
            None,
            "/prompt-29",
            True,
        ),
        (
            (
                CompletionItem(
                    display="/one-huge-item",
                    replacement="/one-huge-item",
                    start=0,
                    end=1,
                    description=LONG_DESCRIPTION,
                ),
            ),
            0,
            1,
            40,
            "/one-huge-item",
            False,
        ),
    ]
    for items, selected_index, max_lines, width, expected_display, fits_target in cases:
        state = CompletionState(items=items, selected_index=selected_index)
        window = tui_app._visible_completion_state(state, max_lines=max_lines, width=width)
        assert window.selected is not None
        assert window.selected.display == expected_display
        assert window.selected_index < len(window.items)
        selected_line = tui_app._completion_selected_render_line(window, width=width)
        assert selected_line < max(max_lines - 1, 1)
        if fits_target:
            assert tui_app._completion_render_line_count(window, width=width) <= max_lines
