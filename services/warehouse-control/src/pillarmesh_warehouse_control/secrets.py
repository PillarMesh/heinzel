from __future__ import annotations

import fcntl
import os
import re
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from cryptography.fernet import Fernet, InvalidToken
from pillarmesh_contract_model import digest
from pydantic import BaseModel, ConfigDict, SecretStr

from .errors import WarehouseSecretRetiredError, WarehouseSecretStorageError

type WarehouseOperationSecretPurpose = Literal[
    "administration",
    "ingestion_runtime",
    "transformation_runtime",
    "customer_sql",
    "catalog",
    "bi",
    "tls_private_key",
    "tls_certificate",
]
type _WarehouseSecretRetirementState = Literal["absent", "complete", "incomplete"]

_OPERATION_ID_PATTERN = re.compile(r"^wop-[0-9a-f]{24}$")
_PURPOSE_FIELDS: dict[str, str] = {
    "administration": "administration_password",
    "ingestion_runtime": "ingestion_runtime_password",
    "transformation_runtime": "transformation_runtime_password",
    "customer_sql": "customer_sql_probe_password",
    "catalog": "catalog_password",
    "bi": "bi_password",
    "tls_private_key": "tls_private_key_pem",
    "tls_certificate": "tls_certificate_pem",
}
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_UPDATE_FLAGS = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
# Fernet's smallest framed token is 73 raw bytes, or 100 URL-safe base64 bytes.
_MINIMUM_COMPLETE_FERNET_TOKEN_BYTES = 100


class WarehouseOperationSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    administration_password: SecretStr
    ingestion_runtime_password: SecretStr
    transformation_runtime_password: SecretStr
    backup_restore_password: SecretStr
    customer_sql_probe_password: SecretStr
    catalog_password: SecretStr
    bi_password: SecretStr
    tls_private_key_pem: SecretStr
    tls_certificate_pem: SecretStr
    backup_encryption_key_b64: SecretStr


class _StoredWarehouseOperationSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    operation_id: str
    administration_password: str
    ingestion_runtime_password: str
    transformation_runtime_password: str
    backup_restore_password: str
    customer_sql_probe_password: str
    catalog_password: str
    bi_password: str
    tls_private_key_pem: str
    tls_certificate_pem: str
    backup_encryption_key_b64: str

    @classmethod
    def from_operation_secrets(
        cls, operation_id: str, operation_secrets: WarehouseOperationSecrets
    ) -> _StoredWarehouseOperationSecrets:
        return cls(
            operation_id=operation_id,
            administration_password=(operation_secrets.administration_password.get_secret_value()),
            ingestion_runtime_password=(
                operation_secrets.ingestion_runtime_password.get_secret_value()
            ),
            transformation_runtime_password=(
                operation_secrets.transformation_runtime_password.get_secret_value()
            ),
            backup_restore_password=(operation_secrets.backup_restore_password.get_secret_value()),
            customer_sql_probe_password=(
                operation_secrets.customer_sql_probe_password.get_secret_value()
            ),
            catalog_password=operation_secrets.catalog_password.get_secret_value(),
            bi_password=operation_secrets.bi_password.get_secret_value(),
            tls_private_key_pem=operation_secrets.tls_private_key_pem.get_secret_value(),
            tls_certificate_pem=operation_secrets.tls_certificate_pem.get_secret_value(),
            backup_encryption_key_b64=(
                operation_secrets.backup_encryption_key_b64.get_secret_value()
            ),
        )

    def to_operation_secrets(self) -> WarehouseOperationSecrets:
        return WarehouseOperationSecrets(
            administration_password=SecretStr(self.administration_password),
            ingestion_runtime_password=SecretStr(self.ingestion_runtime_password),
            transformation_runtime_password=SecretStr(self.transformation_runtime_password),
            backup_restore_password=SecretStr(self.backup_restore_password),
            customer_sql_probe_password=SecretStr(self.customer_sql_probe_password),
            catalog_password=SecretStr(self.catalog_password),
            bi_password=SecretStr(self.bi_password),
            tls_private_key_pem=SecretStr(self.tls_private_key_pem),
            tls_certificate_pem=SecretStr(self.tls_certificate_pem),
            backup_encryption_key_b64=SecretStr(self.backup_encryption_key_b64),
        )


class WarehouseSecretStore(Protocol):
    def resolve(self) -> SecretStr: ...


