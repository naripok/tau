# Automatic Retry of Transient Provider Errors — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically retry transient provider failures (e.g. mid-stream connection drops) at the turn level, with transparent rollback of partial content in the TUI and clean session history.

**Architecture:** Provider adapters (`tau_ai`) classify terminal failures with a `retryable` flag (transport errors, transient statuses after the adapter budget is exhausted, in-stream SSE errors per existing markers; terminal rate limits, context overflow, and cancellation are never retryable). The harness loop (`tau_agent.loop`) re-runs a turn's provider call up to a per-provider budget (`turn_retry_max`, default 2) — failed attempts never enter history and their terminal error event is suppressed; a new `TurnRetryStartEvent` drives TUI rollback and diagnostics. A transport error after the final stream chunk is treated as success with a diagnostic note (Complete-response tail failure).

**Tech Stack:** Python 3.13 (see `pyproject.toml`), httpx, pydantic WireModels, pytest + anyio, Textual. Run everything through `uv` (`uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`).

**Standards:** Apply the shared code standards in every task: DRY, low cyclomatic complexity, type safety, no unnecessary abstractions or fallbacks, no hacks or workarounds, informative docstrings (tests: what behavior the test proves and why it is needed), documentation of current state only. TDD: write the failing test first, see it fail, then implement.

**Feature spec:** `dev-notes/design/2026-08-16-transient-error-retry-spec.md`

**Delta spec:** `dev-notes/design/2026-08-16-transient-error-retry-delta.md`

---

## File map (created or modified)

| File | Responsibility |
| --- | --- |
| `src/tau_agent/retry.py` (new) | Cancellable exponential backoff helpers for both layers; turn-retry delay |
| `src/tau_ai/retry.py` | Re-exports helpers moved down to `tau_agent.retry`; keeps `provider_retry_event` |
| `src/tau_ai/_provider_events.py` | `ProviderErrorEvent.retryable` field |
| `src/tau_agent/provider_events.py` | `AssistantErrorEvent.retryable` field |
| `src/tau_ai/stream.py` | Pass `retryable` through; `attach_tail_read_diagnostic` helper |
| `src/tau_agent/events.py` | New `TurnRetryStartEvent` in the `AgentEvent` union |
| `src/tau_agent/loop.py` | Bounded retry around each turn's provider call; suppression + backoff + cancellation-aware discard |
| `src/tau_agent/harness.py` | `DEFAULT_TURN_RETRIES = 2`, `AgentHarnessConfig.max_turn_retries`, pass-through |
| `src/tau_ai/classify.py` (new) | Shared transient status, terminal rate-limit, and context-overflow classification |
| `src/tau_coding/session.py` | `is_context_overflow_error` imports shared markers; harness budget wiring; retry diagnostics |
| `src/tau_coding/diagnostics.py` | `log_turn_retry` (kind `assistant_retry`) |
| `src/tau_ai/openai_compatible.py` | Classification on final errors, `error_type` data, tail-read restructure, body-aware internal retries |
| `src/tau_ai/anthropic.py` | Same + `message_stop` break + in-stream retryable classification |
| `src/tau_ai/openai_codex.py` | Same + buffered end event + in-stream retryable classification |
| `src/tau_ai/google.py`, `src/tau_ai/mistral.py` | Same restructure + classification |
| `src/tau_coding/provider_config.py` | `turn_retry_max` on the three provider models + validation + serialization + merges |
| `src/tau_coding/cli.py` | `--turn-retry-max` for `tau setup` |
| `src/tau_coding/tui/adapter.py` | Rollback partial state + notice on `TurnRetryStartEvent` |
| `src/tau_coding/tui/state.py` | `format_retry_notice` helper |
| `src/tau_coding/tui/app.py` | Transcript rollback + notice wiring |
| `src/tau_coding/tui/widgets.py` | `TranscriptView.discard_active_assistant` + notice lifecycle |
| `src/tau_coding/rendering/transcript.py` | Print-mode retry notice line |
| `tests/pi_event_helpers.py` | `assistant_error(retryable=...)`, `retryable_error` builder |
| `tests/test_agent_retry.py` (new), `tests/test_agent_loop.py`, `tests/test_agent_harness.py` | Harness retry behavior |
| `tests/test_tau_ai.py` | Classification, flag plumbing, per-adapter envelope behavior |
| `tests/test_coding_session.py`, `tests/test_provider_config.py`, `tests/test_cli.py` | Session diagnostics, config, setup flag |
| `tests/test_tui_adapter.py`, `tests/test_tui_app.py` | TUI rollback + notice |
| `tests/test_rendering.py` | Print renderer notice |
| `dev-notes/*` (docs) | New dev-note; refresh `provider-error-recovery.md` and `architecture/provider-retries.md`; website guides/reference |

---

## Task 1: Shared backoff helpers in the agent layer

**Files:**
- Create: `src/tau_agent/retry.py`
- Modify: `src/tau_ai/retry.py`
- Test: `tests/test_agent_retry.py` (create)

**Delta requirement:** Bounded turn-level retry (backoff growth + cap).

- [ ] **Step 1: Check current callers of the constants**

Run: `rg -n "RETRY_BASE_DELAY_SECONDS|RETRY_POLL_SECONDS" src/ tests/`
Expected: only `src/tau_ai/retry.py` uses them. (If other files import them, keep re-exports of those names too.)

- [ ] **Step 2: Write the failing tests** (`tests/test_agent_retry.py`)

```python
"""Tests for the cancellable retry backoff helpers in the agent layer."""

import asyncio

import pytest

from tau_agent.harness import SimpleCancellationToken
from tau_agent.retry import turn_retry_delay_seconds, wait_for_retry


def test_turn_retry_delays_grow_then_cap() -> None:
    """Prove the retry delay grows exponentially and stops at the one-second cap."""
    delays = [turn_retry_delay_seconds(attempt) for attempt in range(4)]

    assert delays == [0.25, 0.5, 1.0, 1.0]
    assert delays[1] > delays[0]


@pytest.mark.anyio
async def test_wait_for_retry_is_interruptible_by_cancellation() -> None:
    """Prove a cancelled token aborts the backoff sleep instead of waiting it out."""
    signal = SimpleCancellationToken()
    task = asyncio.create_task(wait_for_retry(10.0, signal=signal))
    await asyncio.sleep(0.1)
    signal.cancel()

    assert await task is False


@pytest.mark.anyio
async def test_wait_for_retry_zero_delay_completes() -> None:
    """Prove a zero delay returns immediately without sleeping."""
    assert await wait_for_retry(0.0, signal=None) is True
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent_retry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tau_agent.retry'`

- [ ] **Step 4: Create `src/tau_agent/retry.py`**

```python
"""Cancellable retry backoff helpers shared by the agent and provider layers."""

from __future__ import annotations

from asyncio import sleep

from tau_agent.provider import CancellationToken

RETRY_POLL_SECONDS = 0.05
RETRY_BASE_DELAY_SECONDS = 0.25
TURN_RETRY_MAX_DELAY_SECONDS = 1.0


def retry_delay_seconds(attempt: int, *, max_delay_seconds: float) -> float:
    """Return an exponential retry delay capped by a configured maximum."""
    if max_delay_seconds <= 0:
        return 0.0
    base_delay = min(RETRY_BASE_DELAY_SECONDS, max_delay_seconds)
    return float(min(max_delay_seconds, base_delay * (2**attempt)))


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
```

- [ ] **Step 5: Rewrite `src/tau_ai/retry.py` to re-export the moved helpers**

Replace the entire file content with:

```python
"""Shared retry helpers for provider adapters.

The generic backoff helpers live in ``tau_agent.retry`` so the harness can use
them without depending on the provider layer. This module keeps the
provider-facing ``ProviderRetryEvent`` builder and re-exports the helpers so
the adapters' imports stay unchanged.
"""

from __future__ import annotations

from tau_agent.retry import retry_delay_seconds, wait_for_retry  # noqa: F401
from tau_agent.types import JSONValue
from tau_ai._provider_events import ProviderRetryEvent
from tau_ai.provider import CancellationToken


def provider_retry_event(
    *,
    attempt: int,
    max_retries: int,
    delay_seconds: float,
    reason: str,
    data: dict[str, JSONValue] | None = None,
) -> ProviderRetryEvent:
    """Build a provider-neutral retry progress event."""
    next_attempt = attempt + 2
    max_attempts = max_retries + 1
    delay_suffix = f" in {delay_seconds:g}s" if delay_seconds else ""
    return ProviderRetryEvent(
        attempt=next_attempt,
        max_attempts=max_attempts,
        delay_seconds=delay_seconds,
        message=(
            f"Retrying provider request {next_attempt}/{max_attempts} after {reason}{delay_suffix}."
        ),
        data=data,
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent_retry.py tests/test_tau_ai.py -x -q`
Expected: PASS (existing provider tests confirm the re-export keeps adapters working)

- [ ] **Step 7: Commit**

```bash
git add src/tau_agent/retry.py src/tau_ai/retry.py tests/test_agent_retry.py
git commit -m "refactor(retry): move backoff helpers into the agent layer"
```

---

## Task 2: `retryable` flag plumbing through the provider bridge

**Files:**
- Modify: `src/tau_ai/_provider_events.py`, `src/tau_agent/provider_events.py`, `src/tau_ai/stream.py`, `tests/pi_event_helpers.py`
- Test: `tests/test_tau_ai.py`

**Delta requirement:** Transient-failure classification (flag transport path).

- [ ] **Step 1: Extend the event models**

In `src/tau_ai/_provider_events.py`, add the field to `ProviderErrorEvent` (after `response_provider`):

```python
    type: Literal["error"] = "error"
    message: str
    data: dict[str, JSONValue] | None = None
    response_provider: str | None = None
    retryable: bool = False
```

In `src/tau_agent/provider_events.py`, add the field to `AssistantErrorEvent`:

```python
class AssistantErrorEvent(WireModel):
    type: Literal["error"] = "error"
    reason: ErrorReason
    error: AssistantMessage
    retryable: bool = False
```

- [ ] **Step 2: Pass the flag through the stream bridge**

In `src/tau_ai/stream.py`, the `ProviderErrorEvent` branch of `canonicalize_provider_stream` — change its terminal yield to:

```python
            yield AssistantErrorEvent(
                reason="error",
                error=error,
                retryable=event.retryable,
            )
```

- [ ] **Step 3: Extend the test helper** (`tests/pi_event_helpers.py`)

```python
def assistant_error(
    message: str, data: object = None, *, retryable: bool = False
) -> AssistantErrorEvent:
    del data
    error = AssistantMessage(stop_reason="error", error_message=message)
    return AssistantErrorEvent(reason="error", error=error, retryable=retryable)


def retryable_error(message: str, *, partial: str = "") -> AssistantErrorEvent:
    """A transient provider failure whose partial output is safe to discard."""
    error = AssistantMessage(stop_reason="error", error_message=message, content=partial)
    return AssistantErrorEvent(reason="error", error=error, retryable=True)
```

- [ ] **Step 4: Write the failing tests** (append to `tests/test_tau_ai.py`, next to the existing canonicalize tests — locate them with `rg -n "canonicalize_provider_stream" tests/test_tau_ai.py`)

```python
def _async_events(
    events: list[ProviderEvent],
) -> AsyncIterator[ProviderEvent]:
    """Yield provider events one at a time for canonicalize tests."""
    for event in events:
        yield event


@pytest.mark.anyio
async def test_canonicalize_forwards_retryable_error_flag() -> None:
    """Prove the adapter's retryable classification reaches the agent layer."""
    from tau_agent.provider_events import AssistantErrorEvent
    from tau_ai._provider_events import ProviderErrorEvent
    from tau_ai.stream import canonicalize_provider_stream

    events = [
        event
        async for event in canonicalize_provider_stream(
            _async_events(
                [
                    ProviderErrorEvent(
                        message="peer closed connection",
                        data={"attempts": 1, "error_type": "RemoteProtocolError"},
                        retryable=True,
                    )
                ]
            ),
            api="openai-completions",
            provider="openai",
            model="test-model",
        )
    ]

    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].retryable is True


@pytest.mark.anyio
async def test_canonicalize_defaults_retryable_to_false() -> None:
    """Prove non-classified failures are never retried at the harness level."""
    from tau_agent.provider_events import AssistantErrorEvent
    from tau_ai._provider_events import ProviderErrorEvent
    from tau_ai.stream import canonicalize_provider_stream

    events = [
        event
        async for event in canonicalize_provider_stream(
            _async_events(
                [ProviderErrorEvent(message="invalid api key", data={"attempts": 1})]
            ),
            api="openai-completions",
            provider="openai",
            model="test-model",
        )
    ]

    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].retryable is False
```

