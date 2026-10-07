"""Durable, owner-only secret files under the demonstration's state directory.

Two of the demonstration's secrets outlive the process that made them: the cursor cipher's key,
without which every committed cursor is unopenable, and the dashboard contract signing key, without
which every stored contract fails verification. Both must therefore be made durable before anything
is sealed or signed under them, and both must be created unreadable by anyone but their owner from
the first instant they exist.

This is the one implementation of that, so there is one place where the flags, the mode, and the
two fsyncs are right. What it does not stand in for is key management: these are files under a
directory, never rotated, and deleting one discards everything made under it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

__all__ = ["create_secret_file", "read_secret_file"]

_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)


def read_secret_file(path: Path, *, length: int, refuse: Callable[[], Exception]) -> bytes | None:
    """The secret stored at `path`, or `None` when nothing has been stored there yet.

    Any other failure to read it is the filesystem's, and is reported as the `OSError` it is: that
    names the path and the reason, which the caller's own error does not.
    """
    try:
        descriptor = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        return None
    # One byte more than the secret, so that an overlong file is seen as one rather than silently
    # truncated to something that looks like a secret.
    with os.fdopen(descriptor, "rb") as secret_file:
        stored = secret_file.read(length + 1)
    if len(stored) != length:
        # A short or overlong file is not the secret. Refusing is the only safe answer: carrying on
        # would either fail inside a driver or use something that is not what the committed state
        # was made under.
        raise refuse()
    return stored


def create_secret_file(path: Path, payload: bytes, *, refuse: Callable[[], Exception]) -> None:
    """Create `path` readable only by its owner, or raise `FileExistsError`.

    The mode is set by `os.open` rather than by a later `chmod`: a file written first and restricted
    second is readable by everyone for as long as it takes to reach the second call.
    """
    descriptor = os.open(path, _CREATE_FLAGS, 0o600)
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            # A partial file is not the secret, and the next start will say so rather than use half
            # of one.
            raise refuse()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    # The contents are durable above; this is the file's name. Without the directory's own fsync a
    # crash can lose the name while keeping the state already committed beside it.
    directory_descriptor = os.open(path.parent, _DIRECTORY_FLAGS)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