class WarehouseOperationSecretCapability(WarehouseSecretStore, Protocol):
    pass


class WarehouseBackupCommandSecretCapability(Protocol):
    def resolve_backup_restore_password(self) -> SecretStr: ...

    def resolve_backup_encryption_key(self) -> SecretStr: ...


class WarehouseBackupRetirementCapability(Protocol):
    @property
    def resource_handle(self) -> str: ...

    def retire(self) -> None: ...

    def is_retired(self) -> bool: ...


@dataclass(frozen=True, slots=True, repr=False)
class _OperationSecretCapability:
    value: SecretStr

    def resolve(self) -> SecretStr:
        return self.value


@dataclass(frozen=True, slots=True, repr=False)
class _BackupCommandSecretCapability:
    backup_restore_password: SecretStr
    backup_encryption_key: SecretStr

    def resolve_backup_restore_password(self) -> SecretStr:
        return self.backup_restore_password

    def resolve_backup_encryption_key(self) -> SecretStr:
        return self.backup_encryption_key


@dataclass(frozen=True, slots=True, repr=False)
class _BackupRetirementCapability:
    resource_handle: str
    _retire_callback: Callable[[], None]
    _retired_check: Callable[[], bool]

    def retire(self) -> None:
        self._retire_callback()

    def is_retired(self) -> bool:
        return self._retired_check()


