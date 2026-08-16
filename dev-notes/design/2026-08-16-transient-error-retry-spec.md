# Spec: Automatic retry of transient provider errors

## Domain: Transient provider error retry

No living spec exists for this domain. All requirements below are ADDED.

### ADDED Requirements

#### Requirement: Transient-failure classification

When a main-turn provider call fails and the provider adapter emits a terminal
error, the adapter SHALL mark the failure as retryable when:

- the failure is a transport-level failure (connection dropped, protocol
  error, read failure, connect failure, or timeout), including mid-stream
  after partial content was emitted,
- the failure is an in-stream SSE error matching the adapter's existing
  transient markers (for example overloaded, service unavailable, rate
  limit, internal/server error, timeout), including after partial content,
  or
- the failure is an HTTP status the adapter considers transient (408, 409,
  425, 429, or 5xx) whose adapter-internal retry budget was already
  exhausted.

The adapter SHALL mark the failure as not retryable when it is a non-transient
HTTP status, a terminal rate limit (usage or balance markers such as
insufficient quota, usage limit reached, out of budget), a context-overflow
error (message matching the shared overflow markers), or a user-cancelled
(aborted) stream.

##### Scenario: mid-stream transport drop after partial text

- GIVEN a provider call has streamed partial assistant text and the connection
  then drops mid-body with a transport protocol error
- WHEN the adapter emits the terminal error for the call
- THEN the error is marked retryable.

##### Scenario: exhausted transient status

- GIVEN a provider call receives HTTP 503 on every attempt and the adapter's
  own retry budget is exhausted
- WHEN the adapter emits the terminal error for the call
- THEN the error is marked retryable.

##### Scenario: in-stream overload error after content

- GIVEN an Anthropic- or Codex-shaped stream has emitted content and then
  carries an in-stream SSE error matching the transient markers
- WHEN the adapter emits the terminal error for the call
- THEN the error is marked retryable.

##### Scenario: non-transient status

- GIVEN a provider call fails with HTTP 400 or 401
- WHEN the adapter emits the terminal error for the call
- THEN the error is not marked retryable.

##### Scenario: terminal rate limit

- GIVEN a provider rejects a call with an insufficient-quota or usage-limit
  message
- WHEN the adapter emits the terminal error for the call
- THEN the error is not marked retryable.

##### Scenario: context-overflow error

- GIVEN a provider call fails with a context-overflow message matching the
  shared overflow markers
- WHEN the adapter emits the terminal error for the call
- THEN the error is not marked retryable.

##### Scenario: cancelled stream

- GIVEN the user cancels the run while a provider call is streaming
- WHEN the call ends without a normal terminal event
- THEN the resulting failure is not marked retryable.

#### Requirement: Complete-response tail failure

When a transport error occurs after the provider stream already delivered its
terminal marker (finish reason, done marker, or stream-stop event), the system
SHALL treat the response as complete: the assistant message SHALL finish with
its normal stop reason instead of failing the run, SHALL carry a diagnostic
note about the trailing read failure, and SHALL NOT trigger a retry.

##### Scenario: connection dies after the final chunk

- GIVEN a provider stream delivered its terminal marker and the connection
  fails while reading the remaining response bytes
- WHEN the stream ends
- THEN the run continues with the completed assistant message and no error or
  retry occurs.

#### Requirement: Bounded turn-level retry

When a main-turn provider call fails with a retryable classification, the
number of completed attempts is below the configured retry budget, and the run
is not cancelled, the harness SHALL discard the failed attempt's partial output
without appending it to history or storage, SHALL emit a retry-start event
carrying the next attempt number, the maximum attempts, the backoff delay, and
the failure reason, SHALL wait the backoff delay in a cancellable manner, and
SHALL reissue the provider call with the same request context. Reattempts
SHALL repeat until a call completes normally, the failure is no longer
retryable, the budget is exhausted, or the run is cancelled.

##### Scenario: failure followed by success

- GIVEN the first attempt of a turn fails retryably after partial output and
  the second attempt completes normally
- WHEN the turn runs
- THEN exactly one retry-start event precedes the fresh stream; the run ends
  normally; and harness history and session storage contain exactly one
  assistant message for the turn, with no trace of the failed attempt.

##### Scenario: budget exhausted

- GIVEN the configured budget is two retries and three consecutive attempts
  fail retryably
- WHEN the turn runs
- THEN only two retry-start events are emitted; after the final failure the
  run ends with a terminal error exactly as it does today, with the failed
  message present in history.

##### Scenario: retry disabled

