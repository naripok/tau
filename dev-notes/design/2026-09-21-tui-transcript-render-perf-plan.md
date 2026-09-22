# TUI Transcript Render Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Execute this plan task-by-task with the skill the workflow depth selects: executing-plans for Bounded, subagent-driven-development for Standard or High-risk. The controller marks every checkbox of a task `[x]` in the plan file when the task completes its gate, and records each flip in one tracking commit named `docs(plan): mark <plan-file-stem> Task N complete`.

**Goal:** Make the transcript render faster and less flickery by replacing the full teardown/remount on the display-state refresh route with a fingerprint child-list diff, and by slowing the stream flush cadence to 0.05 s with a per-widget markdown parser.

**Architecture:** All changes stay in `src/tau_coding/tui/widgets.py`. The `update_from_state` route gains a diff path that reuses mounted widgets by render fingerprint and mounts or removes only mismatches; every other `_redraw` call site keeps the full rebuild, now inside `App.batch_update()`. Stream flushes stay coalesced through `_STREAM_FLUSH_INTERVAL`, changed from `0.02` to `0.05`, and each `ThemedMarkdownWidget` builds its `MarkdownIt` parser once.

**Tech Stack:** Python 3.12, Textual 8.2.8 (pinned by `uv.lock`), Rich 15, pytest, markdown-it-py (pinned transitive dependency of Textual; do not add it to `pyproject.toml`).

**Standards:** Apply the shared code standards in every task: DRY, minimal implementation (YAGNI), low cyclomatic complexity, type safety, no unnecessary abstractions or fallbacks, no hacks or workarounds, informative docstrings, documentation of current state only, writing-unambiguous-text prose.

**Feature spec:** `dev-notes/design/2026-09-21-tui-transcript-render-perf-spec.md` (the behavioral contract; 21 requirements, all ADDED)

**Approved proposal:** `dev-notes/design/2026-09-21-tui-transcript-render-perf-proposal.md` (intent, scope, binding architecture, constraints, non-goals, acceptance, and risk treatment; version 7, sha256 `485957e0da1a40ed9f51f95f1bbff5bcc286565894b66cd68745aa9fdabd1bbb`, operator-approved)

**Workflow depth:** Standard. Execute with subagent-driven-development.

---

## Commands

Run all commands from the worktree root: `/workspace/.worktrees/tui-transcript-render-perf`.

```bash
uv run pytest tests/test_tui_transcript_diff.py -q   # the new suite only
uv run pytest tests/test_tui_streaming.py -q         # streaming suite
uv run pytest -q                                     # full suite (about 95 s)
uv run ruff check .                                  # lint
uv run ruff format --check .                         # format check
```

Environment: `uv sync` once if `.venv` is missing. Use `uv run` for every Python and pytest invocation.

Baseline note: the full suite has one timing-flaky test,
`tests/test_tui_app.py::test_tui_app_shows_working_state_during_manual_compaction[asyncio]`.
It passed in isolation and on a full-suite re-run at the planning baseline.
If it fails, re-run it in isolation before you investigate the change.

---

### Task 1: Render fingerprints and the shared placeholder sentinel

**Files:**
- Modify: `src/tau_coding/tui/widgets.py` — add fingerprint bookkeeping to both transcript widget classes and the module-level sentinel item. No rendering behavior changes in this task.
- Test: `tests/test_tui_transcript_diff.py` — create; fingerprint and sentinel tests live here for all tasks.

**Spec or proposal source:** spec requirement "Render fingerprint covers render inputs" (including the closed recompute-point enumeration, the item-backed-row scope, and the timer-row elapsed-suffix scenario); spec requirements "Finalized stream rows keep their widget" (recompute at finalization and item swap); proposal in-scope items "render_fingerprint exposure" and "module-level sentinel item".

**Proposal constraints:**
- Visible output stays identical to the baseline in every covered scenario; this task changes bookkeeping only.
- Boundary markers record no fingerprint (spec scope: item-backed rows only).
- The public method signatures of `TranscriptView` stay unchanged.
- No timing-based assertions anywhere in the new tests.