class _EncryptedDirectoryWarehouseSecretAuthority:
    def __init__(self, *, directory: Path, key: SecretStr) -> None:
        try:
            fernet = Fernet(key.get_secret_value().encode("ascii"))
            directory_descriptor = _open_private_directory(directory.absolute())
        except Exception:
            pass
        else:
            self._fernet = fernet
            self._directory_descriptor = directory_descriptor
            self._namespace_lock = threading.RLock()
            return
        raise WarehouseSecretStorageError from None

    def __del__(self) -> None:
        descriptor = getattr(self, "_directory_descriptor", None)
        if isinstance(descriptor, int) and descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
            self._directory_descriptor = -1

    def store(self, operation_id: str, secrets: WarehouseOperationSecrets) -> str:
        try:
            with self._serialized_operation(operation_id):
                return self._store(operation_id, secrets)
        except WarehouseSecretRetiredError:
            raise
        except Exception:
            pass
        raise WarehouseSecretStorageError from None

    def delete(self, secret_reference: str, *, operation_id: str) -> None:
        try:
            with self._serialized_operation(operation_id):
                self._delete(secret_reference, operation_id=operation_id)
        except Exception:
            pass
        else:
            return
        raise WarehouseSecretStorageError from None

    def operation_capability(
        self,
        secret_reference: str,
        *,
        operation_id: str,
        purpose: WarehouseOperationSecretPurpose,
    ) -> WarehouseOperationSecretCapability:
        try:
            with self._serialized_operation(operation_id):
                if secret_reference != _secret_reference(operation_id):
                    raise WarehouseSecretStorageError
                field = _PURPOSE_FIELDS.get(purpose)
                if field is None:
                    raise WarehouseSecretStorageError
                secrets = self._load_active_secret(operation_id)
                value = getattr(secrets.to_operation_secrets(), field)
                if not isinstance(value, SecretStr):
                    raise WarehouseSecretStorageError
                return _OperationSecretCapability(value=value)
        except WarehouseSecretRetiredError:
            raise
        except Exception:
            pass
        raise WarehouseSecretStorageError from None

    def backup_command_capability(
        self, secret_reference: str, *, operation_id: str
    ) -> WarehouseBackupCommandSecretCapability:
        try:
            with self._serialized_operation(operation_id):
                if secret_reference != _secret_reference(operation_id):
                    raise WarehouseSecretStorageError
                stored = self._load_active_secret(operation_id)
                secrets = stored.to_operation_secrets()
                return _BackupCommandSecretCapability(
                    backup_restore_password=secrets.backup_restore_password,
                    backup_encryption_key=secrets.backup_encryption_key_b64,
                )
        except WarehouseSecretRetiredError:
            raise
        except Exception:
            pass
        raise WarehouseSecretStorageError from None

    def backup_retirement_capability(
        self, secret_reference: str, *, operation_id: str
    ) -> WarehouseBackupRetirementCapability:
        try:
            if secret_reference != _secret_reference(operation_id):
                raise WarehouseSecretStorageError
        except Exception:
            pass
        else:
            return _BackupRetirementCapability(
                resource_handle=f"{secret_reference}#backup-encryption-key",
                _retire_callback=lambda: self.delete(
                    secret_reference,
                    operation_id=operation_id,
                ),
                _retired_check=lambda: self._secret_is_retired(
                    secret_reference,
                    operation_id=operation_id,
                ),
            )
        raise WarehouseSecretStorageError from None

    def _secret_is_retired(self, secret_reference: str, *, operation_id: str) -> bool:
        try:
            with self._serialized_operation(operation_id):
                if secret_reference != _secret_reference(operation_id):
                    raise WarehouseSecretStorageError
                return self._retirement_state(operation_id) == "complete"
        except Exception:
            pass
        raise WarehouseSecretStorageError from None

    def _retirement_state(self, operation_id: str) -> _WarehouseSecretRetirementState:
        destination = _secret_name(operation_id)
        tombstone = _tombstone_name(destination)
        tombstone_status = _lstat_optional(self._directory_descriptor, tombstone)
        if tombstone_status is None:
            return "absent"
        _verify_durable_private_marker(
            self._directory_descriptor,
            tombstone,
            tombstone_status,
        )
        active_statuses = self._load_delete_residue(
            operation_id,
            destination=destination,
            temporary=_temporary_name(destination),
            reject_nonempty_incomplete=True,
        )
        if any(status_result.st_size != 0 for _name, status_result in active_statuses):
            return "incomplete"
        return "complete"

    @contextmanager
    def _serialized_operation(self, operation_id: str) -> Iterator[None]:
        with self._namespace_lock:
            fcntl.flock(self._directory_descriptor, fcntl.LOCK_EX)
            try:
                self._reject_legacy_quarantines(operation_id)
                yield
            finally:
                with suppress(OSError):
                    fcntl.flock(self._directory_descriptor, fcntl.LOCK_UN)

    def _reject_legacy_quarantines(self, operation_id: str) -> None:
        destination = _secret_name(operation_id)
        for name in (
            destination,
            _temporary_name(destination),
            _tombstone_name(destination),
        ):
            if _lstat_optional(self._directory_descriptor, _quarantine_name(name)) is not None:
                raise WarehouseSecretStorageError

    def _store(self, operation_id: str, secrets: WarehouseOperationSecrets) -> str:
        reference = _secret_reference(operation_id)
        stored = _StoredWarehouseOperationSecrets.from_operation_secrets(operation_id, secrets)
        destination = _secret_name(operation_id)
        temporary = _temporary_name(destination)
        retirement_state = self._retirement_state(operation_id)
        if retirement_state == "complete":
            raise WarehouseSecretRetiredError
        if retirement_state == "incomplete":
            raise WarehouseSecretStorageError
        encrypted = self._fernet.encrypt(stored.model_dump_json().encode("utf-8"))
        self._reconcile_store_residue(
            operation_id,
            stored,
            destination=destination,
            temporary=temporary,
            payload=encrypted,
        )
        return reference

    def _reconcile_store_residue(
        self,
        operation_id: str,
        expected: _StoredWarehouseOperationSecrets,
        *,
        destination: str,
        temporary: str,
        payload: bytes,
    ) -> None:
        destination_status = _lstat_optional(self._directory_descriptor, destination)
        temporary_status = _lstat_optional(self._directory_descriptor, temporary)
        if destination_status is None:
            existing_temporary_stored: _StoredWarehouseOperationSecrets | None = None
            if temporary_status is not None:
                existing_temporary_stored, temporary_status = self._load_incomplete_residue(
                    temporary,
                    operation_id,
                    allowed_links=frozenset({1}),
                )
                if existing_temporary_stored is not None and existing_temporary_stored != expected:
                    raise WarehouseSecretStorageError
            _atomic_private_write(
                self._directory_descriptor,
                destination=destination,
                payload=payload,
            )
            written_destination_stored, _ = self._load(destination, operation_id)
            if written_destination_stored != expected:
                raise WarehouseSecretStorageError
            if temporary_status is not None and existing_temporary_stored is None:
                _clear_private_file(
                    self._directory_descriptor,
                    temporary,
                    temporary_status,
                )
            os.fsync(self._directory_descriptor)
            return

        allowed_destination_links = (
            frozenset({1, 2}) if temporary_status is not None else frozenset({1})
        )
        destination_stored, destination_status = self._load_incomplete_residue(
            destination,
            operation_id,
            allowed_links=allowed_destination_links,
        )
        temporary_stored: _StoredWarehouseOperationSecrets | None = None
        if temporary_status is not None:
            temporary_stored, temporary_status = self._load_incomplete_residue(
                temporary,
                operation_id,
                allowed_links=frozenset({1, 2}),
            )
            if destination_status is not None and _same_inode(
                destination_status,
                temporary_status,
            ):
                if destination_status.st_nlink != 2 or destination_stored != temporary_stored:
                    raise WarehouseSecretStorageError
            elif destination_status.st_nlink != 1 or temporary_status.st_nlink != 1:
                raise WarehouseSecretStorageError
            if temporary_stored is not None and temporary_stored != expected:
                raise WarehouseSecretStorageError

        if destination_stored is None:
            _replace_private_file_contents(
                self._directory_descriptor,
                destination,
                destination_status,
                payload,
            )
            destination_stored, destination_status = self._load(
                destination,
                operation_id,
                allowed_links=allowed_destination_links,
            )
        if destination_stored != expected:
            raise WarehouseSecretStorageError
        if (
            temporary_status is not None
            and temporary_stored is None
            and not _same_inode(destination_status, temporary_status)
        ):
            _clear_private_file(
                self._directory_descriptor,
                temporary,
                temporary_status,
            )
        os.fsync(self._directory_descriptor)

    def _delete(self, secret_reference: str, *, operation_id: str) -> None:
        if secret_reference != _secret_reference(operation_id):
            raise WarehouseSecretStorageError
        destination = _secret_name(operation_id)
        temporary = _temporary_name(destination)
        tombstone = _tombstone_name(destination)
        tombstone_status = _lstat_optional(self._directory_descriptor, tombstone)
        if tombstone_status is not None:
            tombstone_status = _verify_durable_private_marker(
                self._directory_descriptor,
                tombstone,
                tombstone_status,
            )
            active_statuses = self._load_delete_residue(
                operation_id,
                destination=destination,
                temporary=temporary,
                reject_nonempty_incomplete=True,
            )
        else:
            active_statuses = self._load_delete_residue(
                operation_id,
                destination=destination,
                temporary=temporary,
                reject_nonempty_incomplete=False,
            )
            tombstone_status = _create_durable_private_marker(
                self._directory_descriptor,
                tombstone,
            )
        self._clear_delete_residue(active_statuses)
        current_tombstone_status = _lstat_required(self._directory_descriptor, tombstone)
        _validate_private_file_status(
            current_tombstone_status,
            allowed_links=frozenset({1}),
        )
        if (
            not _same_inode(tombstone_status, current_tombstone_status)
            or current_tombstone_status.st_size != 0
        ):
            raise WarehouseSecretStorageError
        os.fsync(self._directory_descriptor)

    def _load_delete_residue(
        self,
        operation_id: str,
        *,
        destination: str,
        temporary: str,
        reject_nonempty_incomplete: bool,
    ) -> tuple[tuple[str, os.stat_result], ...]:
        destination_status = _lstat_optional(self._directory_descriptor, destination)
        temporary_status = _lstat_optional(self._directory_descriptor, temporary)
        if destination_status is None and temporary_status is None:
            return ()
        allowed_links = (
            frozenset({1, 2})
            if destination_status is not None and temporary_status is not None
            else frozenset({1})
        )
        destination_stored: _StoredWarehouseOperationSecrets | None = None
        if destination_status is not None:
            destination_stored, destination_status = self._load_incomplete_residue(
                destination,
                operation_id,
                allowed_links=allowed_links,
            )
            if (
                reject_nonempty_incomplete
                and destination_stored is None
                and destination_status.st_size != 0
            ):
                raise WarehouseSecretStorageError
        temporary_stored: _StoredWarehouseOperationSecrets | None = None
        if temporary_status is not None:
            temporary_stored, temporary_status = self._load_incomplete_residue(
                temporary,
                operation_id,
                allowed_links=allowed_links,
            )
            if (
                reject_nonempty_incomplete
                and temporary_stored is None
                and temporary_status.st_size != 0
            ):
                raise WarehouseSecretStorageError
        if destination_status is not None and temporary_status is not None:
            if _same_inode(destination_status, temporary_status):
                if destination_status.st_nlink != 2 or destination_stored != temporary_stored:
                    raise WarehouseSecretStorageError
            elif destination_status.st_nlink != 1 or temporary_status.st_nlink != 1:
                raise WarehouseSecretStorageError
        return tuple(
            (name, status_result)
            for name, status_result in (
                (destination, destination_status),
                (temporary, temporary_status),
            )
            if status_result is not None
        )

    def _clear_delete_residue(
        self,
        active_statuses: tuple[tuple[str, os.stat_result], ...],
    ) -> None:
        cleared_inodes: set[tuple[int, int]] = set()
        for name, status_result in active_statuses:
            inode = (status_result.st_dev, status_result.st_ino)
            if inode in cleared_inodes:
                continue
            _clear_private_file(
                self._directory_descriptor,
                name,
                status_result,
            )
            cleared_inodes.add(inode)
        for name, expected_status in active_statuses:
            current_status = _lstat_required(self._directory_descriptor, name)
            _validate_private_file_status(
                current_status,
                allowed_links=frozenset({expected_status.st_nlink}),
            )
            if not _same_inode(expected_status, current_status) or current_status.st_size != 0:
                raise WarehouseSecretStorageError

    def _load_active_secret(self, operation_id: str) -> _StoredWarehouseOperationSecrets:
        destination = _secret_name(operation_id)
        retirement_state = self._retirement_state(operation_id)
        if retirement_state == "complete":
            raise WarehouseSecretRetiredError
        if retirement_state == "incomplete":
            raise WarehouseSecretStorageError
        temporary = _temporary_name(destination)
        temporary_status = _lstat_optional(self._directory_descriptor, temporary)
        allowed_links = frozenset({1, 2}) if temporary_status is not None else frozenset({1})
        stored, destination_status = self._load(
            destination,
            operation_id,
            allowed_links=allowed_links,
        )
        if temporary_status is None:
            return stored
        temporary_stored, temporary_status = self._load_incomplete_residue(
            temporary,
            operation_id,
            allowed_links=frozenset({1, 2}),
        )
        if _same_inode(destination_status, temporary_status):
            if destination_status.st_nlink != 2 or temporary_stored != stored:
                raise WarehouseSecretStorageError
        elif destination_status.st_nlink != 1 or temporary_status.st_nlink != 1:
            raise WarehouseSecretStorageError
        elif temporary_stored is None:
            if temporary_status.st_size != 0:
                raise WarehouseSecretStorageError
        elif temporary_stored != stored:
            raise WarehouseSecretStorageError
        return stored

    def _load_incomplete_residue(
        self,
        name: str,
        operation_id: str,
        *,
        allowed_links: frozenset[int],
    ) -> tuple[_StoredWarehouseOperationSecrets | None, os.stat_result]:
        encrypted, status = _read_private_file(
            self._directory_descriptor,
            name,
            allowed_links=allowed_links,
        )
        try:
            stored = self._decode(encrypted, operation_id)
        except InvalidToken:
            if len(encrypted) < _MINIMUM_COMPLETE_FERNET_TOKEN_BYTES:
                return None, status
            raise WarehouseSecretStorageError from None
        return stored, status

    def _load(
        self,
        name: str,
        operation_id: str,
        *,
        allowed_links: frozenset[int] = frozenset({1}),
    ) -> tuple[_StoredWarehouseOperationSecrets, os.stat_result]:
        encrypted, status = _read_private_file(
            self._directory_descriptor,
            name,
            allowed_links=allowed_links,
        )
        return self._decode(encrypted, operation_id), status

    def _decode(
        self,
        encrypted: bytes,
        operation_id: str,
    ) -> _StoredWarehouseOperationSecrets:
        decrypted = self._fernet.decrypt(encrypted)
        stored = _StoredWarehouseOperationSecrets.model_validate_json(decrypted)
        if stored.operation_id != operation_id:
            raise WarehouseSecretStorageError
        return stored