> `canonicalize_provider_stream` consumes an async iterable, so the tests wrap
> the event lists in `_async_events`; `ProviderEvent` comes from
> `tau_ai._provider_events` and `AsyncIterator` from `collections.abc` (import
> both at the top of the test section if the file does not already import
> them).

- [ ] **Step 5: Run the tests to verify they fail, then pass after Step 1–2**

Run: `uv run pytest tests/test_tau_ai.py -k "retryable" -v` (verify failures first, then pass after the edits)

- [ ] **Step 6: Run the broader checks**

Run: `uv run pytest tests/test_tau_ai.py tests/test_pi_event_protocol.py -q && uv run ruff check src/tau_ai/stream.py src/tau_ai/_provider_events.py src/tau_agent/provider_events.py && uv run mypy src/tau_ai src/tau_agent`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/tau_ai/_provider_events.py src/tau_agent/provider_events.py src/tau_ai/stream.py tests/pi_event_helpers.py tests/test_tau_ai.py
git commit -m "feat(provider): classify terminal provider errors as retryable"
```

---

## Task 3: Turn-level retry in the agent loop

**Files:**
- Modify: `src/tau_agent/events.py`, `src/tau_agent/loop.py`, `src/tau_agent/harness.py`
- Test: `tests/test_agent_loop.py`, `tests/test_agent_harness.py`

**Delta requirement:** Bounded turn-level retry (all scenarios), Cancellation during retry, Retry scope limited to main turns.

- [ ] **Step 1: Write the failing loop tests** (append to `tests/test_agent_loop.py`; add `TurnRetryStartEvent` and `retryable_error` to the imports from `tau_agent` / `pi_event_helpers`)

```python
@pytest.mark.anyio
async def test_agent_loop_retries_transient_failure_then_succeeds() -> None:
    """Prove a retryable failure is retried invisibly and never touches history."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]
    recovered = AssistantMessage(content="recovered", model="fake")
    provider = FakeProvider(
        [
            [assistant_start(), text_delta("partial"), retryable_error("peer closed connection")],
            [assistant_start(), text_delta("recovered"), assistant_done(recovered)],
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
    assert len(retries) == 1
    assert retries[0].attempt == 2
    assert retries[0].max_attempts == 3
    assert retries[0].error_message == "peer closed connection"
    assert messages[-1] is recovered
    assert len(provider.calls) == 2
    assert provider.calls[0][2] == provider.calls[1][2]
    assert not any(
        isinstance(event, MessageEndEvent)
        and isinstance(event.message, AssistantMessage)
        and event.message.stop_reason == "error"
        for event in events
    )


@pytest.mark.anyio
async def test_agent_loop_exhausts_turn_retry_budget() -> None:
    """Prove two retries are allowed and the third failure ends the run as today."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]
    provider = FakeProvider(
        [
            [assistant_start(), text_delta("a"), retryable_error("drop 1", partial="a")],
            [assistant_start(), text_delta("b"), retryable_error("drop 2", partial="b")],
            [assistant_start(), text_delta("c"), retryable_error("drop 3", partial="c")],
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

    assert sum(isinstance(event, TurnRetryStartEvent) for event in events) == 2
    assert len(provider.calls) == 3
    final = messages[-1]
    assert isinstance(final, AssistantMessage)
    assert final.stop_reason == "error"
    assert final.error_message == "drop 3"
    assert final.text == "c"


@pytest.mark.anyio
async def test_agent_loop_turn_retry_disabled_with_zero_budget() -> None:
    """Prove a zero budget keeps today's terminal behavior exactly."""
    provider = FakeProvider(
        [[assistant_start(), text_delta("partial"), retryable_error("drop", partial="partial")]]
    )
    messages: list[AgentMessage] = [UserMessage(content="hello")]

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=0,
        )
    )

    assert not any(isinstance(event, TurnRetryStartEvent) for event in events)
    assert len(provider.calls) == 1
    assert messages[-1].stop_reason == "error"


@pytest.mark.anyio
async def test_agent_loop_does_not_retry_non_retryable_error() -> None:
    """Prove only adapter-classified transient failures trigger a retry."""
    provider = FakeProvider([[assistant_error("invalid api key")]])

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=[UserMessage(content="hello")],
            tools=[],
            max_turn_retries=2,
        )
    )

    assert not any(isinstance(event, TurnRetryStartEvent) for event in events)
    assert len(provider.calls) == 1


@pytest.mark.anyio
async def test_agent_loop_retry_backoff_delays_grow() -> None:
    """Prove retry delays are exponential and stop at the one-second cap."""
    provider = FakeProvider(
        [
            [assistant_start(), retryable_error("1")],
            [assistant_start(), retryable_error("2")],
            [assistant_start(), retryable_error("3")],
            [assistant_start(), assistant_done(AssistantMessage(content="ok", model="fake"))],
        ]
    )

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=[UserMessage(content="hello")],
            tools=[],
            max_turn_retries=3,
        )
    )

    assert [event.delay_seconds for event in events if isinstance(event, TurnRetryStartEvent)] == [
        0.25,
        0.5,
        1.0,
    ]


@pytest.mark.anyio
async def test_agent_loop_cancel_during_retry_backoff_discards_partial() -> None:
    """Prove cancelling during backoff ends the run with the partial discarded."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]
    provider = FakeProvider(
        [[assistant_start(), text_delta("partial"), retryable_error("drop", partial="partial")]]
    )
    signal = SimpleCancellationToken()
    events: list[AgentEvent] = []

    async for event in run_agent_loop(
        provider=provider,
        model="fake",
        system="You are Tau.",
        messages=messages,
        tools=[],
        signal=signal,
        max_turn_retries=2,
    ):
        events.append(event)
        if isinstance(event, TurnRetryStartEvent):
            signal.cancel()

    assert len(provider.calls) == 1
    assert sum(isinstance(event, TurnRetryStartEvent) for event in events) == 1
    final = messages[-1]
    assert isinstance(final, AssistantMessage)
    assert final.stop_reason == "error"
    assert final.error_message == "drop"
    assert not final.content


@pytest.mark.anyio
async def test_agent_loop_cancel_during_reattempt_ends_run() -> None:
    """Prove cancelling a reattempt never triggers further attempts."""
    provider = FakeProvider(
        [
            [assistant_start(), text_delta("partial"), retryable_error("drop")],
            [
                assistant_start(),
                text_delta("re"),
                assistant_done(AssistantMessage(content="done", model="fake")),
            ],
        ]
    )
    signal = SimpleCancellationToken()
    events: list[AgentEvent] = []

    async for event in run_agent_loop(
        provider=provider,
        model="fake",
        system="You are Tau.",
        messages=[UserMessage(content="hello")],
        tools=[],
        signal=signal,
        max_turn_retries=2,
    ):
        events.append(event)
        if isinstance(event, MessageUpdateEvent) and event.message.text == "re":
            signal.cancel()

    assert len(provider.calls) == 2
    assert sum(isinstance(event, TurnRetryStartEvent) for event in events) == 1
    assert events[-1].type == "agent_end"
```

- [ ] **Step 2: Add the harness-budget test** (append to `tests/test_agent_harness.py`; import `TurnRetryStartEvent` and `DEFAULT_TURN_RETRIES` from `tau_agent`)

```python
@pytest.mark.anyio
async def test_harness_retries_transient_failures_with_configured_budget() -> None:
    """Prove the harness applies its configured turn-retry budget to prompts."""
    recovered = AssistantMessage(content="recovered", model="fake")
    provider = FakeProvider(
        [
            [assistant_start(), text_delta("partial"), retryable_error("drop")],
            [assistant_start(), assistant_done(recovered)],
        ]
    )
    harness = AgentHarness(
        AgentHarnessConfig(
            provider=provider,
            model="fake",
            system="You are Tau.",
            max_turn_retries=1,
        )
    )

    events = await _collect(harness.prompt("hello"))

    assert sum(isinstance(event, TurnRetryStartEvent) for event in events) == 1
    assert harness.messages[-1] is recovered
    assert harness.messages[-1].stop_reason == "stop"


@pytest.mark.anyio
async def test_harness_default_turn_retry_budget_is_two() -> None:
    """Prove the unconfigured harness budget matches the spec default of two."""
    provider = FakeProvider([[]])
    config = AgentHarnessConfig(provider=provider, model="fake", system="You are Tau.")

    assert config.max_turn_retries == DEFAULT_TURN_RETRIES
```

> Note: `test_harness_default_turn_retry_budget_is_two` uses `FakeProvider([[]])` as a placeholder; if `AgentHarnessConfig` requires a usable provider, construct it with any `FakeProvider([[]])` — it is never called.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent_loop.py -k "retry or retries or cancel" tests/test_agent_harness.py -v`
Expected: FAIL — `run_agent_loop() got an unexpected keyword argument 'max_turn_retries'` / `TurnRetryStartEvent` import error

- [ ] **Step 4: Add the harness event** (`src/tau_agent/events.py`)

```python
class TurnRetryStartEvent(WireModel):
    """A transient provider failure will be retried; the failed partial is void."""

    type: Literal["turn_retry_start"] = "turn_retry_start"
    attempt: int
    max_attempts: int
    delay_seconds: float
    reason: str
    error_message: str = ""
    error_type: str | None = None
```

Add `TurnRetryStartEvent` to the `AgentEvent` union (alphabetical position within the Annotated union).

- [ ] **Step 5: Implement the retry loop** (`src/tau_agent/loop.py`)

Add the parameter to `run_agent_loop` (after `max_turns`):

```python
    max_turns: int | None = None,
    max_turn_retries: int = 0,
    signal: CancellationToken | None = None,
```

In `run_agent_loop`'s inner `while has_more_tools or pending:` block, update the provider-consumption site to pass the budget:

```python
            assistant = None
            async for event in _assistant_events(
                provider=provider,
                model=model,
                system=system,
                messages=_provider_context(messages),
                tools=tools,
                signal=signal,
                session_id=session_id,
                max_turn_retries=max_turn_retries,
            ):
                yield event
                if isinstance(event, MessageEndEvent) and isinstance(
                    event.message, AssistantMessage
                ):
                    assistant = event.message
```

Replace the whole `_assistant_events` function and add the two helpers below it:

```python
async def _assistant_events(
    *,
    provider: ModelProvider,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    signal: CancellationToken | None,
    session_id: str | None,
    max_turn_retries: int,
) -> AsyncIterator[AgentEvent]:
    """Stream one turn's provider response, retrying transient failures.

    A failed attempt that the provider classified as retryable is discarded:
    consumers never see its terminal error end, only a retry-start notice
    followed by the reattempt's stream. The failed message is never added to
    harness history because the caller appends only the final message.
    """
    retries_done = 0
    while True:
        source = provider.stream_response(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            signal=signal,
            session_id=session_id,
        )
        started = False
        retry_failure: AssistantMessage | None = None
        async for event in source:
            if isinstance(event, AssistantErrorEvent):
                if (
                    event.retryable
                    and retries_done < max_turn_retries
                    and (signal is None or not signal.is_cancelled())
                ):
                    retry_failure = event.error
                    break
                if not started:
                    yield MessageStartEvent(message=event.error)
                yield MessageEndEvent(message=event.error)
                return
            if isinstance(event, AssistantStartEvent):
                started = True
                yield MessageStartEvent(message=event.partial)
            elif isinstance(event, AssistantDoneEvent):
                if not started:
                    yield MessageStartEvent(message=event.message)
                yield MessageEndEvent(message=event.message)
                return
            else:
                yield MessageUpdateEvent(
                    message=event.partial,
                    assistant_message_event=event,
                )
        if retry_failure is None:
            return
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
        retries_done += 1
        if not await wait_for_retry(delay, signal=signal):
            # Cancelled during the backoff: surface the retryable failure as a
            # terminal error without restoring its discarded partial content.
            discarded = retry_failure.model_copy(deep=True)
            discarded.content = []
            yield MessageStartEvent(message=discarded)
            yield MessageEndEvent(message=discarded)
            return


def _retry_failure_reason(message: AssistantMessage) -> tuple[str, str | None]:
    """Return a short (reason, error_type) pair for a retryable failure."""
    for diagnostic in message.diagnostics or []:
        if diagnostic.type != "provider_error" or not diagnostic.details:
            continue
        error_type = diagnostic.details.get("error_type")
        if isinstance(error_type, str) and error_type:
            return f"network error ({error_type})", error_type
        status_code = diagnostic.details.get("status_code")
        if isinstance(status_code, int) and not isinstance(status_code, bool):
            return f"HTTP {status_code}", f"HTTP {status_code}"
    return "provider error", None
```

Update the imports in `loop.py`:

```python
from tau_agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnRetryStartEvent,
    TurnStartEvent,
)
from tau_agent.retry import turn_retry_delay_seconds, wait_for_retry
```

- [ ] **Step 6: Wire the harness config** (`src/tau_agent/harness.py`)

Add at module level:

```python
DEFAULT_TURN_RETRIES = 2
```

Add the field to `AgentHarnessConfig` (after `max_turns`):

```python
    max_turns: int | None = None
    max_turn_retries: int = DEFAULT_TURN_RETRIES
    queue_mode: QueueMode = "one_at_a_time"
```

In `_run`, pass the budget to `run_agent_loop` (after `max_turns=...`):

```python
                max_turns=self._config.max_turns,
                max_turn_retries=self._config.max_turn_retries,
```

Export `DEFAULT_TURN_RETRIES` AND `TurnRetryStartEvent` from `src/tau_agent/__init__.py` if the package has an explicit export list (check `rg -n "max_turns|AgentHarnessConfig" src/tau_agent/__init__.py` and the `from tau_agent.events import (...)` re-export near the top; the tests import `DEFAULT_TURN_RETRIES` and `TurnRetryStartEvent` from `tau_agent`, so both names must be exported or the tests fail with ImportError).

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent_loop.py tests/test_agent_harness.py tests/test_agent_retry.py -q`
Expected: PASS

- [ ] **Step 8: Run the checks**

Run: `uv run pytest tests/test_agent_loop.py tests/test_agent_harness.py tests/test_tau_ai.py -q && uv run ruff check src/tau_agent && uv run mypy src/tau_agent`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add src/tau_agent/events.py src/tau_agent/loop.py src/tau_agent/harness.py src/tau_agent/__init__.py tests/test_agent_loop.py tests/test_agent_harness.py
git commit -m "feat(agent): retry transient provider failures at the turn level"
```

---

## Task 4: Shared failure classification module

**Files:**
- Create: `src/tau_ai/classify.py`
- Modify: `src/tau_coding/session.py`
- Test: `tests/test_tau_ai.py`, `tests/test_coding_session.py`

**Delta requirement:** Transient-failure classification (all scenarios), Overflow handling unchanged.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_tau_ai.py`, importing from `tau_ai.classify`)

```python
def test_classify_transient_statuses() -> None:
    """Prove the transient HTTP status set is what the adapters retry on."""
    assert is_transient_status(408) is True
    assert is_transient_status(429) is True
    assert is_transient_status(503) is True
    assert is_transient_status(400) is False
    assert is_transient_status(401) is False


def test_classify_terminal_markers_override_transient_status() -> None:
    """Prove usage limits and overflow markers make an exhausted failure terminal."""
    assert is_retryable_http_failure(503, "try later") is True
    assert is_retryable_http_failure(429, "insufficient_quota") is False
    assert is_retryable_http_failure(429, "monthly usage limit reached") is False
    assert is_retryable_http_failure(429, "quota exceeded for this model") is False
    assert is_retryable_http_failure(429, "maximum context length exceeded") is False
    assert is_retryable_http_failure(400, "bad request") is False


def test_classify_context_overflow_markers() -> None:
    """Prove the shared overflow markers cover the session's existing vocabulary."""
    assert is_context_overflow("This model's maximum context length was exceeded.") is True
    assert is_context_overflow("token limit exceeded") is True
    assert is_context_overflow("servers overloaded") is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tau_ai.py -k "classify" -v`
Expected: FAIL — import error

- [ ] **Step 3: Create `src/tau_ai/classify.py`**

```python
"""Shared transient/terminal failure classification for provider adapters."""

