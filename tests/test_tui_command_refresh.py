"""Phase 5 refresh-scope tests for handled slash commands.

Phase 5 stops lightweight slash commands from triggering a full transcript
rebuild. Handled commands previously ended with an unconditional
``TauTuiApp._refresh()``, which remounts every transcript block. Each command
now requests the narrowest refresh scope it needs (full rebuild, chrome-only,
or nothing) and the handler dispatches it exactly once.

These tests pin that classification by counting ``app._refresh`` (full
rebuilds) and ``app._refresh_chrome`` (chrome refreshes; full refreshes imply
one nested chrome refresh) around each command, and by checking the visible
transcript/sidebar output is still correct under the cheaper scopes.
"""

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from textual.widgets import Static

from conftest import isolate_home
from tau_agent import UserMessage
from tau_coding.commands import CommandResult
from tau_coding.reload import CodingReloadSummary, ReloadCategorySummary
from tau_coding.session_manager import CodingSessionRecord
from tau_coding.skills import Skill
from tau_coding.tui.app import (
    CommandOutputScreen,
    ModelPickerScreen,
    PromptInput,
    SessionPickerScreen,
    TauTuiApp,
    ThemePickerScreen,
)
from tau_coding.tui.widgets import TranscriptView
from test_tui_app import FakeSession

_SESSION_RECORD = CodingSessionRecord(
    id="session-1",
    path=Path("/workspace/project/session-1.jsonl"),
    cwd=Path("/workspace/project"),
    model="fake-model",
    title="Test session",
    created_at=1.0,
    updated_at=2.0,
)


class _SessionManagerStub:
    """Minimal session-manager surface the session picker reads from."""

    def __init__(self, records: list[CodingSessionRecord]) -> None:
        self._records = records

    def list_sessions(self, cwd: Path | None = None) -> list[CodingSessionRecord]:
        del cwd
        return self._records

    def get_session(self, session_id: str) -> CodingSessionRecord | None:
        return next((record for record in self._records if record.id == session_id), None)


class _RefreshFakeSession(FakeSession):
    """FakeSession with the real session manager's `/reload` and `/clear` paths.

    FakeSession's built-in ``/reload`` only returns a message; the real
    registry sets ``reload_requested`` and reloads asynchronously. This subclass
    replicates the real command surface so tests exercise the classification for
    the full reload path (``reload_requested`` + async ``reload``) and for the
    legacy ``clear_requested`` flag (no built-in command sets it today, but the
    branch still honors it).
    """

    def handle_command(self, text: str) -> CommandResult:
        if text == "/reload":
            return CommandResult(handled=True, reload_requested=True)
        if text == "/clear":
            return CommandResult(handled=True, clear_requested=True)
        if text == "/combined":
            return CommandResult(handled=True, clear_requested=True, reload_requested=True)
        if text.startswith("/model "):
            model = text.removeprefix("/model ")
            self.set_model(model)
            return CommandResult(handled=True, message=f"Current model: {model}")
        return super().handle_command(text)

    async def reload(self) -> CodingReloadSummary:
        self.reload_count += 1
        self.skills = (
            Skill(name="reloaded", path=self.cwd / "reloaded.md", content="Reloaded skill"),
        )
        return CodingReloadSummary(
            skills=ReloadCategorySummary(before=1, after=1, changed=False),
            prompt_templates=ReloadCategorySummary(before=0, after=0, changed=False),
            context_files=ReloadCategorySummary(before=1, after=1, changed=False),
            extensions=ReloadCategorySummary(before=2, after=2, changed=False),
            diagnostics=ReloadCategorySummary(before=0, after=0, changed=False),
            system_prompt_rebuilt=False,
        )


def _install_refresh_counters(app: TauTuiApp) -> tuple[list[int], list[int]]:
    """Replace the app's refresh entry points with counting wrappers.

    Wrappers delegate to the originals so rendering still happens; callers reset
    the counters right before submitting the command so startup/mount refreshes
    are excluded. ``_refresh`` counts full transcript rebuilds; ``_refresh_chrome``
    counts every chrome refresh, including the one a full rebuild implies.
    """
    full_counts = [0]
    chrome_counts = [0]
    original_refresh = app._refresh
    original_chrome = app._refresh_chrome

    def tracking_refresh() -> None:
        full_counts[0] += 1
        original_refresh()

    def tracking_chrome(**kwargs: object) -> None:
        chrome_counts[0] += 1
        original_chrome(**kwargs)

    app._refresh = tracking_refresh  # type: ignore[method-assign]
    app._refresh_chrome = tracking_chrome  # type: ignore[method-assign]
    return full_counts, chrome_counts


