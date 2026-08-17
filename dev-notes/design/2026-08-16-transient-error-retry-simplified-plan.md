# Simplified Transient-Error Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the shipped per-adapter retry-classification design with a central turn-loop classifier and a fixed two-retry budget, reverting ~1,500 lines of provider/config surface while keeping the shipped TUI rollback, notice, and diagnostics behavior.

**Architecture:** The retry decision moves entirely into the agent turn loop (`tau_agent`). The loop classifies each terminal failure from the `provider_error` diagnostics and message text that upstream adapters already attach (`status_code`, `body`) — no adapter cooperation exists anymore. The budget is the fixed `DEFAULT_TURN_RETRIES = 2`. All `tau_ai` adapter changes, the shared classifier module, the `retryable` event fields, and the `turn_retry_max` config plumbing revert to the pre-feature fork state (`bc2d5a1`), which already includes the upstream merge.

**Tech Stack:** Python 3.12+, httpx-based provider adapters, pytest/anyio, Textual TUI. All commands run through `uv` (`uv run pytest`, `uv run ruff`, `uv run mypy`).

**Standards:** Apply the shared code standards in every task: DRY, low cyclomatic complexity, type safety, no unnecessary abstractions or fallbacks, no hacks or workarounds, informative docstrings, documentation of current state only. Tests carry docstrings stating what they prove and why.

**Feature spec:** `dev-notes/design/2026-08-16-transient-error-retry-simplified-spec.md`

**Delta spec:** `dev-notes/design/2026-08-16-transient-error-retry-simplified-delta.md`

---

## Context for the implementer

- Repo: `/workspace`, branch `main`. The feature was implemented in commits `362fb71`..`e21a5db` (plus `d813ef1` formatting). The pre-feature fork state (including the upstream merge) is commit `bc2d5a1` — the parent of `362fb71`. Restoring files from `bc2d5a1` via `git restore --source=bc2d5a1 -- <path>` is safe: it only undoes the feature's own changes, keeping pre-existing fork features.
- Commits that must be partially kept: `89de0f6` (loop retry + `TurnRetryStartEvent` + harness budget field — kept, modified), `67c0ed4` (session diagnostics — re-applied in T5), `120f6d0` (TUI rollback — kept as-is), `b0c17bc` + `ed4a22b` (print-mode notice — kept as-is; their files were never restored), `e21a5db` (docs — rewritten in T6).
- `tests/test_agent_loop.py` and `tests/test_agent_harness.py` currently import `retryable_error` from `tests/pi_event_helpers.py`; that helper and the `retryable` kwarg on `assistant_error` are reworked in T3 (not reverted earlier — `AssistantErrorEvent.retryable` keeps existing until T3 so each task ends green).
- Every changed/restored file must be `ruff format`-ed before its commit (restored files may carry pre-existing formatting drift from the upstream merge; `d813ef1` fixed it once and the restoration undoes that).

---

## File structure

| File | Role after the change |
| --- | --- |
| `src/tau_agent/retry.py` | Backoff timing **and** the central classification helpers (markers, transient status, retryable decision, reason) |
| `src/tau_agent/loop.py` | Turn loop: retries based on the central classifier; emits `TurnRetryStartEvent` with the new reason |
| `src/tau_agent/provider_events.py` | `AssistantErrorEvent` loses the `retryable` field |
| `src/tau_ai/*` (8 files) | Restored to `bc2d5a1`, including `stream.py`, `_provider_events.py`, `retry.py`; `classify.py` deleted |
| `src/tau_coding/session.py` | Restored to `bc2d5a1` (T1), then re-gains the three `TurnRetryStartEvent` diagnostics blocks (T5) |
| `src/tau_coding/provider_config.py`, `cli.py` | Restored to `bc2d5a1`; `rendering/transcript.py` + `tests/test_rendering.py` keep the print-mode notice (T4) |
| `src/tau_coding/diagnostics.py` | Kept untouched: `log_turn_retry` stays |
| `src/tau_coding/tui/*` | Kept untouched (T10 rollback + notices) |
| `tests/pi_event_helpers.py` | Reworked (T3): `assistant_error(message, data=None, *, status_code=None, body="")`, `transport_error(message, *, partial="")`; no `retryable` |
| `tests/test_agent_loop.py`, `test_agent_harness.py`, `test_agent_retry.py` | Updated/reworked for the central classifier (T2, T3) |
| `tests/test_tau_ai.py`, `test_coding_session.py`, `test_cli.py`, `test_provider_config.py` | Restored to `bc2d5a1`; session tests re-gain the diagnostics tests (T5) |
| Docs: `dev-notes/2026-08-16-transient-error-retry.md`, `dev-notes/provider-error-recovery.md`, `dev-notes/architecture/provider-retries.md`, 4 website pages | Rewritten/updated (T6) |

---

### Task 1: Restore `tau_ai` and `tau_coding/session.py` to the pre-feature state

**Files:**
- Restore: `src/tau_ai/_provider_events.py`, `src/tau_ai/anthropic.py`, `src/tau_ai/google.py`, `src/tau_ai/mistral.py`, `src/tau_ai/openai_codex.py`, `src/tau_ai/openai_compatible.py`, `src/tau_ai/retry.py`, `src/tau_ai/stream.py`, `src/tau_coding/session.py`, `tests/test_tau_ai.py`, `tests/test_coding_session.py`
- Delete: `src/tau_ai/classify.py`

**Delta requirement:** REMOVED "Adapter-local classification entities"; REMOVED "Retry budget configuration" (the session-side wiring); MODIFIED "Transient-failure classification" (classification no longer originates in adapters).

- [ ] **Step 1: Restore the files**

```bash
cd /workspace
for f in src/tau_ai/_provider_events.py src/tau_ai/anthropic.py src/tau_ai/google.py \
         src/tau_ai/mistral.py src/tau_ai/openai_codex.py src/tau_ai/openai_compatible.py \
         src/tau_ai/retry.py src/tau_ai/stream.py \
         src/tau_coding/session.py tests/test_tau_ai.py tests/test_coding_session.py; do
  git restore --source=bc2d5a1 -- "$f"
done
git rm -q src/tau_ai/classify.py
```

- [ ] **Step 2: Verify no stragglers remain in restored code**

