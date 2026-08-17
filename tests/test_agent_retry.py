"""Tests for the cancellable retry backoff helpers in the agent layer."""

import asyncio

import pytest

from tau_agent.harness import SimpleCancellationToken
from tau_agent.messages import AssistantMessage, AssistantMessageDiagnostic
from tau_agent.retry import (
    failure_is_retryable,
    failure_reason,
    is_transient_status,
    turn_retry_delay_seconds,
    wait_for_retry,
)
from tau_agent.types import JSONValue


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


def _error(
    message: str = "peer closed connection",
    *,
    details: dict[str, JSONValue] | None = None,
) -> AssistantMessage:
    """Build a terminal assistant failure with optional provider_error details."""
    diagnostics = (
        [AssistantMessageDiagnostic(type="provider_error", details=details)] if details else None
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
        assert (
            failure_is_retryable(_error(details={"status_code": status_code}), signal=None) is True
        )
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
    assert (
        failure_is_retryable(_error(details={"status_code": 503, "body": "billing"}), signal=None)
        is False
    )
    assert failure_is_retryable(_error(message="monthly usage limit reached"), signal=None) is False
    assert (
        failure_is_retryable(
            _error(details={"status_code": 429, "body": "Insufficient_Quota"}), signal=None
        )
        is False
    )


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