from __future__ import annotations

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


def is_transient_status(status_code: int) -> bool:
    """Return True for HTTP statuses that warrant retrying a provider call."""
    return status_code in TRANSIENT_HTTP_STATUSES or status_code >= 500


def is_terminal_rate_limit(text: str) -> bool:
    """Return True when a provider error body marks a terminal usage limit."""
    normalized = text.lower()
    return any(marker in normalized for marker in TERMINAL_RATE_LIMIT_MARKERS)


def is_context_overflow(text: str) -> bool:
    """Return True when a provider error body marks a context overflow."""
    normalized = text.lower()
    return any(marker in normalized for marker in CONTEXT_OVERFLOW_MARKERS)


def is_retryable_http_failure(status_code: int, body: str) -> bool:
    """Return whether an exhausted-status failure remains worth retrying.

    Terminal conditions (usage limits and context overflow) take precedence
    over the transient status.
    """
    if not is_transient_status(status_code):
        return False
    return not is_terminal_rate_limit(body) and not is_context_overflow(body)
```

- [ ] **Step 4: Point the session overflow check at the shared markers**

In `src/tau_coding/session.py`:

- Add the import alongside the other `tau_ai` imports:

```python
from tau_ai.classify import is_context_overflow
```

- Replace the body of `is_context_overflow_error` (keep the function name and the local marker list removal):

```python
def is_context_overflow_error(message: AssistantMessage) -> bool:
    """Return True when an assistant error looks like a context overflow."""
    return is_context_overflow(message.error_message or "")
```

Also delete the now-unused local `markers` tuple that the old body built.

- [ ] **Step 5: Verify the session markers still match**

Append to `tests/test_coding_session.py`:

```python
async def test_context_overflow_markers_shared_with_adapters(tmp_path: Path) -> None:
    """Prove the overflow detection the session compacts on still matches providers."""
    from tau_coding.session import is_context_overflow_error

    message = AssistantMessage(
        stop_reason="error",
        error_message="This model's maximum context length was exceeded.",
    )
    assert is_context_overflow_error(message) is True
```

(Place near the existing compaction tests; `AssistantMessage` is already imported there.)

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_tau_ai.py -k classify tests/test_coding_session.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/tau_ai/classify.py src/tau_coding/session.py tests/test_tau_ai.py tests/test_coding_session.py
git commit -m "feat(provider): share transient and overflow failure classification"
```

---

## Task 5: OpenAI-compatible envelope — classification and tail-read handling

**Files:**
- Modify: `src/tau_ai/openai_compatible.py`, `src/tau_ai/stream.py`
- Test: `tests/test_tau_ai.py`

**Delta requirement:** Transient-failure classification, Complete-response tail failure.

- [ ] **Step 1: Add the tail-read helper to the stream bridge** (`src/tau_ai/stream.py`)

```python
def attach_tail_read_diagnostic(
    end_event: ProviderResponseEndEvent, exc: Exception
) -> ProviderResponseEndEvent:
    """Return a response-end event whose message notes the trailing read failure.

    The provider stream already delivered its terminal chunk; the connection
    died only while reading the remaining body bytes, so the response is
    complete and the failure is recorded as a diagnostic instead of an error.
    """
    return end_event.model_copy(
        update={
            "message": end_event.message.model_copy(
                update={
                    "diagnostics": [
                        *(end_event.message.diagnostics or []),
                        AssistantMessageDiagnostic(
                            type="response_tail_read",
                            details={"error": str(exc), "error_type": type(exc).__name__},
                        ),
                    ]
                }
            )
        }
    )
```

- [ ] **Step 2: Write the failing adapter tests** (append to `tests/test_tau_ai.py`, modeled on `test_openai_compatible_provider_does_not_retry_after_partial_output` at line ~344)

```python
@pytest.mark.anyio
async def test_openai_compatible_marks_mid_stream_drop_retryable() -> None:
    """Prove a transport drop after partial output is classified retryable."""
    requests: list[httpx.Request] = []

    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError("stream dropped")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            stream=FailingStream(),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(
                api_key="test-key",
                base_url="https://example.test/v1",
                max_retries=2,
                max_retry_delay_seconds=0,
            ),
            client=client,
        )
        events = await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say ok")],
                tools=[],
            )
        )

    assert len(requests) == 1
    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].retryable is True
    assert events[-1].error.text == "partial"


@pytest.mark.anyio
async def test_openai_compatible_exhausted_transient_status_is_retryable() -> None:
    """Prove a transient status that exhausted the adapter budget stays retryable."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, text="try later")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(
                api_key="test-key",
                base_url="https://example.test/v1",
                max_retries=2,
                max_retry_delay_seconds=0,
            ),
            client=client,
        )
        events = await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say ok")],
                tools=[],
            )
        )

    assert len(requests) == 3
    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].retryable is True


@pytest.mark.anyio
async def test_openai_compatible_quota_429_is_not_retryable() -> None:
    """Prove a terminal rate-limit body overrides the transient 429 status."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            429, json={"error": {"message": "You have insufficient quota."}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(
                api_key="test-key",
                base_url="https://example.test/v1",
                max_retries=2,
                max_retry_delay_seconds=0,
            ),
            client=client,
        )
        events = await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say ok")],
                tools=[],
            )
        )

    assert len(requests) == 1
    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].retryable is False


@pytest.mark.anyio
async def test_openai_compatible_tail_read_completes_with_diagnostic() -> None:
    """Prove a drop after the final chunk completes the message with a note."""
    requests: list[httpx.Request] = []

    class TailDroppingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            body = (
                b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\n'
                b"data: [DONE]\n\n"
            )
            yield body

        async def aclose(self) -> None:
            raise httpx.ReadError("tail dropped")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            stream=TailDroppingStream(),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(
                api_key="test-key",
                base_url="https://example.test/v1",
            ),
            client=client,
        )
        events = await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say ok")],
                tools=[],
            )
        )

    assert len(requests) == 1
    assert events[-1].type == "done"
    message = events[-1].message
    assert message.text == "done"
    assert message.stop_reason == "stop"
    assert any(
        diagnostic.type == "response_tail_read" for diagnostic in message.diagnostics or []
    )
```

> The `TailDroppingStream.aclose` raise models the real chunked-body
> terminator failure: the response context manager calls `stream.aclose()` on
> exit, and httpx's `MockTransport` propagates raises from the handler's
> stream `aclose`, so the envelope's `except httpx.HTTPError` branch is
> exercised after `finalize()` already produced the complete events. This is
> the only reliable injection point — raising in `__aiter__` cannot work for
> the tail case because the envelope stops reading at the terminal marker.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tau_ai.py -k "retryable or tail_read" -v`
Expected: FAIL — `AssistantErrorEvent` has no attribute `retryable` / status events not classified

- [ ] **Step 4: Implement the envelope changes** (`src/tau_ai/openai_compatible.py`)

Add imports:

```python
from tau_ai.classify import (
    is_context_overflow,
    is_retryable_http_failure,
    is_terminal_rate_limit,
    is_transient_status,
)
from tau_ai.stream import attach_tail_read_diagnostic  # add to the existing stream import
```

In `_should_retry`, make the status check body-aware:

```python
    def _should_retry(
        self, attempt: int, *, status_code: int | None = None, body: str = ""
    ) -> bool:
        if attempt >= self._config.max_retries:
            return False
        if status_code is None:
            return True
        return (
            is_transient_status(status_code)
            and not is_terminal_rate_limit(body)
            and not is_context_overflow(body)
        )
```

Delete the local `_is_transient_status` function (now shared).

In the `while True:` body of `_stream`'s iterator:

1. Declare the deferred-final-events holder before `try:`:

```python
            parser = parser_factory()
            final_events: list[ProviderEvent] | None = None
            try:
```

2. In the `status_code >= 400` branch, pass the body to `_should_retry` and classify the terminal error:

```python
                        if self._should_retry(
                            attempt,
                            status_code=response.status_code,
                            body=body_text,
                        ):
