---
title: "Provider Retry Events"
---

Tau retries transient provider failures in two places: the `tau_ai` adapters
retry requests that emitted nothing (transient statuses, pre-content transport
errors), and the agent turn loop (`tau_agent.loop`) retries whole turns when a
failure survives the adapter — including mid-stream drops after partial
content. Turn-level classification lives in `tau_agent.retry` and reads the
`provider_error` diagnostics that adapters already attach to terminal errors,
so adapters carry no retry-classification code.

## What Was Added

Provider adapters can emit `ProviderRetryEvent` before retrying a failed request.
The event includes the next attempt number, total attempts, delay, a
human-readable message, and structured diagnostic data.

## Behavior

OpenAI-compatible, Anthropic, and OpenAI Codex subscription providers retry
transient status codes such as `408`, `409`, `429`, and `5xx` responses before
surfacing a final provider error. The default is two retries, for three total
request attempts.

The Anthropic and OpenAI Codex adapters also retry transient *in-stream*
failures. Both APIs can return HTTP 200 and then send an SSE error event. For
Anthropic, retryable error types are `api_error`, `overloaded_error`, and
`rate_limit_error`. Codex classifies `error` and `response.failed` events against
transient markers such as overloaded, service unavailable, rate limit,
internal/server errors, and timeouts.

When an in-stream error arrives before content or thinking deltas, the adapter
emits `ProviderRetryEvent` and reissues the request under the same `max_retries`
budget. Non-transient errors such as `authentication_error` or
`invalid_api_key` stay terminal to avoid replaying visible output or tool calls.

## Turn-level retry on top of adapter retries

Once the adapter budget is exhausted — or a failure arrives after partial
content — the adapter produces a plain terminal `ProviderErrorEvent` with no
classification. The harness loop classifies it centrally
(`tau_agent.retry.failure_is_retryable`): cancelled runs, terminal rate-limit
and context-overflow markers, and non-transient HTTP statuses are terminal;
everything else is retried. See
[`transient-error-retry.md`](../../2026-08-16-transient-error-retry.md) for the
marker lists and the fixed two-retry budget.

While reattempts are below the budget the loop discards the failed attempt (its
terminal error event is suppressed and the partial content never enters
history), emits `TurnRetryStartEvent`, waits a cancellable backoff, and
reissues the provider call. `ProviderRetryEvent` metadata is provider-internal
progress and is not forwarded across the Pi boundary into agent events; turn
retries are announced by `TurnRetryStartEvent` instead.

Backoff is short, exponential (base `0.25s`, doubling, capped at `1s`), and
cancellation is checked during the delay so Escape/TUI cancellation does not
wait for the entire retry sleep to finish.

## Rendering

Transcript and TUI renderers show retry progress as subtle status output. Final
text mode ignores retry progress and only prints the final assistant response or
final error.

## Boundary

Adapters decide their own pre-content retries with provider-specific markers;
`ProviderRetryEvent` progress and the adapter's `data` payload stay in the
provider layer for diagnostics. The agent layer owns the turn-level retry
policy: classification, how many reattempts happen (a fixed budget of two),
what consumers see in between, and what lands in history.