def _secret_reference(operation_id: str) -> str:
    if _OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
        raise WarehouseSecretStorageError
    return (
        "whs-"
        + digest(
            {
                "domain": "pillarmesh-warehouse-operation-secret-v1",
                "operation_id": operation_id,
            }
        )[:32]
    )


def _secret_name(operation_id: str) -> str:
    return f"{_secret_reference(operation_id)}.fernet"


def _temporary_name(destination: str) -> str:
    return f".{destination}.tmp"


def _tombstone_name(destination: str) -> str:
    return f".{destination}.delete"


def _quarantine_name(name: str) -> str:
    return f".quarantine-{name}"


def _open_private_directory(directory: Path) -> int:
    if not directory.is_absolute() or directory.name in {"", ".", ".."}:
        raise WarehouseSecretStorageError
    parent_descriptor = _open_existing_directory(directory.parent)
    directory_descriptor: int | None = None
    try:
        with suppress(FileExistsError):
            os.mkdir(directory.name, 0o700, dir_fd=parent_descriptor)
        directory_descriptor = os.open(
            directory.name,
            _DIRECTORY_FLAGS,
            dir_fd=parent_descriptor,
        )
        _validate_private_directory_status(os.fstat(directory_descriptor))
        os.fsync(parent_descriptor)
        os.fsync(directory_descriptor)
        return directory_descriptor
    except BaseException:
        if directory_descriptor is not None:
            with suppress(OSError):
                os.close(directory_descriptor)
        raise
    finally:
        with suppress(OSError):
            os.close(parent_descriptor)


