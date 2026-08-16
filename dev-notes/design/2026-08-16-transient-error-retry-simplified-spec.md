# Spec: Automatic retry of transient provider errors (simplified design)

This spec supersedes
`dev-notes/design/2026-08-16-transient-error-retry-spec.md`. The system's
behavior for transient provider errors is defined by this document. Behavior
deltas to the superseded spec:

- classification is defined once and applies to every main-turn failure,
  determined from the terminal failure's diagnostic data and message text,
- the retry budget is a fixed default of two retries, not configurable,
- transport errors that occur after a response was effectively complete are
  retried like any other retryable failure,
- the failure reason carried by retry notices is the HTTP status when the
  failure carries one, otherwise the failure's message text (the superseded
  design reported the error type instead),
- every other user-facing behavior of the superseded spec is unchanged.

## Domain: Transient provider error retry

### ADDED Requirements

#### Requirement: Transient-failure classification
When a main-turn provider call fails and the failure surfaces as a terminal
error, the system SHALL classify the failure as retryable when the run is not
cancelled, the failure's diagnostic body and message text carry no terminal
markers, and the failure either carries no HTTP status or carries a transient
HTTP status (408, 409, 425, 429, or any 5xx).

The system SHALL classify the failure as not retryable when the run is
cancelled, the failure carries a non-transient HTTP status, the failure's
diagnostic body or message text matches any of the terminal rate-limit
markers `gousagelimiterror`, `freeusagelimiterror`, `monthly usage limit
reached`, `available balance`, `insufficient_quota`, `out of budget`, `quota
exceeded`, or `billing`, or the failure's diagnostic body or message text
matches any of the context-overflow markers `context length`, `context
window`, `context limit`, `maximum context`, `max context`, `input is too
long`, `input length`, `prompt is too long`, `too many tokens`, `token
limit`, `exceeds the limit`, or `exceeded the limit`. Marker matching SHALL
be case-insensitive substring matching against the lowercased diagnostic
body and message text.

When both a retryable condition and a non-retryable condition apply to the
same failure, the non-retryable condition SHALL govern. In particular, a
transient HTTP status or an in-stream error whose body or message carries a
terminal rate-limit or context-overflow marker SHALL be classified as not
retryable.

##### Scenario: mid-stream transport drop after partial text
- GIVEN a provider call has streamed partial assistant text and the connection
  then drops mid-body with a transport protocol error
- WHEN the turn loop receives the terminal error
- THEN the failure is classified as retryable.

##### Scenario: transient status is retryable
- GIVEN a provider call fails with HTTP 503
- WHEN the turn loop receives the terminal error
- THEN the failure is classified as retryable.

##### Scenario: transport error before content
- GIVEN a provider call fails with a connection error before any content
- WHEN the turn loop receives the terminal error
- THEN the failure is classified as retryable.

##### Scenario: in-stream error without terminal markers
- GIVEN a streaming response carries an in-stream error event whose body and
  message contain no terminal markers
- WHEN the turn loop receives the terminal error
- THEN the failure is classified as retryable.

##### Scenario: non-transient status
- GIVEN a provider call fails with HTTP 400 or 401
- WHEN the turn loop receives the terminal error
- THEN the failure is classified as not retryable.

##### Scenario: terminal rate limit
- GIVEN a provider rejects a call with an insufficient-quota or usage-limit
  message
- WHEN the turn loop receives the terminal error
- THEN the failure is classified as not retryable.

##### Scenario: terminal marker overrides transient condition
- GIVEN a provider call fails with HTTP 429 whose body contains an
  insufficient-quota message
- WHEN the turn loop receives the terminal error
- THEN the failure is classified as not retryable despite the transient
  status.

##### Scenario: context-overflow error
- GIVEN a provider call fails with a context-overflow message matching the
  overflow markers
- WHEN the turn loop receives the terminal error
- THEN the failure is classified as not retryable.

##### Scenario: in-stream terminal markers
- GIVEN a streaming response carries an in-stream error event whose body or
  message contains a terminal rate-limit or context-overflow marker
- WHEN the turn loop receives the terminal error
- THEN the failure is classified as not retryable.

