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