```

```python
                            yield ProviderErrorEvent(
                                message=provider_http_error_message(
                                    provider_name=self._config.provider_name,
                                    status_code=response.status_code,
                                    body=body_text,
                                    model=model,
                                ),
                                data={
                                    "status_code": response.status_code,
                                    "body": body_text,
                                    "attempts": attempt + 1,
                                },
                                retryable=is_retryable_http_failure(
                                    response.status_code, body_text
                                ),
                                response_provider=response_provider,
                            )
                            return
```

3. Capture the final events instead of yielding them inside the `with`, then yield them after a clean exit:

Replace:

```python
                        if parser.fatal:
                            return
                        final_events = parser.finalize()
                        observer = self._config.response_headers_observer
                        if observer is not None:
                            try:
                                observer(dict(response.headers))
                            except Exception as exc:
                                # Observer reporting is also best-effort; response
                                # completion must never depend on metadata hooks.
                                with suppress(Exception):
                                    _append_response_observer_diagnostic(final_events, exc)
                        for parser_event in final_events:
                            yield parser_event
                        return
```

with:

```python
                        if parser.fatal:
                            return
                        final_events = parser.finalize()
                        observer = self._config.response_headers_observer
                        if observer is not None:
                            try:
                                observer(dict(response.headers))
                            except Exception as exc:
                                # Observer reporting is also best-effort; response
                                # completion must never depend on metadata hooks.
                                with suppress(Exception):
                                    _append_response_observer_diagnostic(final_events, exc)
        # The response body ended cleanly; a transport error from here on is a
        # trailing-read failure after a complete response and must not fail the run.
        if final_events is not None:
            for parser_event in final_events:
                yield parser_event
        return
```

4. Replace the `except httpx.HTTPError` branch:

```python
                except httpx.HTTPError as exc:
                    if final_events is not None:
                        # Every chunk was parsed and finalized before the tail
                        # read failed: the response is complete.
                        for index, parser_event in enumerate(final_events):
                            if (
                                index == len(final_events) - 1
                                and isinstance(parser_event, ProviderResponseEndEvent)
                            ):
                                parser_event = attach_tail_read_diagnostic(parser_event, exc)
                            yield parser_event
                        return
                    if not parser.emitted_content and self._should_retry(attempt):
                        delay = retry_delay_seconds(
                            attempt,
                            max_delay_seconds=self._config.max_retry_delay_seconds,
                        )
                        yield provider_retry_event(
                            attempt=attempt,
                            max_retries=self._config.max_retries,
                            delay_seconds=delay,
                            reason="network error",
                            data={
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                            },
                        )
                        attempt += 1
                        if not await wait_for_retry(delay, signal=signal):
                            return
                        continue
                    yield ProviderErrorEvent(
                        message=str(exc),
                        data={
                            "attempts": attempt + 1,
                            "error_type": type(exc).__name__,
                        },
                        retryable=True,
                    )
                    return
```

> Note: the response-end event is always the last entry `parser.finalize()`
> produces in this adapter, so attaching the diagnostic to the final event is
> exact. Do not reorder `finalize` output.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_tau_ai.py -k "openai_compatible or retryable or tail_read" -q`
Expected: PASS (including the pre-existing `does_not_retry_after_partial_output` test)

- [ ] **Step 6: Run the checks**

Run: `uv run pytest tests/test_tau_ai.py -q && uv run ruff check src/tau_ai && uv run mypy src/tau_ai`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/tau_ai/openai_compatible.py src/tau_ai/stream.py tests/test_tau_ai.py
git commit -m "feat(openai): classify transient failures and complete tail reads"
```

---

## Task 6: Anthropic and Codex envelopes

**Files:**
- Modify: `src/tau_ai/anthropic.py`, `src/tau_ai/openai_codex.py`
- Test: `tests/test_tau_ai.py`

**Delta requirement:** Transient-failure classification (in-stream markers), Complete-response tail failure.

### Anthropic part

- [ ] **Step 1: Write the failing tests** (append to `tests/test_tau_ai.py`, modeled on the existing Anthropic stream tests; locate them with `rg -n "anthropic" tests/test_tau_ai.py | head`)

```python
@pytest.mark.anyio
async def test_anthropic_marks_mid_stream_transport_drop_retryable() -> None:
    """Prove an Anthropic transport drop after content is classified retryable."""
    requests: list[httpx.Request] = []

    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield (
                b'data: {"type":"content_block_delta","delta":'
                b'{"type":"text_delta","text":"partial"}}\n\n'
            )
            raise httpx.ReadError("stream dropped")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            stream=FailingStream(),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(
            AnthropicConfig(
                api_key="test-key",
                provider_name="anthropic",
                max_retries=2,
                max_retry_delay_seconds=0,
            ),
            client=client,
        )
        events = await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say ok")],
                tools=[],
            )
        )

    assert len(requests) == 1
    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].retryable is True


@pytest.mark.anyio
async def test_anthropic_in_stream_overload_after_content_is_retryable() -> None:
    """Prove an in-stream overload error after content is classified retryable."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=(
                'data: {"type":"content_block_delta","delta":'
                '{"type":"text_delta","text":"partial"}}\n\n'
                'data: {"type":"error","error":'
                '{"type":"overloaded_error","message":"Overloaded"}}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(
            AnthropicConfig(
                api_key="test-key",
                provider_name="anthropic",
                max_retries=2,
                max_retry_delay_seconds=0,
            ),
            client=client,
        )
        events = await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say ok")],
                tools=[],
            )
        )

    assert len(requests) == 1
    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].retryable is True


@pytest.mark.anyio
async def test_anthropic_in_stream_usage_marker_not_retryable() -> None:
    """Prove a rate-limit stream error with a usage marker stays terminal."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=(
                'data: {"type":"content_block_delta","delta":'
                '{"type":"text_delta","text":"partial"}}\n\n'
                'data: {"type":"error","error":'
                '{"type":"rate_limit_error","message":"monthly usage limit reached"}}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(
            AnthropicConfig(
                api_key="test-key",
                provider_name="anthropic",
                max_retries=2,
                max_retry_delay_seconds=0,
            ),
            client=client,
        )
        events = await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say ok")],
                tools=[],
            )
        )

    assert len(requests) == 1
    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].retryable is False


@pytest.mark.anyio
async def test_anthropic_tail_read_completes_with_diagnostic() -> None:
    """Prove an Anthropic drop after message_stop completes the message."""
    requests: list[httpx.Request] = []

    class TailDroppingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield (
                b'data: {"type":"content_block_delta","index":0,"delta":'
                b'{"type":"text_delta","text":"done"}}\n\n'
                b'data: {"type":"message_stop"}\n\n'
            )

        async def aclose(self) -> None:
            raise httpx.ReadError("tail dropped")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            stream=TailDroppingStream(),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(
            AnthropicConfig(
                api_key="test-key",
                provider_name="anthropic",
            ),
            client=client,
        )
        events = await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say ok")],
                tools=[],
            )
        )

    assert events[-1].type == "done"
    message = events[-1].message
    assert message.text == "done"
    assert any(
        diagnostic.type == "response_tail_read" for diagnostic in message.diagnostics or []
    )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_tau_ai.py -k "anthropic" -v`
Expected: FAIL — the in-stream error and drop tests show `error` ends without `retryable` (assertion error), and the tail test shows an error end (`ReadError: tail dropped`).

- [ ] **Step 3: Implement (`src/tau_ai/anthropic.py`)**

Add imports:

```python
from tau_ai.classify import (
    is_context_overflow,
    is_retryable_http_failure,
    is_terminal_rate_limit,
    is_transient_status,
)
from tau_ai.stream import attach_tail_read_diagnostic
```

Make `_should_retry` body-aware:

```python
    def _should_retry(
        self, attempt: int, *, status_code: int | None = None, body: str = ""
    ) -> bool:
        if attempt >= self._config.max_retries:
            return False
        if status_code is None:
            return True
        return (
            is_transient_status(status_code)
            and not is_terminal_rate_limit(body)
            and not is_context_overflow(body)
        )
```

In the stream loop:

1. Pass the body to `_should_retry` in the status branch and classify the terminal error:

```python
                            if self._should_retry(
                                attempt,
                                status_code=response.status_code,
                                body=body_text,
                            ):
```

```python
                            yield ProviderErrorEvent(
                                message=provider_http_error_message(
                                    provider_name=self._config.provider_name,
                                    status_code=response.status_code,
                                    body=body_text,
                                    model=model,
                                ),
                                data={
                                    "status_code": response.status_code,
                                    "body": body_text,
                                    "attempts": attempt + 1,
                                },
                                retryable=is_retryable_http_failure(
                                    response.status_code, body_text
                                ),
                            )
                            return
```

2. Break on the protocol's terminal marker so a tail read can be distinguished.
   Add a new `elif` in the chunk-type dispatch (before the `error` branch):

```python
                            elif event_type == "message_stop":
                                break
```

3. Classify the in-stream `error` branch. Replace:

```python
                            elif event_type == "error":
                                error_type, message = _anthropic_stream_error_details(chunk)
                                if (
                                    not emitted_content
                                    and self._should_retry(attempt)
                                    and _retryable_anthropic_stream_error(error_type)
                                ):
                                    stream_error = chunk
                                    break
                                yield ProviderErrorEvent(
                                    message=message,
                                    data={"event": chunk, "attempts": attempt + 1},
                                )
                                return
```

with:

```python
                            elif event_type == "error":
                                error_type, message = _anthropic_stream_error_details(chunk)
                                if (
                                    not emitted_content
                                    and self._should_retry(attempt)
                                    and _retryable_anthropic_stream_error(error_type)
                                ):
                                    stream_error = chunk
                                    break
                                yield ProviderErrorEvent(
                                    message=message,
                                    data={"event": chunk, "attempts": attempt + 1},
                                    retryable=_retryable_anthropic_stream_error(error_type)
                                    and not is_context_overflow(message)
                                    and not is_terminal_rate_limit(message),
                                )
                                return
```

4. Defer the terminal yields until after a clean exit. Declare before `try:`:

```python
            attempt = 0
            terminal_payload: tuple[list[ProviderEvent], ProviderResponseEndEvent] | None = None
            while True:
```

Replace the final assembly block (after the `stream_error is not None:` retry block) from:

```python
                        tool_calls = [
                            builder.build(index) for index, builder in sorted(tool_builders.items())
                        ]
                        for tool_call in tool_calls:
                            yield ProviderToolCallEvent(tool_call=tool_call)

                        content = assistant_content("".join(content_parts), tool_calls)
                        if thinking_parts:
                            content.insert(
                                0,
                                ThinkingContent(
                                    thinking="".join(thinking_parts),
                                    thinking_signature=thinking_signature,
                                ),
                            )
                        yield ProviderResponseEndEvent(
                            message=AssistantMessage(
                                content=content,
                                usage=usage or Usage(),
                            ),
                            finish_reason=finish_reason,
                        )
                        return
```

to:

```python
                        tool_calls = [
                            builder.build(index) for index, builder in sorted(tool_builders.items())
                        ]
                        content = assistant_content("".join(content_parts), tool_calls)
                        if thinking_parts:
                            content.insert(
                                0,
                                ThinkingContent(
                                    thinking="".join(thinking_parts),
                                    thinking_signature=thinking_signature,
                                ),
                            )
                        terminal_payload = (
                            [ProviderToolCallEvent(tool_call) for tool_call in tool_calls],
                            ProviderResponseEndEvent(
                                message=AssistantMessage(
                                    content=content,
                                    usage=usage or Usage(),
                                ),
                                finish_reason=finish_reason,
                            ),
                        )
        if terminal_payload is not None:
            for tool_event, end_event in (terminal_payload,):
                for tool_call_event in tool_event:
                    yield tool_call_event
                yield end_event
        return
