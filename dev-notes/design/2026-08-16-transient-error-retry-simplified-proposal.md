# Proposal: Simplified transient-error retry design

## Intent

The shipped implementation of automatic retry for transient provider errors
(13 commits, ~2,100 added lines) is behaviorally complete, but it spreads the
retry policy across layers that change frequently upstream:

- per-adapter classification flags in all five `tau_ai` adapters plus
  parser-local tail-read handling,
- a per-provider `turn_retry_max` configuration field plumbed through the
  three provider config models, merge helpers, settings persistence, a
  `tau setup` flag, and three session wiring sites,
- shared classifier helpers (`tau_ai/classify.py`) and a retry-helper move
  between layers.

Upstream merges regularly rewrite the TUI and adapter files (the TUI was
rewritten mid-implementation), so a policy implemented at those layers is
expensive to maintain. This proposal simplifies the design by moving the
retry decision entirely into the agent turn loop, where it stays close to the
turn semantics it decides. The loop reuses diagnostic data that upstream
adapters already attach to terminal error messages — `status_code` and `body`
in the `provider_error` diagnostic — so no provider-layer cooperation is
needed.

## Scope

**In scope:**

- Central retryability classification in the agent loop: a failure is
  retryable when the run is not cancelled, the diagnostic body and message
  text carry no terminal markers (usage/balance limits, context overflow),
  and the failure either carries no HTTP status (transport-level or
  in-stream errors) or carries a transient HTTP status (408, 409, 425, 429,
  5xx). Non-retryable conditions govern when both apply.
- A fixed turn-level retry budget of two retries (three total attempts,
  counting the original request), with no per-provider configuration surface.
- Retained from the shipped design: bounded turn-level retry mechanics
  (partial-content discard, suppressed terminal error while retries remain,
  retry-start event with attempt/max/delay/reason, cancellable exponential
  backoff with a cap), TUI transparent transcript rollback with a transient
  notice, print-mode retry notice, `assistant_retry` diagnostics, no retry of
  context-overflow failures, no retry of one-shot calls, cancellation
  semantics. The retry notice's failure reason becomes the HTTP status when
  present, otherwise the failure's message text (the shipped design reported
  the error type from adapter-added data, which the revert removes).
- Removal of the tail-read optimization: a transport error that occurs after
  the response was effectively complete is retried like any other retryable
  failure, discarding the complete response and regenerating it within the
  budget.
- Reverts to the pre-feature fork state: all five adapter changes (retryable
  flags, tail-reads, classifier imports, `_should_retry` signature), the
  `tau_ai/classify.py` module, the retry-helper move, the `retryable` fields
  on provider/agent error events, and all `turn_retry_max` configuration
  plumbing.
- Docs: implementation dev-note, `dev-notes/provider-error-recovery.md`,
  `dev-notes/architecture/provider-retries.md`, and the website guides.

**Out of scope:**

- Retrying failed tool executions (app-defined tools, not provider calls).
- One-shot non-streaming provider calls (session auto-naming, compaction).
- Auto-resume of an interrupted run from disk.
- Retrying user-cancelled (`aborted`) runs.
- The manual retry UX ("Send a message to retry") — unchanged.
- Any new configuration knob.

## Approach

1. The agent loop's `_assistant_events` keeps the shipped bounded retry loop
   (attempt counting, terminal-error suppression, `TurnRetryStartEvent`,
   cancellable backoff) and decides retryability centrally: cancelled → not
   retryable; terminal markers in diagnostic body or message text → not
   retryable; `status_code` present → retryable only for transient statuses;
   otherwise (transport/in-stream failure) → retryable.
2. All `tau_ai` adapter changes and all `turn_retry_max` configuration
   plumbing revert to the pre-feature fork state (which already includes the
   upstream merge).
3. The TUI rollback/notice machinery, print-mode notice, session diagnostics,
   and `TurnRetryStartEvent` remain as shipped.

Alternatives considered and rejected:

- *Adapter-driven retry reusing `max_retries`*: retry machinery already lives
  in adapters, but the mid-stream relaxation still needs to be replicated in
  five adapter envelopes, a notice marker must cross the provider boundary,
  and semantics change for one-shot calls.
- *Keeping the shipped design*: behaviorally most precise (per-adapter
  denylists, tail-read success, per-provider budget) but ~2,100 lines of
  merge-hostile surface for one rare failure mode. The three deltas below are
  bounded and rare-by-case.

## Impact

- `tau_agent`: `loop.py` (central classifier, reason text, retry loop kept),
  `retry.py` (kept; classification helpers join the timing helpers here),
  `events.py`/`harness.py` unchanged from shipped.
- `tau_ai`: restored to the pre-feature fork state — zero diff.
- `tau_coding`: `session.py` restored to pre-feature state plus the shipped
  diagnostics block; `cli.py` restored plus the print-mode notice; TUI files
  unchanged from shipped; `provider_config.py` untouched.
- Tests: agent-loop tests reworked around central classification; adapter,
  classifier, and config test additions removed; TUI, diagnostics, and
  print-mode tests kept.
- Website: configuration, providers-and-models, sessions, and CLI guides
  updated (removes `turn_retry_max`, documents the fixed budget and central
  policy). No new dependencies.
