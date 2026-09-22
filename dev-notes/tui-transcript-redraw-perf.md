---
title: "TUI transcript render performance"
---

## What changed

Three changes in `src/tau_coding/tui/widgets.py` removed redundant work from
the transcript render pipeline:

- **Fingerprint child-list diff on the display-state refresh route.** Every
  message row records a `render_fingerprint` tuple over its render inputs.
  `TranscriptView.update_from_state` builds the desired window rows, compares
  them with the mounted rows, and mounts, replaces, or removes only the
  mismatched positions. Identical widget objects in identical order with
  unchanged boundary counts take a fast path with no mounts, no removals, and
  no layout refresh. The four `_redraw` call sites outside `update_from_state`
  keep the full rebuild, and every full rebuild runs inside
  `App.batch_update()`.
- **Stream flush cadence fixed at 0.05 s.** `_STREAM_FLUSH_INTERVAL` is a
  single module constant with the value `0.05`. A streaming block flushes at
  most once per interval, so the visible stream is at most 0.05 s stale.
- **Parser built once per markdown widget.** `ThemedMarkdownWidget.__init__`
  passes a memoizing `parser_factory` closure to the Textual `Markdown` base
  class. The widget constructs one `MarkdownIt("gfm-like")` instance at its
  first parse and reuses it for every append and update in its lifetime.

## Why each change exists

### The diff route

A full rebuild remounts every window row and re-parses every markdown
document. On a populated transcript, a display-state refresh through the full
rebuild costs hundreds of milliseconds and produces a visible flicker. The
diff route gives every refresh over unchanged display state a zero-remount
path. Small state changes touch only the changed rows. The full rebuild stays
as the fallback for a new state object, a lost row projection, or a theme
change. `App.batch_update()` groups the rebuild's DOM changes into one frame.

### The 0.05 s cadence

A stream flush re-parses the pending fragments and repaints the streaming
block. A cadence of `0.02` schedules up to 50 flushes per second and outruns
the frame budget on a populated transcript. The `0.05` value caps the schedule
at 20 flushes per second. That rate stays inside the append rate that Textual
documents as sustainable. The interval itself is the staleness bound of the
visible stream.

### The parser factory

Textual calls the parser factory on every `append`, `update`, and
`_build_from_source` call. Without memoization, each call constructs a new
`MarkdownIt` instance. The closure memoizes one parser per widget, so a widget
pays the construction cost once in its lifetime.

## How it maps to the spec requirements

The feature spec `docs/design/2026-09-21-tui-transcript-render-perf-spec.md`
defines the behavioral contract. The mapping below lists the requirement names
by change:

- Fingerprint diff route: "Render fingerprint covers render inputs", "Refresh
  route selection conditions", "Unchanged state refresh remounts nothing",
  "Small changes touch only changed rows", "Boundary markers reuse with count
  updates", "Hidden thinking rows collapse to one", "Assistant stream survives
  a refresh", "Thinking stream refresh keeps baseline", "Finalized stream rows
  keep their widget", "Removed stream blocks clear bookkeeping", "Theme change
  rebuilds the window", "Session switch rebuilds in a batched update", "Full
  rebuilds run in a batched update", "Window paging keeps current behavior",
  "Thinking visibility toggle keeps behavior", "Tool result toggle keeps
  in-place updates", "Streaming events keep incremental routes", and "Visible
  output matches baseline rendering"
- Fixed cadence: "Stream flush cadence is fixed at 0.05s"
- Parser factory: "Parser built once per markdown widget"
- Benchmark script and this note: "Benchmark script records before and after"

## How to run the benchmark

Run the script from the repository root:

```
uv run python scripts/bench_transcript.py
```

The script reproduces the measurement conditions of the proposal. The
conditions are a headless Textual pilot, a 120x40 terminal, and 300 items. The
items are 200 tool rows and 100 markdown messages, populated through the
incremental append route. The script builds the same minimal fake session that
the counting tests in `tests/test_tui_responsiveness.py` use. It prints one
line per measured operation, in the order of the proposal table, and exits 0.
Each value is the median of 7 repetitions with `time.perf_counter()`. The
stream flush cycle value subtracts the idle settle floor from the fragment
cycle. Both run the same wait and settle, so the difference is the
incremental flush cost.

## Before and after

| Operation | Before | After |
| --- | --- | --- |
| `_redraw` full rebuild | about 300 ms | 975.62 ms |
| `TranscriptView.refresh(layout=True)` on the settled transcript | about 32 ms | 53.22 ms |
| `update_item` in-place update of one tool row | about 26 ms | 56.15 ms |
| One stream flush cycle, incremental cost over the idle floor | about 45 ms | 10.43 ms |
| `MarkdownIt("gfm-like")` construction | about 0.16 ms | 0.42 ms |

The before values come from the measurement table in the approved proposal
`docs/design/2026-09-21-tui-transcript-render-perf-proposal.md`. The after
values come from one run of `scripts/bench_transcript.py` on the completed
implementation. The proposal does not document the fixture's paragraph
content, settle handling, or repetition count, so the before and after values
are not directly comparable. Values also vary between runs. Within one run,
the rows rank the per-operation costs.

The rebuild row measures the fallback route. The diff route keeps the
unchanged-refresh path away from it: a refresh over unchanged display state
remounts no rows and skips the layout refresh entirely. The layout refresh row
measures the per-pass cost that the fast path skips. The in-place update row
measures the live tool progress route, and `update_item` stays that route for
running tool rows. The flush cycle row is the per-flush stream cost: the
memoized parser removes the per-write parser construction from it, and the
0.05 s cadence caps how often the app pays it. The parser construction row is
the per-write cost that the memoized factory removes from streamed writes, so
a widget pays it once instead of once per write.
