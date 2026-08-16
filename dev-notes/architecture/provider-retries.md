---
title: "Provider Retry Events"
---

Tau retries transient provider failures in `tau_ai`, where HTTP status codes and
transport exceptions are visible. This keeps retry classification out of
`tau_agent` while still allowing the portable agent loop to surface progress.

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

Adapter retries only reissue requests that emitted nothing. Once the adapter
budget is exhausted — or a failure arrives after partial content — the adapter
produces a terminal `ProviderErrorEvent` classified with a `retryable` flag
(see [`transient-error-retry.md`](../../2026-08-16-transient-error-retry.md)
for the classification rules, shared markers in `tau_ai/classify.py`, and the
per-provider `turn_retry_max` budget). The adapter never decides to turn-retry:
that is the harness's job.

The harness loop (`tau_agent.loop`) consumes the `retryable` flag on
`AssistantErrorEvent`: while reattempts are below the budget it discards the
failed attempt (its terminal error event is suppressed and the partial content
never enters history), emits `TurnRetryStartEvent`, waits a cancellable
backoff, and reissues the provider call. `ProviderRetryEvent` metadata is
provider-internal progress and is not forwarded across the Pi boundary into
agent events; turn retries are announced by `TurnRetryStartEvent` instead.

A transport error that arrives after the response's terminal marker (done
marker, stream-stop event, or finish-reason chunk) is treated as a complete
response: the run continues and the finished message carries a
`response_tail_read` diagnostic instead of an error.

Backoff is short, exponential, and capped by `max_retry_delay_seconds`.
Cancellation is checked during the backoff delay so Escape/TUI cancellation does
not wait for the entire retry sleep to finish.

## Rendering

Transcript and TUI renderers show retry progress as subtle status output. Final
text mode ignores retry progress and only prints the final assistant response or
final error.

## Boundary

`tau_agent` does not decide whether an HTTP response is retryable. Adapters
classify failures with the `retryable` flag; provider-specific details stay in
the adapter's `data` payload for diagnostics. The harness owns the turn-level
retry budget: how many reattempts happen, what consumers see in between, and
what lands in history.
