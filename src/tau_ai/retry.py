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

__all__ = ["provider_retry_event", "retry_delay_seconds", "wait_for_retry"]


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