**Interface:**
- `TranscriptMessageWidget.render_fingerprint: tuple[object, ...]` — public instance attribute. Set in `__init__`, and recomputed on every `refresh_invocation` exit that follows the stored-input updates (the assignments of `_show_tool_results`, `_invocation`, and `_result_markup`), including the unchanged-render early return. Value: a tuple built from exactly these render inputs: theme name, item role, item text, item tool result text, item update text, item highlight, the tool-result visibility flag, the resolved tool invocation, the resolved tool result markup, the custom markup, and the plain-body flag. Equal inputs produce equal tuples. The tuple also includes the widget's already-computed derived render content: `selection_text` for every message row, and `_markdown_text` for markdown-body rows. The widget computes these derived values at construction and at `refresh_invocation` from the same render inputs, so including them keeps the fingerprint a pure function of the render inputs while covering member-derived content that the base inputs miss: a batch member's `update_text` or member result can change rendered content without changing the parent's own text fields.
- `StreamingTranscriptMessageWidget.render_fingerprint: tuple[object, ...]` — public instance attribute. Value: a tuple of theme name, item role, and item text. Set in `__init__`.
- `StreamingTranscriptMessageWidget.refresh_render_fingerprint() -> None` — new public method: recompute `render_fingerprint` from the current theme, role, and item text. Call it at the end of `finalize()` (both branches: after `replace_text` and after the in-place stop) and from `TranscriptView.finish_assistant_message` immediately after it assigns the canonical item to the widget (`widget.item = item`).
- `_HIDDEN_THINKING_PLACEHOLDER_ITEM: ChatItem` — new module-level constant: a frozen-by-convention `ChatItem(role="thinking", text=_HIDDEN_THINKING_PLACEHOLDER)`. Replace the synthetic placeholder items constructed inline at all four construction sites (`_redraw`, `update_thinking_visibility`, `append_thinking_delta`, and `finish_structured_assistant_message`) with this constant, so placeholder rows fingerprint stably across redraws. Each site keeps its current show-tool-results flag, so baseline rendering is unchanged. The constant is shared read-only; never mutate it.

**Behavior:**
- Two items with equal render inputs produce equal fingerprints; changing any single input changes the fingerprint.
- `refresh_invocation` updates the fingerprint on every exit that follows the stored-input updates, so an in-place row update keeps the fingerprint current even when it early-returns on an unchanged render key.
- A streamed block's fingerprint reflects the full text after `finalize()`, and after an item swap once `refresh_render_fingerprint()` runs.
- Hidden-thinking placeholder widgets share the sentinel item by identity.

**Tests must prove:**
- `test_fingerprint_changes_per_render_input` — table-driven over the render inputs of `TranscriptMessageWidget` (theme, role, text, tool result text, update text, highlight, visibility flag, invocation, result markup, custom markup, plain-body flag): changing one input changes the fingerprint. Map each input to the site where the widget stores it, so the fail-first check fails for the expected reason: invocation and result markup vary on tool rows, custom markup varies on custom rows, the plain-body flag varies through `item.plain_text`, and the remaining inputs vary directly.
- `test_fingerprint_covers_batch_member_content` — a batch tool row whose member `update_text` changes yields a different fingerprint.
- `test_fingerprint_stable_for_equal_inputs` — two equal-input items produce equal fingerprints.
- `test_streaming_fingerprint_recomputed_on_finalize` — stream text, call `finalize()`, and the fingerprint reflects the full text.
- `test_streaming_fingerprint_recomputed_on_item_swap` — after assigning a different item and calling `refresh_render_fingerprint()`, the fingerprint reflects the new item text.
- `test_hidden_thinking_placeholder_uses_shared_sentinel` — run `update_from_state` (or `_redraw`) with `show_thinking=False` and multiple thinking items; every mounted placeholder widget's `.item` is `_HIDDEN_THINKING_PLACEHOLDER_ITEM`.
- `test_boundary_has_no_fingerprint` — `TranscriptWindowBoundary` has no `render_fingerprint` attribute.

**Check:** `uv run pytest tests/test_tui_transcript_diff.py -q && uv run pytest tests/test_tui_streaming.py tests/test_tui_components.py -q && uv run pytest -q && uv run ruff check . && uv run ruff format --check .` — expected: all pass (the full suite also runs because the sentinel swap touches all four placeholder construction sites, exercised by `tests/test_tui_app.py`)