```

> The `for tool_event, end_event in (terminal_payload,):` unpacking is unusual;
> replace it with the plain form: `for tool_call_event in terminal_payload[0]: yield tool_call_event` then `yield terminal_payload[1]`. Keep the code simple.

5. Replace the `except httpx.HTTPError` branch:

```python
                except httpx.HTTPError as exc:
                    if terminal_payload is not None:
                        for tool_call_event in terminal_payload[0]:
                            yield tool_call_event
                        yield attach_tail_read_diagnostic(terminal_payload[1], exc)
                        return
                    if not emitted_content and self._should_retry(attempt):
                        delay = retry_delay_seconds(
                            attempt,
                            max_delay_seconds=self._config.max_retry_delay_seconds,
                        )
                        yield provider_retry_event(
                            attempt=attempt,
                            max_retries=self._config.max_retries,
                            delay_seconds=delay,
                            reason="network error",
                            data={
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                            },
                        )
                        attempt += 1
                        if not await wait_for_retry(delay, signal=signal):
                            return
                        continue
                    yield ProviderErrorEvent(
                        message=str(exc),
                        data={
                            "attempts": attempt + 1,
                            "error_type": type(exc).__name__,
                        },
                        retryable=True,
                    )
                    return
```

- [ ] **Step 4: Run the Anthropic tests**

Run: `uv run pytest tests/test_tau_ai.py -k "anthropic" -q`
Expected: PASS (including pre-existing Anthropic tests)

### Codex part

- [ ] **Step 5: Write the failing Codex tests** (model the SSE payloads on the existing Codex tests around `rg -n "openai_codex" tests/test_tau_ai.py | head`, e.g. `test_openai_codex_provider_surfaces_stream_error_after_retry_exhaustion`)

```python
@pytest.mark.anyio
async def test_openai_codex_in_stream_overload_after_content_is_retryable() -> None:
    """Prove a Codex in-stream overload error after content is classified retryable."""
    requests: list[httpx.Request] = []

    async def credentials() -> OpenAICodexCredentials:
        return OpenAICodexCredentials(access_token="access-token", account_id="account-1")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = (
            'data: {"type":"response.output_text.delta","delta":"partial","item_id":"i1",'
            '"output_index":0,"sequence_number":1}\n\n'
            'data: {"type":"error","error":{"type":"service_unavailable_error",'
            '"code":"server_is_overloaded","message":"Our servers are currently overloaded."},'
            '"sequence_number":2}\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICodexProvider(
            OpenAICodexConfig(
                credential_resolver=credentials,
                base_url="https://chatgpt.test/backend-api",
                provider_name="openai-codex",
                max_retries=2,
                max_retry_delay_seconds=0,
            ),
            client=client,
        )
        events = await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say ok")],
                tools=[],
            )
        )

    assert len(requests) == 1
    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].retryable is True


@pytest.mark.anyio
async def test_openai_codex_tail_read_completes_with_diagnostic() -> None:
    """Prove a Codex drop after response.completed completes the message."""
    requests: list[httpx.Request] = []

    async def credentials() -> OpenAICodexCredentials:
        return OpenAICodexCredentials(access_token="access-token", account_id="account-1")

    class TailDroppingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield (
                b'data: {"type":"response.output_text.delta","delta":"done","item_id":"i1",'
                b'"output_index":0,"sequence_number":1}\n\n'
                b'data: {"type":"response.completed","response":{"id":"r1",'
                b'"status":"completed","output":[]}}\n\n'
            )

        async def aclose(self) -> None:
            raise httpx.ReadError("tail dropped")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            stream=TailDroppingStream(),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICodexProvider(
            OpenAICodexConfig(
                credential_resolver=credentials,
                base_url="https://chatgpt.test/backend-api",
                provider_name="openai-codex",
            ),
            client=client,
        )
        events = await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say ok")],
                tools=[],
            )
        )

    assert events[-1].type == "done"
    message = events[-1].message
    assert "done" in message.text
    assert any(
        diagnostic.type == "response_tail_read" for diagnostic in message.diagnostics or []
    )
```

> The credential resolver and `base_url`/`provider_name` mirror the existing
> Codex tests verbatim (e.g. `test_openai_codex_provider_surfaces_stream_error_after_retry_exhaustion`
> at `tests/test_tau_ai.py:1354`, which also defines the module-level
> `_CODEX_OVERLOAD_ERROR_SSE` fixture). The `response.completed` payload shape
> must match what the existing Codex tests feed — mirror their fixtures exactly
> and add only the tail-drop stream.

- [ ] **Step 6: Run them to verify they fail**

Run: `uv run pytest tests/test_tau_ai.py -k "codex" -v`
Expected: FAIL — `retryable` missing on the in-stream error; tail test shows an error end.

- [ ] **Step 7: Implement (`src/tau_ai/openai_codex.py`)**

Add imports:

```python
from tau_ai.classify import (
    is_context_overflow,
    is_retryable_http_failure,
    is_terminal_rate_limit,
)
from tau_ai.stream import attach_tail_read_diagnostic
```

Delete the local `_is_terminal_rate_limit` function and replace `_is_retryable_status` usages: `_should_retry` keeps its signature but its status check becomes:

```python
        return status_code is None or is_retryable_http_failure(status_code, body)
```

1. In the status branch, the exhausted terminal error gains the classification:

```python
                            yield ProviderErrorEvent(
                                message=provider_http_error_message(
                                    provider_name=self._config.provider_name,
                                    status_code=response.status_code,
                                    body=body_text,
                                    model=model,
                                ),
                                data={
                                    "status_code": response.status_code,
                                    "body": body_text,
                                    "attempts": attempt + 1,
                                },
                                retryable=is_retryable_http_failure(
                                    response.status_code, body_text
                                ),
                            )
                            return
```

2. Buffer the response-end event until the stream fully ends. Declare before `try:`:

```python
            attempt = 0
            final_event: ProviderResponseEndEvent | None = None
            while True:
```

In the event loop, replace:

```python
                        async for event in _codex_provider_events(response, signal=signal):
                            if isinstance(
                                event,
                                ProviderTextDeltaEvent | ProviderToolCallEvent,
                            ):
                                emitted_content = True
                            elif isinstance(event, ProviderThinkingDeltaEvent):
                                emitted_thinking = True
                            if (
                                isinstance(event, ProviderErrorEvent)
                                and not emitted_content
                                and not emitted_thinking
                                and self._should_retry(attempt)
                                and _retryable_stream_error_event(event)
                            ):
                                stream_error = _stream_error_event_data(event)
                                break
                            yield event
                        if stream_error is None:
                            return
```

with:

```python
                        async for event in _codex_provider_events(response, signal=signal):
                            if isinstance(event, ProviderResponseEndEvent):
                                final_event = event
                                continue
                            if isinstance(
                                event,
                                ProviderTextDeltaEvent | ProviderToolCallEvent,
                            ):
                                emitted_content = True
                            elif isinstance(event, ProviderThinkingDeltaEvent):
                                emitted_thinking = True
                            if (
                                isinstance(event, ProviderErrorEvent)
                                and not emitted_content
                                and not emitted_thinking
                                and self._should_retry(attempt)
                                and _retryable_stream_error_event(event)
                            ):
                                stream_error = _stream_error_event_data(event)
                                break
                            yield event
                        if stream_error is None:
                            if final_event is not None:
                                yield final_event
                            return
```

> Simplify: the flag is set where the error events are produced, inside
> `_codex_provider_events`; do not reclassify buffered events in the envelope.

3. Classify the in-stream error events at their production sites. In `_codex_provider_events`, replace the two `yield ProviderErrorEvent(...)` calls (`event_type == "error"` and `event_type == "response.failed"`) with:

```python
        if event_type == "error":
            produced = ProviderErrorEvent(
                message=_error_message(event, fallback="OpenAI Codex returned an error"),
                data={"event": event},
            )
            yield _classify_stream_error(produced)
            return

        if event_type == "response.failed":
            produced = ProviderErrorEvent(
                message=_response_error_message(event),
                data={"event": event},
            )
            yield _classify_stream_error(produced)
            return
```

Add the classifier next to `_retryable_stream_error_event`:

```python
def _classify_stream_error(event: ProviderErrorEvent) -> ProviderErrorEvent:
    """Return an in-stream Codex error event with its retryable classification.

    The transient marker match decides retryability; context-overflow messages
    stay terminal even when a marker matches.
    """
    retryable = _retryable_stream_error_event(event)
    if retryable:
        code, message = _stream_error_details(_stream_error_event_data(event) or {})
        if is_context_overflow(" ".join(part for part in (code, message) if part)):
            retryable = False
    return event.model_copy(update={"retryable": retryable})
```

(`_error_message`, `_retryable_stream_error_event`, `_stream_error_event_data`, and
`_stream_error_details` already exist in this module.)


3. Replace the transport `except httpx.HTTPError` branch:

```python
                except httpx.HTTPError as exc:
                    if final_event is not None:
                        yield attach_tail_read_diagnostic(final_event, exc)
                        return
                    if not emitted_content and self._should_retry(attempt):
                        delay = retry_delay_seconds(
                            attempt,
                            max_delay_seconds=self._config.max_retry_delay_seconds,
                        )
                        yield provider_retry_event(
                            attempt=attempt,
                            max_retries=self._config.max_retries,
                            delay_seconds=delay,
                            reason="network error",
                            data={
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                            },
                        )
                        attempt += 1
                        if not await wait_for_retry(delay, signal=signal):
                            return
                        continue
                    yield ProviderErrorEvent(
                        message=str(exc),
                        data={
                            "attempts": attempt + 1,
                            "error_type": type(exc).__name__,
                        },
                        retryable=True,
                    )
                    return
```

- [ ] **Step 8: Run the Codex tests**

Run: `uv run pytest tests/test_tau_ai.py -k "codex" -q`
Expected: PASS (including pre-existing Codex tests)

- [ ] **Step 9: Full checks + commit**

Run: `uv run pytest tests/test_tau_ai.py -q && uv run ruff check src/tau_ai && uv run mypy src/tau_ai`

```bash
git add src/tau_ai/anthropic.py src/tau_ai/openai_codex.py tests/test_tau_ai.py
git commit -m "feat(providers): classify anthropic and codex transient failures"
```

---

## Task 7: Google and Mistral envelopes

**Files:**
- Modify: `src/tau_ai/google.py`, `src/tau_ai/mistral.py`
- Test: `tests/test_tau_ai.py`

**Delta requirement:** Transient-failure classification, Complete-response tail failure.

- [ ] **Step 1: Write the failing tests** (model on the openai-compatible tests from Task 5; both adapters use `FailingStream`/`TailDroppingStream` shapes)

For google (the real classes are `GoogleGenerativeAIProvider`/`OpenAICompatibleConfig`; model the construction on the existing Google test at `tests/test_tau_ai.py:544`, which uses `base_url="https://generativelanguage.googleapis.com/v1beta"`):

```python
@pytest.mark.anyio
async def test_google_marks_mid_stream_transport_drop_retryable() -> None:
    """Prove a Google transport drop after content is classified retryable."""
    requests: list[httpx.Request] = []

    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield (
                b'data: {"candidates":[{"content":{"parts":[{"text":"partial"}]},'
                b'"finishReason":"STOP"}]}\n\n'
            )
            raise httpx.ReadError("stream dropped")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            stream=FailingStream(),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGenerativeAIProvider(
            OpenAICompatibleConfig(
                api_key="test-key",
                base_url="https://generativelanguage.googleapis.com/v1beta",
                max_retries=2,
                max_retry_delay_seconds=0,
            ),
            client=client,
        )
        events = await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say ok")],
                tools=[],
            )
        )

    assert len(requests) == 1
    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].retryable is True


@pytest.mark.anyio
async def test_google_tail_read_completes_with_diagnostic() -> None:
    """Prove a Google drop after the last chunk completes the message."""
    requests: list[httpx.Request] = []

    class TailDroppingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield (
                b'data: {"candidates":[{"content":{"parts":[{"text":"done"}]},'
                b'"finishReason":"STOP"}]}\n\n'
            )

        async def aclose(self) -> None:
            raise httpx.ReadError("tail dropped")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            stream=TailDroppingStream(),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGenerativeAIProvider(
            OpenAICompatibleConfig(
                api_key="test-key",
                base_url="https://generativelanguage.googleapis.com/v1beta",
            ),
            client=client,
        )
        events = await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say ok")],
                tools=[],
            )
        )

    assert events[-1].type == "done"
    assert any(
        diagnostic.type == "response_tail_read"
        for diagnostic in events[-1].message.diagnostics or []
    )
