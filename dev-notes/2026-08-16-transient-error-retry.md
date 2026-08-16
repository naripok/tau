# Automatic retry of transient provider errors

## What changed

Transient provider failures (connection drops, overloads, transient HTTP
statuses) are now retried automatically at the **turn level** by the agent
harness, on top of the existing provider-internal retries. A failed attempt's
partial output is discarded: it never appears in the final session history and
never reaches the TUI transcript; consumers see a transient "Connection lost —
retrying" notice followed by the reattempt's fresh stream.

The specific production failure that motivated this work —
`Error: peer closed connection without sending complete message body
(incomplete chunked read)` on OpenRouter mid-stream — now recovers by reissuing
the turn instead of ending the run.

## Why it exists

Provider adapters already retried transport errors, but only when nothing had
been emitted yet. A drop after partial output (the common case for long
reasoning streams) was terminal by design: the TUI projected the truncated
thinking text plus the error, and the run ended. Over a single day, an
OpenRouter session hit the incomplete-chunked-read drop repeatedly with
`provider.attempts: 1` — the adapter retry never fired because content had
already streamed.

Turn-level retry moves the decision up: adapters keep classifying failures
(they see status codes, bodies, and SSE events), and the harness loop owns the
retry budget so failed attempts can be kept out of history and storage.

## Architecture

Retryability is classified in `tau_ai` and carried on the provider error event:

- `ProviderErrorEvent.retryable` (`src/tau_ai/_provider_events.py`) is passed
  through `canonicalize_provider_stream` to
  `AssistantErrorEvent.retryable` (`src/tau_agent/provider_events.py`),
  defaulting to `False`.
- `src/tau_ai/classify.py` holds the shared markers: transient HTTP statuses
  (`408`, `409`, `425`, `429`, `5xx`), terminal rate-limit bodies
  (`insufficient_quota`, `quota exceeded`, `billing`, …), and context-overflow
  vocabulary. Terminal markers govern: a `429` with a quota body is never
  retried. The session's `is_context_overflow_error` now uses the same markers,
  so overflow compaction behavior is unchanged.
- All five adapters (`openai_compatible`, `anthropic`, `openai_codex`,
  `google`, `mistral`) classify their exhausted-status errors and their
  transport errors. The adapter-internal retry budget is unchanged; only the
  terminal events it eventually produces carry the `retryable` flag.

The harness (`src/tau_agent/loop.py::_assistant_events`) consumes the flag:

- A retryable failure below the budget emits `TurnRetryStartEvent` (attempt,
  max attempts, backoff delay, reason, error message/type), waits the backoff
  in a cancellable loop (`src/tau_agent/retry.py`, 0.25 s base, doubling, 1 s
  cap), and reissues the provider call with the same request context. The
  failed attempt's terminal error end is suppressed — the caller never sees it
  and never appends it to history.
- Cancellation during backoff surfaces the retryable failure as a terminal
  error with its discarded partial content removed; cancellation during a
  reattempt behaves like a cancelled stream today.
- A zero budget (`max_turn_retries=0`) keeps the pre-feature behavior exactly.
- One-shot provider calls (auto-naming, compaction summaries) are never
  turn-retried; they bypass the harness loop.

Tail reads complete: when a transport error arrives *after* the response's
terminal marker (`[DONE]`/`message_stop`/`response.completed`/a Gemini chunk
carrying a finish reason), the adapter treats the response as complete and
attaches a `response_tail_read` diagnostic to the finished message instead of
failing the run (`attach_tail_read_diagnostic` in `src/tau_ai/stream.py`).

Configuration and UI:

- Per-provider `turn_retry_max` (default `2` = three total attempts, `0`
  disables) on the three provider config models, validated (negatives
  rejected), serialized through `to_json()` and the settings reload path, and
  configurable via `tau setup --turn-retry-max`. The session wires it into the
  harness budget on load and on provider activation.
- The TUI adapter rolls back the failed attempt's partial items and shows the
  notice (`format_retry_notice` in `src/tau_coding/tui/state.py`); the
  transcript discards the failed streaming widgets
  (`TranscriptView.discard_active_assistant`) and mounts a status widget that
  is cleared when the reattempt streams or the terminal projection replaces it.
- Print mode prints the notice line; exhausted retries leave the terminal error
  projection ("Error: …") as before.
- Each reattempt logs one `assistant_retry` entry (attempt, max attempts,
  reason, error message/type, provider/model/run ids) in
  `agent-calls.jsonl`; retried attempts never log `assistant_error`.

## How to test

```bash
uv run pytest tests/test_agent_retry.py tests/test_agent_loop.py tests/test_agent_harness.py
uv run pytest tests/test_tau_ai.py -k "retryable or tail_read or classify"
uv run pytest tests/test_coding_session.py -k "turn_retry or retry_budget"
uv run pytest tests/test_tui_adapter.py -k retry tests/test_tui_app.py -k retry
uv run pytest tests/test_rendering.py -k retry
uv run pytest
```

Manual check: point Tau at a flaky proxy (or a provider that drops
long streams) and confirm the TUI shows the "… Connection lost — retrying
2/3: …" notice, the partial text disappears, the reattempt streams in place,
and `~/.tau/logs/agent-calls.jsonl` contains one `assistant_retry` entry per
reattempt and no `assistant_error` for the successful retry.
