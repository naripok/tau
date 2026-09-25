# Prompt queueing during compaction

## What

While the TUI is running a manual compaction (`/compact`), submitting a
prompt no longer refuses with "wait to submit". Instead the prompt is
queued in the TUI, shown in the queued-messages strip above the prompt
input (labelled `⧗ queued during compaction:`), and digested automatically
as ordinary agent turns right after the compaction worker finishes.

Slash commands other than `/compact` and input-bar terminal commands
(`!cmd`) are still blocked while compaction runs, with the original
"keep editing, but wait to submit" warning.

## Why

Compaction can take many seconds (it performs a summarization provider
call over the whole old context). During that time users often think of
their next message; the previous behavior forced them to keep the editor
text and re-press Enter later, which felt like the TUI was locked. Queueing
matches the existing behavior for prompts typed while the agent is running
(steering/follow-up queue).

## How it works

Manual compaction is a `tau_coding` session method, not an agent turn: the
`AgentHarness` is idle while `session.compact()` runs, so the harness
steer/follow-up queues cannot hold the prompt. The queue therefore lives in
the TUI layer:

- `TuiState.pending_prompts` holds the display list; it feeds the existing
  `#queued-messages` strip and is included in `TuiState.queued_message_count`,
  so `/compact` and `/tree` treat undigested prompts as queued work and wait.
- `TauTuiApp._compaction_pending_prompts` is the source of truth. Submitting
  a prompt while `_is_compaction_active()` appends to it instead of calling
  `handle_command`/`session.prompt`.
- When the compaction worker exits successfully (or fails without touching
  the context), `_run_compaction` calls `_schedule_pending_prompt_flush()`.
  The flush runs in its own worker group (`pending-prompts`) so the
  exclusive default-group prompt worker never cancels the compaction worker
  that scheduled it. `_flush_pending_prompts` submits one prompt per agent
  turn, awaiting each turn worker via `Worker.wait()` to keep order.
- `_run_prompt`'s finally block reschedules the flush when the queue is
  non-empty and the session settled, recovering from a manual submission
  that raced a flush turn.
- Escape (`action_cancel`) cancels compaction or the active turn *and*
  discards the queued prompts with a count ("Discarded 1 prompt queued
  during compaction."). Prompts stay recallable via prompt history
  (arrow-up). `/new` and `/resume` also discard the queue silently because
  they swap the session.

## Scope

Only manual compaction is covered. Auto-compaction (context-overflow and
threshold-based) already runs inside `session.prompt()`, where queued
steering/follow-up messages work unchanged. Slash commands remain blocked
during compaction because many of them (`/new`, `/resume`, `/export`) swap
or rewrite session state and would race the compaction worker.

## How to test/use it

- Automated: `tests/test_tui_app.py::test_tui_app_queues_prompt_while_compacting`,
  `test_tui_app_flushes_multiple_queued_prompts_in_order`,
  `test_tui_app_blocks_commands_and_terminal_commands_while_compacting`,
  `test_tui_app_discards_queued_prompts_when_compaction_cancelled`, and
  `test_tui_app_queued_prompts_block_new_compaction_until_digested`.
- Manual: start `tau`, run `/compact`, type a message and press Enter while
  it runs. The message appears in the queue strip; when the compaction
  summary lands, the agent starts answering the queued prompt.
