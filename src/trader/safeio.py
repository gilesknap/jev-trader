"""Read the strategist's files without following symlinks (issue #49).

The strategist writes state/ and features/custom/ as another, less trusted user. A symlink there
would make the runner read, and quote in alerts, any file the runner can read. So every path
component below STRATEGIST_ROOT is opened with O_NOFOLLOW relative to its parent's descriptor:
a symlink swapped in after any check still can't redirect the read. Paths outside the root (an
explicit `--file`) only get the last component checked. Only regular files are read, so a FIFO
can't hang the runner either.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from trader import config

MAX_BYTES = 1_000_000  # per file: far above any real spec or feature, and it bounds runner memory


class UnsafePath(ValueError):
    """A strategist path the runner refuses to read (a symlink, or not a regular file)."""


def _open_at(dir_fd: int | None, name: str, flags: int, shown: Path) -> int:
    try:
        return os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise FileNotFoundError(f"{shown} not found") from None
        try:
            link = stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
        except OSError:
            link = False
        if link:
            raise UnsafePath(f"{shown} is a symlink, which the runner won't follow: "
                             "use a real file or directory") from None
        raise UnsafePath(f"{shown} can't be opened: {e.strerror}") from None


def _open_dir(path: Path) -> int:
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(config.STRATEGIST_ROOT))
    anchor, parts = (root, path.relative_to(root).parts) if path.is_relative_to(root) else (path, ())
    try:  # the anchor is the runner's own configuration, not the strategist's
        fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError:
        raise FileNotFoundError(f"{anchor} not found") from None
    try:
        for i, part in enumerate(parts):
            nxt = _open_at(fd, part, os.O_RDONLY | os.O_DIRECTORY, anchor.joinpath(*parts[: i + 1]))
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_at(dir_fd: int, name: str, shown: Path) -> bytes:
    fd = _open_at(dir_fd, name, os.O_RDONLY | os.O_NONBLOCK, shown)
    try:  # check the raw fd: fdopen() itself raises on a directory, and fd would leak
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafePath(f"{shown} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as f:
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise UnsafePath(f"{shown} is too large (over {MAX_BYTES // 1_000_000} MB)")
    return data


def read_text(path: Path) -> str:
    """Like Path.read_text(), but refuses symlinks (UnsafePath) and says "not found" when missing."""
    path = Path(os.path.abspath(path))
    d = _open_dir(path.parent)
    try:
        return _read_at(d, path.name, path).decode()
    finally:
        os.close(d)


def read_sources(directory: Path) -> tuple[dict[str, bytes], dict[str, str]]:
    """The *.py files in `directory`: ({name: source}, {name: why it was refused}), both sorted by
    name. A missing directory has none; a symlinked directory raises UnsafePath."""
    try:
        d = _open_dir(directory)
    except FileNotFoundError:
        return {}, {}
    sources, refused = {}, {}
    try:
        for name in sorted(os.listdir(d)):
            if name.endswith(".py"):
                try:
                    sources[name] = _read_at(d, name, Path(directory) / name)
                except (OSError, UnsafePath) as e:
                    refused[name] = str(e)
    finally:
        os.close(d)
    return sources, refused