def _install_transcript_counters(
    transcript: TranscriptView,
) -> tuple[list[int], list[int]]:
    """Wrap the transcript's render entry points with counting wrappers.

    ``append_item`` mounts a single block (the incremental path), while
    ``update_from_state`` is the full rebuild used by ``_refresh``. Counting
    both proves an appended message took the incremental path and that the full
    rebuild happened exactly once (or not at all).
    """
    append_counts = [0]
    update_counts = [0]
    original_append = transcript.append_item
    original_update = transcript.update_from_state

    def tracking_append(*args: object, **kwargs: object) -> object:
        append_counts[0] += 1
        return original_append(*args, **kwargs)

    def tracking_update(*args: object, **kwargs: object) -> object:
        update_counts[0] += 1
        return original_update(*args, **kwargs)

    transcript.append_item = tracking_append  # type: ignore[method-assign]
    transcript.update_from_state = tracking_update  # type: ignore[method-assign]
    return append_counts, update_counts


def _transcript_text(app: TauTuiApp) -> str:
    """Return the plain text currently rendered in the transcript."""
    transcript = app.query_one("#transcript", TranscriptView)
    return "\n".join(line.text for line in transcript.lines)


def _sidebar_text(app: TauTuiApp) -> str:
    """Return the plain text currently rendered in the session sidebar."""
    sidebar = app.query_one("#sidebar-content", Static)
    console = Console(record=True, width=80, file=StringIO())
    console.print(sidebar.content)
    return console.export_text()


# --- name: session-metadata change, chrome only -----------------------------


@pytest.mark.anyio
async def test_tui_refresh_name_rename_needs_only_chrome() -> None:
    """A `/name` rename must not rebuild the transcript.

    Proof: renaming only mutates session title metadata, which lives in the
    chrome (sidebar + terminal title). A full rebuild would remount every
    transcript block to display nothing new, so the handler must settle for a
    single chrome refresh — and the new title must be visible in the sidebar.
    """
    session = FakeSession()
    app = TauTuiApp(session)
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        full_counts[0] = 0
        chrome_counts[0] = 0
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/name Customer bugfix"
        await pilot.press("enter")
        await pilot.pause()

        assert session.session_title == "Customer bugfix"
        assert full_counts[0] == 0
        assert chrome_counts[0] == 1
        assert "Customer bugfix" in _sidebar_text(app)
        assert "Customer bugfix" not in _transcript_text(app)


# --- screen messages and pickers: no refresh ---------------------------------


@pytest.mark.anyio
async def test_tui_refresh_session_message_opens_screen_without_refresh() -> None:
    """`/session` output must open its screen without any refresh.

    Proof: the message renders on a pushed CommandOutputScreen, so neither a
    transcript rebuild nor a chrome refresh adds anything — the handler must
    skip refreshing entirely while the message is still fully visible.
    """
    app = TauTuiApp(FakeSession())
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        full_counts[0] = 0
        chrome_counts[0] = 0
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/session"
        await pilot.press("enter")
        await pilot.pause()

        assert full_counts[0] == 0
        assert chrome_counts[0] == 0
        assert isinstance(app.screen, CommandOutputScreen)
        body = app.screen.query_one("#command-output-body", Static)
        assert "Session info" in body.render().plain


@pytest.mark.anyio
async def test_tui_refresh_model_picker_opens_without_refresh() -> None:
    """The `/model` picker must open without any refresh.

    Proof: opening the picker just pushes a screen; selection happens on its
    own callbacks, which refresh chrome. A full rebuild here would remount the
    entire transcript for a screen the user hasn't interacted with yet.
    """
    app = TauTuiApp(FakeSession())
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        full_counts[0] = 0
        chrome_counts[0] = 0
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/model"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ModelPickerScreen)
        assert full_counts[0] == 0
        assert chrome_counts[0] == 0


@pytest.mark.anyio
async def test_tui_refresh_theme_picker_opens_without_refresh() -> None:
    """The bare `/theme` (pick form) must open the picker without any refresh.

    Proof: like every picker it pushes a screen and defers theme application to
    its selection callback (``_set_tui_theme`` refreshes on its own), so the
    handler must not refresh at all for the pick form.
    """
    app = TauTuiApp(FakeSession())
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        full_counts[0] = 0
        chrome_counts[0] = 0
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/theme"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ThemePickerScreen)
        assert full_counts[0] == 0
        assert chrome_counts[0] == 0