##### Scenario: cancelled stream
- GIVEN the user cancels the run while a provider call is streaming
- WHEN the call ends without a normal terminal event
- THEN the resulting failure is classified as not retryable.

#### Requirement: Bounded turn-level retry
When a main-turn provider call fails with a retryable classification, the
number of reattempts already performed for the turn is below the retry
budget, and the run is not cancelled, the harness SHALL discard the failed
attempt's partial output without appending it to history or storage, SHALL
NOT surface the failed attempt as an error to consumers, SHALL emit a
retry-start event carrying the next attempt number, the maximum attempts, the
backoff delay, and a failure reason, SHALL wait the backoff delay in a
cancellable manner, and SHALL reissue the provider call with the same request
context. Reattempts SHALL repeat until a call completes normally, the failure
is no longer retryable, the budget is exhausted, or the run is cancelled.

The failure reason SHALL identify the failure: the HTTP status when the
failure carries one, otherwise the failure's message text.

The retry-start event and its notice SHALL number attempts counting the
original request as the first attempt.

The backoff delay SHALL grow with each successive attempt until it reaches a
fixed maximum cap and SHALL never exceed that cap; the first delay SHALL be a
fixed base value, and the base value SHALL be smaller than the maximum cap.
The base value and the cap are fixed internal constants, not user-visible
configuration.

##### Scenario: failure followed by success
- GIVEN the first attempt of a turn fails retryably after partial output and
  the second attempt completes normally
- WHEN the turn runs
- THEN exactly one retry-start event precedes the fresh stream; the run ends
  normally; and harness history and session storage contain exactly one
  assistant message for the turn, with no trace of the failed attempt.

##### Scenario: budget exhausted
- GIVEN a turn fails retryably three consecutive times
- WHEN the turn runs
- THEN exactly two retry-start events are emitted; after the final failure the
  run ends with a terminal error exactly as it does today, with the failed
  message present in history.

##### Scenario: backoff grows with attempts
- GIVEN two consecutive retryable failures of the same turn
- WHEN the retry-start events are emitted
- THEN the second event's delay is strictly greater than the first event's
  delay and neither delay exceeds the fixed maximum cap.

##### Scenario: reason names the failure
- GIVEN a turn's provider call fails retryably with HTTP 503
- WHEN the retry-start event is emitted
- THEN its reason equals `HTTP 503`.

##### Scenario: mixed outcomes end the retry sequence
- GIVEN the first attempt of a turn fails retryably with HTTP 503 and the
  reattempt fails with a non-retryable HTTP 401
- WHEN the turn runs
- THEN exactly one retry-start event is emitted, the run ends with the
  terminal error projection, and the diagnostic log records exactly one retry
  entry and exactly one terminal `assistant_error` entry.

##### Scenario: reason names transport errors
- GIVEN a turn's provider call fails retryably with a transport error message
- WHEN the retry-start event is emitted
- THEN its reason equals the failure's message text.

#### Requirement: Fixed retry budget
The turn-level retry budget SHALL be a fixed default of two retries, for a
maximum of three total attempts per turn. The budget SHALL NOT be
configurable through provider configuration, the setup interface, or any
other user-facing surface, and there SHALL be no setting to disable it.

##### Scenario: default budget
- GIVEN a turn whose provider call fails retryably
- WHEN the turn runs
- THEN at most two reattempts occur.

##### Scenario: no configuration surface
- GIVEN provider configuration or the setup interface
- WHEN it is inspected
- THEN neither exposes any turn-level retry setting.

#### Requirement: Cancellation during retry
When the run is cancelled during a retry backoff delay, the harness SHALL NOT
reissue further attempts, SHALL end the run with the same message-append and
error projection semantics as when the retry budget is exhausted, SHALL write
exactly one terminal error entry in the diagnostics log for the failure, and
SHALL emit no retry-start events after the cancellation. The mounted retry
notice SHALL be replaced by that terminal error projection. The failed
attempt's partial content SHALL remain discarded: the appended failed message
SHALL NOT retain it in history or storage.

When the run is cancelled during a reattempt, the harness SHALL NOT reissue
further attempts, SHALL append the in-flight attempt's message and end the run
exactly as it does today when a stream is cancelled mid-flight, and SHALL emit
no retry-start events after the cancellation. The mounted retry notice and the
reattempt's partial content SHALL be finalized into the same terminal
projection today's cancelled-stream behavior produces.

