# Proposal: TUI transcript render performance

## Intent

The interactive transcript lags and flickers while a model streams output.
It also freezes for hundreds of milliseconds on every full refresh path,
including interrupt, queued-message edit, and error boundaries.

The prior responsiveness work (`dev-notes/tui-responsiveness-perf.md`)
optimized the typing path. The transcript render pipeline is the remaining
source of the problem, with two measured root causes:

1. Every `_refresh()` call tears down and remounts the whole transcript DOM.
2. Streamed markdown triggers a full screen layout pass at up to 50 flushes
   per second, which outruns the frame budget on a populated transcript.

This proposal fixes both causes inside `src/tau_coding/tui/widgets.py`.

## Baseline Evidence

**Baseline branch: undocumented existing domain.** The TUI transcript
rendering pipeline has behavior but no living spec. `docs/specs/` holds only
`fork-maintenance.md` and `provider-catalog.md`. Prior documentation covers
the typing path only (`dev-notes/tui-responsiveness-perf.md`).

**Current behavior, from the implementation:**

- `TranscriptView.update_from_state` always calls `_redraw`.
- `_redraw` removes every mounted transcript widget and mounts new widgets
  for the current window of items.
- Each Textual `Markdown` widget parses its whole document on mount.
- `_STREAM_FLUSH_INTERVAL` is `0.02`, which schedules up to 50 markdown
  stream flushes per second.
- `Markdown.append` constructs a new `MarkdownIt` instance on every call.
- A `_refresh()` during an active thinking stream re-renders the thinking
  text from state into a widget that the streaming bookkeeping no longer
  tracks. The next delta mounts a new streaming block for later deltas, and
  structured finalization mounts the canonical blocks next to the untracked
  widget. The old thinking text can appear twice. This is a known baseline
  defect and stays out of scope.

**Measurements.** Conditions: Textual 8.2.8, Rich 15, headless Textual
pilot, 120x40 terminal, 300 items (200 tool rows, 100 markdown messages).

| Operation | Measured cost |
| --- | --- |
| `_redraw` full rebuild | about 300 ms |
| `TranscriptView.refresh(layout=True)` on the settled transcript | about 32 ms |
| `update_item` in-place update of one tool row | about 26 ms |
| One stream flush cycle, incremental cost over the idle floor | about 45 ms |
| `MarkdownIt("gfm-like")` construction | about 0.16 ms |

**Material discrepancy and resolution.** An investigation report claimed
that the activity-indicator border write forces a layout every 150 ms.
Textual 8.2.8 `BoxProperty` skips the refresh when the value is unchanged,
so the claim is false. The activity indicator is out of scope for this
change, and the resolved fact does not affect any decision below.

**Consumers and interfaces.** `TauTuiApp` calls `_refresh()` at 27 sites in
`src/tau_coding/tui/app.py`. The streaming event pump calls `TranscriptView`
update methods once per session event. The suites
`tests/test_tui_streaming.py`, `tests/test_tui_components.py`,
`tests/test_tui_app.py`, and `tests/test_tui_responsiveness.py` exercise
these paths.

**Contract, data, security, privacy:** None.

**Operations and rollout:** None. Rollback: one git revert restores the
prior state. No migration or recovery work exists.

## Required Outcomes

1. A `_refresh()` over unchanged display state remounts no transcript
   widget. Unchanged display state means that every item produces the same
   render fingerprint across the two refreshes.
2. A `_refresh()` after small state changes mounts and removes only widgets
   for changed items, plus any running tool timer row whose elapsed suffix
   advanced. In-place item changes and tail appends or removals
   touch exactly the changed widgets. A positional shift, which no current
   scenario produces, remounts the shifted suffix of the window.
3. A `_refresh()` during an active assistant stream keeps the same
   assistant streaming widget object with no remount and no content loss.
   A `_refresh()` during an active thinking stream keeps the baseline
   visible behavior: the thinking text re-renders from state into a new
   widget on the first refresh that resolves the streaming block, and
   later refreshes with unchanged fingerprints reuse that widget per
   Outcome 1. The next delta mounts a new streaming block.
4. A session switch keeps the full-rebuild behavior, inside
   `App.batch_update()`.
5. Window paging and thinking-visibility toggles keep their current
   behavior.
6. The stream flush cadence is a fixed `0.05` module constant.
7. Each `ThemedMarkdownWidget` constructs its parser once in its lifetime.
8. Visible transcript output stays identical to the baseline in every
   covered scenario.
9. The committed benchmark script reproduces the rows and methodology of
   the measurement table, and the dev-note records the before and after
   values.

## Acceptance Examples

