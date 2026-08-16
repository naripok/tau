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