```

For mistral, the real classes are `MistralConversationsProvider`/`OpenAICompatibleConfig` (the adapter appends `/v1` to the base URL itself). There are NO existing mistral tests in the suite — the SSE shape is the same chat-completions contract as the openai-compatible parser (`choices[].delta.content` chunks plus a `data: [DONE]` terminator), which is exactly what `_MistralStreamParser.feed` consumes. Use this test:

```python
@pytest.mark.anyio
async def test_mistral_marks_mid_stream_drop_retryable() -> None:
    """Prove a Mistral transport drop after content is classified retryable."""
    requests: list[httpx.Request] = []

    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError("stream dropped")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            stream=FailingStream(),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = MistralConversationsProvider(
            OpenAICompatibleConfig(
                api_key="test-key",
                base_url="https://api.mistral.ai",
                max_retries=2,
                max_retry_delay_seconds=0,
            ),
            client=client,
        )
        events = await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say ok")],
                tools=[],
            )
        )

    assert len(requests) == 1
    assert isinstance(events[-1], AssistantErrorEvent)
    assert events[-1].retryable is True
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_tau_ai.py -k "google or mistral" -v`
Expected: FAIL — `retryable` assertions fail on the drop tests; tail test shows an error end.

- [ ] **Step 3: Implement `src/tau_ai/google.py`**

Apply the same changes as Task 5 (openai-compatible), adapted to Google's structure:

1. Imports: `is_context_overflow, is_retryable_http_failure, is_terminal_rate_limit, is_transient_status` from `tau_ai.classify`; `attach_tail_read_diagnostic` from `tau_ai.stream`.
2. `_should_retry` gains `body: str = ""` and the body-aware checks.
3. The status branch passes `body=body_text` and the terminal error gains `retryable=is_retryable_http_failure(response.status_code, body_text)`.
4. Declare `final_events: list[ProviderEvent] | None = None` before `try:`; inside the `with`, replace the final two lines `for parser_event in parser.finalize(): yield parser_event` and `return` with:

```python
                        final_events = parser.finalize()
        if final_events is not None:
            for parser_event in final_events:
                yield parser_event
        return
```

5. The `except httpx.HTTPError` branch gains the tail-read check first, then the existing retry, then the classified terminal error:

```python
                except httpx.HTTPError as exc:
                    if final_events is not None:
                        for index, parser_event in enumerate(final_events):
                            if (
                                index == len(final_events) - 1
                                and isinstance(parser_event, ProviderResponseEndEvent)
                            ):
                                parser_event = attach_tail_read_diagnostic(parser_event, exc)
                            yield parser_event
                        return
                    if not parser.emitted_content and self._should_retry(attempt):
                        delay = retry_delay_seconds(
                            attempt,
                            max_delay_seconds=self._config.max_retry_delay_seconds,
                        )
                        yield provider_retry_event(
                            attempt=attempt,
                            max_retries=self._config.max_retries,
                            delay_seconds=delay,
                            reason="network error",
                            data={
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                            },
                        )
                        attempt += 1
                        if not await wait_for_retry(delay, signal=signal):
                            return
                        continue
                    yield ProviderErrorEvent(
                        message=str(exc),
                        data={
                            "attempts": attempt + 1,
                            "error_type": type(exc).__name__,
                        },
                        retryable=True,
                    )
                    return
```

- [ ] **Step 4: Implement `src/tau_ai/mistral.py`**

Same five changes, adapted to Mistral's structure (it has a `_StreamParser` protocol and its own `feed`/`finalize`; the deferral pattern is identical to google's).

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_tau_ai.py -k "google or mistral" -q`
Expected: PASS (including pre-existing tests)

- [ ] **Step 6: Full checks + commit**

Run: `uv run pytest tests/test_tau_ai.py -q && uv run ruff check src/tau_ai && uv run mypy src/tau_ai`

```bash
git add src/tau_ai/google.py src/tau_ai/mistral.py tests/test_tau_ai.py
git commit -m "feat(providers): classify google and mistral transient failures"
```

---

## Task 8: Retry diagnostics in the session log

**Files:**
- Modify: `src/tau_coding/diagnostics.py`, `src/tau_coding/session.py`
- Test: `tests/test_coding_session.py`

**Delta requirement:** Retry diagnostics (both scenarios), Cancellation during retry (diagnostics entry).

- [ ] **Step 1: Write the failing session tests** (append to `tests/test_coding_session.py`; model the setup on `test_prompt_logs_safe_provider_stream_error_details` at line ~342 — same storage/paths/config construction; import `retryable_error` from `pi_event_helpers`)

```python
async def test_prompt_logs_turn_retry_diagnostics(tmp_path: Path) -> None:
    """Prove each reattempt logs one retry entry and no terminal error entry."""
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    tau_paths = TauPaths(home=tmp_path / "tau-home", agents_home=tmp_path / "agents-home")
    recovered = AssistantMessage(content="recovered", model="fake")
    provider = FakeProvider(
        [
            [assistant_start(), text_delta("partial"), retryable_error("peer closed connection")],
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
    assert retry_entries[0]["retry"]["reason"] == "provider error"
    assert retry_entries[0]["retry"]["error_message"] == "peer closed connection"
    assert retry_entries[0]["provider_name"] == "openai"
    assert error_entries == []


async def test_prompt_logs_exhausted_retries_and_terminal_error(tmp_path: Path) -> None:
    """Prove exhausted retries log each reattempt plus exactly one terminal error."""
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    tau_paths = TauPaths(home=tmp_path / "tau-home", agents_home=tmp_path / "agents-home")
    provider = FakeProvider(
        [
            [assistant_start(), retryable_error("drop 1")],
            [assistant_start(), retryable_error("drop 2")],
            [assistant_start(), retryable_error("drop 3")],
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
```

Append the one-shot scope test after it:

```python
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

> The auto-naming call consumes the first fake stream — a transient-looking
> error — and the harness consumes the second. Because retry logic lives only
> in the harness loop, the one-shot call is attempted exactly once. If the
> session's auto-naming path already swallows failed names silently, the test
> still holds: provider call count proves no retry, and the log proves no
> `assistant_retry` entry.

> Note: the `CodingSession` harness uses `max_turn_retries` from its harness
> config. Until Task 9 lands, the harness default (2) applies — these tests
> exercise that default. If `session.prompt` performs auto-compaction or
> auto-naming provider calls, disable them for the test (check how existing
> `stream_error` tests keep those quiet — they pass `auto_compact_enabled=False`
> if needed and avoid auto-naming by setting a session title or opting out; copy
> the existing tests' knobs).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_coding_session.py -k "turn_retry" -v`
Expected: FAIL — no `assistant_retry` entries in the log.

- [ ] **Step 3: Add the log method** (`src/tau_coding/diagnostics.py`), after `log_assistant_error`:

```python
    def log_turn_retry(
        self,
        *,
        context: AgentCallDiagnosticContext,
        attempt: int,
        max_attempts: int,
        reason: str,
        error_message: str,
        error_type: str | None,
    ) -> Path:
        """Log one harness turn-level retry for a transient provider failure."""
        entry = _base_entry(context, phase="agent_loop", kind="assistant_retry")
        retry: dict[str, Any] = {
            "attempt": attempt,
            "max_attempts": max_attempts,
            "reason": reason,
            "error_message": error_message,
        }
        if error_type:
            retry["error_type"] = error_type
        entry["retry"] = retry
        self._append(entry)
        return self.path
```

- [ ] **Step 4: Wire the session loops** (`src/tau_coding/session.py`)

Add `TurnRetryStartEvent` to the `tau_agent.events` import list in session.py.

In `prompt()`, inside the main `async for event in events:` loop, add a branch (next to the existing error-logging branch):

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

In `continue_()`, add the same branch inside its `async for event in events:`.
In the overflow-retry inner loop of `prompt()` (the `async for retry_event in events:` block after `CompactionEndEvent`), add the same branch too.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_coding_session.py -k "turn_retry or stream_error or compacts" -q`
Expected: PASS

- [ ] **Step 6: Checks + commit**

Run: `uv run pytest tests/test_coding_session.py -q && uv run ruff check src/tau_coding && uv run mypy src/tau_coding`

```bash
git add src/tau_coding/diagnostics.py src/tau_coding/session.py tests/test_coding_session.py
git commit -m "feat(session): log turn-level retries in agent-calls diagnostics"
```

---

## Task 9: Per-provider turn-retry budget configuration

**Files:**
- Modify: `src/tau_coding/provider_config.py`, `src/tau_coding/cli.py`, `src/tau_coding/session.py`
- Test: `tests/test_provider_config.py`, `tests/test_cli.py`, `tests/test_coding_session.py`

**Delta requirement:** Retry budget configuration (all scenarios), Setup interface persists the budget.

- [ ] **Step 1: Write the failing config tests**

In `tests/test_provider_config.py`, add (model on the existing provider-config tests — locate the max_retries validation tests with `rg -n "max_retries" tests/test_provider_config.py`):

```python
def test_provider_config_turn_retry_max_defaults_to_two() -> None:
    """Prove the per-provider turn-retry budget defaults to the spec default."""
    assert OpenAICompatibleProviderConfig(name="test").turn_retry_max == DEFAULT_TURN_RETRIES


def test_provider_config_rejects_negative_turn_retry_max() -> None:
    """Prove a negative turn-retry budget is rejected at validation."""
    with pytest.raises(ProviderConfigError):
        OpenAICompatibleProviderConfig(name="test", turn_retry_max=-1)


def test_provider_config_serializes_turn_retry_max() -> None:
    """Prove the turn-retry budget round-trips through the settings JSON shape."""
    provider = OpenAICompatibleProviderConfig(name="test", turn_retry_max=4)

    assert provider.to_json()["turn_retry_max"] == 4