```bash
cd /workspace
grep -rn "turn_retry\|classify" src/tau_ai/ | grep -v __pycache__ || true
grep -n "classify" src/tau_coding/session.py || true
grep -rn "turn_retry_max" src/tau_coding/session.py || true
```

Expected: empty output. If anything prints, the restore failed; redo Step 1.

Note: `src/tau_ai/` at `bc2d5a1` legitimately contains pre-existing `_retryable_*` identifiers (`_retryable_anthropic_stream_error` in `anthropic.py`, `_retryable_stream_error_event` / `_is_retryable_status` in `openai_codex.py`) — these are upstream's own provider-internal retry classification and must NOT be touched; the pattern above deliberately avoids them. `tests/pi_event_helpers.py` and `src/tau_agent/*` intentionally still carry the shipped retryable state at this point (removed in T3); ignore them in this check.

- [ ] **Step 3: Format the restored files and run their tests**

```bash
cd /workspace
uv run ruff format src/tau_ai/ src/tau_coding/session.py tests/test_tau_ai.py tests/test_coding_session.py
uv run ruff check src/tau_ai/ src/tau_coding/session.py tests/test_tau_ai.py tests/test_coding_session.py
uv run pytest tests/test_tau_ai.py tests/test_coding_session.py tests/test_agent_retry.py
```

Expected: ruff clean; the selected tests pass. `tests/test_coding_session.py` at `bc2d5a1` has no turn-retry tests, so it passes even though the `67c0ed4` diagnostics blocks are re-added later (T5); the `log_turn_retry` method in `src/tau_coding/diagnostics.py` is untouched and simply unused until T5.

- [ ] **Step 4: Commit**

```bash
cd /workspace
git add -A src/tau_ai/ src/tau_coding/session.py tests/test_tau_ai.py tests/test_coding_session.py
git commit -m "revert(provider): return adapters and session to their pre-retry state"
```

---

### Task 2: Central retry classification helpers (`tau_agent/retry.py`)

**Files:**
- Modify: `src/tau_agent/retry.py`
- Test: `tests/test_agent_retry.py`

**Delta requirement:** MODIFIED "Transient-failure classification"; MODIFIED "Bounded turn-level retry" (reason content, helpers consumed by the loop).

- [ ] **Step 1: Write the failing tests** — extend `tests/test_agent_retry.py`:

First, merge these imports into the file header (do NOT append a second import block — ruff E402 is selected in the lint rules):

```python
from tau_agent.harness import SimpleCancellationToken
from tau_agent.messages import AssistantMessage, AssistantMessageDiagnostic, TextContent
from tau_agent.retry import (
    failure_is_retryable,
    failure_reason,
    is_transient_status,
    turn_retry_delay_seconds,
    wait_for_retry,
)
from tau_agent.types import JSONValue
```

(`asyncio` and `pytest` stay where they are; `SimpleCancellationToken`, `turn_retry_delay_seconds`, and `wait_for_retry` are already imported by the header — drop the duplicates.)

Then append the tests below at the end of the file:

```python
def _error(
    message: str = "peer closed connection",
    *,
    details: dict[str, JSONValue] | None = None,
) -> AssistantMessage:
    """Build a terminal assistant failure with optional provider_error details."""
    diagnostics = (
        [AssistantMessageDiagnostic(type="provider_error", details=details)]
        if details
        else None
    )
    return AssistantMessage(
        stop_reason="error",
        error_message=message,
        diagnostics=diagnostics,
    )


def test_failure_is_retryable_for_transport_errors() -> None:
    """Prove failures without status or terminal markers are retried."""
    assert failure_is_retryable(_error(), signal=None) is True
    assert failure_is_retryable(_error(message="connection reset"), signal=None) is True


def test_failure_is_retryable_for_transient_statuses() -> None:
    """Prove transient HTTP statuses are retried."""
    for status_code in (408, 409, 425, 429, 500, 503):
        assert failure_is_retryable(_error(details={"status_code": status_code}), signal=None) is True
    assert is_transient_status(503) is True
    assert is_transient_status(401) is False


def test_failure_is_not_retryable_for_non_transient_status() -> None:
    """Prove non-transient HTTP statuses end the run without retry."""
    assert failure_is_retryable(_error(details={"status_code": 401}), signal=None) is False
    assert failure_is_retryable(_error(details={"status_code": 400}), signal=None) is False


def test_failure_is_not_retryable_for_terminal_rate_limits() -> None:
    """Prove usage-limit markers in body or message suppress retries."""
    assert (
        failure_is_retryable(
            _error(details={"status_code": 429, "body": "Your account has insufficient_quota."}),
            signal=None,
        )
        is False
    )
    assert failure_is_retryable(_error(details={"status_code": 503, "body": "billing"}), signal=None) is False
    assert failure_is_retryable(_error(message="monthly usage limit reached"), signal=None) is False
    assert failure_is_retryable(_error(details={"status_code": 429, "body": "Insufficient_Quota"}), signal=None) is False


def test_failure_is_not_retryable_for_context_overflow() -> None:
    """Prove context-overflow markers suppress retries regardless of status."""
    assert (
        failure_is_retryable(
            _error(details={"status_code": 503, "body": "maximum context length exceeded"}),
            signal=None,
        )
        is False
    )
    assert failure_is_retryable(_error(message="the prompt is too long"), signal=None) is False


def test_failure_is_not_retryable_when_cancelled() -> None:
    """Prove a cancelled run never retries, even without terminal markers."""
    signal = SimpleCancellationToken()
    signal.cancel()

    assert failure_is_retryable(_error(), signal=signal) is False


def test_failure_reason_uses_status_then_message_text() -> None:
    """Prove the retry notice reason names the failure precisely."""
    assert failure_reason(_error(details={"status_code": 503})) == "HTTP 503"
    assert failure_reason(_error()) == "peer closed connection"
    assert failure_reason(_error(message="first line\nsecond line")) == "first line"
    assert failure_reason(AssistantMessage(stop_reason="error")) == "provider error"


def test_failure_is_not_retryable_when_marker_is_in_content_text() -> None:
    """Prove terminal markers are matched against rendered content text too."""
    error = AssistantMessage(
        stop_reason="error",
        content="The request failed: quota exceeded.",
    )

    assert failure_is_retryable(error, signal=None) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent_retry.py -x`
Expected: collection error — `failure_is_retryable` not defined.