- [x] Write the failing tests for the behaviors above. Run them and check that each fails for the expected reason. One exception: `test_boundary_has_no_fingerprint` is written to pass both before and after the change (it guards scope, not behavior), so the fail-first check applies to every other test
- [x] Implement the interface and behavior
- [x] Run verification (tests, lint, format check)
- [x] Commit: `git add src/tau_coding/tui/widgets.py tests/test_tui_transcript_diff.py && git commit -m "feat: add render fingerprints and shared placeholder sentinel to transcript widgets"`

---

### Task 2: Diff-based redraw on the display-state refresh route

**Files:**
- Modify: `src/tau_coding/tui/widgets.py` — route selection in `update_from_state`, the new `_diff_redraw` method, and the batch-update plus mounted-check treatment of `_redraw`.
- Test: `tests/test_tui_transcript_diff.py` — the route, diff, and preservation tests.

**Spec or proposal source:** spec requirements "Refresh route selection conditions", "Unchanged state refresh remounts nothing", "Small changes touch only changed rows", "Boundary markers reuse with count updates", "Hidden thinking rows collapse to one", "Assistant stream survives a refresh", "Thinking stream refresh keeps baseline", "Finalized stream rows keep their widget", "Removed stream blocks clear bookkeeping", "Theme change rebuilds the window", "Session switch rebuilds in a batched update", "Full rebuilds run in a batched update", "Window paging keeps current behavior", "Thinking visibility toggle keeps behavior", "Tool result toggle keeps in-place updates", "Streaming events keep incremental routes", and "Visible output matches baseline rendering". Proposal-owned items carried here: the diff path lives only on the `update_from_state` route; all `_redraw` call sites outside it keep the full rebuild; every full-rebuild site runs inside `App.batch_update()`; the full-rebuild path gains an explicit mounted check; app.py call sites stay unchanged.

**Proposal constraints:**
- Do not change the four direct `_redraw` call sites (`_shift_window`, both `append_item` repage sites, `finish_structured_assistant_message`); they keep the full rebuild.
- Constructed rows use the same class with the same arguments the full rebuild constructs today. For state items that class is always `TranscriptMessageWidget`, including assistant and thinking roles.
- The retry notice stays outside the desired sequence on every diff render; a displayed notice is removed by any diff render.
- The diff path requests follow scroll under the same condition the baseline redraw uses.
- No timing-based assertions. Do not touch the flaky baseline test.
- `app.py` stays unchanged in this task.

