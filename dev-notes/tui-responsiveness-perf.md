---
title: "TUI responsiveness optimization phases"
---

## What changed

A set of six focused phases made the Tau TUI feel immediate while keeping the
agent architecture, provider behavior, tool semantics, session format, and
extension API untouched. All changes are confined to `src/tau_coding/tui/`
(the Textual frontend); nothing under `tau_agent` or `tau_ai` was modified.

The goal was a typing hot path that does almost nothing for ordinary prose:

```text
keystroke
    ↓
Textual modifies TextArea
    ↓
cheap completion-context check
    ↓
usually nothing else
```

## Why each phase exists

### 1. Ordinary prompt edits are now essentially free

`on_text_area_changed` used to do four things on every keystroke:

- **Session index reads** — `_build_completion_state` unconditionally called
  `_session_options(session)`, which walks `SessionManager.list_sessions()`
  and parses every session record (sync filesystem + Pydantic). With a few
  hundred indexed sessions that was ~5 ms of work per character, even while
  typing plain prose. Session options are now loaded only when the prompt is
  actually completing a `/resume <arg>` (new autocomplete helper
  `is_resume_argument_completion`, which mirrors the exact condition under
  which argument completions consume options), cached for that one
  interaction, and discarded as soon as the prompt leaves it.
- **Shell-mode restyling** — `_sync_prompt_shell_mode` re-assigned styles and
  explicitly refreshed the prompt widget for every edit. It is now
  transition-based: only entering/leaving `!command` mode touches classes,
  activity indicator, or refresh. The theme's shell style is pushed on mount
  and on theme changes.
- **Empty autocomplete redraws** — `_refresh_completions` re-rendered the
  (empty) completion widget and footer on every keystroke. The handler now
  compares the freshly built `CompletionState` (a frozen dataclass, so `==`
  is structural) and renders only when it actually changed — including the
  one transition that hides a previously visible menu.
- **Pending-paste bookkeeping** — `sync_pending_paste` runs only when a
  large-paste placeholder actually exists (`has_pending_pastes`).

Measured effect: ordinary keystrokes dropped from ~5 ms (with a session read
per key) to ~15-40 µs, and one `/resume` interaction performs exactly one
session listing instead of one per character.

### 2. Completion layout is measured once per render

The completion window search built candidate slices and rendered each one
through a fresh Rich `Console` to count wrapped lines, repeatedly measuring
the same items. `_visible_completion_state` now precomputes every item's
wrapped height once and answers all window queries with prefix sums over item
rows and category separators. The height measurement itself is memoized on
`(display, description, category, width)` with an LRU bound (width is part of
the key, so resizes invalidate naturally). A 60-item menu with wrapped
descriptions dropped from ~250-860 ms to ~0.02-0.18 ms per render — output is
byte-identical to the old algorithm, verified against a reference
implementation battery in the tests.

### 3. `@file` completion scans the repo once per interaction

Typing `@s`, `@sr`, `@src`, `@src/` recursively traversed the repository on
every keystroke. The scan is now cached per working directory for a 3 s TTL
(`FileCompletionIndex`), with a bounded registry (8 cwds) and an explicit
`clear_file_completion_index()` interaction boundary. The tradeoff is
intentional: a file created mid-typing can take up to the TTL to appear,
which is fine for completion and still reflects new files quickly.

### 4. Streamed Markdown is coalesced

Every provider delta previously hit Textual's `MarkdownStream` immediately,
so high-frequency chunks competed with keyboard input. `append_fragment`
still updates canonical state (`item.text`, `selection_text`) instantly, but
the presentation write is buffered and flushed at ~20 ms cadence — at most
one scheduled flush task per widget, and never one per token. Pending text is
flushed before `finalize`, `replace_text`, `_stop_stream`, thinking/assistant
boundaries, and session switches; unmount cancels the scheduled flush and
stops the stream without leaking tasks. Every character is preserved; only
the repaint rate is decoupled from the delta rate.

### 5. Slash commands request a targeted refresh scope

Every handled command used to end with `_refresh()`, which rebuilds the
entire transcript by remounting every block — pure overhead for pickers,
notifications, and screen messages. Commands now resolve to
`RefreshScope.NONE | CHROME | TRANSCRIPT` and the handler dispatches exactly
once:

- **TRANSCRIPT** for `clear`/`reload` (visible state actually rewritten);
- **CHROME** for `/name` renames, `/model` switches, and incremental command
  output (`/reload`, `/system`, `/export` — appended via
  `TranscriptView.append_item`, the same incremental pattern
  `_run_terminal_command` already used);
- **NONE** for pickers, modals, notifications, exit, and commands whose own
  helper already refreshes (`/new`, `/resume`, `/theme`, `/compact`,
  thinking changes) — previously those rebuilt the transcript twice.

### 6. Dev-only slow-interceptor warning

Extension key interceptors run synchronously before every main-screen key, so
a handler doing disk/network/traversal work stalls the whole UI. With
`TAU_DEBUG_TUI_PERF=1`, interceptors are timed and each slow handler
(> 5 ms) logs one warning; disabled by default, the production path reads no
clock and logs nothing.

## What did not change

- The agent loop, provider/model APIs, session persistence format, and
  autocomplete/file-reference behavior (ignored dirs, hidden paths, relative
  refs, limits) are all preserved.
- `SessionManager` still lists sessions synchronously when asked — the bug
  was the TUI calling it from a keystroke hot path, not the manager itself.
- The 200-item mounted transcript window and paging mechanism are untouched.

## How to test or use it

- `uv run pytest tests/test_tui_responsiveness.py tests/test_tui_completion_layout.py tests/test_tui_file_completions.py tests/test_tui_streaming.py tests/test_tui_command_refresh.py tests/test_tui_interceptor_perf.py` — the new regression suites assert *what work occurs* (session reads, scans, renders, refresh counts, write counts) rather than wall-clock timings, so they stay deterministic in CI.
- Manual: hold down a key while a model streams output; the input should stay
  visually immediate. Type `/resume ` and watch the session list appear once;
  type `@src` in a large repo; `export TAU_DEBUG_TUI_PERF=1 tau` while
  extensions are loaded to surface slow key interceptors.

## How it maps to Pi's design

All of this lives in the frontend layer (`tau_coding.tui`). The agent harness
and the TUI communicate through the existing adapter/event boundary without
any new coupling: batching happens at the presentation widget, caching at the
autocomplete layer, and refresh scoping inside the app's command dispatch —
the reusable agent brain stays clean.
