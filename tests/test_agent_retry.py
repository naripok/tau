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