1. Two consecutive `_refresh()` calls with unchanged state leave every
   mounted widget object identical and perform zero mounts. The state has
   no running tool timer. With no displayed retry notice, they perform
   zero removals. With a displayed retry notice, they perform exactly one
   removal: the notice.
2. A change to one tool result mounts exactly one replacement widget and
   removes exactly one widget. Every other widget keeps its identity. The
   state has no running tool timer.
3. A `_refresh()` during an active assistant stream keeps the same
   assistant streaming widget object. After `finalize()`, that widget shows
   the full canonical text.
4. `scripts/bench_transcript.py` prints one row per measured operation. The
   dev-note records the before and after value for each row.

## Scope

**In scope:**

- The `update_from_state` route gains the diff path described under
  Approach. All `_redraw` call sites outside `update_from_state` keep the
  full rebuild.
- `TranscriptMessageWidget` and `StreamingTranscriptMessageWidget` expose a
  `render_fingerprint`.
- A module-level sentinel item replaces the synthetic hidden-thinking
  placeholder item, so placeholder rows fingerprint stably across redraws.
- Window boundary widgets get reuse with count updates instead of remounts.
- Every full-rebuild site runs inside `App.batch_update()`.
- `_STREAM_FLUSH_INTERVAL` changes from `0.02` to `0.05`.
- `ThemedMarkdownWidget.__init__` passes a per-widget `parser_factory`.
- New tests in `tests/test_tui_transcript_diff.py`.
- One adjustment to an existing test: the sleeps in
  `test_fragments_across_flush_windows_produce_ordered_writes`
  (`tests/test_tui_streaming.py`) become relative to a monkeypatched flush
  interval, so the test keeps its meaning at any constant.
- One more adjustment to an existing test: the wait in
  `test_streaming_code_block_hides_horizontal_scrollbar_until_finalized`
  (`tests/test_tui_app.py`) spans the replaced flush window, so the
  streamed code block exists when the test queries it. The tested behavior
  is unchanged; only the wait adapts to the `0.05` cadence.
- New `scripts/bench_transcript.py` and
  `dev-notes/tui-transcript-redraw-perf.md`.
- A one-line correction to the cadence sentence in
  `dev-notes/tui-responsiveness-perf.md`, whose phase 4 note describes the
  `0.02` cadence this change replaces.

**Out of scope:**

- Chrome refresh scoping and widget reference caching.
- The refresh scope of queued-message edit and interrupt
  (`action_edit_queued_message`, `_cancel_active_prompt`).
- In-place finalize for structured messages
  (`finish_structured_assistant_message`) and the
  `update_thinking_visibility` remount path.
- The window paging rewrite (`TranscriptView._shift_window`).
- The activity-indicator border-write dedupe, the completion rebuild dedupe,
  the picker list rebuilds, the session-index input-thread reads, the
  synchronous settings write on theme change, the prompt shell-prefix span
  memoization, and the mouse-move selection cost.
- Adaptive flush cadence.
- Changes in `tau_agent`, `tau_ai`, or any provider layer.
- Visual or CSS changes.

## Constraints

- Textual 8.2.8 and Rich 15 stay pinned by `uv.lock`.
- The public method signatures of `TranscriptView` stay compatible with
  existing call sites and tests.
- `_STREAM_FLUSH_INTERVAL` stays a module-level constant that tests can
  monkeypatch.
- No Textual version upgrade is part of this change.

## Approach

**Task 1: fingerprint child-list diff.** Each transcript widget records a
`render_fingerprint` tuple at construction. `update_from_state` builds a
desired child sequence for the current window: the top boundary, one entry
per window item, the active assistant widget when the window is latest and
`assistant_buffer` is non-empty, and the bottom boundary. The bottom
boundary appears only when the window is not latest, so the assistant entry
and the bottom boundary are mutually exclusive. When the buffer is
non-empty and no active assistant widget is mounted, the entry constructs
one from the buffer. Consecutive hidden thinking items collapse into one
desired entry: one placeholder widget per run, as today, and every item of
the run maps to the shared widget in `_item_widgets`. Each item entry
resolves per item: when the mounted
widget from `_item_widgets` exists, its parent is the transcript view, and
its recorded `render_fingerprint` equals the fingerprint of the item's
current render inputs, the entry holds that widget. Otherwise the entry
holds a newly constructed widget: the same class with the same arguments
that the full rebuild constructs for that item today. For state items that
class is always `TranscriptMessageWidget`, including assistant and
thinking roles. A `StreamingTranscriptMessageWidget`
recomputes its fingerprint when finalization completes and when its item
is swapped, so a finalized message keeps its widget across later
refreshes. The resolved invocation of a running tool includes its elapsed
time suffix, so a running tool row re-resolves per refresh and remounts
when the suffix changed. `update_item` stays the update route for live
tool progress. The diff path requests follow scroll under the same
`should_follow` condition as the baseline redraw. A two-pointer
positional merge walks
the desired sequence and the mounted transcript children together. A
mounted child is reused when the desired entry at that position is the
same widget object, or when the class and the `render_fingerprint` of
both widgets are equal. Mismatched positions mount the desired widget
before the next reused anchor. Removal of unmatched mounted children
happens after mounting, so no empty frame appears. The diff path updates
the active-widget bookkeeping for every streaming widget it removes,
mirroring the clearing that the full rebuild performs. On every pass it
also recomputes the hidden-thinking placeholder flag with the same rule
the full rebuild uses, including the keep case. A fast
path skips
mounting, removal, and `refresh(layout=True)` when the desired sequence
and the mounted children hold identical widget objects in identical order
and the boundary counts are unchanged. The genuine full rebuild stays for
the retained-projection condition that `update_from_state` already
computes (a new state object, or a same-state refresh with no retained
item widgets), extended in this change by a changed-theme condition.
Theme is a render input of both widget
classes, so a theme change remounts the window through the full rebuild.
Session switches clear and reload the same `TuiState`
object, so they take the full rebuild through the second condition. The
full rebuild runs inside `App.batch_update()`. The retry notice stays
outside the desired sequence. This preserves the current behavior of a
full rebuild dropping the notice.