@pytest.mark.anyio
async def test_tui_refresh_resume_picker_opens_without_refresh() -> None:
    """The bare `/resume` (picker form) must open the picker without any refresh.

    Proof: the picker pushes a screen; resume itself happens on selection and
    refreshes internally. The handler must not refresh for the pick form.
    """
    session = FakeSession()
    session.session_manager = _SessionManagerStub([_SESSION_RECORD])
    app = TauTuiApp(session)
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        full_counts[0] = 0
        chrome_counts[0] = 0
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/resume"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, SessionPickerScreen)
        assert full_counts[0] == 0
        assert chrome_counts[0] == 0


@pytest.mark.anyio
async def test_tui_refresh_ctrl_r_opens_session_picker_without_refresh() -> None:
    """Ctrl+R (the session-picker keybinding) must open the picker without refresh.

    Proof: the keybinding routes straight to the same screen push as `/resume`,
    so it shares the no-refresh classification; the picker's own selection
    callback owns any later refresh.
    """
    session = FakeSession()
    session.session_manager = _SessionManagerStub([_SESSION_RECORD])
    app = TauTuiApp(session)
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        full_counts[0] = 0
        chrome_counts[0] = 0
        await pilot.press("ctrl+r")
        await pilot.pause()

        assert isinstance(app.screen, SessionPickerScreen)
        assert full_counts[0] == 0
        assert chrome_counts[0] == 0


# --- commands whose helpers refresh internally: exactly one rebuild ----------


