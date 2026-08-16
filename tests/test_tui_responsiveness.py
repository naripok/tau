"""TUI prompt-typing responsiveness tests.

These tests prove Phase 1 of the TUI responsiveness plan: ordinary prompt
typing performs no unnecessary work. Every assertion counts work performed
(session-index listings, completion redraws, shell-mode restyling, paste
syncs, prompt repaints) instead of measuring wall-clock time, so the tests
stay deterministic and catch regressions that would make every keystroke
noticeably more expensive.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot

from conftest import isolate_home
from tau_coding.session_manager import CodingSessionRecord
from tau_coding.session_stats import SessionStats
from tau_coding.tui.app import PromptInput, TauTuiApp
from tau_coding.tui.autocomplete import is_resume_argument_completion
from tau_coding.tui.config import TAU_DARK_THEME, TAU_LIGHT_THEME


class FakeSessionManager:
    """Session index stub that counts how often the index is listed."""

    def __init__(self) -> None:
        self.list_sessions_calls = 0
        self.records = (
            CodingSessionRecord(
                id="session-1",
                path=Path("/workspace/project/.tau/sessions/session-1.jsonl"),
                cwd=Path("/workspace/project"),
                model="fake-model",
                title=None,
                created_at=0.0,
                updated_at=0.0,
            ),
        )

    def list_sessions(self, cwd: Path | None = None) -> list[CodingSessionRecord]:
        self.list_sessions_calls += 1
        return list(self.records)


class FakeSession:
    """Minimal session stub exposing only what prompt typing touches."""

    def __init__(self) -> None:
        self.cwd = Path("/workspace/project")
        self.provider_name = "openai"
        self.model = "fake-model"
        self.thinking_level = "medium"
        self.session_title: str | None = None
        self.context_token_estimate = 12_034
        self.has_provider_context_usage = True
        self.auto_compact_token_threshold = 200_000
        self.context_window_tokens = 216_384
        self.session_stats = SessionStats()
        self.extension_names: Sequence[object] = ()
        self.tools: Sequence[object] = ()
        self.skills: Sequence[object] = ()
        self.prompt_templates: Sequence[object] = ()
        self.context_files: Sequence[object] = ()
        self.available_models: Sequence[object] = ()
        self.available_providers: Sequence[object] = ("openai",)
        self.available_thinking_levels: Sequence[object] = ("off", "low", "medium", "high")
        self.messages: Sequence[object] = ()
        self.session_manager = FakeSessionManager()

    async def emit_pending_session_start(self) -> None:
        """No-op stand-in for the deferred session-start emission."""
        return None


def _count_attr(target: Any, name: str, calls: list[int]) -> Any:
    """Wrap *name* on *target* with a counter appending to *calls*.

    Returns the original attribute so callers can restore it.
    """
    original = getattr(target, name)

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(len(calls))
        return original(*args, **kwargs)

    setattr(target, name, counting)
    return original


async def _type_prompt(prompt: PromptInput, pilot: Pilot, text: str) -> None:
    """Set the prompt text and dispatch its ``Changed`` event."""
    prompt.text = text
    await pilot.pause()


@pytest.mark.anyio
async def test_prose_typing_performs_no_session_listing_or_redraws() -> None:
    """Prove plain prose keystrokes do zero session-index listings, completion
    redraws, shell-mode restylings, or prompt repaints after the initial state.

    Session listing walks the session index synchronously on every keystroke
    unless it is gated; completion redraws and prompt repaints repaint the
    screen; shell-mode restyling re-renders the prompt lines. If any guard
    regresses, every typed character becomes visibly slower, so each work
    source is counted rather than timed.
    """
    session = FakeSession()
    app = TauTuiApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        completion_renders: list[int] = []
        activity_updates: list[int] = []
        set_class_calls: list[int] = []
        _count_attr(app, "_refresh_completions", completion_renders)
        _count_attr(app, "_apply_activity_indicator", activity_updates)
        _count_attr(prompt, "set_class", set_class_calls)
        await pilot.pause()
        del completion_renders[:], activity_updates[:], set_class_calls[:]

        for text in ("h", "he", "hello", "hello world", "hello world again"):
            prompt.text = text
            # Count prompt repaints only while the Changed handler dispatches:
            # Textual's own load_text() legitimately repaints once during the
            # assignment above, before this wrapper is installed.
            prompt_refreshes: list[int] = []
            prompt_original_refresh = _count_attr(prompt, "refresh", prompt_refreshes)
            await pilot.pause()
            prompt.refresh = prompt_original_refresh  # type: ignore[method-assign]
            assert prompt_refreshes == [], f"app repainted the prompt for {text!r}"

        assert session.session_manager.list_sessions_calls == 0
        assert completion_renders == []
        assert activity_updates == []
        assert set_class_calls == []
        assert app._prompt_shell_mode is False
        assert not prompt.has_class("-shell-mode")


@pytest.mark.anyio
async def test_resume_completion_session_listing_is_lazy_and_cached() -> None:
    """Prove the exact keystroke-to-listing table for ``/resume <text>``.

    The session index must not be listed while typing the command or its bare
    name, must be listed exactly once when the first argument character is
    typed, must stay cached for further argument characters, must be discarded
    the moment the prompt leaves the resume interaction, and must be listed
    again on re-entry. Listing on every keystroke would make ``/resume``
    argument typing hit the filesystem and JSON index for each character.
    """
    session = FakeSession()
    app = TauTuiApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        await pilot.pause()
        manager = session.session_manager

        def listings() -> int:
            return manager.list_sessions_calls

        for text in ("/", "/r", "/res", "/resume"):
            await _type_prompt(prompt, pilot, text)
            assert listings() == 0, f"listed sessions while typing {text!r}"

        await _type_prompt(prompt, pilot, "/resume ")
        assert listings() == 1, "expected exactly one listing on first argument char"

        for text in ("/resume s", "/resume se", "/resume sess", "/resume session-1"):
            await _type_prompt(prompt, pilot, text)
            assert listings() == 1, f"re-listed sessions while typing {text!r}"

        # Leaving the interaction discards the cache...
        await _type_prompt(prompt, pilot, "hello ordinary prose")
        assert listings() == 1

        # ...so re-entering lists fresh rather than reusing stale options.
        await _type_prompt(prompt, pilot, "/resume ")
        assert listings() == 2
        await _type_prompt(prompt, pilot, "/resume session-1")
        assert listings() == 2


@pytest.mark.anyio
async def test_shell_mode_styling_updates_only_on_mode_transitions() -> None:
    """Prove shell-mode styling is transition-based, not per-character.

    The prompt must not restyle itself while ordinary text is typed, must
    restyle exactly once when entering and once when leaving shell mode, and
    must do nothing while shell-mode text is extended. Per-character restyling
    would re-render the prompt's text lines on every keystroke.
    """
    app = TauTuiApp(FakeSession())
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        activity_updates: list[int] = []
        set_class_calls: list[tuple[bool, tuple[str, ...]]] = []
        _count_attr(app, "_apply_activity_indicator", activity_updates)
        original_set_class = prompt.set_class

        def counting_set_class(add: bool, *class_names: str, **kwargs: object) -> object:
            set_class_calls.append((add, class_names))
            return original_set_class(add, *class_names, **kwargs)

        prompt.set_class = counting_set_class  # type: ignore[method-assign]
        await pilot.pause()
        del activity_updates[:], set_class_calls[:]

        # Ordinary prose: the initial (non-shell) state is already established
        # at mount, so nothing may happen on subsequent characters.
        for text in ("h", "he", "hello"):
            await _type_prompt(prompt, pilot, text)
        assert activity_updates == []
        assert set_class_calls == []
        assert app._prompt_shell_mode is False

        # One transition into shell mode: one restyle, one indicator update.
        await _type_prompt(prompt, pilot, "!ls")
        assert activity_updates == [0]
        assert set_class_calls == [(True, ("-shell-mode",))]
        assert app._prompt_shell_mode is True
        del activity_updates[:], set_class_calls[:]

        # Continuing within shell mode must not restyle again.
        for text in ("!l", "!ls", "!ls -la"):
            await _type_prompt(prompt, pilot, text)
        assert activity_updates == []
        assert set_class_calls == []
        assert app._prompt_shell_mode is True

        # One transition back out of shell mode.
        await _type_prompt(prompt, pilot, "ls")
        assert activity_updates == [0]
        assert set_class_calls == [(False, ("-shell-mode",))]
        assert app._prompt_shell_mode is False
        assert not prompt.has_class("-shell-mode")


@pytest.mark.anyio
async def test_theme_change_updates_shell_style_while_in_shell_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Prove a theme change while the prompt is in shell mode re-pushes the
    terminal-command text style and leaves shell mode active.

    The prompt's terminal-command text styling comes from the theme's tool-role
    border color. If the theme-change path forgot to re-push it, shell-mode
    text would keep the previous theme's color while the rest of the UI
    switches, so the visible style and the preserved mode are both asserted.
    """
    isolate_home(monkeypatch, tmp_path)
    app = TauTuiApp(FakeSession())
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        await pilot.pause()

        await _type_prompt(prompt, pilot, "!ls")
        dark_border = TAU_DARK_THEME.role_styles["tool"].border
        light_border = TAU_LIGHT_THEME.role_styles["tool"].border
        assert dark_border != light_border
        assert prompt.shell_mode_style == dark_border
        assert prompt.has_class("-shell-mode")

        app._set_tui_theme("tau-light")
        await pilot.pause()

        assert app.tui_settings.theme == "tau-light"
        assert prompt.shell_mode_style == light_border
        assert prompt.has_class("-shell-mode")
        assert app._prompt_shell_mode is True


