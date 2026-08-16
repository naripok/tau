"""Tests for ``@`` file-reference completion scanning and its interaction-scoped index.

These tests exercise the Phase 3 caching layer: a short-lived index (with a
TTL) prevents ``@`` reference completion from recursively re-scanning a large
repository on every keystroke. No assertions depend on wall-clock timing;
expiry is simulated through ``FILE_COMPLETION_INDEX_TTL`` and explicit
invalidation via ``clear_file_completion_index``, never with real sleeps.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tau_coding.tui import autocomplete
from tau_coding.tui.autocomplete import clear_file_completion_index


@pytest.fixture(autouse=True)
def _clear_file_completion_index() -> Iterator[None]:
    """Run every test against an empty index so cases stay independent.

    The module-level registry is keyed by cwd and persists across tests; an
    autouse clear both before and after each test prevents one test's cached
    snapshot from leaking into the next.
    """
    clear_file_completion_index()
    yield
    clear_file_completion_index()


@pytest.fixture
def scan_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Wrap ``_scan_file_reference_paths`` and record every invocation.

    Counting raw scanner invocations is the only reliable way to prove the
    cache is actually hit: completion results are identical either way, so a
    naive result assertion would pass even if the cache were bypassed.
    """
    calls: list[Path] = []
    original_scan = autocomplete._scan_file_reference_paths

    def counting_scan(cwd: Path) -> tuple[Path, ...]:
        calls.append(cwd)
        return original_scan(cwd)

    monkeypatch.setattr(autocomplete, "_scan_file_reference_paths", counting_scan)
    return calls


def _build_sample_repo(root: Path) -> None:
    """Create a small workspace: nested source files plus an ignored node_modules."""
    (root / "README.md").write_text("# Project\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "src" / "lib").mkdir()
    (root / "src" / "lib" / "util.py").write_text(
        "def util() -> None:\n    pass\n", encoding="utf-8"
    )
    (root / "node_modules").mkdir()
    (root / "node_modules" / "ignored.js").write_text("ignored\n", encoding="utf-8")


def test_iter_file_reference_paths_scans_once_per_interaction(
    tmp_path: Path,
    scan_calls: list[Path],
) -> None:
    """The interaction-scoped index scans the repository exactly once per TTL window.

    Every keystroke while typing ``@...`` re-invokes ``_iter_file_reference_paths``,
    so a fresh recursive walk per keystroke is the pathological behavior this
    phase removes. Proving the wrapped scanner runs exactly once across several
    completion calls — and that every cached call returns the identical tuple —
    guards the cache against being bypassed, drifting between snapshots, or
    returning stale combinations of paths.
    """
    _build_sample_repo(tmp_path)

    for text in ("@s", "@sr", "@src", "@src/"):
        autocomplete._file_reference_completions(text=text, cwd=tmp_path)

    first = autocomplete._iter_file_reference_paths(tmp_path)
    second = autocomplete._iter_file_reference_paths(tmp_path)

    assert len(scan_calls) == 1
    assert first == second
    assert [path.relative_to(tmp_path).as_posix() for path in first] == [
        "README.md",
        "src",
        "src/app.py",
        "src/lib",
        "src/lib/util.py",
    ]


def test_file_completion_index_expiry_controls_rescanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scan_calls: list[Path],
) -> None:
    """A lapsed TTL forces a re-scan while a live entry is still reused.

    Expiry proves the cache is bounded in time rather than indefinite. With a
    TTL of zero the second call must count as expired and re-scan; with a large
    TTL the second call must be served from the cached snapshot. Time is
    simulated entirely through the TTL constant, so the test cannot flake on a
    slow machine.
    """
    _build_sample_repo(tmp_path)

    monkeypatch.setattr(autocomplete, "FILE_COMPLETION_INDEX_TTL", 0.0)
    autocomplete._iter_file_reference_paths(tmp_path)
    autocomplete._iter_file_reference_paths(tmp_path)
    assert len(scan_calls) == 2

    clear_file_completion_index()
    monkeypatch.setattr(autocomplete, "FILE_COMPLETION_INDEX_TTL", 3600.0)
    autocomplete._iter_file_reference_paths(tmp_path)
    autocomplete._iter_file_reference_paths(tmp_path)
    assert len(scan_calls) == 3


def test_cached_scan_still_excludes_ignored_paths(tmp_path: Path) -> None:
    """Ignored directories never enter the cached index (and never reappear).

    The index must not weaken the existing ignore behaviour: entries produced
    by a fresh scan and then reused from the cache must be exactly the same
    path tuple. node_modules and .git are excluded while ordinary dotfiles such
    as .env remain visible, mirroring pre-cache reference completion.
    """
    _build_sample_repo(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=value\n", encoding="utf-8")

    first = autocomplete._iter_file_reference_paths(tmp_path)
    second = autocomplete._iter_file_reference_paths(tmp_path)

    relative_names = {path.relative_to(tmp_path).as_posix() for path in second}
    assert first == second
    assert "node_modules/ignored.js" not in relative_names
    assert ".git/config" not in relative_names
    assert ".env" in relative_names


def test_clear_file_completion_index_forces_rescan(
    tmp_path: Path,
    scan_calls: list[Path],
) -> None:
    """Explicit invalidation ends the reuse window at interaction boundaries.

    ``clear_file_completion_index`` stands in for an interaction ending (a
    prompt submitted or the cwd changing). Proving a subsequent call re-scans
    keeps the index from becoming indefinite: the next interaction must observe
    fresh disk state instead of a snapshot from a previous one.
    """
    _build_sample_repo(tmp_path)

    autocomplete._file_reference_completions(text="@s", cwd=tmp_path)
    assert len(scan_calls) == 1

    clear_file_completion_index()
    autocomplete._file_reference_completions(text="@sr", cwd=tmp_path)
    assert len(scan_calls) == 2


def test_cached_and_fresh_completions_are_identical(tmp_path: Path) -> None:
    """Completion items computed from the cache equal a fresh-scan expectation.

    The index must be transparent to ``_file_reference_completions``: filtering
    ``@sr`` against the cached tuple must produce exactly the items the pre-cache
    code path would. Comparing display strings against a directly scanned
    expectation, plus an item-for-item equality check between a cached and a
    cache-cleared run, catches drift between the two code paths.
    """
    _build_sample_repo(tmp_path)

    scan_root = tmp_path.resolve()
    expected_paths = autocomplete._scan_file_reference_paths(scan_root)
    expected_displays = [
        f"@{path.relative_to(scan_root).as_posix()}{'/' if path.is_dir() else ''}"
        for path in expected_paths
        if "sr" in path.relative_to(scan_root).as_posix().lower()
    ]

    cached_items = autocomplete._file_reference_completions(text="@sr", cwd=tmp_path)
    clear_file_completion_index()
    fresh_items = autocomplete._file_reference_completions(text="@sr", cwd=tmp_path)

    assert [item.display for item in cached_items] == expected_displays
    assert [item.display for item in fresh_items] == expected_displays
    assert fresh_items == cached_items