```

In `tests/test_cli.py`, add (modeled exactly on `test_setup_command_writes_provider_settings` at line ~1604):

```python
def test_setup_command_persists_turn_retry_max(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Prove `tau setup` persists the turn-retry budget with the provider."""
    isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCAL_API_KEY", "test-key")

    result = CliRunner().invoke(
        app,
        [
            "--provider",
            "local",
            "--base-url",
            "http://localhost:11434/v1/",
            "--api-key-env",
            "LOCAL_API_KEY",
            "--turn-retry-max",
            "5",
            "--model",
            "qwen",
            "setup",
        ],
    )

    settings = load_provider_settings(TauPaths(home=tmp_path / ".tau"))
    provider = settings.get_provider("local")
    assert result.exit_code == 0
    assert provider.turn_retry_max == 5
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_provider_config.py -k "turn_retry" tests/test_cli.py -k setup -v`
Expected: FAIL — `turn_retry_max` field missing / unexpected keyword.

- [ ] **Step 3: Add the field and validation** (`src/tau_coding/provider_config.py`)

1. Import the harness default (alongside the other `tau_agent` imports):

```python
from tau_agent.harness import DEFAULT_TURN_RETRIES
```

2. Add the field to each of the three provider models (`OpenAICompatibleProviderConfig`, `AnthropicProviderConfig`, `OpenAICodexProviderConfig`), right after `max_retry_delay_seconds`:

```python
    max_retries: int = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES
    max_retry_delay_seconds: float = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS
    turn_retry_max: int = DEFAULT_TURN_RETRIES
```

3. Extend `_validate_provider_numbers` (at line ~2093) with the new parameter and check:

```python
def _validate_provider_numbers(
    *,
    timeout_seconds: float,
    max_retries: int,
    max_retry_delay_seconds: float,
    turn_retry_max: int,
) -> None:
    if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ProviderConfigError("Provider timeout_seconds must be greater than 0")
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
        raise ProviderConfigError("Provider max_retries must be 0 or greater")
    if (
        not isinstance(max_retry_delay_seconds, int | float)
        or isinstance(max_retry_delay_seconds, bool)
        or max_retry_delay_seconds < 0
    ):
        raise ProviderConfigError("Provider max_retry_delay_seconds must be 0 or greater")
    if not isinstance(turn_retry_max, int) or isinstance(turn_retry_max, bool) or turn_retry_max < 0:
        raise ProviderConfigError("Provider turn_retry_max must be 0 or greater")
```

(Keep the rest of the existing validation body intact — read it first and only add the new parameter/check.)

4. Update every `_validate_provider_numbers(...)` call site in the three `__post_init__` methods to pass `turn_retry_max=self.turn_retry_max`.

5. Add `"turn_retry_max": self.turn_retry_max,` to the `to_json()` dict of all three models (after `max_retry_delay_seconds`).

6. Preserve existing values in the three merge helpers (`_merge_openai_compatible_provider`, `_merge_anthropic_provider`, and the inline `OpenAICodexProviderConfig` merge): add `turn_retry_max=existing.turn_retry_max` to each `replace(...)` call.
7. Make `turn_retry_max` survive the settings round-trip, mirroring exactly how `max_retries` travels:
   - `_provider_preference_to_json` (line ~1053): add `"turn_retry_max": provider.turn_retry_max,` next to `"max_retries"`.
   - `_apply_provider_preference` (line ~1251): when `"turn_retry_max" in value`, parse it with `_non_negative_int(value.get("turn_retry_max"), f"provider_preferences.{provider.name}.turn_retry_max")`, and pass `turn_retry_max=turn_retry_max` into both `replace(...)` constructions in the function.
   - `_provider_from_json` (line ~1940): read `turn_retry_max = _non_negative_int(data.get("turn_retry_max", DEFAULT_TURN_RETRIES), f"providers[{name}].turn_retry_max")` and pass it into every provider-config construction branch (anthropic, openai-codex, openai-compatible, google-generative-ai, mistral-conversations).
   Without these, `load_provider_settings` silently drops the budget back to the default and the setup round-trip test fails.

- [ ] **Step 4: Wire the harness budget in the session** (`src/tau_coding/session.py`)

1. At the harness construction site (line ~487), pass the budget:

```python
        harness = AgentHarness(
            AgentHarnessConfig(
                provider=config.provider,
                model=active_model,
                system=system,
                tools=tools,
                session_id=config.session_id,
                max_turn_retries=(
                    config.runtime_provider_config.turn_retry_max
                    if config.runtime_provider_config is not None
                    else DEFAULT_TURN_RETRIES
                ),
            ),
            messages=state.messages,
        )
```

(Import `DEFAULT_TURN_RETRIES` from `tau_agent.harness` in session.py.)

2. In `_activate_runtime_provider` (line ~1381), keep the harness budget in sync with the active provider:

```python
        self._harness.config.max_turn_retries = provider_config.turn_retry_max
```

3. The direct `self._harness.config.provider = provider` assignment inside `set_model`/`set_provider` (line ~1199 area) also sets the budget — add the same line right after it wherever `_runtime_provider_config` is set, or route that path through `_activate_runtime_provider` if it already does (verify with a grep and keep the change minimal).

- [ ] **Step 5: Add the setup CLI flag** (`src/tau_coding/cli.py`)

1. `setup_command` gains the parameter and passes it through:

```python
    max_retry_delay_seconds: float = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS,
    turn_retry_max: int = DEFAULT_TURN_RETRIES,
    set_default: bool = True,
```

and in the `OpenAICompatibleProviderConfig(...)` construction add `turn_retry_max=turn_retry_max,` (plus the `from tau_agent.harness import DEFAULT_TURN_RETRIES` import).

2. The typer option (next to `setup_max_retry_delay_seconds`):

```python
    setup_turn_retry_max: Annotated[
        int,
        typer.Option("--turn-retry-max", help="Turn-level retry count for `tau setup`."),
    ] = DEFAULT_TURN_RETRIES,
```

3. The `setup_command(...)` call site passes `turn_retry_max=setup_turn_retry_max,`.

- [ ] **Step 6: Add the session-level budget test** (append to `tests/test_coding_session.py`)

```python
async def test_session_uses_provider_turn_retry_budget(tmp_path: Path) -> None:
    """Prove the session harness honors the active provider's retry budget."""
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    tau_paths = TauPaths(home=tmp_path / "tau-home", agents_home=tmp_path / "agents-home")
    recovered = AssistantMessage(content="recovered", model="fake")
    provider = FakeProvider(
        [
            [assistant_start(), retryable_error("drop")],
            [assistant_start(), retryable_error("drop again")],
            [assistant_start(), assistant_done(recovered)],
        ]
    )
    runtime_config = OpenAICompatibleProviderConfig(
        name="test",
        turn_retry_max=2,
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
            runtime_provider_config=runtime_config,
            resource_paths=TauResourcePaths(root=tau_paths.home, paths=tau_paths),
        )
    )

    await _collect_session_events(session.prompt("Hello"))

    assert len(provider.calls) == 3
    assert session.messages[-1].text == "recovered"
```

> `CodingSession` has no public `harness` property, so the assertion is
> behavioral: two retries happened (three provider calls) and the turn ended
> with the recovered message.

Append a zero-budget companion right after it:

```python
async def test_session_zero_turn_retry_budget_disables_retries(tmp_path: Path) -> None:
    """Prove a provider with a zero budget ends immediately on a retryable error."""
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    tau_paths = TauPaths(home=tmp_path / "tau-home", agents_home=tmp_path / "agents-home")
    provider = FakeProvider(
        [
            [assistant_start(), retryable_error("drop")],
            [assistant_start(), assistant_done(AssistantMessage(content="never", model="fake"))],
        ]
    )
    runtime_config = OpenAICompatibleProviderConfig(
        name="test",
        turn_retry_max=0,
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
            runtime_provider_config=runtime_config,
            resource_paths=TauResourcePaths(root=tau_paths.home, paths=tau_paths),
        )
    )

    await _collect_session_events(session.prompt("Hello"))

    assert len(provider.calls) == 1
```

(If `CodingSessionConfig.runtime_provider_config` typing is narrower, cast to the union type the config accepts; verify with the type checker.)

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_provider_config.py tests/test_cli.py tests/test_coding_session.py -q`
Expected: PASS

- [ ] **Step 8: Full checks + commit**

Run: `uv run pytest tests/test_provider_config.py tests/test_cli.py tests/test_coding_session.py -q && uv run ruff check src/tau_coding && uv run mypy src/tau_coding`

```bash
git add src/tau_coding/provider_config.py src/tau_coding/cli.py src/tau_coding/session.py tests/test_provider_config.py tests/test_cli.py tests/test_coding_session.py
git commit -m "feat(config): per-provider turn-retry budget"
```

---

## Task 10: TUI rollback and retry notice

**Files:**
- Modify: `src/tau_coding/tui/adapter.py`, `src/tau_coding/tui/state.py`, `src/tau_coding/tui/app.py`, `src/tau_coding/tui/widgets.py`
- Test: `tests/test_tui_adapter.py`, `tests/test_tui_app.py`

**Delta requirement:** Transcript rollback on retry (both scenarios), Terminal failure projection unchanged.

- [ ] **Step 1: Write the failing adapter test** (append to `tests/test_tui_adapter.py`; model on existing adapter tests, import `TurnRetryStartEvent` from `tau_agent`, `retryable_error` from `pi_event_helpers`, and the `assistant_start`/`text_delta` builders)

```python
def test_adapter_rolls_back_partial_assistant_on_retry_start() -> None:
    """Prove the retry notice replaces the failed attempt's partial state."""
    state = TuiState()
    adapter = TuiEventAdapter(state)
    adapter.apply(assistant_start())
    adapter.apply(MessageUpdateEvent(
        message=AssistantMessage(content="partial"),
        assistant_message_event=text_delta("partial"),
    ))

    adapter.apply(
        TurnRetryStartEvent(
            attempt=2,
            max_attempts=3,
            delay_seconds=0.25,
            reason="network error (RemoteProtocolError)",
            error_message="peer closed connection",
        )
    )

    assert state.assistant_buffer == ""
    assert all(item.role != "assistant" for item in state.items)
    assert state.items[-1].role == "status"
    assert "2/3" in state.items[-1].text


def test_adapter_discards_retry_notice_when_reattempt_starts() -> None:
    """Prove the notice leaves the state once the reattempt starts streaming."""
    state = TuiState()
    adapter = TuiEventAdapter(state)
    adapter.apply(assistant_start())
    adapter.apply(MessageUpdateEvent(
        message=AssistantMessage(content="partial"),
        assistant_message_event=text_delta("partial"),
    ))
    adapter.apply(
        TurnRetryStartEvent(
            attempt=2, max_attempts=3, delay_seconds=0.25,
            reason="network error", error_message="peer closed connection",
        )
    )

    adapter.apply(assistant_start())

    assert all(item.role != "status" for item in state.items)
    assert state.assistant_buffer == ""
```

