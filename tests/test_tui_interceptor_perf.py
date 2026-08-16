"""Development-only timing diagnostics for extension key interceptors.

These drive the real ``TauTuiApp._run_extension_key_interceptors`` seam on a
bare app (the method is synchronous, so no ``run_test`` is needed), proving
that the default production path is behaviorally unchanged while
``TAU_DEBUG_TUI_PERF`` exposes slow handlers. Interceptors run on every
main-screen key before Textual dispatch, so a handler that blocks does real
damage; the diagnostic must find it without ever altering key handling.
"""

import time
from types import SimpleNamespace

import pytest

from tau_coding.tui import app as tui_app
from tau_coding.tui.app import TauTuiApp
from test_tui_app import (
    FakeSession,  # noqa: E402 - sibling test module (see test_tui_components.py)
)


def _slow_interceptor(event: object, text: str) -> bool:
    """Return True after exceeding the slow threshold to mimic a heavy handler."""
    time.sleep(0.01)
    return True


def _fast_interceptor(event: object, text: str) -> bool:
    """Return False immediately to mimic a cheap, non-consuming handler."""
    return False


class _RecordingLogger:
    """Minimal stand-in for Textual's app logger that captures warning text."""

    def __init__(self, records: list[str]) -> None:
        self._records = records

    def warning(self, message: str, *args: object, **kwargs: object) -> None:
        self._records.append(message)


def test_no_warning_for_slow_interceptor_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove the production default never measures or warns.

    A slow handler must keep today's exact contract (consumed return value, no
    diagnostic activity) when ``TAU_DEBUG_TUI_PERF`` is unset, so shipping the
    diagnostic cannot change key handling for normal users.
    """
    monkeypatch.delenv("TAU_DEBUG_TUI_PERF", raising=False)
    app = TauTuiApp(FakeSession())
    warned: list[tuple[object, float]] = []
    monkeypatch.setattr(
        app,
        "_warn_slow_key_interceptor",
        lambda _interceptor, _elapsed: warned.append((_interceptor, _elapsed)),
    )
    app._register_extension_key_interceptor(_slow_interceptor)
    event = SimpleNamespace(key="x", character="x")
    assert app._run_extension_key_interceptors(event, "text") is True
    assert warned == []


def test_slow_interceptor_warns_exactly_once_and_identifies_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove debug mode warns once per slow interceptor, naming it.

    With ``TAU_DEBUG_TUI_PERF`` enabled a slow handler is surfaced exactly once
    across repeated keypresses (dedupe, so a hot slow handler cannot spam the
    log) and the message carries the interceptor's identity, which is what lets
    a developer find the offending extension.
    """
    monkeypatch.setenv("TAU_DEBUG_TUI_PERF", "1")
    app = TauTuiApp(FakeSession())
    records: list[str] = []
    monkeypatch.setattr(app, "_logger", _RecordingLogger(records))
    app._register_extension_key_interceptor(_slow_interceptor)
    event = SimpleNamespace(key="x", character="x")
    for _ in range(3):
        # The slow interceptor's return value is still honored while timing.
        assert app._run_extension_key_interceptors(event, "text") is True
    assert len(records) == 1
    assert records[0].startswith("slow TUI key interceptor")
    assert "_slow_interceptor" in records[0]
    assert "ms" in records[0]


def test_fast_interceptor_never_warns_when_debug_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the diagnostic stays silent for handlers under the threshold.

    A cheap handler must not produce warnings even with the debug clock on;
    the diagnostic exists to surface offenders, not to add noise.
    """
    monkeypatch.setenv("TAU_DEBUG_TUI_PERF", "1")
    app = TauTuiApp(FakeSession())
    records: list[str] = []
    monkeypatch.setattr(app, "_logger", _RecordingLogger(records))
    app._register_extension_key_interceptor(_fast_interceptor)
    event = SimpleNamespace(key="x", character="x")
    assert app._run_extension_key_interceptors(event, "text") is False
    assert records == []


def test_raising_interceptor_unchanged_under_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove debug mode keeps the isolation contract for raising handlers.

    A raising interceptor must still be recorded as an extension-component
    failure and treated as not consuming the key when the debug clock is on —
    the diagnostic observes the hot path, it does not alter it.
    """
    monkeypatch.setenv("TAU_DEBUG_TUI_PERF", "1")

    def _raising_interceptor(event: object, text: str) -> bool:
        del text
        raise RuntimeError("boom")

    app = TauTuiApp(FakeSession())
    records: list[str] = []
    monkeypatch.setattr(app, "_logger", _RecordingLogger(records))
    # notify() schedules a timer, which needs a live app loop; this test drives
    # the seam synchronously, so only the visible-notification step is stubbed
    # while the real failure recording (dedupe set + log) still runs.
    monkeypatch.setattr(app, "_notify", lambda *_args, **_kwargs: None)
    app._register_extension_key_interceptor(_raising_interceptor)  # type: ignore[arg-type]
    event = SimpleNamespace(key="x", character="x")
    assert app._run_extension_key_interceptors(event, "text") is False
    assert (
        f"key_interceptor:{id(_raising_interceptor)}"
        in app._extension_component_failures_reported
    )
    # Raised immediately, so not slow: no perf warning expected.
    assert records == []


def test_no_perf_counter_timing_when_debug_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove the disabled path never touches the clock.

    Reading ``time.perf_counter`` is the only unconditional cost the diagnostic
    could impose; asserting it is never called when ``TAU_DEBUG_TUI_PERF`` is
    unset guarantees the production hot path is behaviorally identical to the
    pre-diagnostic build.
    """
    monkeypatch.delenv("TAU_DEBUG_TUI_PERF", raising=False)
    calls = 0
    real_perf_counter = time.perf_counter

    def _spy_perf_counter() -> float:
        nonlocal calls
        calls += 1
        return real_perf_counter()

    monkeypatch.setattr(tui_app.time, "perf_counter", _spy_perf_counter)
    app = TauTuiApp(FakeSession())
    app._register_extension_key_interceptor(_slow_interceptor)
    event = SimpleNamespace(key="x", character="x")
    assert app._run_extension_key_interceptors(event, "text") is True
    assert calls == 0
