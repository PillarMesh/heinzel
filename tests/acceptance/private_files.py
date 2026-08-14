from __future__ import annotations

import os
import secrets
import stat
from contextlib import suppress
from pathlib import Path
from types import TracebackType

from .config import AcceptanceConfig, HarnessError

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def require_private_directory(path: Path) -> os.stat_result:
    current = Path(os.path.abspath(path))
    while True:
        if current.is_symlink():
            raise HarnessError("private parent directory is unsafe")
        if current == current.parent:
            break
        current = current.parent
    try:
        value = path.lstat()
    except FileNotFoundError:
        raise HarnessError("private parent directory does not exist") from None
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise HarnessError("private parent directory is unsafe")
    if value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) != 0o700:
        raise HarnessError("private parent directory must be owner-only mode 0700")
    return value


def ensure_private_directory(path: Path) -> os.stat_result:
    with suppress(FileExistsError):
        os.mkdir(path, mode=0o700)
    return require_private_directory(path)


def _open_parent(path: Path) -> int:
    expected = require_private_directory(path)
    descriptor = os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(descriptor)
        raise HarnessError("private parent directory changed during access")
    return descriptor


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])


def write_private_file(path: Path, payload: bytes) -> None:
    parent_fd = _open_parent(path.parent)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except FileExistsError:
        raise HarnessError("private file already exists") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def atomic_private_replace(
    path: Path,
    payload: bytes,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    parent_fd = _open_parent(path.parent)
    temporary_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
    descriptor: int | None = None
    replacement_identity: tuple[int, int] | None = None
    try:
        try:
            destination = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            destination = None
        if expected_identity is not None and (
            destination is None or (destination.st_dev, destination.st_ino) != expected_identity
        ):
            raise HarnessError("private cleanup ledger changed during run")
        if destination is not None and (
            not stat.S_ISREG(destination.st_mode)
            or destination.st_uid != os.getuid()
            or stat.S_IMODE(destination.st_mode) != 0o600
        ):
            raise HarnessError("private cleanup ledger destination is unsafe")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        replacement = os.fstat(descriptor)
        replacement_identity = (replacement.st_dev, replacement.st_ino)
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        return replacement_identity
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=parent_fd)
        os.close(parent_fd)


class RetainedPrivateFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._parent_fd: int | None = None
        self._descriptor: int | None = None
        self._identity: tuple[int, int] | None = None

    def __enter__(self) -> RetainedPrivateFile:
        self._parent_fd = _open_parent(self.path.parent)
        try:
            self._descriptor = os.open(
                self.path.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
                dir_fd=self._parent_fd,
            )
        except FileExistsError:
            os.close(self._parent_fd)
            self._parent_fd = None
            raise HarnessError("private file was created concurrently") from None
        value = os.fstat(self._descriptor)
        self._identity = (value.st_dev, value.st_ino)
        return self

    def assert_intact(self) -> None:
        if self._parent_fd is None or self._descriptor is None or self._identity is None:
            raise HarnessError("private file reservation is not active")
        try:
            value = os.stat(self.path.name, dir_fd=self._parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise HarnessError("private path changed during run") from None
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.getuid()
            or stat.S_IMODE(value.st_mode) != 0o600
            or (value.st_dev, value.st_ino) != self._identity
        ):
            raise HarnessError("private path changed during run")

    def replace(self, payload: bytes) -> None:
        self.assert_intact()
        identity = self._identity
        if identity is None:
            raise HarnessError("private file reservation is not active")
        replacement_identity = atomic_private_replace(
            self.path, payload, expected_identity=identity
        )
        if self._descriptor is not None:
            os.close(self._descriptor)
        if self._parent_fd is None:
            raise HarnessError("private file reservation is not active")
        self._descriptor = os.open(
            self.path.name,
            os.O_RDONLY | _NOFOLLOW,
            dir_fd=self._parent_fd,
        )
        value = os.fstat(self._descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.getuid()
            or stat.S_IMODE(value.st_mode) != 0o600
            or (value.st_dev, value.st_ino) != replacement_identity
        ):
            raise HarnessError("private cleanup ledger changed during run")
        observed = bytearray()
        while chunk := os.read(self._descriptor, 1024 * 1024):
            observed.extend(chunk)
        if bytes(observed) != payload:
            raise HarnessError("private cleanup ledger changed during run")
        self._identity = (value.st_dev, value.st_ino)
        self.assert_intact()

    def close(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        if self._parent_fd is not None:
            os.close(self._parent_fd)
            self._parent_fd = None
        self._identity = None

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


def read_private_file(path: Path) -> bytes:
    parent_fd = _open_parent(path.parent)
    descriptor: int | None = None
    try:
        value = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.getuid()
            or stat.S_IMODE(value.st_mode) != 0o600
        ):
            raise HarnessError("private cleanup ledger is unsafe")
        descriptor = os.open(path.name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    except (FileNotFoundError, OSError):
        raise HarnessError("private cleanup ledger is unsafe") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


class EnvironmentReservation:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock_parent_fd: int | None = None
        self._lock_fd: int | None = None
        self._lock_name = path.name
        self._identity: tuple[int, int] | None = None

    def __enter__(self) -> EnvironmentReservation:
        ensure_private_directory(self._path.parent)
        self._lock_parent_fd = _open_parent(self._path.parent)
        try:
            self._lock_fd = os.open(
                self._lock_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
                dir_fd=self._lock_parent_fd,
            )
            value = os.fstat(self._lock_fd)
            self._identity = (value.st_dev, value.st_ino)
        except FileExistsError:
            os.close(self._lock_parent_fd)
            self._lock_parent_fd = None
            raise HarnessError("acceptance environment is already reserved") from None
        return self

    def assert_intact(self) -> None:
        if self._lock_parent_fd is None or self._lock_fd is None:
            raise HarnessError("acceptance reservation is not active")
        try:
            value = os.stat(
                self._lock_name,
                dir_fd=self._lock_parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            raise HarnessError("acceptance reservation changed during run") from None
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_IMODE(value.st_mode) != 0o600
            or (value.st_dev, value.st_ino) != self._identity
        ):
            raise HarnessError("acceptance reservation changed during run")

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        if self._lock_parent_fd is not None:
            try:
                current = os.stat(
                    self._lock_name,
                    dir_fd=self._lock_parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == self._identity:
                    os.unlink(self._lock_name, dir_fd=self._lock_parent_fd)
            os.close(self._lock_parent_fd)
            self._lock_parent_fd = None
        self._identity = None


class RunReservation:
    def __init__(self, config: AcceptanceConfig) -> None:
        self._config = config
        self._environment = EnvironmentReservation(config.reservation_path)
        self._parents: dict[Path, tuple[int, int]] = {}
        self._output_identity: tuple[int, int] | None = None
        self._state = RetainedPrivateFile(config.state_path)
        self._ledger = RetainedPrivateFile(config.cleanup_ledger_path)

    def __enter__(self) -> RunReservation:
        ensure_private_directory(self._config.reservation_path.parent)
        for parent in {
            self._config.state_path.parent,
            self._config.output_dir.parent,
            self._config.cleanup_ledger_path.parent,
            self._config.reservation_path.parent,
        }:
            value = require_private_directory(parent)
            self._parents[parent] = (value.st_dev, value.st_ino)
        self._environment.__enter__()
        try:
            try:
                os.mkdir(self._config.output_dir, mode=0o700)
            except FileExistsError:
                raise HarnessError("PILLARMESH_OUTPUT_DIR already exists") from None
            output = self._config.output_dir.lstat()
            self._output_identity = (output.st_dev, output.st_ino)
            self._state.__enter__()
            self._ledger.__enter__()
            self.assert_intact()
        except BaseException as error:
            self._state.close()
            self._ledger.close()
            self._environment.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def write_ledger(self, payload: bytes) -> None:
        self.assert_intact()
        self._ledger.replace(payload)
        self.assert_intact()

    def assert_intact(self) -> None:
        self._environment.assert_intact()
        for parent, identity in self._parents.items():
            value = require_private_directory(parent)
            if (value.st_dev, value.st_ino) != identity:
                raise HarnessError("private path changed during run")
        try:
            output = self._config.output_dir.lstat()
        except FileNotFoundError:
            raise HarnessError("private path changed during run") from None
        if (
            stat.S_ISLNK(output.st_mode)
            or not stat.S_ISDIR(output.st_mode)
            or output.st_uid != os.getuid()
            or stat.S_IMODE(output.st_mode) != 0o700
            or (output.st_dev, output.st_ino) != self._output_identity
        ):
            raise HarnessError("private path changed during run")
        self._state.assert_intact()
        self._ledger.assert_intact()

    def assert_private_tree(self) -> None:
        self.assert_intact()
        for root, directories, files in os.walk(self._config.output_dir, followlinks=False):
            root_path = Path(root)
            for name in (*directories, *files):
                path = root_path / name
                value = path.lstat()
                if stat.S_ISLNK(value.st_mode) or value.st_uid != os.getuid():
                    raise HarnessError("private output tree is unsafe")
                mode = stat.S_IMODE(value.st_mode)
                if mode & 0o077:
                    raise HarnessError("private output tree is not owner-only")

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._state.close()
        self._ledger.close()
        self._environment.__exit__(_exception_type, _exception, _traceback)