(The exact `TuiState`/`TuiEventAdapter` import names and constructor usage must match existing tests in the file — adjust if they differ.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_tui_adapter.py -v`
Expected: FAIL — adapter does not handle `TurnRetryStartEvent`.

- [ ] **Step 3: Add the notice formatter** (`src/tau_coding/tui/state.py`)

```python
def format_retry_notice(attempt: int, max_attempts: int, reason: str) -> str:
    """Return the transient notice text shown while a turn retry is pending."""
    return f"… Connection lost — retrying {attempt}/{max_attempts}: {reason}"
```

- [ ] **Step 4: Handle the event in the adapter** (`src/tau_coding/tui/adapter.py`)

Add `TurnRetryStartEvent` to the `tau_agent.events` import and `format_retry_notice` to the state import. Add the handler before the `MessageStartEvent` branch:

```python
        if isinstance(event, TurnRetryStartEvent):
            start = self._assistant_start_item_index
            if start is not None:
                del self.state.items[start:]
            self.state.assistant_buffer = ""
            self._assistant_start_item_index = None
            self._discard_retry_notice()
            self.state.add_item(
                "status",
                format_retry_notice(event.attempt, event.max_attempts, event.reason),
            )
            self._retry_notice_index = len(self.state.items) - 1
            return
```

Add the notice index to `__init__` and the helper:

```python
        self._retry_notice_index: int | None = None
```

```python
    def _discard_retry_notice(self) -> None:
        """Drop the pending retry notice from the canonical display state."""
        index = self._retry_notice_index
        self._retry_notice_index = None
        if index is not None and 0 <= index < len(self.state.items):
            del self.state.items[index]
```

Call `self._discard_retry_notice()` at the top of the `MessageStartEvent` assistant branch (before setting the new buffer/index) and in the `MessageEndEvent` assistant branch (before `add_assistant_error`, so the terminal projection replaces the notice).

- [ ] **Step 5: Add the transcript method** (`src/tau_coding/tui/widgets.py`, `TranscriptView`)

Add the field to `__init__`:

```python
        self._retry_notice_widget: TranscriptMessageWidget | None = None
```

Add the methods:

```python
    def _clear_retry_notice(self) -> None:
        """Remove the pending retry notice widget when the notice is consumed."""
        widget = self._retry_notice_widget
        self._retry_notice_widget = None
        if widget is not None and widget.parent is self:
            widget.remove()

    async def discard_active_assistant(self, notice: str) -> None:
        """Roll back the in-flight assistant widgets and show a retry notice."""
        for widget in tuple(self._active_message_widgets):
            if widget.parent is self:
                await widget.remove()
        self._active_message_widgets.clear()
        self._active_assistant_widget = None
        self._active_thinking_widget = None
        self._hidden_thinking_placeholder_visible = False
        self._clear_retry_notice()
        state = self._render_state
        if state is not None:
            self._window_end = len(state.items)
        notice_widget = TranscriptMessageWidget(
            ChatItem(role="status", text=notice),
            theme=self._render_theme,
        )
        self._retry_notice_widget = notice_widget
        await self.mount(notice_widget, before=self._bottom_boundary)
        self._request_follow_scroll(force=True)
```

Call `self._clear_retry_notice()` at the top of `start_assistant_message`, `append_thinking_delta`, `finish_assistant_message`, and `finish_structured_assistant_message` (before their existing logic).

- [ ] **Step 6: Wire the app** (`src/tau_coding/tui/app.py`)

In `_apply_streaming_transcript_event`, before the `AutoRetryStartEvent` branch:

```python
        if isinstance(event, TurnRetryStartEvent):
            notice = format_retry_notice(event.attempt, event.max_attempts, event.reason)
            await transcript.discard_active_assistant(notice)
            self._refresh_chrome()
            return
```

(Import `TurnRetryStartEvent` from `tau_agent.events` and `format_retry_notice` from `tau_coding.tui.state`.)

- [ ] **Step 7: Add the app-level test** (`tests/test_tui_app.py`; model on the existing prompt-worker tests — locate with `rg -n "prompt_worker|run_prompt" tests/test_tui_app.py | head`)

```python
@pytest.mark.anyio
async def test_prompt_worker_rolls_back_partial_text_on_turn_retry() -> None:
    """Prove a retried turn shows only the reattempt's content in the transcript."""
    partial = AssistantMessage()
    recovered = AssistantMessage(content="recovered")
    session = FakeSession(
        events=[
            AgentStartEvent(),
            MessageStartEvent(message=partial),
            MessageUpdateEvent(
                message=partial,
                assistant_message_event=TextDeltaEvent(
                    content_index=0, delta="partial", partial=partial
                ),
            ),
            TurnRetryStartEvent(
                attempt=2,
                max_attempts=3,
                delay_seconds=0.25,
                reason="network error (RemoteProtocolError)",
                error_message="peer closed connection",
            ),
            MessageStartEvent(message=recovered),
            MessageUpdateEvent(
                message=recovered,
                assistant_message_event=TextDeltaEvent(
                    content_index=0, delta="recovered", partial=recovered
                ),
            ),
            MessageEndEvent(message=recovered),
            AgentEndEvent(),
        ]
    )
    app = TauTuiApp(session)

    async with app.run_test(size=(120, 30)) as pilot:
        await app._run_prompt("stream")
        await pilot.pause()

        transcript = app.query_one("#transcript", TranscriptView)
        text = "\n".join(line.text for line in transcript.lines)

    assert "partial" not in text
    assert "recovered" in text
    assert "Error" not in text
    assert "retrying 2/3" not in text


@pytest.mark.anyio
async def test_prompt_worker_shows_only_final_attempt_on_exhausted_retries() -> None:
    """Prove exhausted retries show one error projection and no earlier attempts."""
    partial_one = AssistantMessage()
    partial_two = AssistantMessage()
    failed = AssistantMessage(
        stop_reason="error",
        error_message="drop 2",
        content="partial-two",
    )
    session = FakeSession(
        events=[
            AgentStartEvent(),
            MessageStartEvent(message=partial_one),
            MessageUpdateEvent(
                message=partial_one,
                assistant_message_event=TextDeltaEvent(
                    content_index=0, delta="partial-one", partial=partial_one
                ),
            ),
            TurnRetryStartEvent(
                attempt=2,
                max_attempts=3,
                delay_seconds=0.25,
                reason="network error",
                error_message="drop 1",
            ),
            MessageStartEvent(message=partial_two),
            MessageUpdateEvent(
                message=partial_two,
                assistant_message_event=TextDeltaEvent(
                    content_index=0, delta="partial-two", partial=partial_two
                ),
            ),
            MessageEndEvent(message=failed),
            AgentEndEvent(),
        ]
    )
    app = TauTuiApp(session)

    async with app.run_test(size=(120, 30)) as pilot:
        await app._run_prompt("stream")
        await pilot.pause()

        transcript = app.query_one("#transcript", TranscriptView)
        text = "\n".join(line.text for line in transcript.lines)

    assert "partial-one" not in text
    assert "partial-two" in text
    assert "Error: drop 2" in text
    assert "retrying" not in text
```

> The fulfilled-events shape mirrors `test_tui_streaming_deltas_update_active_message_without_full_refresh`
> (line ~2114); `FakeSession(events=...)` yields the events verbatim, so the
> retry-start event must be imported from `tau_agent.events` and the delta
> builder from `tau_ai.events` in this test file.

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/test_tui_adapter.py tests/test_tui_app.py -q`
Expected: PASS (including existing TUI tests)

- [ ] **Step 9: Full checks + commit**

Run: `uv run pytest tests/test_tui_adapter.py tests/test_tui_app.py tests/test_tui_streaming.py -q && uv run ruff check src/tau_coding/tui && uv run mypy src/tau_coding/tui`

```bash
git add src/tau_coding/tui/adapter.py src/tau_coding/tui/state.py src/tau_coding/tui/app.py src/tau_coding/tui/widgets.py tests/test_tui_adapter.py tests/test_tui_app.py
git commit -m "feat(tui): roll back partial transcripts and show turn-retry notices"
```

---

## Task 11: Print-mode retry notice

**Files:**
- Modify: `src/tau_coding/rendering/transcript.py`
- Test: `tests/test_rendering.py`

**Delta requirement:** Print-mode retry notice.

- [ ] **Step 1: Write the failing test** (append to `tests/test_rendering.py`; model on the existing renderer tests — locate with `rg -n "TranscriptRenderer" tests/test_rendering.py | head`)

```python
def test_transcript_renderer_prints_turn_retry_notice(capsys: pytest.CaptureFixture[str]) -> None:
    """Prove print mode reports a retry without failing the final output."""
    renderer = TranscriptRenderer(custom_message_renderer=None)
    renderer.render(MessageUpdateEvent(
        message=AssistantMessage(content="partial"),
        assistant_message_event=text_delta("partial"),
    ))
    renderer.render(
        TurnRetryStartEvent(
            attempt=2,
            max_attempts=3,
            delay_seconds=0.25,
            reason="network error (RemoteProtocolError)",
            error_message="peer closed connection",
        )
    )
    renderer.render(MessageUpdateEvent(
        message=AssistantMessage(content="done"),
        assistant_message_event=text_delta("done"),
    ))
    renderer.render(MessageEndEvent(message=AssistantMessage(content="done", model="fake")))

    captured = capsys.readouterr()
    assert "retrying 2/3" in captured.err
    assert "Error" not in captured.err
```

(Adjust for how `TranscriptRenderer.render` output is captured in existing tests — they may use `capsys` on stderr per the renderer's `Console(stderr=True)`.)

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_rendering.py -v`
Expected: FAIL — the notice is not printed.

- [ ] **Step 3: Implement the branch** (`src/tau_coding/rendering/transcript.py`)

Add `TurnRetryStartEvent` to the `tau_agent.events` import. Add a branch next to the `AutoRetryStartEvent` one:

```python
        if isinstance(event, TurnRetryStartEvent):
            self._newline()
            self._console.print(
                Text(
                    f"… Connection lost — retrying {event.attempt}/{event.max_attempts}: "
                    f"{event.reason}",
                    style="bright_black",
                )
            )
            return
```

- [ ] **Step 4: Run the tests + commit**

Run: `uv run pytest tests/test_rendering.py -q && uv run ruff check src/tau_coding/rendering`

```bash
git add src/tau_coding/rendering/transcript.py tests/test_rendering.py
git commit -m "feat(cli): print turn-retry notices in print mode"
```

---

## Task 12: Documentation

**Files:**
- Create: `dev-notes/2026-08-16-transient-error-retry.md`
- Modify: `dev-notes/provider-error-recovery.md`, `dev-notes/architecture/provider-retries.md`, `website/content/guides/providers-and-models.md`, `website/content/guides/sessions.md`, `website/content/reference/configuration.md`, `website/content/reference/cli.md`

**Delta requirement:** All — documentation of the implemented behavior.

- [ ] **Step 1: Write the dev-note** (`dev-notes/2026-08-16-transient-error-retry.md`)

Follow the existing dev-note format (see `dev-notes/codex-stream-error-recovery.md`): sections `## What changed`, `## Why it exists`, `## Architecture`, `## How to test`. Cover:

- Turn-level retry in `tau_agent.loop` with suppression of the failed attempt's terminal error end; failed attempts never enter history/storage.
- Adapter classification (`retryable` on the provider error), terminal conditions (usage limits, overflow, cancellation) never retried; shared markers in `tau_ai/classify.py`.
- Tail-read completion (transport error after the terminal chunk → completed message with a `response_tail_read` diagnostic).
- `TurnRetryStartEvent`, backoff (0.25 s base, doubling, 1 s cap), per-provider `turn_retry_max` (default 2, `0` disables; `tau setup --turn-retry-max`).
- TUI rollback + notice; print-mode notice line; `assistant_retry` diagnostics entries.
- How to test: the pytest commands per layer and a manual check with a flaky provider.

- [ ] **Step 2: Update `dev-notes/provider-error-recovery.md`**

Replace the sentence "A broader policy for partially failed turns and unmatched tool calls can be handled separately." with a pointer to the new behavior (turn-level retry now covers retryable transient failures; see the new dev-note).

- [ ] **Step 3: Update `dev-notes/architecture/provider-retries.md`**

Add a section describing the harness turn-level retry layer on top of the adapter retries: classification stays in adapters, the loop owns the retry decision for retryable terminal errors, and the tail-read completion rule. Note the current code swallows adapter `ProviderRetryEvent`s at the Pi boundary (the old "RetryEvent forwarded by run_agent_loop" paragraph is stale — replace it).

- [ ] **Step 4: Update the website docs**

- `website/content/reference/configuration.md`: document `turn_retry_max` (default `2`, `0` disables) next to `max_retries` in the provider configuration reference.
- `website/content/guides/providers-and-models.md`: mention `turn_retry_max` alongside `max_retries` in the provider-settings example.
- `website/content/guides/sessions.md`: update the failure-handling paragraph to state that transient failures are retried automatically at the turn level with partial output discarded, and only exhausted retries show the "send a message to retry" hint.
- `website/content/reference/cli.md`: add `--turn-retry-max` to the `tau setup` flags table.

- [ ] **Step 5: Verify docs build links are consistent**

Run: `grep -rn "turn_retry_max\|turn-retry-max" website/content/` — confirm the new references appear in the four files.

- [ ] **Step 6: Commit**

```bash
git add dev-notes/2026-08-16-transient-error-retry.md dev-notes/provider-error-recovery.md dev-notes/architecture/provider-retries.md website/content/
git commit -m "docs: document turn-level transient error retry"
```

---

## Task 13: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the complete test suite**

Run: `uv run pytest`
Expected: PASS

- [ ] **Step 2: Run lint, format, and type checks**

Run:
```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
```
Expected: PASS

- [ ] **Step 3: Inspect the final diff for scope**

Run: `git log --oneline -13 && git status --short`
Expected: the last 12–13 commits are the tasks above and the working tree is clean.

- [ ] **Step 4: Verify the feature spec scenarios are covered**

For each scenario in `dev-notes/design/2026-08-16-transient-error-retry-spec.md`, run the corresponding test by name and confirm it exists and passes:

```bash
uv run pytest tests/test_agent_loop.py -k "retries or retry or backoff or cancel" tests/test_agent_harness.py -k retry tests/test_tau_ai.py -k "retryable or tail_read or classify" tests/test_coding_session.py -k "turn_retry or retry_budget" tests/test_tui_adapter.py -k retry tests/test_tui_app.py -k retry tests/test_rendering.py -k retry -q
```
Expected: PASS

---

## Self-review notes

- Feature-spec → delta: every feature-spec requirement appears in the delta (all ADDED; no living spec exists).
- Delta → plan: each requirement has at least one failing test per task mapping (classification → Tasks 4–7, tail failure → Tasks 5–7, bounded retry → Task 3, budget config → Task 9, setup interface → Task 9, cancellation → Task 3, transcript rollback → Task 10, terminal projection → Task 10, print notice → Task 11, retry diagnostics → Task 8, overflow unchanged → Task 4, retry scope → Task 3).
- Plan → spec: no task falls outside the spec's scope (docs/verification tasks are required by AGENTS.md, not scope creep).
- Type consistency: `max_turn_retries` is used identically in `run_agent_loop`, `AgentHarnessConfig`, and session wiring; `TurnRetryStartEvent` fields are consumed by adapter/app/renderer/diagnostics with the same names; `retryable` defaults to `False` everywhere so existing providers/tests keep their behavior.
- Known follow-ups flagged for the implementer (not blockers): Codex SSE fixture payloads for the new tail-read and in-stream tests must mirror the shapes in the existing Codex tests (`_CODEX_OVERLOAD_ERROR_SSE`, `response.completed`); the TUI/session test harness shapes are pinned to the existing tests they cite; `DEFAULT_TURN_RETRIES` and `TurnRetryStartEvent` exports in `tau_agent/__init__.py` must be added (Task 3 Step 6); the anthropic `message_stop` break (Task 6) is a behavior-preserving change for well-formed streams since `message_stop` is the protocol's terminal event.
