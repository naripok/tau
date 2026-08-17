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
        marker in lowered for marker in (*TERMINAL_RATE_LIMIT_MARKERS, *CONTEXT_OVERFLOW_MARKERS)
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