- [ ] **Step 3: Implement the classifiers** — replace the whole content of `src/tau_agent/retry.py` with:

```python
"""Cancellable retry backoff and turn-level failure classification helpers."""

from __future__ import annotations

from asyncio import sleep

from tau_agent.messages import AssistantMessage, TextContent
from tau_agent.provider import CancellationToken
from tau_agent.types import JSONValue

RETRY_POLL_SECONDS = 0.05
RETRY_BASE_DELAY_SECONDS = 0.25
TURN_RETRY_MAX_DELAY_SECONDS = 1.0

TRANSIENT_HTTP_STATUSES = frozenset({408, 409, 425, 429})

TERMINAL_RATE_LIMIT_MARKERS = (
    "gousagelimiterror",
    "freeusagelimiterror",
    "monthly usage limit reached",
    "available balance",
    "insufficient_quota",
    "out of budget",
    "quota exceeded",
    "billing",
)

CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context window",
    "context limit",
    "maximum context",
    "max context",
    "input is too long",
    "input length",
    "prompt is too long",
    "too many tokens",
    "token limit",
    "exceeds the limit",
    "exceeded the limit",
)


def turn_retry_delay_seconds(attempt: int) -> float:
    """Return the harness turn-retry delay: exponential, capped at one second."""
    return float(min(TURN_RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * (2**attempt)))


async def wait_for_retry(
    delay_seconds: float,
    *,
    signal: CancellationToken | None,
) -> bool:
    """Sleep before a retry while allowing cancellation to interrupt backoff."""
    if delay_seconds <= 0:
        return signal is None or not signal.is_cancelled()

    remaining = delay_seconds
    while remaining > 0:
        if signal is not None and signal.is_cancelled():
            return False
        step = min(RETRY_POLL_SECONDS, remaining)
        await sleep(step)
        remaining -= step
    return signal is None or not signal.is_cancelled()


def is_transient_status(status_code: int) -> bool:
    """Return True for HTTP statuses that warrant retrying a provider call."""
    return status_code in TRANSIENT_HTTP_STATUSES or status_code >= 500


def _provider_error_details(message: AssistantMessage) -> list[dict[str, JSONValue]]:
    """Return the provider_error diagnostic details attached to a failure."""
    return [
        diagnostic.details
        for diagnostic in (message.diagnostics or [])
        if diagnostic.type == "provider_error" and diagnostic.details
    ]


def _failure_status_code(message: AssistantMessage) -> int | None:
    """Return the HTTP status of a failure, or None for transport-type errors."""
    for details in _provider_error_details(message):
        status_code = details.get("status_code")
        if isinstance(status_code, int) and not isinstance(status_code, bool):
            return status_code
    return None


def _terminal_marker_hit(message: AssistantMessage) -> bool:
    """Return True when a failure body or message matches a terminal marker."""
    parts = [message.error_message or ""]
    parts.extend(block.text for block in message.content if isinstance(block, TextContent))
    for details in _provider_error_details(message):
        body = details.get("body")
        if isinstance(body, str):
            parts.append(body)
    lowered = "\n".join(parts).lower()
    return any(
        marker in lowered
        for marker in (*TERMINAL_RATE_LIMIT_MARKERS, *CONTEXT_OVERFLOW_MARKERS)
    )


def failure_is_retryable(
    message: AssistantMessage,
    *,
    signal: CancellationToken | None,
) -> bool:
    """Return whether a terminal assistant failure warrants a turn-level retry.

    Cancelled runs, terminal rate limits, context overflow, and non-transient
    HTTP statuses are never retried; transport-level and transient failures
    are.
    """
    if signal is not None and signal.is_cancelled():
        return False
    status_code = _failure_status_code(message)
    if status_code is not None and not is_transient_status(status_code):
        return False
    return not _terminal_marker_hit(message)


def failure_reason(message: AssistantMessage) -> str:
    """Return the retry notice reason for a retryable failure."""
    status_code = _failure_status_code(message)
    if status_code is not None:
        return f"HTTP {status_code}"
    text = (message.error_message or "").strip()
    if text:
        return text.splitlines()[0]
    return "provider error"
```

Note: `retry_delay_seconds` is removed — its only consumer was the `tau_ai` re-export layer reverted in T1; `loop.py` still imports `turn_retry_delay_seconds` and `wait_for_retry`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent_retry.py -v`
Expected: all classifier tests pass; the pre-existing timing tests pass unchanged.

- [ ] **Step 5: Commit**

```bash
cd /workspace
uv run ruff format src/tau_agent/retry.py tests/test_agent_retry.py
uv run ruff check src/tau_agent/retry.py tests/test_agent_retry.py
git add src/tau_agent/retry.py tests/test_agent_retry.py
git commit -m "feat(agent): classify turn-retry eligibility from failure diagnostics"
```

---

### Task 3: Rework the turn loop, remove the `retryable` field

**Files:**
- Modify: `src/tau_agent/loop.py`, `src/tau_agent/provider_events.py`, `tests/pi_event_helpers.py`, `tests/test_agent_loop.py`, `tests/test_agent_harness.py`

**Delta requirement:** MODIFIED "Transient-failure classification"; MODIFIED "Bounded turn-level retry" (reason; central decision); REMOVED "Adapter-local classification entities" (`retryable` fields).

- [ ] **Step 1: Rework the test event helpers** — in `tests/pi_event_helpers.py`:

Replace the imports line with:

```python
from tau_agent.messages import AssistantMessage, AssistantMessageDiagnostic, ThinkingContent, ToolCall
```

and add `from tau_agent.types import JSONValue`.

Replace the `assistant_error` function and delete `retryable_error`, replacing both with:

```python
def assistant_error(
    message: str,
    data: object = None,
    *,
    status_code: int | None = None,
    body: str = "",
) -> AssistantErrorEvent:
    del data
    details: dict[str, JSONValue] = {}
    if status_code is not None:
        details["status_code"] = status_code
    if body:
        details["body"] = body
    diagnostics = (
        [AssistantMessageDiagnostic(type="provider_error", details=details)]
        if details
        else None
    )
    error = AssistantMessage(
        stop_reason="error",
        error_message=message,
        diagnostics=diagnostics,
    )
    return AssistantErrorEvent(reason="error", error=error)


