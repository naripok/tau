# Turn-level retry of transient provider errors

## Why it exists

Providers occasionally drop a connection mid-stream after partial content has
been emitted, for example:

```
Error: peer closed connection without sending complete message body (incomplete chunked read)
```

Tau already retried transient HTTP statuses and pre-content transport failures
inside the `tau_ai` adapters, but a drop *after* content started was terminal
by design. This note describes the turn-level retry that makes such drops
transient: the agent loop reissues the failed turn while the TUI rolls the
partial content back.

## How it works

The retry decision lives entirely in the agent layer (`tau_agent.loop`). When a
main-turn provider call ends in a terminal error, the loop classifies it with
a single rule (`tau_agent.retry.failure_is_retryable`):

- a cancelled run is never retried;
- a failure whose body or message matches a terminal rate-limit marker
  (`gousagelimiterror`, `freeusagelimiterror`, `monthly usage limit reached`,
  `available balance`, `insufficient_quota`, `out of budget`, `quota exceeded`,
  `billing`) or a context-overflow marker (`context length`, `context window`,
  `context limit`, `maximum context`, `max context`, `input is too long`,
  `input length`, `prompt is too long`, `too many tokens`, `token limit`,
  `exceeds the limit`, `exceeded the limit`) is never retried;
- a failure carrying an HTTP status is retried only for transient statuses
  (`408`, `409`, `425`, `429`, any `5xx`);
- any other failure (transport-level errors, in-stream errors without terminal
  markers) is retried.

The classification reads the `provider_error` diagnostic that adapters already
attach to terminal errors (`status_code`, `body`) plus the error message text —
no adapter cooperation is required, and adapter behavior is unchanged.

On a retryable failure the loop discards the failed attempt's partial output
(consumers never see the failed attempt's terminal error event while retries
remain), emits `TurnRetryStartEvent` (attempt, max attempts, delay, reason),
waits a cancellable exponential backoff (base `0.25s`, doubling, capped at
`1s`), and reissues the call with identical context. The budget is a fixed
default of two retries — three total attempts counting the original request —
and is not configurable. The reason names the failure: `HTTP <status>` when
the failure carries one, otherwise its message text. Cancellation during the
backoff surfaces the failure terminally with the discarded partial content
removed.

## What happens in each UI

- **TUI**: on `TurnRetryStartEvent` the transcript rolls back the failed
  attempt's partial text and thinking blocks and shows
  `… Connection lost — retrying N/M: <reason>`; the reattempt's stream renders
  in place. Exhausted or non-retryable failures project the terminal error
  exactly as before.
- **Print mode**: a notice line prints; already-printed partial text cannot be
  unprinted and stays.
- **agent-calls.jsonl**: one non-secret `assistant_retry` entry per reattempt
  (attempt, max attempts, reason, message). Retried attempts never produce a
  terminal `assistant_error` entry; a final failure produces exactly one.

## Scope

Only main assistant turns inside `run_agent_loop` are retried. One-shot
provider calls (auto-naming, compaction summarization) and tool executions are
never turn-retried. Context overflow is never retried: the existing compaction
path still handles it. Failed attempts never enter harness history, session
storage, or provider context.

A transport error that arrives after the response was effectively complete is
retried like any other failure — the complete response is discarded and
regenerated. This is intentional: the loop cannot distinguish it from a
mid-stream drop without adapter cooperation, and the failure is rare.

## How to test

```bash
uv run pytest tests/test_agent_retry.py tests/test_agent_loop.py
uv run pytest tests/test_coding_session.py -k "retry or auto_naming"
uv run pytest tests/test_tui_app.py tests/test_tui_adapter.py tests/test_rendering.py
uv run pytest
```

## Design history

The first version of this feature classified retries in every provider adapter
(per-adapter `retryable` flags, parser tail-read handling, a shared
`tau_ai/classify.py`) and exposed a per-provider `turn_retry_max` budget. It
was replaced because the policy belongs with the turn semantics it decides and
the adapter/config surface was expensive to maintain against upstream merge
churn. The simplified design keeps the observable behavior (rollback, notices,
diagnostics, three attempts by default) while reverting the adapter and
configuration changes.