- GIVEN the configured retry budget is zero
- WHEN a turn's provider call fails retryably
- THEN no retry-start event is emitted and the run ends with today's terminal
  error behavior.

#### Requirement: Retry budget configuration

The retry budget SHALL be configurable per provider, SHALL default to two
retries, SHALL accept zero to disable turn-level retries, and SHALL reject
negative values.

##### Scenario: negative budget rejected

- GIVEN a provider configuration with a negative retry budget
- WHEN the configuration is validated
- THEN the configuration is rejected.

##### Scenario: per-provider values

- GIVEN two providers with budgets of zero and three respectively
- WHEN both fail retryably
- THEN the first provider's run ends immediately and the second provider's
  run retries up to three times.

#### Requirement: Cancellation during retry

When the run is cancelled during a retry backoff delay or during a reattempt,
the harness SHALL NOT reissue further attempts, SHALL append the failed message
and end the run as it does today, and SHALL emit no retry-start events after
the cancellation.

##### Scenario: cancel during backoff

- GIVEN a retryable failure is waiting out its backoff delay
- WHEN the user cancels the run
- THEN no further attempts occur and the run ends with today's terminal error
  behavior.

#### Requirement: Transcript rollback on retry

When a retry-start event arrives while the TUI is mounted, the TUI SHALL remove
the failed attempt's partial assistant content (text and thinking blocks) from
the mounted transcript and from the canonical display state, SHALL show a
transient notice with the attempt number, maximum attempts, and failure
reason, and SHALL render the reattempt's streamed content in place of the
notice. After the turn, the mounted transcript SHALL contain no trace of the
failed attempt's partial content.

##### Scenario: visible rollback

- GIVEN partial text A has streamed into the transcript and a retry-start
  event then arrives
- WHEN the reattempt streams final text B
- THEN the mounted transcript shows B and a notice, and does not show A.

#### Requirement: Terminal failure projection unchanged

When every retry attempt fails, the TUI SHALL project the final failure exactly
as it does today: the last attempt's partial content followed by an error
block carrying the failure message, the diagnostic log path, and the manual
retry hint.

##### Scenario: exhausted retries with partial content

- GIVEN a turn fails retryably, is reattempted, and every attempt fails
- WHEN the run ends
- THEN the transcript shows the last attempt's partial content and the
  terminal error block with the log path and retry hint.

#### Requirement: Print-mode retry notice

In print-mode runs, the renderer SHALL print a retry notice line (attempt
number, maximum attempts, and failure reason) when a retry-start event
arrives, SHALL continue rendering the reattempt normally, and SHALL keep
today's final output behavior.

##### Scenario: print mode with retry

- GIVEN a print-mode run whose first attempt fails retryably and whose second
  attempt succeeds
- WHEN the run completes
- THEN the output contains a retry notice line and the final assistant
  response, and no error block.

#### Requirement: Retry diagnostics

The diagnostic log SHALL record one non-secret entry per retried attempt,
containing the attempt number, the maximum attempts, the failure reason, the
error type, the provider, the model, and the run/session identifiers. Retried
attempts SHALL NOT produce terminal `assistant_error` entries; a terminal
failure after retries SHALL still produce exactly one `assistant_error` entry
as today.

##### Scenario: failure then success logged once

- GIVEN a turn fails retryably once and then succeeds
- WHEN the run completes
- THEN the diagnostic log contains one retry entry for the attempt and no
  terminal error entry for that turn.

##### Scenario: exhausted retries fully logged

- GIVEN a turn fails retryably twice and then terminally
- WHEN the run completes
- THEN the diagnostic log contains two retry entries and exactly one terminal
  error entry.

#### Requirement: Overflow handling unchanged

Context-overflow failures SHALL NOT be retried by the turn-level retry
mechanism, and the existing session overflow handling (compaction followed by
an automatic retry after compaction) SHALL trigger exactly as it does today.

##### Scenario: overflow bypasses turn retry

- GIVEN a provider call fails with a context-overflow error
- WHEN the run continues
- THEN no turn-level retry-start event is emitted and the session emits its
  compaction events as today.

#### Requirement: Retry scope limited to main turns

Turn-level retry SHALL apply only to main assistant-turn provider calls inside
the agent loop. One-shot provider calls (for example session auto-naming and
compaction summarization) SHALL NOT be retried by this mechanism.

##### Scenario: one-shot call not retried

- GIVEN a session auto-naming provider call fails with a transient error
- WHEN the call completes
- THEN no retry occurs and the failure surfaces exactly as it does today.