@pytest.mark.anyio
async def test_tui_refresh_theme_argument_refreshes_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/theme tau-dark` must produce exactly one full rebuild — from `_set_tui_theme`.

    Proof: `_set_tui_theme` refreshes internally, so the handler must not
    refresh again or the command would rebuild the transcript twice. The single
    full refresh is the one the theme helper owns.
    """
    isolate_home(monkeypatch, tmp_path)
    app = TauTuiApp(FakeSession())
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        full_counts[0] = 0
        chrome_counts[0] = 0
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/theme tau-dark"
        await pilot.press("enter")
        await pilot.pause()

        assert app.tui_settings.theme == "tau-dark"
        assert full_counts[0] == 1
        assert chrome_counts[0] == 1


@pytest.mark.anyio
async def test_tui_refresh_new_command_refreshes_exactly_once_from_helper() -> None:
    """`/new` must produce exactly one full rebuild — from `_new_session`.

    Proof: `_new_session` clears state and refreshes internally, so a handler
    refresh would double-rebuild. Exactly one full refresh must happen and the
    visible transcript must show the fresh (empty) session.
    """
    session = FakeSession(messages=[UserMessage(content="Earlier")])
    app = TauTuiApp(session)
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        full_counts[0] = 0
        chrome_counts[0] = 0
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/new"
        await pilot.press("enter")
        await pilot.pause()

        assert session.new_session_count == 1
        assert full_counts[0] == 1
        assert chrome_counts[0] == 1
        assert app.state.items == []
        assert "Earlier" not in _transcript_text(app)


@pytest.mark.anyio
async def test_tui_refresh_resume_command_refreshes_exactly_once_from_helper() -> None:
    """`/resume <id>` must produce exactly one full rebuild — from `_resume_session`.

    Proof: `_resume_session` loads the restored messages and refreshes
    internally, so a handler refresh would double-rebuild. Exactly one full
    refresh must happen and the restored transcript must be visible.
    """
    session = FakeSession(messages=[UserMessage(content="Earlier")])
    app = TauTuiApp(session)
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        full_counts[0] = 0
        chrome_counts[0] = 0
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/resume session-1"
        await pilot.press("enter")
        await pilot.pause()

        assert session.resumed_session_ids == ["session-1"]
        assert full_counts[0] == 1
        assert chrome_counts[0] == 1
        assert [(item.role, item.text) for item in app.state.items] == [
            ("user", "Restored prompt")
        ]
        assert "Restored prompt" in _transcript_text(app)


# --- transcript-worthy changes: one full rebuild ------------------------------


@pytest.mark.anyio
async def test_tui_refresh_clear_command_rebuilds_transcript_once() -> None:
    """`/clear` must rebuild the transcript exactly once.

    Proof: clearing wipes every visible item, which only a full rebuild can
    reflect; the flag classification therefore requests TRANSCRIPT and the
    handler dispatches it exactly once, leaving an empty transcript.
    """
    session = _RefreshFakeSession(messages=[UserMessage(content="Earlier")])
    app = TauTuiApp(session)
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        full_counts[0] = 0
        chrome_counts[0] = 0
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/clear"
        await pilot.press("enter")
        await pilot.pause()

        assert full_counts[0] == 1
        assert chrome_counts[0] == 1
        assert app.state.items == []
        assert "Earlier" not in _transcript_text(app)


@pytest.mark.anyio
async def test_tui_refresh_reload_appends_message_and_rebuilds_once() -> None:
    """`/reload` must append its summary incrementally and rebuild exactly once.

    Proof: the reload summary is a transcript message, so it must take the
    incremental ``append_item`` path (one append, no rebuild for the message
    itself), while the reload's resource swap still warrants exactly one full
    rebuild — and the summary must be visible after it, with updated skills.
    """
    session = _RefreshFakeSession()
    session.skills = (Skill(name="review", path=Path("/w/review.md"), content="R"),)
    app = TauTuiApp(session)
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        transcript = app.query_one("#transcript", TranscriptView)
        append_counts, update_counts = _install_transcript_counters(transcript)

        full_counts[0] = 0
        chrome_counts[0] = 0
        append_counts[0] = 0
        update_counts[0] = 0

        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/reload"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert session.reload_count == 1
        assert append_counts[0] == 1
        assert update_counts[0] == 1
        assert full_counts[0] == 1
        assert chrome_counts[0] == 1
        text = _transcript_text(app)
        assert "Reloaded local coding resources and project context." in text
        assert [skill.name for skill in app.state.skills] == ["reloaded"]


# --- incremental message appends with no full rebuild ------------------------


@pytest.mark.anyio
async def test_tui_refresh_export_appends_message_without_full_refresh() -> None:
    """`/export` must append its status message incrementally with zero rebuilds.

    Proof: exporting neither clears state nor swaps session resources, so a full
    transcript rebuild would be pure overhead — the status line must be appended
    through the single-block incremental path and still be visible.
    """
    session = FakeSession()
    app = TauTuiApp(session)
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        transcript = app.query_one("#transcript", TranscriptView)
        append_counts, _update_counts = _install_transcript_counters(transcript)

        full_counts[0] = 0
        chrome_counts[0] = 0
        append_counts[0] = 0

        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/export"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert session.export_calls == [(None, None)]
        assert append_counts[0] == 1
        assert full_counts[0] == 0
        assert chrome_counts[0] == 1
        assert "Exported session to" in _transcript_text(app)


# --- explicit session-metadata switches keep chrome current ------------------


@pytest.mark.anyio
async def test_tui_refresh_explicit_model_switch_refreshes_chrome_only() -> None:
    """An explicit `/model <name>` switch must refresh chrome but not the transcript.

    Proof: switching the model mutates ``session.model``, which feeds the chrome
    (compact session info + sidebar fingerprint), without changing anything in
    the transcript. The handler must therefore grant CHROME, and the new model
    must be visible in the chrome afterward.
    """
    session = _RefreshFakeSession()
    app = TauTuiApp(session)
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        full_counts[0] = 0
        chrome_counts[0] = 0
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/model delete-me"
        await pilot.press("enter")
        await pilot.pause()

        assert full_counts[0] == 0
        assert chrome_counts[0] == 1


# --- sanity: scope dispatch is once per combined command ---------------------


@pytest.mark.anyio
async def test_tui_refresh_combined_clear_and_reload_rebuilds_once() -> None:
    """A command combining clear and reload must still rebuild exactly once.

    Proof: both flags map to TRANSCRIPT; the scope computation must collapse
    them into a single dispatch so a combined command cannot double-rebuild.
    """
    session = _RefreshFakeSession()
    app = TauTuiApp(session)
    full_counts, chrome_counts = _install_refresh_counters(app)

    async with app.run_test() as pilot:
        full_counts[0] = 0
        chrome_counts[0] = 0
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/combined"
        await pilot.press("enter")
        await pilot.pause()

        assert full_counts[0] == 1
        assert chrome_counts[0] == 1