def transport_error(message: str, *, partial: str = "") -> AssistantErrorEvent:
    """A transport-level provider failure carrying no status or terminal markers."""
    error = AssistantMessage(stop_reason="error", error_message=message, content=partial)
    return AssistantErrorEvent(reason="error", error=error)
```

- [ ] **Step 2: Rework the loop fixtures** — in `tests/test_agent_loop.py`:

- In the `pi_event_helpers` import block, delete the `retryable_error,` line and add `transport_error,` after `tool_call_end` (isort order: `assistant_done, assistant_error, assistant_start, text_delta, thinking_delta, tool_call_end, transport_error`).
- Replace every `retryable_error(` occurrence with `transport_error(` — there are exactly 10 call sites across 6 tests: `test_agent_loop_retries_transient_failure_then_succeeds` (1), `test_agent_loop_exhausts_turn_retry_budget` (3), `test_agent_loop_retry_backoff_delays_grow` (3), `test_agent_loop_turn_retry_disabled_with_zero_budget` (1), `test_agent_loop_cancel_during_retry_backoff_discards_partial` (1), and `test_agent_loop_cancel_during_reattempt_ends_run` (1). (The non-retryable error test uses `assistant_error`, not `retryable_error`; it is reworked in the bullet below.)
- In `test_agent_loop_retries_transient_failure_then_succeeds`, add this assertion after the `retries[0].error_message` assertion:

```python
    assert retries[0].reason == "peer closed connection"
```

- Rework `test_agent_loop_does_not_retry_non_retryable_error` (its plain `assistant_error("invalid api key")` would now classify as retryable — the default for no-status failures):

```python
@pytest.mark.anyio
async def test_agent_loop_does_not_retry_non_retryable_error() -> None:
    """Prove only centrally classified transient failures trigger a retry."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]
    provider = FakeProvider([[assistant_error("invalid api key", status_code=401)]])

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=2,
        )
    )

    assert not any(isinstance(event, TurnRetryStartEvent) for event in events)
    assert len(provider.calls) == 1
    assert messages[-1].stop_reason == "error"
```

- [ ] **Step 3: Add the new loop-level classification tests** — append to `tests/test_agent_loop.py`:

```python
@pytest.mark.anyio
async def test_agent_loop_retries_exhausted_transient_status() -> None:
    """Prove a 503 that outlives the adapter is retried at turn level with an HTTP reason."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]
    provider = FakeProvider(
        [
            [assistant_error("boom", status_code=503)],
            [assistant_error("boom", status_code=503)],
            [assistant_error("boom", status_code=503)],
        ]
    )

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=2,
        )
    )

    retries = [event for event in events if isinstance(event, TurnRetryStartEvent)]
    assert len(retries) == 2
    assert all(retry.reason == "HTTP 503" for retry in retries)
    assert messages[-1].stop_reason == "error"