def _open_existing_directory(directory: Path) -> int:
    if not directory.is_absolute():
        raise WarehouseSecretStorageError
    descriptor = os.open(directory.anchor, _DIRECTORY_FLAGS)
    try:
        for component in directory.parts[1:]:
            if component in {"", ".", ".."}:
                raise WarehouseSecretStorageError
            next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            with suppress(OSError):
                os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise


def _validate_private_directory_status(status_result: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(status_result.st_mode)
        or stat.S_IMODE(status_result.st_mode) != 0o700
        or status_result.st_uid != os.geteuid()
    ):
        raise WarehouseSecretStorageError


def _atomic_private_write(
    directory_descriptor: int,
    *,
    destination: str,
    payload: bytes,
) -> None:
    descriptor = os.open(destination, _WRITE_FLAGS, 0o600, dir_fd=directory_descriptor)
    try:
        descriptor_status = os.fstat(descriptor)
        _validate_private_file_status(descriptor_status, allowed_links=frozenset({1}))
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        final_status = os.fstat(descriptor)
        _validate_private_file_status(final_status, allowed_links=frozenset({1}))
        name_status = _lstat_required(directory_descriptor, destination)
        _validate_private_file_status(name_status, allowed_links=frozenset({1}))
        if (
            not _same_inode(descriptor_status, final_status)
            or not _same_inode(final_status, name_status)
            or final_status.st_size != len(payload)
        ):
            raise WarehouseSecretStorageError
        os.fsync(directory_descriptor)
    finally:
        with suppress(OSError):
            os.close(descriptor)