**Interface:**
- `TranscriptView.update_from_state(state, *, theme)` — signature unchanged. New routing before any rebuild: compute `same_state` (identity), `retained_projection` (same state and at least one `state.items` id present in `_item_widgets`), and `theme_changed` (`self._render_theme.name != theme.name`). Route to the full rebuild when `not same_state`, or `not retained_projection`, or `theme_changed`. Otherwise route to `_diff_redraw(scroll_end=should_follow)`. The route conditions, including `theme_changed`, are computed before the `_render_state` and `_render_theme` assignments. Those assignments happen before `_diff_redraw` runs. Keep the existing `should_follow` computation and the existing forced-follow rule for the non-retained case. The full-rebuild branch keeps today's exact call arguments (`scroll_end=should_follow`, `preserve_window=retained_projection and not should_follow`).
- `TranscriptView._diff_redraw(*, scroll_end: bool) -> None` — new private method. Contract, in order:
  1. Window bounds: when `scroll_end` is true, extend to the latest window exactly as `_redraw` does (`_window_end = len(state.items)`, `_window_start = max(0, _window_end - TRANSCRIPT_WINDOW_ITEMS)`). Otherwise keep the current bounds, clamped to the valid range.
  2. Desired sequence, in order: the top boundary when `_window_start > 0` (reuse the existing boundary widget and update its count through `update_count`); one entry per window item with consecutive hidden thinking items collapsed into one placeholder entry (the placeholder widget is reused when its fingerprint matches, and every item of the run maps to the shared widget in `_item_widgets`; the placeholder entry's fingerprint uses the same render inputs the full rebuild passes to the placeholder constructor: the shared sentinel item's render inputs with the tool-result visibility flag set to `state.show_tool_results`, which is why a second refresh reuses it; a placeholder widget constructed at the delta site with flag `False` matches by row kind and content while the flag differs, and the difference surfaces only when the fingerprint disjunct is reached, so the flag value is part of the desired fingerprint); the active assistant widget entry when the window is latest and `assistant_buffer` is non-empty (resolve by identity to `_active_assistant_widget`; construct a new streaming block from the buffer when none is mounted); the bottom boundary when the window is not latest (reuse and update its count). The assistant entry and the bottom boundary are mutually exclusive.
  3. Acquisition per item: reuse the mounted widget from `_item_widgets` when it exists, its parent is the transcript view, and its `render_fingerprint` equals the item's current fingerprint; otherwise construct exactly as `_redraw` constructs today (same class, same arguments, including custom markup, invocation, and result markup resolution). The item's current fingerprint uses the fingerprint formula of the mounted widget's class, so a finalized streaming block compares against the streaming formula (the tuple of theme name, role, and item text) and every other row compares against the message formula (the tuple of eleven render inputs plus the derived `selection_text` for every message row and `_markdown_text` for markdown-body rows); a literal cross-shape comparison remounts every finalized streamed row on every refresh.
  4. Display-item identity and pre-pass: display-item equality throughout the merge means object identity (`is`) between the underlying display items, and the assistant buffer entry's display item is a synthetic buffer-backed item that never equals a state item. Before the row merge, remove every mounted streaming block that the desired sequence does not resolve by identity (live thinking blocks, and a stale assistant block whose entry is not desired), clearing the active-widget bookkeeping for each removed block. This implements the thinking-stream baseline (replacement from state) and the buffer-emptied case. The pre-pass is limited to streaming blocks: the placeholder row that a hidden-thinking delta mounted is not a streaming block and is untracked in `_item_widgets`, the merge reuse rule handles it, and the pre-pass does not remove it. The pre-pass also removes the displayed retry-notice widget and clears `_retry_notice_widget`, so every diff render removes a displayed notice.
  5. Item-row merge: the top boundary is the first child, the bottom boundary the last, and the active assistant block sits between the item rows and the bottom boundary. Reuse existing boundary widgets with count updates; construct one only when absent. Remove a mounted boundary marker the desired sequence omits: the bottom boundary when the window is latest, and the top boundary when `_window_start == 0`. Clear `_bottom_boundary` or `_top_boundary` with the removal. A boundary removal counts as a removal in the fast-path guard (step 8) and in the paint condition (step 9). The end anchor for mounting item rows is the active assistant block when present, otherwise the bottom boundary, otherwise the transcript end. Run the merge over the desired item entries versus the mounted message rows only; the merge's mounted set is every mounted message child of the transcript view, whether or not `_item_widgets` tracks it (this includes the untracked placeholder row that a hidden-thinking delta mounted); boundaries, the active assistant block, and the retry notice stay excluded and end-anchored. Keep a position when the desired entry is that exact widget object, or when both rows share the same row kind and an equal fingerprint (the class-specific fingerprint formula from step 3). At a mismatch: when the mounted row's display item is the same item as the desired entry's (a content-only change), replace exactly that row: mount the newly constructed row before the next mounted item row or the end anchor, and remove the old row. When the display items differ, a remounted suffix starts at this position: construct fresh rows for every remaining desired item entry without reusing mounted rows from the suffix, mount them in order before the end anchor, then remove every mounted item row from the mismatch position to the end of the item rows after mounting. When the desired item entries are exhausted, remove every remaining mounted item row; when the mounted item rows are exhausted, mount the remaining desired entries in order before the end anchor. Never pass an already-mounted widget to mount; when the desired entry resolves to a widget that is still mounted elsewhere, construct a fresh widget instead.
  6. Assistant entry: when desired (the window is latest and the assistant buffer is non-empty), the active assistant block stays mounted untouched: the same object, never removed and never re-mounted, so its object identity and content survive every interleaving, including thinking deltas appended mid-stream. When desired but absent, construct the block from the buffer and register it in the active-widget bookkeeping. When not desired, the pre-pass already removed it.
  7. Bookkeeping: register new `_item_widgets` mappings; when the merge reuses an untracked row by the row-kind-plus-fingerprint disjunct (the placeholder that a hidden-thinking delta mounted), register that row in `_item_widgets` for every desired entry it now represents, so later refreshes resolve it as tracked; delete mappings whose widgets were removed; for every streaming widget the diff removes, update the active-widget bookkeeping (`_active_assistant_widget`, `_active_thinking_widget`, `_active_message_widgets`) the way `_redraw` clears it; the one exception is the active assistant block with a desired entry, which stays mounted untouched (the scoped rule of step 6); recompute `_hidden_thinking_placeholder_visible` on every pass with `_redraw`'s rule, including the keep case; maintain `_window_start`, `_window_end`, `_top_boundary`, and `_bottom_boundary`.
  8. Fast path: when the merge performed no mounts and no removals (every desired entry kept its mounted row through either the identity disjunct or the row-kind-plus-fingerprint disjunct), the boundary counts are unchanged, the assistant entry state matches what is mounted, and the pre-pass removed no displayed notice, perform no mounts, no removals, and no `refresh(layout=True)`. A displayed notice or a boundary removal makes the fast path unavailable on that pass.
  9. Paint: when any mount or removal happened, including a boundary removal, call `self.refresh(layout=True)` once at the end. When `scroll_end` is true, request the follow scroll the same way `_redraw` does (`_request_follow_scroll()`).