@pytest.mark.anyio
async def test_agent_loop_mixed_outcomes_end_the_retry_sequence() -> None:
    """Prove a non-retryable failure stops retrying and is projected terminally."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]
    provider = FakeProvider(
        [
            [assistant_error("boom", status_code=503)],
            [assistant_error("unauthorized", status_code=401)],
        ]
    )

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=2,
        )
    )

    assert sum(isinstance(event, TurnRetryStartEvent) for event in events) == 1
    assert len(provider.calls) == 2
    assert messages[-1].error_message == "unauthorized"


@pytest.mark.anyio
async def test_agent_loop_does_not_retry_terminal_rate_limit() -> None:
    """Prove a quota 429 is terminal despite its transient status code."""
    provider = FakeProvider(
        [[assistant_error("quota", status_code=429, body="Your plan has insufficient_quota.")]]
    )
    messages: list[AgentMessage] = [UserMessage(content="hello")]

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=2,
        )
    )

    assert not any(isinstance(event, TurnRetryStartEvent) for event in events)
    assert len(provider.calls) == 1
    assert messages[-1].stop_reason == "error"


@pytest.mark.anyio
async def test_agent_loop_does_not_retry_context_overflow() -> None:
    """Prove context-overflow failures bypass turn-level retry entirely."""
    provider = FakeProvider(
        [[assistant_error("overflow", status_code=400, body="maximum context length exceeded")]]
    )
    messages: list[AgentMessage] = [UserMessage(content="hello")]

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=2,
        )
    )

    assert not any(isinstance(event, TurnRetryStartEvent) for event in events)
    assert len(provider.calls) == 1
    assert messages[-1].stop_reason == "error"
```

- [ ] **Step 4: Rework the harness test** — in `tests/test_agent_harness.py`, change `retryable_error,` to `transport_error,` in the import block and `retryable_error("drop")` to `transport_error("drop")` at the single call site.

- [ ] **Step 5: Run the tests to verify they fail (loop not yet reworked)**

Run: `uv run pytest tests/test_agent_loop.py tests/test_agent_harness.py -x`
Expected: failures — `transport_error` imported but the loop still reads `event.retryable`, so all retried scenarios end terminally on the first attempt.

- [ ] **Step 6: Rework the loop** — in `src/tau_agent/loop.py`:

Change the retry import to:

```python
from tau_agent.retry import (
    failure_is_retryable,
    failure_reason,
    turn_retry_delay_seconds,
    wait_for_retry,
)
```

In `_assistant_events`, replace the retry gate inside the `AssistantErrorEvent` branch:

```python
                if (
                    event.retryable
                    and retries_done < max_turn_retries
                    and (signal is None or not signal.is_cancelled())
                ):
```

with:

```python
                if (
                    retries_done < max_turn_retries
                    and failure_is_retryable(event.error, signal=signal)
                ):
```

Replace the notice construction:

```python
        delay = turn_retry_delay_seconds(retries_done)
        reason, error_type = _retry_failure_reason(retry_failure)
        yield TurnRetryStartEvent(
            attempt=retries_done + 2,
            max_attempts=max_turn_retries + 1,
            delay_seconds=delay,
            reason=reason,
            error_message=retry_failure.error_message or "",
            error_type=error_type,
        )
```

with:

```python
        delay = turn_retry_delay_seconds(retries_done)
        yield TurnRetryStartEvent(
            attempt=retries_done + 2,
            max_attempts=max_turn_retries + 1,
            delay_seconds=delay,
            reason=failure_reason(retry_failure),
            error_message=retry_failure.error_message or "",
        )
```

Delete the now-unused `_retry_failure_reason` function at the bottom of `_assistant_events`' file scope.

- [ ] **Step 7: Remove the `retryable` field** — in `src/tau_agent/provider_events.py`, delete the line `    retryable: bool = False` from `AssistantErrorEvent`.

- [ ] **Step 8: Run the tests to verify they pass**

```bash
cd /workspace
uv run ruff format src/tau_agent/loop.py src/tau_agent/provider_events.py \
  tests/pi_event_helpers.py tests/test_agent_loop.py tests/test_agent_harness.py
uv run ruff check src/tau_agent/loop.py src/tau_agent/provider_events.py \
  tests/pi_event_helpers.py tests/test_agent_loop.py tests/test_agent_harness.py
uv run pytest tests/test_agent_loop.py tests/test_agent_harness.py tests/test_agent_retry.py -v
```

Expected: all pass (including I001 import sorting), with the new classification tests and the kept TUI-agnostic rollback assertions (`messages[-1] is recovered`, no error `MessageEndEvent`, attempt/max counts).

- [ ] **Step 9: Verify no feature `retryable` references remain**

```bash
cd /workspace
grep -rn "event\.retryable\|retryable_error\|retryable=\|retryable: bool" src/ tests/ --include=*.py | grep -v __pycache__ || true
```

Expected: empty. NOTE — the word `retryable` itself legitimately remains in the codebase after this task: the new `failure_is_retryable` symbol in `src/tau_agent/retry.py`/`loop.py`/`tests/test_agent_retry.py` and the pre-existing `_retryable_*` identifiers in restored `src/tau_ai/` are all expected and must NOT be flagged; that is why the first pattern targets only the removed feature's call shapes. Any hit in `tau_agent/*` or `tests/pi_event_helpers.py` beyond those shapes means a step above was missed.

Also note for later tasks: `TurnRetryStartEvent.error_type` (in `src/tau_agent/events.py`) is intentionally kept as a field even though the loop no longer sets it — removing it would churn the TUI/print/diagnostics consumers that read it; `log_turn_retry` already omits falsy `error_type` from the log entry.

- [ ] **Step 10: Commit**

```bash
cd /workspace
git add src/tau_agent/loop.py src/tau_agent/provider_events.py tests/pi_event_helpers.py \
  tests/test_agent_loop.py tests/test_agent_harness.py
git commit -m "feat(agent): retry turns on centrally classified failures"
```

---

### Task 4: Revert the config surface; keep the print-mode notice

**Files:**
- Restore: `src/tau_coding/provider_config.py`, `src/tau_coding/cli.py`, `tests/test_provider_config.py`, `tests/test_cli.py`
- Re-apply (keep): `src/tau_coding/rendering/transcript.py`, `tests/test_rendering.py` (the print-mode retry notice from commits `b0c17bc` + `ed4a22b`)

**Delta requirement:** REMOVED "Retry budget configuration"; REMOVED "Setup interface persists the budget"; ADDED "Fixed retry budget" (no configuration surface). The print-mode notice behavior is unchanged (MODIFIED "Bounded turn-level retry" keeps it).

- [ ] **Step 1: Verify the print-mode files are untouched since `bc2d5a1`**

```bash
cd /workspace
git diff bc2d5a1 b0c17bc^ -- src/tau_coding/rendering/transcript.py tests/test_rendering.py
```

Expected: empty output. This proves the kept hunk applies cleanly onto the restored tree.

- [ ] **Step 2: Restore the config files**

```bash
cd /workspace
for f in src/tau_coding/provider_config.py src/tau_coding/cli.py \
         tests/test_provider_config.py tests/test_cli.py; do
  git restore --source=bc2d5a1 -- "$f"
done
```

- [ ] **Step 3: Verify the print-mode notice (kept feature) is present**

The print-mode notice was added by `b0c17bc`/`ed4a22b`, which touched only `src/tau_coding/rendering/transcript.py` and `tests/test_rendering.py` — neither file was restored in Step 2, so both keep the shipped notice code. Verify it survived:

```bash
cd /workspace
grep -n "TurnRetryStartEvent\|retrying" src/tau_coding/rendering/transcript.py
grep -n "retrying 2/3" tests/test_rendering.py
```

Expected: the `TurnRetryStartEvent` handler in `transcript.py` and the notice test in `test_rendering.py` both match (this is the same notification the simplified loop still emits — nothing changes).

- [ ] **Step 4: Verify no config-budget remnants in restored code**

```bash
cd /workspace
grep -rn "turn_retry_max" src/tau_coding/ | grep -v __pycache__ || true
```

Expected: empty. (`log_turn_retry` in `src/tau_coding/diagnostics.py` legitimately matches the narrower `turn_retry` spelling and is kept; `DEFAULT_TURN_RETRIES` remains used by `tau_agent/harness.py` + `tau_agent/__init__.py` + `tau_agent/loop.py` — the fixed budget.)

- [ ] **Step 5: Adapt the restored print-mode failure test, format, and run**

Under the central classifier a plain failure with no status and no terminal markers is now retryable, so the restored `test_run_print_mode_fails_on_non_recoverable_error` (whose single failing stream would be reattempted into an exhausted `FakeProvider`, producing the defensive "Provider produced no assistant message" path and failing its assertions) must use a genuinely non-recoverable shape. In `tests/test_cli.py`, change the error at line ~718:

```python
                assistant_error(message="provider failed"),
```

into:

```python
                assistant_error(message="provider failed", status_code=401),
```

Then:

```bash
cd /workspace
uv run ruff format src/tau_coding/provider_config.py src/tau_coding/cli.py \
  src/tau_coding/rendering/transcript.py tests/test_provider_config.py tests/test_cli.py tests/test_rendering.py
uv run pytest tests/test_cli.py tests/test_rendering.py tests/test_provider_config.py
```

Expected: all pass (the 401 keeps the run single-attempt and terminal, so `ok is False`, `captured.out == ""`, and `"Error: provider failed" in captured.err` hold as before).

- [ ] **Step 6: Commit**

```bash
cd /workspace
git add src/tau_coding/provider_config.py src/tau_coding/cli.py \
  src/tau_coding/rendering/transcript.py tests/test_provider_config.py tests/test_cli.py tests/test_rendering.py
git commit -m "revert(config): drop the per-provider turn-retry budget"
```

---

### Task 5: Re-apply the session diagnostics logging

**Files:**
- Modify: `src/tau_coding/session.py`, `tests/test_coding_session.py`

**Delta requirement:** MODIFIED "Retry diagnostics" (entries carry the failure message text; one `assistant_retry` entry per reattempt, no terminal entry for retried attempts).

- [ ] **Step 1: Insert the three `TurnRetryStartEvent` handling blocks into `session.py`**

Add `TurnRetryStartEvent` to the `tau_agent.events` import (alphabetical, alongside the existing `ToolExecutionEndEvent` import):

```python
from tau_agent.events import AgentEndEvent, AgentEvent, MessageEndEvent, ToolExecutionEndEvent, TurnRetryStartEvent
```

**Block A** — in the main agent-loop handler, insert between the overflow-message block and the `AgentEndEvent` check (find `if is_context_overflow_error(event.message):` … `overflow_message = event.message` followed by `if isinstance(event, AgentEndEvent):`):

```python
                if isinstance(event, TurnRetryStartEvent):
                    self._last_diagnostic_log_path = self._diagnostic_logger.log_turn_retry(
                        context=context,
                        attempt=event.attempt,
                        max_attempts=event.max_attempts,
                        reason=event.reason,
                        error_message=event.error_message,
                        error_type=event.error_type,
                    )
```

**Block B** — in the compaction-continuation loop, insert before `if isinstance(retry_event, AgentEndEvent):`:

```python
                        if isinstance(retry_event, TurnRetryStartEvent):
                            self._last_diagnostic_log_path = (
                                self._diagnostic_logger.log_turn_retry(
                                    context=context,
                                    attempt=retry_event.attempt,
                                    max_attempts=retry_event.max_attempts,
                                    reason=retry_event.reason,
                                    error_message=retry_event.error_message,
                                    error_type=retry_event.error_type,
                                )
                            )
```

**Block C** — in the auto-continuation loop, insert between the `MessageEndEvent` diagnostics block (`phase="agent_loop", message=event.message,`) and `if isinstance(event, AgentEndEvent): yield SessionAgentEndEvent(messages=event.messages, will_retry=False)`:

```python
                if isinstance(event, TurnRetryStartEvent):
                    self._last_diagnostic_log_path = self._diagnostic_logger.log_turn_retry(
                        context=context,
                        attempt=event.attempt,
                        max_attempts=event.max_attempts,
                        reason=event.reason,
                        error_message=event.error_message,
                        error_type=event.error_type,
                    )
```

There are exactly three `yield SessionAgentEndEvent` sites in `session.py`; each block lands immediately before its `AgentEndEvent` check. `src/tau_coding/diagnostics.py` already contains `log_turn_retry` (kept from `67c0ed4`, untouched).

- [ ] **Step 2: Re-add the diagnostics tests** — in `tests/test_coding_session.py`:

Change the `pi_event_helpers` import from:

```python
from pi_event_helpers import assistant_done, assistant_error, assistant_start
```

to:

```python
from pi_event_helpers import (
    assistant_done,
    assistant_error,
    assistant_start,
    text_delta,
    transport_error,
)
```

Append at the end of the file (exact upstream shape from `67c0ed4`, adapted to the simplified design — `transport_error` instead of `retryable_error`, and the reason assertion is the failure message text instead of `"provider error"`):

```python
@pytest.mark.anyio
async def test_prompt_logs_turn_retry_diagnostics(tmp_path: Path) -> None:
    """Prove each reattempt logs one retry entry and no terminal error entry."""
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    tau_paths = TauPaths(home=tmp_path / "tau-home", agents_home=tmp_path / "agents-home")
    recovered = AssistantMessage(content="recovered", model="fake")
    provider = FakeProvider(
        [
            [assistant_start(), text_delta("partial"), transport_error("peer closed connection")],
            [assistant_start(), assistant_done(recovered)],
        ]
    )
    session = await CodingSession.load(
        CodingSessionConfig(
            provider=provider,
            model="fake",
            system="You are Tau.",
            storage=storage,
            cwd=tmp_path,
            provider_name="openai",
            session_id="session-1",
            resource_paths=TauResourcePaths(root=tau_paths.home, paths=tau_paths),
        )
    )

    await _collect_session_events(session.prompt("Hello"))

    log_path = tau_paths.agent_calls_log_path
    entries = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    retry_entries = [entry for entry in entries if entry["kind"] == "assistant_retry"]
    error_entries = [entry for entry in entries if entry["kind"] == "assistant_error"]
    assert len(retry_entries) == 1
    assert retry_entries[0]["retry"]["attempt"] == 2
    assert retry_entries[0]["retry"]["max_attempts"] == 3
    assert retry_entries[0]["retry"]["reason"] == "peer closed connection"
    assert retry_entries[0]["retry"]["error_message"] == "peer closed connection"
    assert retry_entries[0]["provider_name"] == "openai"
    assert error_entries == []


@pytest.mark.anyio
async def test_prompt_logs_exhausted_retries_and_terminal_error(tmp_path: Path) -> None:
    """Prove exhausted retries log each reattempt plus exactly one terminal error."""
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    tau_paths = TauPaths(home=tmp_path / "tau-home", agents_home=tmp_path / "agents-home")
    provider = FakeProvider(
        [
            [assistant_start(), transport_error("drop 1")],
            [assistant_start(), transport_error("drop 2")],
            [assistant_start(), transport_error("drop 3")],
        ]
    )
    session = await CodingSession.load(
        CodingSessionConfig(
            provider=provider,
            model="fake",
            system="You are Tau.",
            storage=storage,
            cwd=tmp_path,
            provider_name="openai",
            session_id="session-1",
            resource_paths=TauResourcePaths(root=tau_paths.home, paths=tau_paths),
        )
    )

    await _collect_session_events(session.prompt("Hello"))

    log_path = tau_paths.agent_calls_log_path
    entries = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    retry_entries = [entry for entry in entries if entry["kind"] == "assistant_retry"]
    error_entries = [entry for entry in entries if entry["kind"] == "assistant_error"]
    assert len(retry_entries) == 2
    assert len(error_entries) == 1
    assert error_entries[0]["error"]["stop_reason"] == "error"


@pytest.mark.anyio
async def test_auto_naming_failure_is_never_turn_retried(tmp_path: Path) -> None:
    """Prove one-shot provider calls (auto-naming) bypass turn-level retry."""
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    tau_paths = TauPaths(home=tmp_path / "tau-home", agents_home=tmp_path / "agents-home")
    provider = FakeProvider(
        [
            # stream 0: auto-naming call (one-shot, transient-shaped failure)
            [assistant_error("peer closed connection")],
            # stream 1: the main assistant turn
            [assistant_start(), assistant_done(AssistantMessage(content="ok", model="fake"))],
        ]
    )
    session = await CodingSession.load(
        CodingSessionConfig(
            provider=provider,
            model="fake",
            system="You are Tau.",
            storage=storage,
            cwd=tmp_path,
            provider_name="openai",
            session_id="session-1",
            auto_compact_enabled=False,
            session_manager=SessionManager(paths=tau_paths),
            resource_paths=TauResourcePaths(root=tau_paths.home, paths=tau_paths),
        )
    )

    await _collect_session_events(session.prompt("Hello"))

    assert len(provider.calls) == 2
    log_path = tau_paths.agent_calls_log_path
    entries = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if "retry" in line
    ]
    assert all(entry["kind"] != "assistant_retry" for entry in entries)
```

(Note: a failed auto-naming call surfaces as a session `log_exception` entry, not an `assistant_error` — the test's point is that one-shot calls never produce turn-retry entries, so the assertion checks exactly that.)

- [ ] **Step 3: Adapt two restored session tests whose failing streams would now be retried**

Under the central classifier, the two restored failure-logging tests below hit the retry path (single failing stream, no status, no markers) and fail their `session_ids` and single-`assistant_error`-entry assertions. Make their failures terminal without changing the tests' intent (logging error diagnostic data safely). The diagnostics sanitizer in `src/tau_coding/diagnostics.py` copies only `status_code`, `attempts`, and a sanitized `event` into the log, so the expected entries stay predictable.

- In `test_prompt_logs_error_event_diagnostic_data` (tests/test_coding_session.py, ~line 301), the helper call currently passes `data={"status_code": 400, "body": "bad request"}` which the helper discards. Change it to:

```python
                assistant_error(
                    message="provider failed",
                    status_code=400,
                    body="bad request",
                )
```

  and update the strict entry assertion to include the sanitized status (body is filtered out by the sanitizer):

```python
    assert entry["error"] == {
        "message": "provider failed",
        "stop_reason": "error",
        "provider": {"status_code": 400},
    }
```

- In `test_prompt_logs_safe_provider_stream_error_details` (~line 342), add a non-transient status to the existing diagnostic details so classification is terminal, and mirror it in the expected entry (the sanitized `event` part is unchanged):

```python
                details={
                    "status_code": 400,
                    "event": {
                        "type": "error",
                        "error": {
                            "type": "service_unavailable_error",
                            "code": "server_is_overloaded",
                            "message": "Our servers are currently overloaded. "
                            "Please try again later.",
                            "param": None,
                        },
                        "sequence_number": 2,
                    },
                },
```

  and in the assertion add `"status_code": 400,` as the first key of the expected `provider` dict.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /workspace
uv run ruff format src/tau_coding/session.py tests/test_coding_session.py
uv run pytest tests/test_coding_session.py -v
```

Expected: all pass, including the three re-added diagnostics tests and the two adapted failure-logging tests. If any further restored test in this suite fails with a retry notice or a double provider call, apply the same treatment: make its failing main-turn stream terminal-shaped (non-transient `status_code`, or terminal markers in body/message) and adjust strict assertions to the sanitizer whitelist (`status_code`/`attempts`/`event`).

- [ ] **Step 5: Commit**

```bash
cd /workspace
git add src/tau_coding/session.py tests/test_coding_session.py
git commit -m "feat(session): log turn-level retries in agent-calls diagnostics"
```

---

### Task 6: Documentation

**Files:**
- Rewrite: `dev-notes/2026-08-16-transient-error-retry.md`
- Update: `dev-notes/provider-error-recovery.md`, `dev-notes/architecture/provider-retries.md`
- Update: `website/content/guides/sessions.md`, `website/content/guides/providers-and-models.md`, `website/content/reference/cli.md`, `website/content/reference/configuration.md`

**Delta requirement:** All requirements (documents the simplified classification, fixed budget, reason, removed tail-read/config surface).

- [ ] **Step 1: Rewrite the implementation dev-note** — replace `dev-notes/2026-08-16-transient-error-retry.md` entirely with:

```markdown
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
```

- [ ] **Step 2: Update the recovery note** — in `dev-notes/provider-error-recovery.md`, replace the paragraph that starts `Only empty \`error\` and \`aborted\` assistant turns...` (currently referencing `transient-error-retry.md`) with:

```markdown
Only empty `error` and `aborted` assistant turns are filtered. A failed message
with text, thinking, or tool-call content remains in provider context for now so
this focused fix does not silently discard a partial response. Partially failed
turns are handled by the turn-level transient retry described in
[`transient-error-retry.md`](2026-08-16-transient-error-retry.md): retryable
drops after partial content are retried with the partial discarded, and only
exhausted or non-retryable failures reach this projection path.
```

(Verify the current wording first with `sed -n 28,40p dev-notes/provider-error-recovery.md` and update only the changed sentences.)

- [ ] **Step 3: Update the architecture note** — in `dev-notes/architecture/provider-retries.md`:

- Replace the first content paragraph (lines 5–7, after the frontmatter: `Tau retries transient provider failures in \`tau_ai\`, where HTTP status codes and transport exceptions are visible. This keeps retry classification out of \`tau_agent\`...`) with:

```markdown
Tau retries transient provider failures in two places: the `tau_ai` adapters
retry requests that emitted nothing (transient statuses, pre-content transport
errors), and the agent turn loop (`tau_agent.loop`) retries whole turns when a
failure survives the adapter — including mid-stream drops after partial
content. Turn-level classification lives in `tau_agent.retry` and reads the
`provider_error` diagnostics that adapters already attach to terminal errors,
so adapters carry no retry-classification code.
```

- Replace the `## Turn-level retry on top of adapter retries` section (from that heading through the paragraph that ends `instead.`) with:

```markdown
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
```

- Delete the `response_tail_read` paragraph (the tail-read special case no longer exists: a transport error after a complete response is retried like any other failure).
- Keep the backoff/cancellation paragraph, reworded: `Backoff is short, exponential (base 0.25s, cap 1s), and cancellation is checked during the delay so Escape/TUI cancellation does not wait for the entire retry sleep to finish.` (drop the `max_retry_delay_seconds` reference and the `response_tail_read` sentence above it).

- [ ] **Step 4: Update the website pages** (all current mentions were located with `grep -rn "turn_retry\|turn-retry\|turn-level retry" website/content/`):

- `website/content/guides/sessions.md`: replace the retry paragraph (the one beginning `The provider adapter first retries with its configured \`max_retries\`` and ending `...project the terminal error`) with:

```markdown
Requests first retry with the provider's configured `max_retries`; if a
transient failure outlives those attempts — for example a connection dropped
mid-stream after partial output — Tau retries the whole turn up to two more
times. Failed attempts are discarded: their partial text is rolled back from
the transcript and never written to session history, and a transient
“Connection lost — retrying N/M” notice appears until the reattempt streams.
Only exhausted or non-transient failures (usage limits, context overflow,
non-transient statuses, cancellation) project the terminal error.
```

- `website/content/guides/providers-and-models.md`: remove `turn_retry_max` from the JSON example and delete the `turn_retry_max` parenthetical from the sentence listing saved provider fields (keep `headers`, `timeout_seconds`, `max_retries`, `max_retry_delay_seconds`).
- `website/content/reference/cli.md`: delete the `--turn-retry-max` table row.
- `website/content/reference/configuration.md`: remove `turn_retry_max` from the JSON example; replace the sentence `\`turn_retry_max\` defaults to \`2\` (≥ 0, \`0\` disables turn-level retries).` with `Turn-level retries after the adapter budget is exhausted are fixed at two attempts and are not configurable.`; replace the sentence `When a failure outlives the adapter retries — including drops after partial output — Tau retries the whole turn up to \`turn_retry_max\` more times, discarding the failed attempt's partial output and showing a transient “Connection lost — retrying” notice; only exhausted retries project the terminal error.` with `When a failure outlives the adapter retries — including drops after partial output — Tau retries the whole turn up to two more times, discarding the failed attempt's partial output and showing a transient “Connection lost — retrying” notice; only exhausted retries project the terminal error.`

- [ ] **Step 5: Verify no stale mentions remain**

```bash
cd /workspace
grep -rn "turn_retry\|--turn-retry-max\|response_tail_read" website/ dev-notes/ | grep -v design/ || true
```

Expected: empty (the old design docs under `dev-notes/design/` are dated artifacts and intentionally keep their historical content).

- [ ] **Step 6: Commit**

```bash
cd /workspace
git add dev-notes/2026-08-16-transient-error-retry.md dev-notes/provider-error-recovery.md \
  dev-notes/architecture/provider-retries.md website/content/
git commit -m "docs: document centrally classified turn-level retry"
```

---

### Task 7: Final verification gates

**Files:** any stragglers found by the greps below.

**Delta requirement:** All — the final state must satisfy the full spec.

- [ ] **Step 1: Leftover-symbol sweep**

```bash
cd /workspace
grep -rn "turn_retry_max\|turn-retry-max\|--turn-retry-max\|turn_retry_max=" src/ tests/ website/ || true
grep -rn "event\.retryable\|retryable_error\|retryable=\|retryable: bool" src/ tests/ --include=*.py | grep -v __pycache__ || true
grep -rn "from tau_ai.classify\|tau_ai\.classify\|attach_tail_read_diagnostic" src/ tests/ || true
```

Expected: empty. (The new `failure_is_retryable` symbol and the pre-feature `_retryable_*` adapter identifiers are expected survivors and are not matched by these patterns.)

- [ ] **Step 2: Full test suite**

Run: `uv run pytest`
Expected: all tests pass (baseline was `1621 passed, 3 skipped` before the feature; the final count will be lower — roughly `1500+` — because the adapter/classifier/config test additions were removed; the kept TUI/diagnostics/loop tests must pass).

- [ ] **Step 3: Lint, format, and types**

```bash
cd /workspace
uv run ruff check .
uv run ruff format --check .     # if it reports drift, run `uv run ruff format <listed files>` and commit
uv run mypy
```

Expected: all clean. If formatting drift is reported, format the listed files, re-verify, and commit as `style: apply ruff format after reverts`.

- [ ] **Step 4: Confirm the final commit sequence**

Run: `git log --oneline -14`
Expected: the feature's original 13 commits are superseded by the simplification commits; `git status` shows a clean tree.

---

## Self-review checklist

1. **Spec coverage:** Every requirement in the simplified spec maps to a delta entry; every MODIFIED/ADDED requirement has a task with a failing test — classification (T2/T3), reason content (T2/T3), fixed budget with no config surface (T4 + grep), cancellation (kept tests, T3 fixtures), rollback/notice (kept TUI tests), diagnostics (T5), overflow/one-shot scope (T5), tail-read replacement (T3 transport classification; adapter revert T1).
2. **Delta coverage:** `MODIFIED Transient-failure classification` → T2/T3; `MODIFIED Bounded turn-level retry` → T2/T3; `MODIFIED Retry diagnostics` → T5; `REMOVED Complete-response tail failure` → T1/T3; `REMOVED budget config/setup` → T4; `REMOVED adapter entities` → T1/T3; `ADDED Fixed retry budget` → T4 + harness default kept; `MODIFIED Cancellation` → kept T3 tests.
3. **Reverse coverage:** no task lacks a delta requirement.
4. **Placeholders:** every code step carries complete code; every command has expected output.
5. **Type consistency:** `failure_is_retryable(message, *, signal)` and `failure_reason(message)` are defined in T2 and consumed in T3; `transport_error`/`assistant_error` signatures fixed in T3 and consumed by the kept print-mode tests (T4) and the re-added session tests (T5); `TurnRetryStartEvent` fields unchanged.
6. **Standards:** no fallbacks introduced (`failure_reason`'s `"provider error"` fallback only fires for a blank message text and is covered by a test); docstrings are behavioral; docs describe only the final state (the design-history paragraph in the dev-note is explicitly labeled as history).
