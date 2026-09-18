from __future__ import annotations

import hashlib
import os
import secrets
import stat
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

_CHUNK_SIZE = 1024 * 1024
_DIGEST_LENGTH = 64
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class AcquisitionArtifactStoreError(RuntimeError):
    def __init__(self, *, operation: str, detail: str) -> None:
        super().__init__(f"{operation} failed: {detail}")


class InvalidAcquisitionArtifactIdentifierError(AcquisitionArtifactStoreError):
    def __init__(self, detail: str) -> None:
        super().__init__(operation="validate artifact identifier", detail=detail)


class AcquisitionArtifactNotFoundError(AcquisitionArtifactStoreError):
    def __init__(self) -> None:
        super().__init__(operation="open artifact", detail="artifact does not exist")


class AcquisitionArtifactDigestMismatchError(AcquisitionArtifactStoreError):
    def __init__(self) -> None:
        super().__init__(operation="stream artifact", detail="content digest does not match")


class AcquisitionArtifactIntegrityError(AcquisitionArtifactStoreError):
    def __init__(self, detail: str) -> None:
        super().__init__(operation="verify artifact", detail=detail)


class LocalAcquisitionArtifactStore:
    def __init__(self, root: Path, *, chunk_size: int = _CHUNK_SIZE) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self._root = Path(os.path.abspath(root))
        self._chunk_size = chunk_size
        _ensure_private_directory(self._root)

    def put_if_absent(
        self,
        *,
        tenant_id: str,
        artifact_digest: str,
        reader: BinaryIO,
    ) -> None:
        tenant_name, digest = _validate_identifiers(tenant_id, artifact_digest)
        root_descriptor = _open_private_directory(self._root, description="artifact root")
        tenant_descriptor: int | None = None
        try:
            tenant_descriptor = _open_or_create_tenant_directory(root_descriptor, tenant_name)
            existing = self._open_verified_optional(tenant_descriptor, digest)
            if existing is not None:
                existing.close()
                self._synchronize_tenant_directory(tenant_descriptor)
                return
            self._write_and_publish(
                tenant_descriptor=tenant_descriptor,
                artifact_digest=digest,
                reader=reader,
            )
        except AcquisitionArtifactStoreError:
            raise
        except OSError:
            raise AcquisitionArtifactStoreError(
                operation="access artifact store",
                detail="storage operation failed",
            ) from None
        finally:
            if tenant_descriptor is not None:
                _close_safely(tenant_descriptor)
            _close_safely(root_descriptor)

    def open_verified(self, *, tenant_id: str, artifact_digest: str) -> BinaryIO:
        tenant_name, digest = _validate_identifiers(tenant_id, artifact_digest)
        root_descriptor = _open_private_directory(self._root, description="artifact root")
        tenant_descriptor: int | None = None
        try:
            tenant_descriptor = _open_existing_tenant_directory(root_descriptor, tenant_name)
            if tenant_descriptor is None:
                raise AcquisitionArtifactNotFoundError
            reader = self._open_verified_optional(tenant_descriptor, digest)
            if reader is None:
                raise AcquisitionArtifactNotFoundError
            return reader
        except AcquisitionArtifactStoreError:
            raise
        except OSError:
            raise AcquisitionArtifactIntegrityError(
                "artifact path is unsafe or unavailable"
            ) from None
        finally:
            if tenant_descriptor is not None:
                _close_safely(tenant_descriptor)
            _close_safely(root_descriptor)

    def exists_verified(self, *, tenant_id: str, artifact_digest: str) -> bool:
        tenant_name, digest = _validate_identifiers(tenant_id, artifact_digest)
        root_descriptor = _open_private_directory(self._root, description="artifact root")
        tenant_descriptor: int | None = None
        try:
            tenant_descriptor = _open_existing_tenant_directory(root_descriptor, tenant_name)
            if tenant_descriptor is None:
                return False
            reader = self._open_verified_optional(tenant_descriptor, digest)
            if reader is None:
                return False
            reader.close()
            return True
        except AcquisitionArtifactStoreError:
            raise
        except OSError:
            raise AcquisitionArtifactIntegrityError(
                "artifact path is unsafe or unavailable"
            ) from None
        finally:
            if tenant_descriptor is not None:
                _close_safely(tenant_descriptor)
            _close_safely(root_descriptor)

    def _write_and_publish(
        self,
        *,
        tenant_descriptor: int,
        artifact_digest: str,
        reader: BinaryIO,
    ) -> None:
        temporary_name = f".{artifact_digest}.{secrets.token_hex(12)}.tmp"
        descriptor: int | None = None
        primary_error: BaseException | None = None
        temporary_exists = False
        try:
            descriptor = os.open(
                temporary_name,
                _WRITE_FLAGS,
                0o600,
                dir_fd=tenant_descriptor,
            )
            temporary_exists = True
            observed_digest = self._stream_to_descriptor(reader, descriptor)
            if observed_digest != artifact_digest:
                raise AcquisitionArtifactDigestMismatchError
            try:
                _fsync(descriptor)
            except OSError:
                raise AcquisitionArtifactStoreError(
                    operation="synchronize artifact",
                    detail="storage operation failed",
                ) from None
            os.close(descriptor)
            descriptor = None
            concurrent = self._open_verified_optional(tenant_descriptor, artifact_digest)
            if concurrent is not None:
                concurrent.close()
            else:
                try:
                    _link(
                        temporary_name,
                        artifact_digest,
                        src_dir_fd=tenant_descriptor,
                        dst_dir_fd=tenant_descriptor,
                    )
                except FileExistsError:
                    concurrent = self._open_verified_optional(
                        tenant_descriptor,
                        artifact_digest,
                    )
                    if concurrent is None:
                        raise AcquisitionArtifactIntegrityError(
                            "concurrent artifact disappeared during publication"
                        ) from None
                    concurrent.close()
                except OSError:
                    raise AcquisitionArtifactStoreError(
                        operation="publish artifact",
                        detail="atomic no-clobber publication failed",
                    ) from None
            try:
                _unlink(temporary_name, dir_fd=tenant_descriptor)
                temporary_exists = False
            except FileNotFoundError:
                published = self._open_verified_optional(
                    tenant_descriptor,
                    artifact_digest,
                )
                if published is None:
                    raise AcquisitionArtifactIntegrityError(
                        "published artifact disappeared during cleanup"
                    ) from None
                published.close()
                temporary_exists = False
            except OSError:
                raise AcquisitionArtifactStoreError(
                    operation="clean up artifact",
                    detail="temporary file cleanup failed",
                ) from None
            self._synchronize_tenant_directory(tenant_descriptor)
        except BaseException as error:
            primary_error = error
        finally:
            if descriptor is not None:
                _close_safely(descriptor)
            if temporary_exists:
                try:
                    _unlink(temporary_name, dir_fd=tenant_descriptor)
                except FileNotFoundError:
                    pass
                except OSError:
                    if primary_error is None:
                        primary_error = AcquisitionArtifactStoreError(
                            operation="clean up artifact",
                            detail="temporary file cleanup failed",
                        )
        if primary_error is not None:
            raise primary_error from None

    @staticmethod
    def _synchronize_tenant_directory(tenant_descriptor: int) -> None:
        try:
            _fsync(tenant_descriptor)
        except OSError:
            raise AcquisitionArtifactStoreError(
                operation="synchronize artifact directory",
                detail="storage operation failed",
            ) from None

    def _stream_to_descriptor(self, reader: BinaryIO, descriptor: int) -> str:
        digest = hashlib.sha256()
        try:
            while True:
                chunk = reader.read(self._chunk_size)
                if chunk == b"":
                    return digest.hexdigest()
                if not isinstance(chunk, bytes):
                    raise TypeError("binary reader must return bytes")
                digest.update(chunk)
                _write_all(descriptor, chunk)
        except AcquisitionArtifactStoreError:
            raise
        except Exception:
            raise AcquisitionArtifactStoreError(
                operation="stream artifact",
                detail="binary reader or storage operation failed",
            ) from None

    def _open_verified_optional(
        self,
        tenant_descriptor: int,
        artifact_digest: str,
    ) -> BinaryIO | None:
        try:
            expected = os.stat(
                artifact_digest,
                dir_fd=tenant_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        expected = self._repair_store_temporary_link(
            tenant_descriptor,
            artifact_digest,
            expected,
        )
        _require_private_regular_file(expected)
        try:
            descriptor = os.open(artifact_digest, _READ_FLAGS, dir_fd=tenant_descriptor)
        except OSError:
            raise AcquisitionArtifactIntegrityError(
                "artifact path is unsafe or changed during access"
            ) from None
        try:
            opened = os.fstat(descriptor)
            _require_private_regular_file(opened)
            if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                raise AcquisitionArtifactIntegrityError("artifact changed during access")
            observed_digest = _digest_descriptor(descriptor, self._chunk_size)
            if observed_digest != artifact_digest:
                raise AcquisitionArtifactIntegrityError("artifact digest does not match its path")
            os.lseek(descriptor, 0, os.SEEK_SET)
            return os.fdopen(descriptor, "rb", closefd=True)
        except BaseException:
            _close_safely(descriptor)
            raise

    def _repair_store_temporary_link(
        self,
        tenant_descriptor: int,
        artifact_digest: str,
        expected: os.stat_result,
    ) -> os.stat_result:
        _require_private_regular_file_metadata(expected)
        if expected.st_nlink == 1:
            return expected
        if expected.st_nlink != 2:
            raise AcquisitionArtifactIntegrityError("artifact path is unsafe")
        try:
            names = os.listdir(tenant_descriptor)
        except OSError:
            raise AcquisitionArtifactIntegrityError(
                "artifact temporary link could not be inspected"
            ) from None
        matching_temporary_names: list[str] = []
        for name in names:
            if not _is_store_temporary_name(name, artifact_digest):
                continue
            try:
                candidate = os.stat(
                    name,
                    dir_fd=tenant_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if (candidate.st_dev, candidate.st_ino) == (expected.st_dev, expected.st_ino):
                matching_temporary_names.append(name)
        if len(matching_temporary_names) != 1:
            raise AcquisitionArtifactIntegrityError("artifact path is unsafe")
        self._synchronize_tenant_directory(tenant_descriptor)
        try:
            _unlink(matching_temporary_names[0], dir_fd=tenant_descriptor)
        except FileNotFoundError:
            pass
        except OSError:
            raise AcquisitionArtifactStoreError(
                operation="clean up artifact",
                detail="temporary link recovery failed",
            ) from None
        self._synchronize_tenant_directory(tenant_descriptor)
        try:
            current = os.stat(
                artifact_digest,
                dir_fd=tenant_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise AcquisitionArtifactIntegrityError(
                "artifact temporary link recovery could not be verified"
            ) from None
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise AcquisitionArtifactIntegrityError("artifact changed during recovery")
        _require_private_regular_file(current)
        return current


def _validate_identifiers(tenant_id: str, artifact_digest: str) -> tuple[str, str]:
    if not tenant_id:
        raise InvalidAcquisitionArtifactIdentifierError("tenant_id must be non-empty")
    if len(artifact_digest) != _DIGEST_LENGTH or any(
        character not in "0123456789abcdef" for character in artifact_digest
    ):
        raise InvalidAcquisitionArtifactIdentifierError(
            "artifact_digest must be a lowercase SHA-256 digest"
        )
    tenant_name = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
    return tenant_name, artifact_digest


def _ensure_private_directory(path: Path) -> None:
    try:
        os.mkdir(path, mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise AcquisitionArtifactStoreError(
            operation="initialize artifact store",
            detail="root directory creation failed",
        ) from None
    try:
        value = path.lstat()
    except OSError:
        raise AcquisitionArtifactStoreError(
            operation="initialize artifact store",
            detail="root directory inspection failed",
        ) from None
    _require_private_directory(value, description="artifact root")
    parent_descriptor: int | None = None
    try:
        parent_descriptor = os.open(path.parent, _DIRECTORY_FLAGS)
        _fsync(parent_descriptor)
    except OSError:
        raise AcquisitionArtifactStoreError(
            operation="synchronize artifact root directory",
            detail="storage operation failed",
        ) from None
    finally:
        if parent_descriptor is not None:
            _close_safely(parent_descriptor)


def _open_private_directory(path: Path, *, description: str) -> int:
    try:
        expected = path.lstat()
        _require_private_directory(expected, description=description)
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except AcquisitionArtifactStoreError:
        raise
    except OSError:
        raise AcquisitionArtifactIntegrityError(f"{description} is unsafe or unavailable") from None
    opened = os.fstat(descriptor)
    try:
        _require_private_directory(opened, description=description)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise AcquisitionArtifactIntegrityError(f"{description} changed during access")
    except BaseException:
        _close_safely(descriptor)
        raise
    return descriptor


def _open_or_create_tenant_directory(root_descriptor: int, tenant_name: str) -> int:
    with suppress(FileExistsError):
        os.mkdir(tenant_name, mode=0o700, dir_fd=root_descriptor)
    try:
        _fsync(root_descriptor)
    except OSError:
        raise AcquisitionArtifactStoreError(
            operation="synchronize tenant directory",
            detail="storage operation failed",
        ) from None
    descriptor = _open_existing_tenant_directory(root_descriptor, tenant_name)
    if descriptor is None:
        raise AcquisitionArtifactIntegrityError("tenant directory disappeared during access")
    return descriptor


def _open_existing_tenant_directory(root_descriptor: int, tenant_name: str) -> int | None:
    try:
        expected = os.stat(tenant_name, dir_fd=root_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    _require_private_directory(expected, description="tenant artifact directory")
    try:
        descriptor = os.open(tenant_name, _DIRECTORY_FLAGS, dir_fd=root_descriptor)
    except OSError:
        raise AcquisitionArtifactIntegrityError("tenant artifact directory is unsafe") from None
    opened = os.fstat(descriptor)
    try:
        _require_private_directory(opened, description="tenant artifact directory")
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise AcquisitionArtifactIntegrityError(
                "tenant artifact directory changed during access"
            )
    except BaseException:
        _close_safely(descriptor)
        raise
    return descriptor


def _require_private_directory(value: os.stat_result, *, description: str) -> None:
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o700
    ):
        raise AcquisitionArtifactIntegrityError(f"{description} is unsafe")


def _require_private_regular_file(value: os.stat_result) -> None:
    _require_private_regular_file_metadata(value)
    if value.st_nlink != 1:
        raise AcquisitionArtifactIntegrityError("artifact path is unsafe")


def _require_private_regular_file_metadata(value: os.stat_result) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o600
    ):
        raise AcquisitionArtifactIntegrityError("artifact path is unsafe")


def _is_store_temporary_name(name: str, artifact_digest: str) -> bool:
    prefix = f".{artifact_digest}."
    suffix = ".tmp"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return False
    token = name[len(prefix) : -len(suffix)]
    return len(token) == 24 and all(character in "0123456789abcdef" for character in token)


def _digest_descriptor(descriptor: int, chunk_size: int) -> str:
    digest = hashlib.sha256()
    try:
        while chunk := os.read(descriptor, chunk_size):
            digest.update(chunk)
    except OSError:
        raise AcquisitionArtifactIntegrityError("artifact could not be verified") from None
    return digest.hexdigest()


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("artifact write made no progress")
        offset += written


def _close_safely(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)


def _fsync(descriptor: int) -> None:
    os.fsync(descriptor)


def _link(
    source: str,
    destination: str,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    os.link(
        source,
        destination,
        src_dir_fd=src_dir_fd,
        dst_dir_fd=dst_dir_fd,
        follow_symlinks=False,
    )


def _unlink(path: str, *, dir_fd: int) -> None:
    os.unlink(path, dir_fd=dir_fd)