- `TranscriptView._redraw(...)` — two treatment changes only: return early when the widget is not mounted (`not self.is_mounted`), and wrap the removal, mounting, and `refresh(layout=True)` work in `with self.app.batch_update():`. All other `_redraw` behavior stays identical.

**Behavior:**
- A second `update_from_state` with the same state and theme performs zero mounts, zero removals, and no layout refresh.
- In-place item changes and tail appends or removals mount and remove exactly the changed rows, below the append-compaction threshold. A running tool row whose elapsed suffix advanced remounts; `update_item` stays the live-progress route.
- A refresh during an active assistant stream keeps the same streaming widget object with no content loss; after `finalize()` the widget shows the full canonical text. A refresh during an active thinking stream keeps baseline behavior: the first such refresh removes the streaming block and its bookkeeping, re-renders the thinking text from state, and the next delta mounts a new streaming block.
- A finalized message row keeps its widget across later refreshes.
- A theme change, a session switch (`state.clear()` plus reload on the same object), and every other full-rebuild route take the batched full rebuild.

**Tests must prove:**
- `test_route_selection_conditions` — a new state object rebuilds; the same state with no retained rows rebuilds; the same state with retained rows diffs; a theme change rebuilds; the new-state-object rebuild (no retained rows) re-enables follow mode before the render.
- `test_second_identical_refresh_remounts_nothing` — populate the transcript with one initial `update_from_state` and let it settle (that first call over a populated state mounts rows because the bookkeeping starts empty); then count `mount` and `remove` invocations across the two counted identical `update_from_state` calls that follow; both counts are zero and every widget object is identical after the second call.
- `test_fast_path_skips_layout_refresh` — the second identical refresh performs no `refresh(layout=True)` call.
- `test_retry_notice_removed_by_every_diff_render` — with a displayed notice, a diff render removes exactly the notice; a second diff render removes nothing.
- `test_tool_result_change_replaces_exactly_one_row` — mutate one tool item's `tool_result_text`; exactly one mount and one removal; every other widget keeps identity.
- `test_in_place_update_keeps_fingerprint_current` — send a tool progress update through `update_item` so the row's `update_text` changes the rendered content (the changed-render path, not the unchanged-render early return), then run one `update_from_state` refresh with no further changes; the updated row keeps its widget object. This test lives in Task 2 because at Task 1 the display-state refresh still always full-rebuilds.
- `test_tail_append_mounts_only_new_row` — append below the append-compaction threshold; exactly one mount, zero removals.
- `test_tail_removal_removes_only_that_row` — remove the last item; exactly one removal, zero mounts.
- `test_positional_shift_remounts_shifted_suffix` — insertion variant: insert an item mid-window; rows before the insertion keep identity, rows at and after the insertion are new. Removal variant: remove a display item before the tail; rows before the shift keep identity, rows at and after the shift remount; this variant exercises the suffix removal range past the desired sequence end.
- `test_following_viewport_stays_pinned_on_diff_render` — in follow mode, a diff render keeps the viewport pinned to the bottom.
- `test_scrolled_up_viewport_does_not_jump_on_diff_render` — scrolled away from the bottom, a diff render preserves the scroll position.
- `test_running_tool_row_remounts_on_elapsed_advance` — a tool item with `started_at` set and no result; advance `time.monotonic` (monkeypatch `tau_coding.tui.state.time.monotonic`); the refresh remounts that row and no other.
- `test_boundary_counts_update_without_remount` — grow the state while the window stays preserved and the viewport does not follow (per the spec scenario's GIVEN: follow mode extends the window and removes the bottom boundary); the boundary widgets keep identity and their count labels update.
- `test_boundary_removed_when_window_extends_to_latest` — with a bottom boundary mounted (window preserved after the state grew) and follow mode re-engaged, a diff render with `scroll_end=True` removes the bottom boundary.
- `test_hidden_thinking_run_collapses_to_one_row` — several hidden thinking items produce exactly one placeholder row; the row bookkeeping maps every item of the run to that shared placeholder widget; a second refresh reuses it.
- `test_placeholder_flag_matches_rebuild_rule_after_refresh` — a hidden-thinking delta mounts the placeholder row as the newest content of a latest window (the GIVEN starts from the delta-mounted placeholder, untracked in `_item_widgets`, not from a prior diff render, per the spec scenario); a diff render runs, another hidden-thinking delta arrives, and no second placeholder row mounts.
- `test_assistant_stream_survives_refresh` — stream deltas, refresh, and the streaming widget object is unchanged; after `finalize()` it shows the full canonical text.
- `test_no_bottom_marker_while_stream_shows` — with the active assistant stream showing at the latest window, no bottom boundary widget is mounted.
- `test_constructed_from_buffer_block_registers_bookkeeping` — refresh while the buffer is non-empty with no active assistant widget mounted; the constructed block is registered as the active assistant widget and in the active message widgets, and the next assistant delta appends to that constructed block.
- `test_thinking_stream_refresh_keeps_baseline` — thinking deltas, then a refresh: the streaming block is removed, the state text renders, and the next delta mounts a new streaming block; after the resolving refresh, a second refresh with no new deltas keeps the re-rendered thinking row's widget object.
- `test_finalized_row_keeps_widget_across_refresh` — finalize a single-block assistant message, then refresh; the same widget object stays mounted.
- `test_removed_stream_block_clears_bookkeeping` — with an active assistant stream, a refresh after the buffer is flushed into state (buffer empty) removes the streaming block, clears its active-widget bookkeeping, and the next delta mounts a new block.
- `test_theme_change_rebuilds_window_batched` — switch to a theme with a different name; every mounted row is new and the rebuild runs inside `App.batch_update()` (count batch-update entries with a counting wrapper around the app method).
- `test_session_switch_rebuilds_in_batch` — `state.clear()` plus reload on the same object; full rebuild inside `App.batch_update()`; the transcript follows the newest content after the switch.
- `test_full_rebuild_wraps_batch_update_and_checks_mounted` — call `_redraw` directly on a mounted view and count one batch-update entry; call `_redraw` on an unmounted view and it returns without raising.
- `test_diff_output_matches_full_rebuild_output` — build a mixed state (user, assistant, thinking, tool rows with results and batches, a grouped file row, a custom row, an error row, and one status row); run the comparison twice, once per thinking-visibility value: for each value, run the diff route on one app and forced full rebuilds on an identical state in a second app; compare widget kinds, row order, and selection texts for equality.
- `test_window_paging_unchanged` — scroll to the top edge; `_shift_window` still performs a full rebuild of 200 items with the anchor preserved.
- `test_thinking_toggle_keeps_behavior` — hiding thinking through `update_thinking_visibility` replaces only thinking rows (one placeholder row per run), every tool row keeps its widget object, and the viewport position is preserved.
- `test_tool_toggle_keeps_in_place_updates` — toggling `show_tool_results` through `update_tool_results_visibility` updates rows in place with zero remounts, and only tool, skill, branch-summary, and compaction-summary rows; after the toggle, a later refresh with no further changes remounts nothing (use a rendering-insensitive tool row in the fixture: a completed tool row with no result text and `started_at` None, so the row gains no elapsed suffix while the test runs; the running-row elapsed case has its own deterministic test, `test_running_tool_row_remounts_on_elapsed_advance`).
- `test_append_item_keeps_incremental_mount` — `append_item` still mounts one row incrementally and extends the window without a full rebuild.
- `test_streaming_events_apply_without_full_rebuild` — drive text deltas, thinking deltas, and a tool progress update through the app's streaming event path; assert no full rebuild runs and the tool event touches only its own row.
- `test_terminal_error_boundary_renders_once` — drive a terminal-error assistant message through the established fake-session event pattern of `tests/test_tui_app.py` (fake sessions whose event stream yields a terminal-error assistant message); assert exactly one display-state render at the boundary, the error row becomes visible, and a full rebuild runs only when the route-selection conditions force one.

**Check:** `uv run pytest tests/test_tui_transcript_diff.py -q && uv run pytest -q && uv run ruff check . && uv run ruff format --check .` — expected: all pass (the full-suite command takes about 95 s)

- [x] Write the failing tests for the behaviors above. Run them and check that each fails for the expected reason
- [x] Implement the interface and behavior
- [x] Run verification (full suite, lint, format check)
- [x] Commit: `git add src/tau_coding/tui/widgets.py tests/test_tui_transcript_diff.py && git commit -m "feat: diff-based transcript redraw on the display-state refresh route"`

---

### Task 3: Fixed flush cadence and per-widget markdown parser

**Files:**
- Modify: `src/tau_coding/tui/widgets.py` — the `_STREAM_FLUSH_INTERVAL` value and the `ThemedMarkdownWidget` parser factory.
- Modify: `tests/test_tui_streaming.py` — parameterize the sleeps in `test_fragments_across_flush_windows_produce_ordered_writes` against a monkeypatched flush interval.
- Test: `tests/test_tui_transcript_diff.py` — cadence and parser tests.

**Spec or proposal source:** spec requirements "Stream flush cadence is fixed at 0.05s" and "Parser built once per markdown widget"; proposal Task 2; proposal constraint that `_STREAM_FLUSH_INTERVAL` stays a module-level constant that tests can monkeypatch; proposal in-scope item "one adjustment to an existing test".

**Proposal constraints:**
- The constant stays monkeypatchable; the value changes from `0.02` to `0.05` and nothing else about the flush scheduling changes.
- No dependency is added to `pyproject.toml`; `markdown_it` is a pinned transitive dependency of Textual in `uv.lock`.
- No timing-based assertions beyond the existing sleeps-relative-to-interval pattern.

**Interface:**
- `_STREAM_FLUSH_INTERVAL = 0.05` — value-only change; the docstring below it stays accurate.
- `ThemedMarkdownWidget.__init__(self, markdown=None, *, theme, classes=None)` — creates a per-widget parser factory closure and passes it to the Textual `Markdown` constructor as `parser_factory`. The Textual `Markdown` base class stores the closure where it keeps the factory (`_parser_factory`). The closure memoizes lazily: its first invocation constructs one `markdown_it.MarkdownIt("gfm-like")` for that widget, and every later invocation returns that same instance. Import `MarkdownIt` from `markdown_it` at module top. Textual invokes the factory on every `append`, `update`, and `_build_from_source` call, so only the per-widget memoization makes the widget build its parser once, at its first parse, instead of once per `append` or `update` call.
- `tests/test_tui_streaming.py::test_fragments_across_flush_windows_produce_ordered_writes` — monkeypatch `tui_widgets._STREAM_FLUSH_INTERVAL` to a small value (for example `0.01`) and make each group sleep longer than that value (for example `0.03`). Keep every existing assertion unchanged.

**Behavior:**
- Streamed fragments flush at most 20 times per second; the staleness bound is the constant itself.
- Each `ThemedMarkdownWidget` constructs exactly one `MarkdownIt` instance for its lifetime, across mount, `update`, and stream appends. Two widgets construct two parsers.

**Tests must prove:**
- `test_flush_interval_is_0_05_and_monkeypatchable` — the module constant equals `0.05` and a monkeypatched value is honored by the flush scheduling (reuse the existing fake-stream pattern: patch the interval, append fragments, and observe the flush window).
- `test_parser_constructed_once_per_widget` — count factory invocations and `MarkdownIt` constructions separately (monkeypatch the `MarkdownIt` name in the widgets module and wrap the widget's stored `_parser_factory` attribute to count invocations) across widget construction, mount with initial text, one `update()`, and several stream appends; the factory is invoked on every parse call, exactly one parser is constructed per widget lifetime across mount, `update`, and stream appends, and a second widget performs its own separate construction.
- Existing suite `tests/test_tui_streaming.py` passes with the parameterized test.

**Check:** `uv run pytest tests/test_tui_transcript_diff.py tests/test_tui_streaming.py -q && uv run pytest -q && uv run ruff check . && uv run ruff format --check .` — expected: all pass

- [ ] Adjust the existing test's sleeps per the contract above, run it, and check that it passes
- [ ] Write the failing tests for the behaviors above. Run them and check that each fails for the expected reason. One exception: `test_parser_constructed_once_per_widget` monkeypatches `MarkdownIt` in the widgets module, an attribute that exists only after the module-top import is added, so the pre-implementation run must use `monkeypatch.setattr(..., raising=False)` (or write the test after the import) and then fails or passes on the construction count
- [ ] Implement the interface and behavior
- [ ] Run verification (full suite, lint, format check)
- [ ] Commit: `git add src/tau_coding/tui/widgets.py tests/test_tui_streaming.py tests/test_tui_transcript_diff.py && git commit -m "feat: slow stream flush cadence to 50ms and cache the markdown parser"`

---

### Task 4: Benchmark script and performance dev-notes

**Files:**
- Create: `scripts/bench_transcript.py` — standalone headless benchmark of the five measured operations.
- Create: `dev-notes/tui-transcript-redraw-perf.md` — what changed, why, how to test, and the before/after table.
- Modify: `dev-notes/tui-responsiveness-perf.md` — one-line cadence correction in the phase 4 section.

**Spec or proposal source:** spec requirement "Benchmark script records before and after" (both scenarios); proposal Required Outcome 9, Acceptance Example 4, and the retained non-behavioral in-scope items (new script, new dev-note, one-line correction to the old dev-note).

**Proposal constraints:**
- The script reproduces the rows and methodology of the proposal's measurement table: headless Textual pilot, 120x40 terminal, 300 items (200 tool rows, 100 markdown messages), one row per measured operation.
- The dev-note records the before and after value for each row; before values come from the proposal's measurement table, after values from this script's output on the completed implementation.
- Documentation of current state only; the old dev-note correction updates the stale cadence value within its sentence rather than describing history twice.

**Interface:**
- `scripts/bench_transcript.py` — runnable with `uv run python scripts/bench_transcript.py`. Structure: build a fake session (the same minimal attribute set the counting tests in `tests/test_tui_responsiveness.py` use), run `TauTuiApp(...).run_test(size=(120, 40))`, populate 300 items through the incremental append route (100 pending tool rows updated through `update_item`, 100 completed tool rows, 100 markdown assistant messages of realistic paragraph length), then measure with median-of-N timing: (1) a full `_redraw(scroll_end=False)` rebuild, (2) `transcript.refresh(layout=True)` on the settled transcript, (3) one `update_item` in-place update, (4) one stream flush cycle (fragments plus flush plus settle) minus the idle settle floor, and (5) `MarkdownIt("gfm-like")` construction time. Print exactly one line per operation in the order of the proposal table. Exit code 0.
- `dev-notes/tui-transcript-redraw-perf.md` — sections: what changed (diff route, cadence, parser), why each exists, how it maps to the plan's spec requirements, how to run the benchmark, and the before/after table with one row per measured operation.
- `dev-notes/tui-responsiveness-perf.md` — in the phase 4 section, update the cadence value inside the sentence that states the ~20 ms flush cadence, so the sentence states the 0.05 s cadence and points to `dev-notes/tui-transcript-redraw-perf.md`. Keep the sentence's still-true coalescing facts (the presentation write stays buffered and flushed, at most one scheduled flush task per widget, never one per token).

**Behavior:**
- The script runs green on the completed implementation and prints five operation rows.
- The dev-note table shows the after values next to the before values from the proposal table.

**Tests must prove:** (no pytest coverage; the proposal maps this to the script output and the dev-note)
- Running `uv run python scripts/bench_transcript.py` prints one line per measured operation, five lines total, matching the proposal table's operations in order.
- The dev-note contains one before and one after value per row.

**Check:** `uv run python scripts/bench_transcript.py && uv run pytest -q && uv run ruff check . && uv run ruff format --check .` — expected: five benchmark rows printed, all tests pass, lint and format clean

- [ ] Write the benchmark script, run it on the completed implementation, and check that it prints five rows
- [ ] Write the new dev-note with the before/after table
- [ ] Apply the one-line cadence correction to `dev-notes/tui-responsiveness-perf.md`
- [ ] Commit: `git add scripts/bench_transcript.py dev-notes/tui-transcript-redraw-perf.md dev-notes/tui-responsiveness-perf.md && git commit -m "docs: add transcript render benchmark and before/after perf notes"`