@pytest.mark.anyio
async def test_completion_redraw_happens_only_when_rendered_state_changes() -> None:
    """Prove the completion box re-renders only when its state actually differs.

    Showing a completion must render once, hiding it (a real state change) must
    render once, and continuing to type while the empty state is unchanged must
    not render at all. Without the equality guard every keystroke re-renders
    the completion window even when nothing visible changed.
    """
    session = FakeSession()
    app = TauTuiApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        completion_renders: list[int] = []
        _count_attr(app, "_refresh_completions", completion_renders)
        await pilot.pause()
        del completion_renders[:]

        await _type_prompt(prompt, pilot, "/res")
        assert completion_renders == [0]
        assert bool(app._completion_state.items), "/res should surface completions"

        await _type_prompt(prompt, pilot, "normal text")
        assert completion_renders == [0, 1]
        assert not app._completion_state.items

        await _type_prompt(prompt, pilot, "normal text continues")
        assert completion_renders == [0, 1]
        assert not app._completion_state.items


@pytest.mark.anyio
async def test_pending_paste_sync_is_skipped_without_pending_pastes() -> None:
    """Prove typing invokes ``sync_pending_paste`` only when a large paste is
    pending, and that the property reflects pending-paste presence.

    ``sync_pending_paste`` scans the whole pending-paste list on every call;
    skipping the call for the common no-paste case keeps the keystroke path
    cheap, while the guard still fires when a paste is actually stored.
    """
    app = TauTuiApp(FakeSession())
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        await pilot.pause()
        sync_calls: list[int] = []
        original_sync = _count_attr(prompt, "sync_pending_paste", sync_calls)

        assert prompt.has_pending_pastes is False
        for text in ("hello", "hello world", "/resume session-1"):
            await _type_prompt(prompt, pilot, text)
        assert sync_calls == []

        # With a stored large paste, the guard lets the sync run exactly once
        # and the (now-orphaned) placeholder is dropped.
        prompt._pending_pastes.append(("[Pasted content #1: 5,000 characters]", "x" * 5_000))
        assert prompt.has_pending_pastes is True
        await _type_prompt(prompt, pilot, "ordinary text")
        assert sync_calls == [0]
        assert prompt.has_pending_pastes is False
        assert prompt._pending_pastes == []

        prompt.sync_pending_paste = original_sync  # type: ignore[method-assign]


def test_is_resume_argument_completion_matches_argument_consumption() -> None:
    """Prove the resume-detection helper agrees with the argument completions
    branch that consumes session options.

    The helper gates the session listing; if it disagrees with the completion
    branch, options would be loaded when never consumed (wasted IO) or not
    cached when consumed (per-character listing). Case-insensitivity and the
    argument-text requirement are the exact conditions of the consumption.
    """
    assert is_resume_argument_completion("/resume x")
    assert is_resume_argument_completion("/RESUME x")
    assert is_resume_argument_completion("/resume x y")
    assert is_resume_argument_completion("/resume ")
    assert not is_resume_argument_completion("/resume")
    assert not is_resume_argument_completion("/res")
    assert not is_resume_argument_completion("/")
    assert not is_resume_argument_completion("")
    assert not is_resume_argument_completion("hello world")
    assert not is_resume_argument_completion("/resume:foo x")
    assert not is_resume_argument_completion("/other x")
