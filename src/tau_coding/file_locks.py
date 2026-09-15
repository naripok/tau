"""Cross-process advisory locks for shared user-home state files."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.util import find_spec
from pathlib import Path
from typing import IO


@contextmanager
def exclusive_file_lock(target: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``<target>.lock`` across the block.

    Multiple Tau processes (for example a parent session and spawned
    subagents) share state files such as the session index. An advisory lock
    serializes their read-modify-write cycles: the loser re-reads the file
    after the winner's write instead of clobbering it with stale content. The
    lock file is a persistent sibling of the target file; deleting it reopens
    the race, so nothing removes it.

    A platform with no ``fcntl`` or ``msvcrt`` primitive has no cross-process
    lock and the block runs unlocked.
    """
    lock_path = Path(f"{target}.lock")
    primitive = "msvcrt" if os.name == "nt" else "fcntl"
    if find_spec(primitive) is None:
        yield
        return
    handle = lock_path.open("a+b")
    try:
        lock_file(handle)
        try:
            yield
        finally:
            unlock_file(handle)
    finally:
        handle.close()


def lock_file(handle: IO[bytes]) -> None:
    """Lock ``handle`` exclusively; an ``OSError`` propagates to the caller."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def unlock_file(handle: IO[bytes]) -> None:
    """Release the advisory lock; closing ``handle`` releases it either way."""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