Alternatives considered and rejected:

- Revision counters in `TuiState` and `ChatItem`: every state mutation site
  must bump a counter, and one missed bump shows a stale row. The risk
  spreads across `state.py` instead of concentrating in one widget.
- `App.batch_update()` alone: it removes a rare duplicated frame but keeps
  the 300 ms rebuild and the full markdown re-parse. It does not meet the
  required outcomes.

**Task 2: fixed flush cadence and parser caching.**
`_STREAM_FLUSH_INTERVAL` changes from `0.02` to `0.05` (20 flushes per
second). Textual documents saturation near 20 appends per second, and the
50 ms staleness bound stays invisible for readability.
`ThemedMarkdownWidget.__init__` passes a per-widget `parser_factory`, so the
`MarkdownIt` instance is built once per widget instead of once per append or
update.

## Impact

- Code: `src/tau_coding/tui/widgets.py` is the primary change.
  `src/tau_coding/tui/app.py` call sites stay unchanged.
- Tests: new `tests/test_tui_transcript_diff.py`. The existing suites pass
  unchanged except the two named test adjustments in Scope; they carry the
  behavior-preserving proof.
- New files: `scripts/bench_transcript.py` (new top-level `scripts/`
  directory, run with `uv run python scripts/bench_transcript.py`) and
  `dev-notes/tui-transcript-redraw-perf.md`.
- Dependencies: none added. The change uses the Textual public API only:
  `App.batch_update()` and the `parser_factory` parameter of `Markdown`.
- Data, security, privacy, operations: None.

## Risks

1. A fingerprint input is missing, so a changed row keeps a stale widget.
   Treatment: the fingerprint is built where the render inputs are set
   (`__init__`, `refresh_invocation`, and the streaming widget's finalize
   and item-swap points). The new tests force a replacement per input
   field, and the existing suites check rendered output.
2. The positional merge duplicates or drops widgets. Treatment: mount
   happens before removal, the merge tests use identity assertions, and the
   window paging tests stay unchanged.
3. `App.batch_update()` runs while the app is not mounted. Treatment:
   `App.batch_update()` is a batch counter plus an idle check, and it is
   harmless while the app is not mounted. The full-rebuild path gains an
   explicit mounted check.
4. Stream updates at 20 Hz feel less smooth than at 50 Hz. Treatment:
   `0.05` stays at the append saturation rate Textual documents, and the
   staleness bound is the constant itself.
5. The baseline suite has one timing-flaky test
   (`test_tui_app_shows_working_state_during_manual_compaction[asyncio]`
   in `tests/test_tui_app.py`). One more test in the same file
   (`test_streaming_transcript_deltas_do_not_force_scroll_end_during_scrollback`)
   failed once under full-suite load with the `0.05` cadence and passed in
   isolation and on a re-run.
   Treatment: the isolate-and-re-run caveat covers both. This change adds
   no timing-based assertions.

## Assumptions

- Headless pilot measurements track real terminal costs closely enough for
  relative comparisons.
- `App.batch_update()` is safe inside a synchronous widget method when the
  app is mounted. `App.batch_update()` only increments a batch counter and
  requests an idle check, and the idle check is guarded while the app is
  not running.
- One `MarkdownIt` instance per widget lifetime is safe for parse-only use
  across that widget's appends and updates.

## Unresolved Decisions

None