##### Scenario: cancel during backoff
- GIVEN a retryable failure is waiting out its backoff delay
- WHEN the user cancels the run
- THEN no further attempts occur, the run ends with the exhausted-budget
  terminal semantics, the transcript shows the terminal error projection with
  no lingering retry notice, and the failed message in history and storage
  retains none of the discarded partial content.

##### Scenario: cancel during a reattempt
- GIVEN a reattempt is streaming partial content with the retry notice
  mounted
- WHEN the user cancels the run
- THEN no further attempts occur, the in-flight message is appended as today's
  cancelled-stream behavior does, and the transcript shows the terminal
  projection with no lingering retry notice.

#### Requirement: Transcript rollback on retry
When a retry-start event arrives while the TUI is mounted, the TUI SHALL
remove the failed attempt's partial assistant content (text and thinking
blocks) and its error projection from the mounted transcript and from the
canonical display state, SHALL show a transient notice with the attempt
number, maximum attempts, and failure reason, and SHALL render the reattempt's
streamed content in place of the notice. After the turn, the mounted
transcript SHALL contain no trace of the failed attempt's partial content or
error projection.

##### Scenario: visible rollback
- GIVEN partial text A has streamed into the transcript and a retry-start
  event then arrives
- WHEN the reattempt streams final text B
- THEN the mounted transcript shows B and a notice, and shows neither A nor
  any error block from the failed attempt.

#### Requirement: Terminal failure projection unchanged
When every retry attempt fails, the TUI SHALL project the final failure
exactly as it does today: the last attempt's partial content followed by an
error block carrying the failure message, the diagnostic log path, and the
manual retry hint, replacing the retry notice.

##### Scenario: exhausted retries with partial content
- GIVEN a turn fails retryably, is reattempted, and every attempt fails
- WHEN the run ends
- THEN the transcript shows the last attempt's partial content and exactly one
  terminal error block with the log path and retry hint, and contains no
  error block, notice, or partial content from earlier attempts.

#### Requirement: Print-mode retry notice
In print-mode runs, the renderer SHALL print a retry notice line (attempt
number, maximum attempts, and failure reason) when a retry-start event
arrives, SHALL continue rendering the reattempt normally, and SHALL keep
today's final output behavior. Already-printed partial text from a failed
attempt SHALL be allowed to remain in the output, as a terminal cannot unprint
it.

##### Scenario: print mode with retry
- GIVEN a print-mode run whose first attempt fails retryably and whose second
  attempt succeeds
- WHEN the run completes
- THEN the output contains a retry notice line and the final assistant
  response, and no error block; the failed attempt's partial text may remain
  in the output.

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

##### Scenario: compaction call not retried
- GIVEN a compaction summarization provider call fails with a transient error
- WHEN the call completes
- THEN no retry occurs and the failure surfaces exactly as it does today.

### REMOVED Requirements

#### Requirement: Complete-response tail failure
Removed. The previous design treated a transport error after the stream had
delivered its terminal marker as success with a diagnostic. In the simplified
design, such an error is indistinguishable at the turn loop from any other
transport failure and is retried within the budget: the effectively complete
response is discarded, a retry-start event is emitted, and the response is
regenerated. The scenario below documents the replacement behavior:

##### Scenario: transport error after complete response
- GIVEN a provider stream delivered a complete response and the connection
  then fails while reading the remaining response bytes
- WHEN the turn loop receives the terminal error
- THEN the failure is classified as retryable, a retry-start event is emitted,
  and the reattempt's response replaces the discarded response.

#### Requirement: Retry budget configuration
Removed. The previous design exposed a per-provider configurable budget
(`turn_retry_max`, default 2, zero disables) through provider configuration
and the setup interface. The simplified design uses a fixed default budget of
two retries with no configuration surface (see "Fixed retry budget"). The
setup interface accordingly exposes no retry-budget setting.

#### Requirement: Setup interface persists the budget
Removed with the configuration surface above; nothing is persisted and the
setup interface exposes no turn-level retry setting.
