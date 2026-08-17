# Delta: Simplified transient provider error retry

This delta supersedes
`dev-notes/design/2026-08-16-transient-error-retry-delta.md`. It compares the
simplified feature spec
(`dev-notes/design/2026-08-16-transient-error-retry-simplified-spec.md`)
against the currently implemented behavior (the superseded spec, shipped in
commits `362fb71`..`e21a5db`). `docs/specs/` does not exist, so the
superseded spec serves as the living contract.

## Domain: Transient provider error retry

### ADDED Requirements

#### Requirement: Fixed retry budget
The turn-level retry budget SHALL be a fixed default of two retries, for a
maximum of three total attempts per turn, and SHALL NOT be configurable
through provider configuration, the setup interface, or any other
user-facing surface. No setting exists to disable it.

##### Scenario: default budget
- GIVEN a turn whose provider call fails retryably
- WHEN the turn runs
- THEN at most two reattempts occur.

##### Scenario: no configuration surface
- GIVEN provider configuration or the setup interface
- WHEN it is inspected
- THEN neither exposes any turn-level retry setting.

### MODIFIED Requirements

#### Requirement: Transient-failure classification
The classification previously resided with provider adapters, which marked
terminal errors retryable per adapter-local rules (transport errors,
in-stream errors matching per-adapter transient markers, transient statuses
after adapter budget exhaustion) and not retryable otherwise (non-transient
statuses, terminal rate-limit bodies, overflow bodies, cancelled streams;
non-retryable governing). The system SHALL instead classify every main-turn
failure with a single rule driven by the failure's diagnostic data and
message text: retryable unless the run is cancelled, the body or message
matches one of the terminal rate-limit markers (`gousagelimiterror`,
`freeusagelimiterror`, `monthly usage limit reached`, `available balance`,
`insufficient_quota`, `out of budget`, `quota exceeded`, `billing`) or one of
the context-overflow markers (`context length`, `context window`, `context
limit`, `maximum context`, `max context`, `input is too long`, `input
length`, `prompt is too long`, `too many tokens`, `token limit`, `exceeds the
limit`, `exceeded the limit`) via case-insensitive substring matching, the
failure carries a non-transient HTTP status (retryable only for 408, 409,
425, 429, or 5xx), or — for failures carrying no HTTP status (transport-level
or in-stream errors) — the classification defaults to retryable. Non-retryable
conditions continue to govern when both apply.

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
Mechanics are unchanged (discard partial output, suppress the failed
attempt's terminal error, emit a retry-start event with next attempt number,
maximum attempts, cancellable backoff delay, and failure reason, wait
cancellably, reissue with identical context; repeat until success, a
non-retryable failure, budget exhaustion, or cancellation; attempts count the
original request as number one; backoff grows exponentially from a fixed base
to a fixed cap). The failure reason SHALL change: it SHALL equal `HTTP
<status>` when the failure carries an HTTP status, otherwise the failure's
message text.

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

##### Scenario: reason names transport errors
- GIVEN a turn's provider call fails retryably with a transport error message
- WHEN the retry-start event is emitted
- THEN its reason equals the failure's message text.

##### Scenario: mixed outcomes end the retry sequence
- GIVEN the first attempt of a turn fails retryably with HTTP 503 and the
  reattempt fails with a non-retryable HTTP 401
- WHEN the turn runs
- THEN exactly one retry-start event is emitted, the run ends with the
  terminal error projection, and the diagnostic log records exactly one retry
  entry and exactly one terminal `assistant_error` entry.

#### Requirement: Retry diagnostics
Diagnostic `assistant_retry` entries remain one per reattempt, non-secret,
carrying the attempt number, maximum attempts, failure reason, provider,
model, and run/session identifiers, and retried attempts SHALL NOT produce
terminal `assistant_error` entries. The entries SHALL carry the failure
message text instead of a provider error-type field (the error-type data the
adapters previously attached is removed with the classification revert).

##### Scenario: failure then success logged once
- GIVEN a turn fails retryably once and then succeeds
- WHEN the run completes
- THEN the diagnostic log contains one retry entry for the attempt, carrying
  the failure's message text, and no terminal error entry for that turn.

##### Scenario: exhausted retries fully logged
- GIVEN a turn fails retryably twice and then terminally
- WHEN the run completes
- THEN the diagnostic log contains two retry entries and exactly one terminal
  error entry.

#### Requirement: Complete-response tail failure
Removed. The system previously treated a transport error after the stream
delivered its terminal marker as success with a diagnostic note. The system
SHALL instead classify such a failure like any other transport failure and
retry it within the budget: the effectively complete response is discarded, a
retry-start event is emitted, and the reattempt's response replaces it.

##### Scenario: transport error after complete response
- GIVEN a provider stream delivered a complete response and the connection
  then fails while reading the remaining response bytes
- WHEN the turn loop receives the terminal error
- THEN the failure is classified as retryable, a retry-start event is emitted,
  and the reattempt's response replaces the discarded response.

#### Requirement: Cancellation during retry
Unchanged: cancellation during backoff ends the run with the exhausted-budget
terminal semantics (message appended, exactly one terminal error entry, no
further retry-start events, notice replaced by the terminal projection,
partial content discarded); cancellation during a reattempt appends the
in-flight message as today's cancelled-stream behavior does.

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

### REMOVED Requirements

#### Requirement: Retry budget configuration
Removed: the per-provider configurable budget (`turn_retry_max`, default two,
zero disables) and its validation are replaced by the fixed default budget
(see "Fixed retry budget"). The provider configuration schema, merge helpers,
settings persistence, and the `tau setup` interface lose the field entirely.

#### Requirement: Setup interface persists the budget
Removed with the configuration surface: the setup interface exposes and
persists no turn-level retry setting.

#### Requirement: Tail-read success handling
Non-behavioral removal: the per-adapter tail-read machinery (treating a
transport error after the terminal marker as success with a
`response_tail_read` diagnostic) is removed along with the adapter revert;
the "Complete-response tail failure" modified requirement above defines the
replacement behavior.

#### Requirement: Adapter-local classification entities
Non-behavioral removal: the `retryable` field on provider and assistant error
events, the shared `tau_ai` classifier module, the classifier imports and
`_should_retry` signature changes in all five adapters, and the shared marker
helpers in `tau_coding` are removed; classification lives in the agent layer
as described in "Transient-failure classification".