# POSIX cannot atomically unlink a name only when it still identifies a verified inode. Secret
# deletion therefore creates and fsyncs a distinct zero-length tombstone, then clears only verified
# descriptors. Every namespace name remains as a durable marker; hard links never grant authority.
def _create_durable_private_marker(
    directory_descriptor: int,
    name: str,
) -> os.stat_result:
    descriptor = os.open(name, _WRITE_FLAGS, 0o600, dir_fd=directory_descriptor)
    try:
        initial_status = os.fstat(descriptor)
        _validate_private_file_status(initial_status, allowed_links=frozenset({1}))
        if initial_status.st_size != 0:
            raise WarehouseSecretStorageError
        os.fsync(descriptor)
        final_status = os.fstat(descriptor)
        name_status = _lstat_required(directory_descriptor, name)
        _validate_private_file_status(final_status, allowed_links=frozenset({1}))
        _validate_private_file_status(name_status, allowed_links=frozenset({1}))
        if (
            not _same_inode(initial_status, final_status)
            or not _same_inode(final_status, name_status)
            or final_status.st_size != 0
        ):
            raise WarehouseSecretStorageError
        os.fsync(directory_descriptor)
        return name_status
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _verify_durable_private_marker(
    directory_descriptor: int,
    name: str,
    expected_status: os.stat_result,
) -> os.stat_result:
    _validate_private_file_status(expected_status, allowed_links=frozenset({1}))
    if expected_status.st_size != 0:
        raise WarehouseSecretStorageError
    descriptor = _open_verified_private_file(
        directory_descriptor,
        name,
        expected_status,
    )
    try:
        os.fsync(descriptor)
        _verify_private_file_after_descriptor_mutation(
            directory_descriptor,
            name,
            descriptor,
            expected_status,
            expected_size=0,
        )
        os.fsync(directory_descriptor)
        return _lstat_required(directory_descriptor, name)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _replace_private_file_contents(
    directory_descriptor: int,
    name: str,
    expected_status: os.stat_result,
    payload: bytes,
) -> None:
    descriptor = _open_verified_private_file(
        directory_descriptor,
        name,
        expected_status,
    )
    try:
        os.ftruncate(descriptor, 0)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        _verify_private_file_after_descriptor_mutation(
            directory_descriptor,
            name,
            descriptor,
            expected_status,
            expected_size=len(payload),
        )
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _clear_private_file(
    directory_descriptor: int,
    name: str,
    expected_status: os.stat_result,
) -> None:
    descriptor = _open_verified_private_file(
        directory_descriptor,
        name,
        expected_status,
    )
    try:
        descriptor_status = os.fstat(descriptor)
        if descriptor_status.st_size != 0:
            os.ftruncate(descriptor, 0)
        os.fsync(descriptor)
        _verify_private_file_after_descriptor_mutation(
            directory_descriptor,
            name,
            descriptor,
            expected_status,
            expected_size=0,
        )
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _open_verified_private_file(
    directory_descriptor: int,
    name: str,
    expected_status: os.stat_result,
) -> int:
    descriptor = os.open(name, _UPDATE_FLAGS, dir_fd=directory_descriptor)
    try:
        descriptor_status = os.fstat(descriptor)
        name_status = _lstat_required(directory_descriptor, name)
        allowed_links = frozenset({expected_status.st_nlink})
        _validate_private_file_status(descriptor_status, allowed_links=allowed_links)
        _validate_private_file_status(name_status, allowed_links=allowed_links)
        if not _same_inode(expected_status, descriptor_status) or not _same_inode(
            descriptor_status,
            name_status,
        ):
            raise WarehouseSecretStorageError
        return descriptor
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise


def _verify_private_file_after_descriptor_mutation(
    directory_descriptor: int,
    name: str,
    descriptor: int,
    expected_status: os.stat_result,
    *,
    expected_size: int,
) -> None:
    descriptor_status = os.fstat(descriptor)
    name_status = _lstat_required(directory_descriptor, name)
    allowed_links = frozenset({expected_status.st_nlink})
    _validate_private_file_status(descriptor_status, allowed_links=allowed_links)
    _validate_private_file_status(name_status, allowed_links=allowed_links)
    if (
        not _same_inode(expected_status, descriptor_status)
        or not _same_inode(descriptor_status, name_status)
        or descriptor_status.st_size != expected_size
        or name_status.st_size != expected_size
    ):
        raise WarehouseSecretStorageError


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise WarehouseSecretStorageError
        remaining = remaining[written:]


def _read_private_file(
    directory_descriptor: int,
    name: str,
    *,
    allowed_links: frozenset[int],
) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_descriptor)
    with os.fdopen(descriptor, "rb") as stream:
        descriptor_status = os.fstat(stream.fileno())
        _validate_private_file_status(descriptor_status, allowed_links=allowed_links)
        name_status = _lstat_required(directory_descriptor, name)
        _validate_private_file_status(name_status, allowed_links=allowed_links)
        if not _same_inode(descriptor_status, name_status):
            raise WarehouseSecretStorageError
        return stream.read(), descriptor_status


def _validate_private_file_status(
    status_result: os.stat_result,
    *,
    allowed_links: frozenset[int],
) -> None:
    if (
        not stat.S_ISREG(status_result.st_mode)
        or stat.S_IMODE(status_result.st_mode) != 0o600
        or status_result.st_uid != os.geteuid()
        or status_result.st_nlink not in allowed_links
    ):
        raise WarehouseSecretStorageError


def _lstat_optional(directory_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _lstat_required(directory_descriptor: int, name: str) -> os.stat_result:
    status_result = _lstat_optional(directory_descriptor, name)
    if status_result is None:
        raise WarehouseSecretStorageError
    return status_result


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


__all__ = [
    "WarehouseBackupCommandSecretCapability",
    "WarehouseBackupRetirementCapability",
    "WarehouseOperationSecretCapability",
    "WarehouseOperationSecretPurpose",
    "WarehouseOperationSecrets",
    "WarehouseSecretRetiredError",
    "WarehouseSecretStorageError",
    "WarehouseSecretStore",
]
