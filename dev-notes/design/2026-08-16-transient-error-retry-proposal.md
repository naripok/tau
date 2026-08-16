# Proposal: Automatic retry of transient provider errors

## Intent

Users sometimes hit terminal run failures caused by transient network or
provider errors, for example:

```
Error: peer closed connection without sending complete message body (incomplete chunked read)
Run ended before completion. Send a message to retry.
```

Tau already retries transient HTTP statuses and pre-content transport failures
inside the `tau_ai` adapters, but a connection drop *mid-stream after content
has been emitted* is terminal by design — `dev-notes/provider-error-recovery.md`
explicitly defers a "broader policy for partially failed turns". Long
generations are exactly when connections drop mid-stream, so users see
avoidable run deaths. This change adds automatic turn-level retry for
transient failures with transparent rollback of the partial content.

## Scope

**In scope:**

- Harness-level retry of main-turn provider calls whose failure the provider
  adapter classifies as transient-retryable. Retried attempts never enter
  harness history, session storage, or provider context.
- Adapter classification of terminal failures: transport errors (including
  mid-stream drops after partial content), in-stream SSE errors matching each
  adapter's existing transient markers, and transient HTTP statuses whose
  adapter budget was exhausted are retryable; non-transient statuses, terminal
  rate limits, context-overflow errors, and user-cancelled streams are not.
- Trailing-read edge case: a transport error after the parser already saw the
  terminal stream marker means the response is actually complete — the message
  completes normally with a diagnostic attached instead of failing the run.
- A harness retry-start event (attempt, max attempts, delay, reason); the
  failed attempt's terminal error event is suppressed while retries remain.
- TUI: rollback of the partial assistant transcript and a transient retry
  notice, replaced by the fresh stream; the final-failure projection stays as
  today (last attempt's partial text + error block + log path + retry hint).
- Print-mode CLI: a retry notice line; already-printed partial text stays
  (a terminal cannot unprint).
- `agent-calls.jsonl`: one non-secret `assistant_retry` diagnostic entry per
  reattempt; terminal failures still produce exactly one `assistant_error`
  entry as today.
- Per-provider configuration `turn_retry_max` (default 2, `0` disables),
  wired through provider config, `tau setup` flag, and runtime into the
  harness budget; website docs updated.
- Shared transient/overflow marker helpers in `tau_ai` so `tau_coding` stops
  duplicating the context-overflow marker list.
- Tests at every layer, a dev-note, and website doc updates.

**Out of scope:**

- Retrying failed *tool executions* (app-defined tools, not provider calls).
- One-shot non-streaming provider calls (session auto-naming, compaction).
- Auto-resume of an interrupted run from disk.
- Retrying user-cancelled (`aborted`) runs.
- The manual retry UX ("Send a message to retry") — unchanged.

## Approach

Turn-level retry inside the agent loop, with classification in the adapters:

1. Each `tau_ai` adapter marks its terminal error with a retryable
   classification (transport errors, transient statuses after budget
   exhaustion, in-stream SSE errors per existing markers → retryable; other
   statuses, terminal rate limits, overflow markers, aborted → not).
2. The harness loop, on a retryable failure within its own budget, discards
   the failed attempt's partial output (it was never appended to history),
   emits a retry-start event, waits a cancellable exponential backoff, and
   reissues the request with identical context.
3. The TUI rolls the partial content back on the retry-start event, mounting
   a transient notice; the fresh stream renders in its place.

Alternatives considered and rejected:

- *Adapter-internal retry of mid-stream drops*: retry machinery already lives
  in adapters, but consumers can only roll back via signals crossing the Pi
  provider boundary, and the same relaxation would need to be replicated in
  five adapter envelopes. Retrying a failed turn is a turn-level concern.
- *Session-level orchestration like the context-overflow path*: would keep the
  failed turn visible in history and transcript (duplicate/partial content),
  and would persist a failed turn that later succeeds.
- *Fully unified harness retry* (removing adapter-internal transport retries):
  regression risk on well-tested behavior with no clear benefit.

## Impact

- `tau_agent`: agent loop (bounded retry around each turn's provider call),
  harness config (budget), one new agent event, a retryable flag on the
  provider error path, generic backoff helpers moved down from `tau_ai`.
- `tau_ai`: classification in all five adapters (openai-compatible, anthropic,
  codex, google, mistral), retryable flag on the terminal error event, the
  stream bridge (tail-read success + flag pass-through), shared marker
  helpers.
- `tau_coding`: per-provider config field + settings schema export, `tau setup`
  flag, provider runtime pass-through, session (diagnostics, harness build),
  TUI state/adapter/transcript rollback and notice, print-mode renderer.
- `website`: configuration reference, providers-and-models guide, sessions
  guide, CLI reference.
- No new dependencies. Docs: new dev-note; update the "broader policy" note
  in `dev-notes/provider-error-recovery.md`.
