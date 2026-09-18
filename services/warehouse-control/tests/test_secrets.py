from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import cast

import heinzel_warehouse_control as warehouse_control
import pytest
from cryptography.fernet import Fernet
from heinzel_warehouse_control import (
    WarehouseOperationSecretPurpose,
    WarehouseOperationSecrets,
    WarehouseSecretRetiredError,
    WarehouseSecretStorageError,
    WarehouseSecretStore,
)
from heinzel_warehouse_control import secrets as secrets_module
from pydantic import SecretStr, ValidationError

OPERATION_A = "wop-111111111111111111111111"
OPERATION_B = "wop-222222222222222222222222"
REFERENCE_A = "whs-dab66d10cfcfc0dd34da4cb319144b91"
DESTINATION_A = f"{REFERENCE_A}.fernet"
TEMPORARY_A = f".{DESTINATION_A}.tmp"
TOMBSTONE_A = f".{DESTINATION_A}.delete"
QUARANTINED_DESTINATION_A = f".quarantine-{DESTINATION_A}"
QUARANTINED_TEMPORARY_A = f".quarantine-{TEMPORARY_A}"
INVALID_TEMPORARY_PAYLOADS = (
    pytest.param(b"", id="empty"),
    pytest.param(b"gAAAAA-truncated", id="truncated"),
    pytest.param(b"x" * 99, id="adjacent_99_bytes"),
)
COMPLETE_UNVERIFIABLE_RESIDUE_KINDS = (
    "authenticated_malformed_json",
    "authenticated_missing_required_field",
    "authenticated_unknown_field",
    "wrong_operation",
    "second_key",
    "exact_100_bytes",
    "adjacent_101_bytes",
)


def _secrets() -> WarehouseOperationSecrets:
    return WarehouseOperationSecrets(
        administration_password=SecretStr("administration-secret-canary"),
        ingestion_runtime_password=SecretStr("ingestion-secret-canary"),
        transformation_runtime_password=SecretStr("transformation-secret-canary"),
        answer_runtime_password=SecretStr("answer-runtime-secret-canary"),
        backup_restore_password=SecretStr("backup-secret-canary"),
        customer_sql_probe_password=SecretStr("customer-sql-secret-canary"),
        catalog_password=SecretStr("catalog-secret-canary"),
        bi_password=SecretStr("bi-secret-canary"),
        tls_private_key_pem=SecretStr("tls-private-key-secret-canary"),
        tls_certificate_pem=SecretStr("tls-certificate-secret-canary"),
        backup_encryption_key_b64=SecretStr("backup-encryption-secret-canary"),
    )


class SimulatedSecretCrash(BaseException):
    pass


def _authority(
    directory: Path, *, key: bytes | None = None
) -> secrets_module._EncryptedDirectoryWarehouseSecretAuthority:
    selected_key = Fernet.generate_key() if key is None else key
    return secrets_module._EncryptedDirectoryWarehouseSecretAuthority(
        directory=directory,
        key=SecretStr(selected_key.decode("ascii")),
    )


def _assert_sanitized(error: BaseException) -> None:
    rendered = str(error)
    assert rendered == "warehouse operation secret storage is unavailable"
    assert error.args == ("warehouse operation secret storage is unavailable",)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "wop-" not in rendered
    assert "secret-canary" not in rendered


def _write_unfsynced_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if payload:
            assert os.write(descriptor, payload) == len(payload)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _write_fsynced_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        remaining = memoryview(payload)
        while remaining:
            remaining = remaining[os.write(descriptor, remaining) :]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stored_payload(operation_id: str) -> dict[str, str]:
    operation_secrets = _secrets()
    return {
        "schema_version": "1",
        "operation_id": operation_id,
        "administration_password": (operation_secrets.administration_password.get_secret_value()),
        "ingestion_runtime_password": (
            operation_secrets.ingestion_runtime_password.get_secret_value()
        ),
        "transformation_runtime_password": (
            operation_secrets.transformation_runtime_password.get_secret_value()
        ),
        "answer_runtime_password": operation_secrets.answer_runtime_password.get_secret_value(),
        "backup_restore_password": operation_secrets.backup_restore_password.get_secret_value(),
        "customer_sql_probe_password": (
            operation_secrets.customer_sql_probe_password.get_secret_value()
        ),
        "catalog_password": operation_secrets.catalog_password.get_secret_value(),
        "bi_password": operation_secrets.bi_password.get_secret_value(),
        "tls_private_key_pem": operation_secrets.tls_private_key_pem.get_secret_value(),
        "tls_certificate_pem": operation_secrets.tls_certificate_pem.get_secret_value(),
        "backup_encryption_key_b64": (
            operation_secrets.backup_encryption_key_b64.get_secret_value()
        ),
    }


def _valid_ciphertext(key: bytes) -> bytes:
    return Fernet(key).encrypt(json.dumps(_stored_payload(OPERATION_A)).encode("utf-8"))


def _complete_unverifiable_payload(key: bytes, residue_kind: str) -> bytes:
    if residue_kind == "second_key":
        return Fernet(Fernet.generate_key()).encrypt(b"complete-unverifiable-residue")
    if residue_kind == "exact_100_bytes":
        return b"x" * 100
    if residue_kind == "adjacent_101_bytes":
        return b"x" * 101
    if residue_kind == "authenticated_malformed_json":
        return Fernet(key).encrypt(b'{"schema_version":"1","operation_id":')
    payload = _stored_payload(OPERATION_B if residue_kind == "wrong_operation" else OPERATION_A)
    if residue_kind == "authenticated_missing_required_field":
        payload.pop("bi_password")
    if residue_kind == "authenticated_unknown_field":
        payload["unexpected"] = "field"
    return Fernet(key).encrypt(json.dumps(payload).encode("utf-8"))


def _assert_zero_length_private_files(directory: Path, expected_names: set[str]) -> None:
    assert {entry.name for entry in directory.iterdir()} == expected_names
    for name in expected_names:
        status_result = (directory / name).stat(follow_symlinks=False)
        assert stat.S_ISREG(status_result.st_mode)
        assert stat.S_IMODE(status_result.st_mode) == 0o600
        assert status_result.st_size == 0


def _assert_private_file_unchanged(
    path: Path,
    expected_status: os.stat_result,
    expected_digest: bytes,
) -> None:
    current_status = path.stat(follow_symlinks=False)
    assert (current_status.st_dev, current_status.st_ino) == (
        expected_status.st_dev,
        expected_status.st_ino,
    )
    assert stat.S_IMODE(current_status.st_mode) == 0o600
    assert hashlib.sha256(path.read_bytes()).digest() == expected_digest


def test_operation_secret_model_is_frozen_strict_and_exact() -> None:
    secrets = _secrets()

    with pytest.raises(ValidationError, match="frozen"):
        secrets.bi_password = SecretStr("replacement")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        WarehouseOperationSecrets.model_validate(
            {**secrets.model_dump(), "diagnostic_password": SecretStr("forbidden")}
        )
    assert tuple(WarehouseOperationSecrets.model_fields) == (
        "administration_password",
        "ingestion_runtime_password",
        "transformation_runtime_password",
        "answer_runtime_password",
        "backup_restore_password",
        "customer_sql_probe_password",
        "catalog_password",
        "bi_password",
        "tls_private_key_pem",
        "tls_certificate_pem",
        "backup_encryption_key_b64",
    )


def test_public_consumer_store_exposes_only_preresolved_ordinary_resolution() -> None:
    implemented = {
        name
        for name, value in WarehouseSecretStore.__dict__.items()
        if callable(value) and not name.startswith("_")
    }

    assert implemented == {"resolve"}
    assert not hasattr(warehouse_control, "EncryptedDirectoryWarehouseSecretStore")
    assert "_EncryptedDirectoryWarehouseSecretAuthority" not in secrets_module.__all__


def test_encrypted_store_round_trips_with_private_modes_and_operation_only_names(
    tmp_path: Path,
) -> None:
    first_directory = tmp_path / "first"
    second_directory = tmp_path / "second"
    key = Fernet.generate_key()
    secrets = _secrets()
    first = _authority(first_directory, key=key)
    second = _authority(second_directory, key=key)

    first_reference = first.store(OPERATION_A, secrets)
    second_reference = second.store(OPERATION_A, secrets)
    stored_file = next(first_directory.iterdir())

    assert first_reference == second_reference
    assert stored_file.name == next(second_directory.iterdir()).name
    assert stat.S_IMODE(first_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(stored_file.stat().st_mode) == 0o600
    assert (
        secrets.administration_password.get_secret_value().encode() not in stored_file.read_bytes()
    )
    assert (
        first.operation_capability(
            first_reference,
            operation_id=OPERATION_A,
            purpose="administration",
        )
        .resolve()
        .get_secret_value()
        == secrets.administration_password.get_secret_value()
    )


@pytest.mark.parametrize(
    ("purpose", "field"),
    (
        ("administration", "administration_password"),
        ("ingestion_runtime", "ingestion_runtime_password"),
        ("transformation_runtime", "transformation_runtime_password"),
        ("answer_runtime", "answer_runtime_password"),
        ("customer_sql", "customer_sql_probe_password"),
        ("catalog", "catalog_password"),
        ("bi", "bi_password"),
        ("tls_private_key", "tls_private_key_pem"),
        ("tls_certificate", "tls_certificate_pem"),
    ),
)
def test_ordinary_capability_resolves_each_exact_non_backup_purpose(
    tmp_path: Path, purpose: WarehouseOperationSecretPurpose, field: str
) -> None:
    authority = _authority(tmp_path / "private")
    secrets = _secrets()
    reference = authority.store(OPERATION_A, secrets)

    resolved = authority.operation_capability(
        reference,
        operation_id=OPERATION_A,
        purpose=purpose,
    ).resolve()

    expected = getattr(secrets, field)
    assert isinstance(expected, SecretStr)
    assert resolved.get_secret_value() == expected.get_secret_value()


def test_generic_capabilities_cannot_request_backup_secrets(tmp_path: Path) -> None:
    authority = _authority(tmp_path / "private")
    reference = authority.store(OPERATION_A, _secrets())
    generic_purposes = (
        "administration",
        "ingestion_runtime",
        "transformation_runtime",
        "answer_runtime",
        "customer_sql",
        "catalog",
        "bi",
        "tls_private_key",
        "tls_certificate",
    )

    for purpose in generic_purposes:
        capability = authority.operation_capability(
            reference,
            operation_id=OPERATION_A,
            purpose=cast(WarehouseOperationSecretPurpose, purpose),
        )
        assert capability.resolve().get_secret_value()
        assert not hasattr(capability, "resolve_backup_restore_password")
        assert not hasattr(capability, "resolve_backup_encryption_key")
        assert not hasattr(capability, "store")
        assert not hasattr(capability, "secret_reference")
        assert not hasattr(capability, "operation_id")
        assert not hasattr(capability, "purpose")

    for blocked_purpose in ("backup_restore", "backup_encryption", "diagnostics"):
        with pytest.raises(WarehouseSecretStorageError) as failure:
            authority.operation_capability(
                reference,
                operation_id=OPERATION_A,
                purpose=cast(WarehouseOperationSecretPurpose, blocked_purpose),
            )
        _assert_sanitized(failure.value)


def test_backup_secrets_require_the_distinct_private_command_capability(tmp_path: Path) -> None:
    authority = _authority(tmp_path / "private")
    secrets = _secrets()
    reference = authority.store(OPERATION_A, secrets)

    capability = authority.backup_command_capability(reference, operation_id=OPERATION_A)

    assert capability.resolve_backup_restore_password().get_secret_value() == (
        secrets.backup_restore_password.get_secret_value()
    )
    assert capability.resolve_backup_encryption_key().get_secret_value() == (
        secrets.backup_encryption_key_b64.get_secret_value()
    )


def test_backup_retirement_capability_is_narrow_durable_and_operation_scoped(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path / "private")
    reference = authority.store(OPERATION_A, _secrets())

    capability = authority.backup_retirement_capability(
        reference,
        operation_id=OPERATION_A,
    )

    assert capability.resource_handle == f"{reference}#backup-encryption-key"
    assert capability.is_retired() is False
    assert not hasattr(capability, "resolve")
    assert not hasattr(capability, "resolve_backup_encryption_key")

    capability.retire()

    assert capability.is_retired() is True
    with pytest.raises(WarehouseSecretRetiredError) as retired:
        authority.backup_command_capability(reference, operation_id=OPERATION_A)
    _assert_sanitized(retired.value)
    assert not hasattr(capability, "resolve")
    assert not hasattr(capability, "store")
    assert not hasattr(capability, "secret_reference")
    assert not hasattr(capability, "operation_id")


def test_store_classifies_only_completed_retirement_as_retired(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    reference = authority.store(OPERATION_A, _secrets())

    authority.delete(reference, operation_id=OPERATION_A)

    with pytest.raises(WarehouseSecretRetiredError) as retired:
        authority.store(OPERATION_A, _secrets())
    _assert_sanitized(retired.value)


@pytest.mark.parametrize(
    "invalid_retirement",
    ("nonempty_tombstone", "wrong_permissions", "symlink", "active_ciphertext", "unavailable"),
)
def test_store_does_not_classify_unproven_retirement_as_retired(
    tmp_path: Path,
    invalid_retirement: str,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    authority.store(OPERATION_A, _secrets())
    destination = directory / DESTINATION_A
    tombstone = directory / TOMBSTONE_A

    if invalid_retirement == "nonempty_tombstone":
        _write_fsynced_private_file(tombstone, b"incomplete-retirement")
    elif invalid_retirement == "wrong_permissions":
        _write_fsynced_private_file(tombstone, b"")
        os.chmod(tombstone, 0o640)
    elif invalid_retirement == "symlink":
        marker_target = directory / ".marker-target"
        _write_fsynced_private_file(marker_target, b"")
        tombstone.symlink_to(marker_target.name)
    elif invalid_retirement == "active_ciphertext":
        _write_fsynced_private_file(tombstone, b"")
    else:
        os.close(authority._directory_descriptor)
        authority._directory_descriptor = -1

    if invalid_retirement in {"nonempty_tombstone", "wrong_permissions", "symlink"}:
        descriptor = os.open(destination, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if invalid_retirement != "unavailable":
        expected_status = destination.stat(follow_symlinks=False)
        expected_digest = hashlib.sha256(destination.read_bytes()).digest()

    with pytest.raises(WarehouseSecretStorageError) as failure:
        authority.store(OPERATION_A, _secrets())

    assert not isinstance(failure.value, WarehouseSecretRetiredError)
    _assert_sanitized(failure.value)
    if invalid_retirement != "unavailable":
        _assert_private_file_unchanged(destination, expected_status, expected_digest)


def test_backup_retirement_replays_a_durable_tombstone_until_ciphertext_is_erased(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    fixed_width_value = SecretStr("x" * 12)
    reference = authority.store(
        OPERATION_A,
        WarehouseOperationSecrets(
            administration_password=fixed_width_value,
            ingestion_runtime_password=fixed_width_value,
            transformation_runtime_password=fixed_width_value,
            answer_runtime_password=fixed_width_value,
            backup_restore_password=fixed_width_value,
            customer_sql_probe_password=fixed_width_value,
            catalog_password=fixed_width_value,
            bi_password=fixed_width_value,
            tls_private_key_pem=fixed_width_value,
            tls_certificate_pem=fixed_width_value,
            backup_encryption_key_b64=fixed_width_value,
        ),
    )
    capability = authority.backup_retirement_capability(reference, operation_id=OPERATION_A)
    active_ciphertext = directory / DESTINATION_A
    real_ftruncate = os.ftruncate

    def crash_after_tombstone(_descriptor: int, _length: int) -> None:
        raise SimulatedSecretCrash

    monkeypatch.setattr(os, "ftruncate", crash_after_tombstone)
    with pytest.raises(SimulatedSecretCrash):
        capability.retire()

    assert (directory / TOMBSTONE_A).stat(follow_symlinks=False).st_size == 0
    assert active_ciphertext.stat(follow_symlinks=False).st_size == 760
    assert capability.is_retired() is False

    monkeypatch.setattr(os, "ftruncate", real_ftruncate)
    capability.retire()

    assert active_ciphertext.stat(follow_symlinks=False).st_size == 0
    assert capability.is_retired() is True


@pytest.mark.parametrize(
    ("reference_update", "operation_id", "purpose"),
    (
        ("../warehouse-secret", OPERATION_A, "administration"),
        ("warehouse/secret", OPERATION_A, "administration"),
        (None, OPERATION_B, "administration"),
        (None, OPERATION_A, "diagnostics"),
    ),
)
def test_store_rejects_traversal_cross_operation_and_unknown_purpose(
    tmp_path: Path,
    reference_update: str | None,
    operation_id: str,
    purpose: str,
) -> None:
    authority = _authority(tmp_path / "private")
    reference = authority.store(OPERATION_A, _secrets())

    with pytest.raises(WarehouseSecretStorageError) as failure:
        authority.operation_capability(
            reference if reference_update is None else reference_update,
            operation_id=operation_id,
            purpose=cast(WarehouseOperationSecretPurpose, purpose),
        )

    _assert_sanitized(failure.value)


def test_store_rejects_an_operation_identifier_with_traversal(tmp_path: Path) -> None:
    authority = _authority(tmp_path / "private")

    with pytest.raises(WarehouseSecretStorageError) as failure:
        authority.store("../wop-escape", _secrets())

    _assert_sanitized(failure.value)
    assert not tuple((tmp_path / "private").iterdir())


def test_store_fails_closed_for_wrong_key_malformed_ciphertext_and_unknown_fields(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private"
    key = Fernet.generate_key()
    authority = _authority(directory, key=key)
    reference = authority.store(OPERATION_A, _secrets())
    stored_file = directory / DESTINATION_A
    valid_ciphertext = stored_file.read_bytes()

    with pytest.raises(WarehouseSecretStorageError) as wrong_key:
        _authority(directory, key=Fernet.generate_key()).operation_capability(
            reference,
            operation_id=OPERATION_A,
            purpose="administration",
        )
    _assert_sanitized(wrong_key.value)

    stored_file.write_bytes(b"malformed-ciphertext-secret-canary")
    os.chmod(stored_file, 0o600)
    with pytest.raises(WarehouseSecretStorageError) as malformed:
        authority.operation_capability(
            reference,
            operation_id=OPERATION_A,
            purpose="administration",
        )
    _assert_sanitized(malformed.value)
    assert not isinstance(malformed.value, WarehouseSecretRetiredError)

    payload = json.loads(Fernet(key).decrypt(valid_ciphertext))
    payload["unexpected"] = "field"
    stored_file.write_bytes(Fernet(key).encrypt(json.dumps(payload).encode("utf-8")))
    os.chmod(stored_file, 0o600)
    with pytest.raises(WarehouseSecretStorageError) as unknown:
        authority.operation_capability(
            reference,
            operation_id=OPERATION_A,
            purpose="administration",
        )
    _assert_sanitized(unknown.value)

    payload = json.loads(Fernet(key).decrypt(valid_ciphertext))
    payload["operation_id"] = OPERATION_B
    stored_file.write_bytes(Fernet(key).encrypt(json.dumps(payload).encode("utf-8")))
    os.chmod(stored_file, 0o600)
    with pytest.raises(WarehouseSecretStorageError) as wrong_operation:
        authority.operation_capability(
            reference,
            operation_id=OPERATION_A,
            purpose="administration",
        )
    _assert_sanitized(wrong_operation.value)


@pytest.mark.parametrize(
    "residue_kind",
    ("authenticated_malformed_json", "authenticated_missing_required_field"),
)
@pytest.mark.parametrize("resolution_kind", ("ordinary", "backup"))
def test_authenticated_malformed_published_envelope_is_preserved_and_fails_closed(
    tmp_path: Path,
    residue_kind: str,
    resolution_kind: str,
) -> None:
    directory = tmp_path / "private"
    key = Fernet.generate_key()
    authority = _authority(directory, key=key)
    reference = authority.store(OPERATION_A, _secrets())
    stored_file = directory / DESTINATION_A
    malformed_ciphertext = _complete_unverifiable_payload(key, residue_kind)
    stored_file.write_bytes(malformed_ciphertext)
    os.chmod(stored_file, 0o600)
    expected_status = stored_file.stat(follow_symlinks=False)
    expected_digest = hashlib.sha256(malformed_ciphertext).digest()

    with pytest.raises(WarehouseSecretStorageError) as failure:
        if resolution_kind == "ordinary":
            authority.operation_capability(
                reference,
                operation_id=OPERATION_A,
                purpose="administration",
            )
        else:
            authority.backup_command_capability(reference, operation_id=OPERATION_A)

    _assert_sanitized(failure.value)
    _assert_private_file_unchanged(stored_file, expected_status, expected_digest)


def test_store_rejects_wrong_directory_and_file_permissions(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o755)

    with pytest.raises(WarehouseSecretStorageError) as directory_failure:
        _authority(directory)
    _assert_sanitized(directory_failure.value)

    os.chmod(directory, 0o700)
    authority = _authority(directory)
    reference = authority.store(OPERATION_A, _secrets())
    stored_file = directory / DESTINATION_A
    os.chmod(stored_file, 0o644)

    with pytest.raises(WarehouseSecretStorageError) as file_failure:
        authority.operation_capability(
            reference,
            operation_id=OPERATION_A,
            purpose="administration",
        )
    _assert_sanitized(file_failure.value)


def test_store_rejects_symlinked_directory_and_secret_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    directory_link = tmp_path / "private-link"
    directory_link.symlink_to(target, target_is_directory=True)

    with pytest.raises(WarehouseSecretStorageError) as directory_failure:
        _authority(directory_link)
    _assert_sanitized(directory_failure.value)

    directory = tmp_path / "private"
    authority = _authority(directory)
    reference = authority.store(OPERATION_A, _secrets())
    stored_file = directory / DESTINATION_A
    outside = tmp_path / "outside"
    outside.write_bytes(stored_file.read_bytes())
    os.chmod(outside, 0o600)
    stored_file.unlink()
    stored_file.symlink_to(outside)

    with pytest.raises(WarehouseSecretStorageError) as file_failure:
        authority.operation_capability(
            reference,
            operation_id=OPERATION_A,
            purpose="administration",
        )
    _assert_sanitized(file_failure.value)


def test_delete_is_operation_scoped_and_never_follows_a_cross_handle(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    reference = authority.store(OPERATION_A, _secrets())

    with pytest.raises(WarehouseSecretStorageError):
        authority.delete(reference, operation_id=OPERATION_B)
    assert tuple(directory.iterdir())

    authority.delete(reference, operation_id=OPERATION_A)
    authority.delete(reference, operation_id=OPERATION_A)

    _assert_zero_length_private_files(directory, {DESTINATION_A, TOMBSTONE_A})


def test_delete_retains_distinct_zero_length_lifecycle_markers(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    reference = authority.store(OPERATION_A, _secrets())

    authority.delete(reference, operation_id=OPERATION_A)

    destination_status = (directory / DESTINATION_A).stat(follow_symlinks=False)
    tombstone_status = (directory / TOMBSTONE_A).stat(follow_symlinks=False)
    assert (destination_status.st_dev, destination_status.st_ino) != (
        tombstone_status.st_dev,
        tombstone_status.st_ino,
    )
    assert destination_status.st_nlink == 1
    assert tombstone_status.st_nlink == 1
    assert destination_status.st_size == 0
    assert tombstone_status.st_size == 0


@pytest.mark.parametrize(
    "replayed_operation",
    ("store", "delete", "ordinary_resolution", "backup_resolution"),
)
def test_durable_tombstone_is_authoritative_for_every_replay_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replayed_operation: str,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    operation_secrets = _secrets()
    reference = authority.store(OPERATION_A, operation_secrets)
    destination = directory / DESTINATION_A
    expected_status = destination.stat(follow_symlinks=False)
    expected_digest = hashlib.sha256(destination.read_bytes()).digest()
    real_ftruncate = os.ftruncate

    def crash_before_first_descriptor_clear(descriptor: int, length: int) -> None:
        del descriptor, length
        raise SimulatedSecretCrash

    monkeypatch.setattr(os, "ftruncate", crash_before_first_descriptor_clear)
    with pytest.raises(SimulatedSecretCrash):
        authority.delete(reference, operation_id=OPERATION_A)

    tombstone = directory / TOMBSTONE_A
    assert tombstone.stat(follow_symlinks=False).st_size == 0
    _assert_private_file_unchanged(destination, expected_status, expected_digest)
    monkeypatch.setattr(os, "ftruncate", real_ftruncate)

    if replayed_operation == "delete":
        authority.delete(reference, operation_id=OPERATION_A)
        assert destination.stat(follow_symlinks=False).st_size == 0
        return

    with pytest.raises(WarehouseSecretStorageError) as failure:
        if replayed_operation == "store":
            authority.store(OPERATION_A, operation_secrets)
        elif replayed_operation == "ordinary_resolution":
            authority.operation_capability(
                reference,
                operation_id=OPERATION_A,
                purpose="administration",
            )
        else:
            authority.backup_command_capability(reference, operation_id=OPERATION_A)

    _assert_sanitized(failure.value)
    _assert_private_file_unchanged(destination, expected_status, expected_digest)


def test_delete_without_residue_durably_tombstones_and_prevents_resurrection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    events: list[str] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        if descriptor == authority._directory_descriptor:
            events.append("directory_fsynced")
        else:
            descriptor_status = os.fstat(descriptor)
            try:
                tombstone_status = os.stat(
                    TOMBSTONE_A,
                    dir_fd=authority._directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                if (descriptor_status.st_dev, descriptor_status.st_ino) == (
                    tombstone_status.st_dev,
                    tombstone_status.st_ino,
                ):
                    events.append("tombstone_fsynced")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    authority.delete(REFERENCE_A, operation_id=OPERATION_A)

    tombstone = directory / TOMBSTONE_A
    first_status = tombstone.stat(follow_symlinks=False)
    first_digest = hashlib.sha256(tombstone.read_bytes()).digest()
    assert events.index("tombstone_fsynced") < events.index("directory_fsynced")
    assert {entry.name for entry in directory.iterdir()} == {TOMBSTONE_A}

    events.clear()
    authority.delete(REFERENCE_A, operation_id=OPERATION_A)

    _assert_private_file_unchanged(tombstone, first_status, first_digest)
    assert events.index("tombstone_fsynced") < events.index("directory_fsynced")
    with pytest.raises(WarehouseSecretStorageError) as store_failure:
        authority.store(OPERATION_A, _secrets())
    with pytest.raises(WarehouseSecretStorageError) as ordinary_failure:
        authority.operation_capability(
            REFERENCE_A,
            operation_id=OPERATION_A,
            purpose="administration",
        )
    with pytest.raises(WarehouseSecretStorageError) as backup_failure:
        authority.backup_command_capability(REFERENCE_A, operation_id=OPERATION_A)
    for failure in (store_failure.value, ordinary_failure.value, backup_failure.value):
        _assert_sanitized(failure)


def test_delete_fails_closed_when_the_published_name_changes_before_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    reference = authority.store(OPERATION_A, _secrets())
    destination = directory / DESTINATION_A
    replacement_name = ".replacement"
    preserved_name = ".preserved"
    replacement = directory / replacement_name
    _write_fsynced_private_file(replacement, b"replacement")
    original_status = destination.stat(follow_symlinks=False)
    original_digest = hashlib.sha256(destination.read_bytes()).digest()
    replacement_status = replacement.stat(follow_symlinks=False)
    replacement_digest = hashlib.sha256(replacement.read_bytes()).digest()
    real_replace = os.replace
    real_open = os.open
    swapped = False

    def open_after_swap(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == TOMBSTONE_A and flags & os.O_CREAT and not swapped:
            real_replace(
                DESTINATION_A,
                preserved_name,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            real_replace(
                replacement_name,
                DESTINATION_A,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", open_after_swap)

    with pytest.raises(WarehouseSecretStorageError) as failure:
        authority.delete(reference, operation_id=OPERATION_A)

    _assert_sanitized(failure.value)
    assert swapped
    _assert_private_file_unchanged(destination, replacement_status, replacement_digest)
    _assert_private_file_unchanged(
        directory / preserved_name,
        original_status,
        original_digest,
    )
    assert (directory / TOMBSTONE_A).stat(follow_symlinks=False).st_size == 0

    with pytest.raises(WarehouseSecretStorageError) as replay_failure:
        authority.delete(reference, operation_id=OPERATION_A)

    _assert_sanitized(replay_failure.value)
    _assert_private_file_unchanged(destination, replacement_status, replacement_digest)


def test_open_directory_descriptor_remains_anchored_after_path_replacement(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private"
    moved_directory = tmp_path / "moved"
    replacement_directory = tmp_path / "replacement"
    authority = _authority(directory)
    secrets = _secrets()
    reference = authority.store(OPERATION_A, secrets)
    directory.rename(moved_directory)
    replacement_directory.mkdir(mode=0o700)
    directory.symlink_to(replacement_directory, target_is_directory=True)

    resolved = authority.operation_capability(
        reference,
        operation_id=OPERATION_A,
        purpose="administration",
    ).resolve()

    assert resolved.get_secret_value() == secrets.administration_password.get_secret_value()
    assert not tuple(replacement_directory.iterdir())


@pytest.mark.parametrize("invalid_payload", INVALID_TEMPORARY_PAYLOADS)
def test_store_replaces_verified_invalid_temp_created_before_file_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_payload: bytes,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    temporary = directory / TEMPORARY_A
    _write_unfsynced_private_file(temporary, invalid_payload)
    temporary_status = temporary.stat(follow_symlinks=False)
    events: list[str] = []
    real_fsync = os.fsync
    real_ftruncate = os.ftruncate

    def record_fsync(descriptor: int) -> None:
        if descriptor == authority._directory_descriptor:
            events.append("directory_fsynced")
        else:
            status_result = os.fstat(descriptor)
            if (status_result.st_dev, status_result.st_ino) == (
                temporary_status.st_dev,
                temporary_status.st_ino,
            ):
                events.append("temporary_fsynced")
        real_fsync(descriptor)

    def record_ftruncate(descriptor: int, length: int) -> None:
        status_result = os.fstat(descriptor)
        if (status_result.st_dev, status_result.st_ino) == (
            temporary_status.st_dev,
            temporary_status.st_ino,
        ):
            events.append("temporary_cleared")
        real_ftruncate(descriptor, length)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "ftruncate", record_ftruncate)

    reference = authority.store(OPERATION_A, _secrets())

    assert reference == REFERENCE_A
    temporary_fsync_index = events.index("temporary_fsynced")
    assert "directory_fsynced" in events[:temporary_fsync_index]
    assert events.count("temporary_cleared") == (1 if invalid_payload else 0)
    assert {entry.name for entry in directory.iterdir()} == {DESTINATION_A, TEMPORARY_A}
    assert temporary.stat(follow_symlinks=False).st_size == 0
    assert (
        authority.operation_capability(
            reference,
            operation_id=OPERATION_A,
            purpose="administration",
        )
        .resolve()
        .get_secret_value()
        == _secrets().administration_password.get_secret_value()
    )


@pytest.mark.parametrize("invalid_payload", INVALID_TEMPORARY_PAYLOADS)
def test_delete_removes_verified_invalid_temp_created_before_file_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_payload: bytes,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    temporary = directory / TEMPORARY_A
    _write_unfsynced_private_file(temporary, invalid_payload)
    temporary_status = temporary.stat(follow_symlinks=False)
    events: list[str] = []
    real_fsync = os.fsync
    real_ftruncate = os.ftruncate

    def record_fsync(descriptor: int) -> None:
        if descriptor == authority._directory_descriptor:
            events.append("directory_fsynced")
        else:
            status_result = os.fstat(descriptor)
            if (status_result.st_dev, status_result.st_ino) == (
                temporary_status.st_dev,
                temporary_status.st_ino,
            ):
                events.append("temporary_fsynced")
            else:
                try:
                    tombstone_status = os.stat(
                        TOMBSTONE_A,
                        dir_fd=authority._directory_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    if (status_result.st_dev, status_result.st_ino) == (
                        tombstone_status.st_dev,
                        tombstone_status.st_ino,
                    ):
                        events.append("tombstone_fsynced")
        real_fsync(descriptor)

    def record_ftruncate(descriptor: int, length: int) -> None:
        status_result = os.fstat(descriptor)
        if (status_result.st_dev, status_result.st_ino) == (
            temporary_status.st_dev,
            temporary_status.st_ino,
        ):
            events.append("temporary_cleared")
        real_ftruncate(descriptor, length)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "ftruncate", record_ftruncate)

    authority.delete(REFERENCE_A, operation_id=OPERATION_A)
    authority.delete(REFERENCE_A, operation_id=OPERATION_A)

    tombstone_fsync_index = events.index("tombstone_fsynced")
    temporary_fsync_index = events.index("temporary_fsynced")
    directory_fsync_index = events.index("directory_fsynced", tombstone_fsync_index + 1)
    assert tombstone_fsync_index < directory_fsync_index < temporary_fsync_index
    assert events.count("temporary_cleared") == (1 if invalid_payload else 0)
    _assert_zero_length_private_files(directory, {TEMPORARY_A, TOMBSTONE_A})


@pytest.mark.parametrize("residue_kind", COMPLETE_UNVERIFIABLE_RESIDUE_KINDS)
def test_store_preserves_complete_unverifiable_temporary_ciphertext(
    tmp_path: Path,
    residue_kind: str,
) -> None:
    directory = tmp_path / "private"
    key = Fernet.generate_key()
    authority = _authority(directory, key=key)
    temporary = directory / TEMPORARY_A
    payload = _complete_unverifiable_payload(key, residue_kind)
    _write_fsynced_private_file(temporary, payload)
    expected_status = temporary.stat(follow_symlinks=False)
    expected_digest = hashlib.sha256(payload).digest()

    with pytest.raises(WarehouseSecretStorageError) as failure:
        authority.store(OPERATION_A, _secrets())

    _assert_sanitized(failure.value)
    _assert_private_file_unchanged(temporary, expected_status, expected_digest)
    assert not (directory / DESTINATION_A).exists()


@pytest.mark.parametrize("residue_kind", COMPLETE_UNVERIFIABLE_RESIDUE_KINDS)
def test_delete_preserves_complete_unverifiable_temporary_ciphertext(
    tmp_path: Path,
    residue_kind: str,
) -> None:
    directory = tmp_path / "private"
    key = Fernet.generate_key()
    authority = _authority(directory, key=key)
    temporary = directory / TEMPORARY_A
    payload = _complete_unverifiable_payload(key, residue_kind)
    _write_fsynced_private_file(temporary, payload)
    expected_status = temporary.stat(follow_symlinks=False)
    expected_digest = hashlib.sha256(payload).digest()

    with pytest.raises(WarehouseSecretStorageError) as failure:
        authority.delete(REFERENCE_A, operation_id=OPERATION_A)

    _assert_sanitized(failure.value)
    _assert_private_file_unchanged(temporary, expected_status, expected_digest)
    assert not (directory / DESTINATION_A).exists()


def test_store_preserves_valid_published_secret_while_clearing_invalid_temp(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    secrets = _secrets()
    reference = authority.store(OPERATION_A, secrets)
    published = directory / DESTINATION_A
    published_ciphertext = published.read_bytes()
    _write_unfsynced_private_file(directory / TEMPORARY_A, b"gAAAAA-truncated")

    replayed_reference = authority.store(OPERATION_A, secrets)

    assert replayed_reference == reference
    assert published.read_bytes() == published_ciphertext
    assert (directory / TEMPORARY_A).stat(follow_symlinks=False).st_size == 0
    assert (
        authority.operation_capability(
            reference,
            operation_id=OPERATION_A,
            purpose="administration",
        )
        .resolve()
        .get_secret_value()
        == secrets.administration_password.get_secret_value()
    )


def test_delete_preserves_legacy_secret_tombstone_and_incomplete_temp(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    reference = authority.store(OPERATION_A, _secrets())
    os.replace(
        DESTINATION_A,
        TOMBSTONE_A,
        src_dir_fd=authority._directory_descriptor,
        dst_dir_fd=authority._directory_descriptor,
    )
    os.fsync(authority._directory_descriptor)
    tombstone = directory / TOMBSTONE_A
    temporary = directory / TEMPORARY_A
    _write_unfsynced_private_file(temporary, b"gAAAAA-truncated")
    tombstone_status = tombstone.stat(follow_symlinks=False)
    tombstone_digest = hashlib.sha256(tombstone.read_bytes()).digest()
    temporary_status = temporary.stat(follow_symlinks=False)
    temporary_digest = hashlib.sha256(temporary.read_bytes()).digest()

    with pytest.raises(WarehouseSecretStorageError) as failure:
        authority.delete(reference, operation_id=OPERATION_A)

    _assert_sanitized(failure.value)
    _assert_private_file_unchanged(tombstone, tombstone_status, tombstone_digest)
    _assert_private_file_unchanged(temporary, temporary_status, temporary_digest)


@pytest.mark.parametrize("residue_kind", ("symlink", "hard_link", "wrong_permissions"))
def test_invalid_temp_reconciliation_rejects_unsafe_namespace_entries(
    tmp_path: Path,
    residue_kind: str,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    temporary = directory / TEMPORARY_A
    outside = tmp_path / "outside-residue"
    _write_unfsynced_private_file(outside, b"gAAAAA-truncated")
    if residue_kind == "symlink":
        temporary.symlink_to(outside)
    elif residue_kind == "hard_link":
        os.link(outside, temporary)
    else:
        _write_unfsynced_private_file(temporary, b"gAAAAA-truncated")
        os.chmod(temporary, 0o640)

    with pytest.raises(WarehouseSecretStorageError) as failure:
        authority.store(OPERATION_A, _secrets())

    _assert_sanitized(failure.value)
    assert temporary.exists() or temporary.is_symlink()
    assert outside.exists()
    assert not (directory / DESTINATION_A).exists()


def test_invalid_temp_reconciliation_rejects_unexpected_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    temporary = directory / TEMPORARY_A
    _write_unfsynced_private_file(temporary, b"gAAAAA-truncated")
    temporary_status = temporary.stat(follow_symlinks=False)
    real_fstat = os.fstat

    def report_unexpected_owner(descriptor: int) -> os.stat_result:
        status_result = real_fstat(descriptor)
        if status_result.st_dev == temporary_status.st_dev and status_result.st_ino == (
            temporary_status.st_ino
        ):
            fields = list(status_result)
            fields[4] = status_result.st_uid + 1
            return os.stat_result(fields)
        return status_result

    monkeypatch.setattr(os, "fstat", report_unexpected_owner)

    with pytest.raises(WarehouseSecretStorageError) as failure:
        authority.store(OPERATION_A, _secrets())

    _assert_sanitized(failure.value)
    assert temporary.exists()
    assert not (directory / DESTINATION_A).exists()


def test_store_fails_closed_when_temporary_name_changes_before_descriptor_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    temporary = directory / TEMPORARY_A
    replacement_name = ".replacement-invalid-temp"
    preserved_name = ".preserved-invalid-temp"
    _write_unfsynced_private_file(temporary, b"gAAAAA-truncated")
    replacement = directory / replacement_name
    _write_fsynced_private_file(replacement, b"replacement")
    replacement_status = replacement.stat(follow_symlinks=False)
    replacement_digest = hashlib.sha256(replacement.read_bytes()).digest()
    real_replace = os.replace
    real_ftruncate = os.ftruncate
    swapped = False

    def swap_before_descriptor_clear(descriptor: int, length: int) -> None:
        nonlocal swapped
        descriptor_status = os.fstat(descriptor)
        temporary_status = temporary.stat(follow_symlinks=False)
        if not swapped and (descriptor_status.st_dev, descriptor_status.st_ino) == (
            temporary_status.st_dev,
            temporary_status.st_ino,
        ):
            real_replace(
                TEMPORARY_A,
                preserved_name,
                src_dir_fd=authority._directory_descriptor,
                dst_dir_fd=authority._directory_descriptor,
            )
            real_replace(
                replacement_name,
                TEMPORARY_A,
                src_dir_fd=authority._directory_descriptor,
                dst_dir_fd=authority._directory_descriptor,
            )
            swapped = True
        real_ftruncate(descriptor, length)

    monkeypatch.setattr(os, "ftruncate", swap_before_descriptor_clear)

    with pytest.raises(WarehouseSecretStorageError) as failure:
        authority.store(OPERATION_A, _secrets())

    _assert_sanitized(failure.value)
    assert swapped
    _assert_private_file_unchanged(temporary, replacement_status, replacement_digest)
    assert (directory / preserved_name).stat(follow_symlinks=False).st_size == 0
    assert (directory / DESTINATION_A).exists()


def test_delete_descriptor_clear_never_mutates_a_replacement_at_the_active_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    reference = authority.store(OPERATION_A, _secrets())
    destination = directory / DESTINATION_A
    replacement_name = ".replacement-at-clear"
    preserved_name = ".preserved-at-clear"
    replacement = directory / replacement_name
    _write_fsynced_private_file(replacement, b"replacement-must-survive")
    replacement_status = replacement.stat(follow_symlinks=False)
    replacement_digest = hashlib.sha256(replacement.read_bytes()).digest()
    real_replace = os.replace
    real_ftruncate = os.ftruncate
    swapped = False

    def swap_active_name_before_clear(descriptor: int, length: int) -> None:
        nonlocal swapped
        if not swapped:
            real_replace(
                DESTINATION_A,
                preserved_name,
                src_dir_fd=authority._directory_descriptor,
                dst_dir_fd=authority._directory_descriptor,
            )
            real_replace(
                replacement_name,
                DESTINATION_A,
                src_dir_fd=authority._directory_descriptor,
                dst_dir_fd=authority._directory_descriptor,
            )
            swapped = True
        real_ftruncate(descriptor, length)

    monkeypatch.setattr(os, "ftruncate", swap_active_name_before_clear)

    with pytest.raises(WarehouseSecretStorageError) as failure:
        authority.delete(reference, operation_id=OPERATION_A)

    _assert_sanitized(failure.value)
    assert swapped
    _assert_private_file_unchanged(destination, replacement_status, replacement_digest)
    assert (directory / preserved_name).stat(follow_symlinks=False).st_size == 0
    tombstone = directory / TOMBSTONE_A
    tombstone_status = tombstone.stat(follow_symlinks=False)
    tombstone_digest = hashlib.sha256(tombstone.read_bytes()).digest()
    assert tombstone_status.st_size == 0

    with pytest.raises(WarehouseSecretStorageError) as replay_failure:
        authority.delete(reference, operation_id=OPERATION_A)

    _assert_sanitized(replay_failure.value)
    _assert_private_file_unchanged(destination, replacement_status, replacement_digest)
    _assert_private_file_unchanged(tombstone, tombstone_status, tombstone_digest)


def test_store_never_invokes_the_exact_quarantine_unlink_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    temporary = directory / TEMPORARY_A
    replacement_name = ".replacement-exact-unlink-target"
    replacement = directory / replacement_name
    _write_unfsynced_private_file(temporary, b"gAAAAA-truncated")
    _write_fsynced_private_file(replacement, b"replacement-must-survive")
    replacement_status = replacement.stat(follow_symlinks=False)
    replacement_digest = hashlib.sha256(replacement.read_bytes()).digest()
    attacked_unlink_targets: list[str] = []

    def reject_exact_quarantine_unlink(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
    ) -> None:
        del dir_fd
        attacked_unlink_targets.append(os.fsdecode(path))
        raise AssertionError("secret storage must not invoke a pathname unlink hook")

    monkeypatch.setattr(os, "unlink", reject_exact_quarantine_unlink)

    reference = authority.store(OPERATION_A, _secrets())

    assert reference == REFERENCE_A
    assert attacked_unlink_targets == []
    _assert_private_file_unchanged(replacement, replacement_status, replacement_digest)


@pytest.mark.parametrize("operation", ("store", "delete", "resolve"))
def test_operations_preserve_legacy_quarantine_and_fail_closed(
    tmp_path: Path,
    operation: str,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    secrets = _secrets()
    reference = authority.store(OPERATION_A, secrets)
    os.replace(
        DESTINATION_A,
        QUARANTINED_DESTINATION_A,
        src_dir_fd=authority._directory_descriptor,
        dst_dir_fd=authority._directory_descriptor,
    )
    os.fsync(authority._directory_descriptor)
    quarantine = directory / QUARANTINED_DESTINATION_A
    expected_status = quarantine.stat(follow_symlinks=False)
    expected_digest = hashlib.sha256(quarantine.read_bytes()).digest()

    with pytest.raises(WarehouseSecretStorageError) as failure:
        if operation == "store":
            authority.store(OPERATION_A, secrets)
        elif operation == "delete":
            authority.delete(reference, operation_id=OPERATION_A)
        else:
            authority.operation_capability(
                reference,
                operation_id=OPERATION_A,
                purpose="administration",
            )

    _assert_sanitized(failure.value)
    _assert_private_file_unchanged(quarantine, expected_status, expected_digest)
    assert not (directory / DESTINATION_A).exists()
    assert not (directory / TOMBSTONE_A).exists()


def test_store_replays_complete_temporary_residue_without_content_erasure(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private"
    key = Fernet.generate_key()
    authority = _authority(directory, key=key)
    secrets = _secrets()
    temporary = directory / TEMPORARY_A
    _write_fsynced_private_file(temporary, _valid_ciphertext(key))
    temporary_status = temporary.stat(follow_symlinks=False)
    temporary_digest = hashlib.sha256(temporary.read_bytes()).digest()

    reference = authority.store(OPERATION_A, secrets)

    _assert_private_file_unchanged(temporary, temporary_status, temporary_digest)
    assert (
        authority.operation_capability(
            reference,
            operation_id=OPERATION_A,
            purpose="administration",
        )
        .resolve()
        .get_secret_value()
        == secrets.administration_password.get_secret_value()
    )
    assert {entry.name for entry in directory.iterdir()} == {DESTINATION_A, TEMPORARY_A}


def test_store_replays_published_and_temporary_hard_links_without_name_deletion(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    secrets = _secrets()
    reference = authority.store(OPERATION_A, secrets)
    os.link(
        DESTINATION_A,
        TEMPORARY_A,
        src_dir_fd=authority._directory_descriptor,
        dst_dir_fd=authority._directory_descriptor,
        follow_symlinks=False,
    )
    os.fsync(authority._directory_descriptor)

    replayed_reference = authority.store(OPERATION_A, secrets)

    destination_status = (directory / DESTINATION_A).stat(follow_symlinks=False)
    temporary_status = (directory / TEMPORARY_A).stat(follow_symlinks=False)
    assert replayed_reference == reference
    assert (destination_status.st_dev, destination_status.st_ino) == (
        temporary_status.st_dev,
        temporary_status.st_ino,
    )
    assert destination_status.st_nlink == 2
    assert temporary_status.st_size >= 100
    assert (
        authority.operation_capability(
            replayed_reference,
            operation_id=OPERATION_A,
            purpose="administration",
        )
        .resolve()
        .get_secret_value()
        == secrets.administration_password.get_secret_value()
    )


@pytest.mark.parametrize(
    "invalid_payload",
    (
        pytest.param(b"", id="empty"),
        pytest.param(b"x" * 99, id="adjacent_99_bytes"),
    ),
)
def test_store_replays_incomplete_destination_created_before_file_fsync(
    tmp_path: Path,
    invalid_payload: bytes,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    destination = directory / DESTINATION_A
    _write_unfsynced_private_file(destination, invalid_payload)
    expected_status = destination.stat(follow_symlinks=False)

    reference = authority.store(OPERATION_A, _secrets())

    current_status = destination.stat(follow_symlinks=False)
    assert (current_status.st_dev, current_status.st_ino) == (
        expected_status.st_dev,
        expected_status.st_ino,
    )
    assert current_status.st_size >= 100
    assert (
        authority.operation_capability(
            reference,
            operation_id=OPERATION_A,
            purpose="administration",
        )
        .resolve()
        .get_secret_value()
        == _secrets().administration_password.get_secret_value()
    )


@pytest.mark.parametrize("clear_before_crash", (False, True))
def test_delete_replays_tombstone_before_or_after_descriptor_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clear_before_crash: bool,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    reference = authority.store(OPERATION_A, _secrets())
    real_ftruncate = os.ftruncate
    crashed = False

    def crash_descriptor_clear(descriptor: int, length: int) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            if clear_before_crash:
                real_ftruncate(descriptor, length)
            raise SimulatedSecretCrash
        real_ftruncate(descriptor, length)

    monkeypatch.setattr(os, "ftruncate", crash_descriptor_clear)
    with pytest.raises(SimulatedSecretCrash):
        authority.delete(reference, operation_id=OPERATION_A)

    assert {entry.name for entry in directory.iterdir()} == {DESTINATION_A, TOMBSTONE_A}
    assert (directory / TOMBSTONE_A).stat(follow_symlinks=False).st_size == 0
    destination_size = (directory / DESTINATION_A).stat(follow_symlinks=False).st_size
    if clear_before_crash:
        assert destination_size == 0
    else:
        assert destination_size >= 100

    monkeypatch.setattr(os, "ftruncate", real_ftruncate)
    authority.delete(reference, operation_id=OPERATION_A)
    authority.delete(reference, operation_id=OPERATION_A)

    _assert_zero_length_private_files(directory, {DESTINATION_A, TOMBSTONE_A})


def test_existing_tombstone_replay_redurabilizes_marker_before_descriptor_erasure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "private"
    key = Fernet.generate_key()
    authority = _authority(directory, key=key)
    reference = authority.store(OPERATION_A, _secrets())
    destination = directory / DESTINATION_A
    temporary = directory / TEMPORARY_A
    _write_fsynced_private_file(temporary, _valid_ciphertext(key))
    destination_status = destination.stat(follow_symlinks=False)
    destination_digest = hashlib.sha256(destination.read_bytes()).digest()
    temporary_status = temporary.stat(follow_symlinks=False)
    temporary_digest = hashlib.sha256(temporary.read_bytes()).digest()
    real_fsync = os.fsync
    real_ftruncate = os.ftruncate

    def first_crash_before_descriptor_clear(descriptor: int, length: int) -> None:
        del descriptor, length
        raise SimulatedSecretCrash

    monkeypatch.setattr(os, "ftruncate", first_crash_before_descriptor_clear)
    with pytest.raises(SimulatedSecretCrash):
        authority.delete(reference, operation_id=OPERATION_A)

    marker_status = (directory / TOMBSTONE_A).stat(follow_symlinks=False)
    assert marker_status.st_size == 0
    _assert_private_file_unchanged(destination, destination_status, destination_digest)
    _assert_private_file_unchanged(temporary, temporary_status, temporary_digest)

    events: list[str] = []

    def second_crash_before_directory_fsync(descriptor: int) -> None:
        if descriptor == authority._directory_descriptor:
            events.append("directory_fsync_attempted")
            raise SimulatedSecretCrash
        descriptor_status = os.fstat(descriptor)
        if (descriptor_status.st_dev, descriptor_status.st_ino) == (
            marker_status.st_dev,
            marker_status.st_ino,
        ):
            events.append("tombstone_fsynced")
        real_fsync(descriptor)

    def record_descriptor_clear(descriptor: int, length: int) -> None:
        events.append("descriptor_cleared")
        real_ftruncate(descriptor, length)

    monkeypatch.setattr(os, "fsync", second_crash_before_directory_fsync)
    monkeypatch.setattr(os, "ftruncate", record_descriptor_clear)
    with pytest.raises(SimulatedSecretCrash):
        authority.delete(reference, operation_id=OPERATION_A)

    assert events == ["tombstone_fsynced", "directory_fsync_attempted"]
    _assert_private_file_unchanged(destination, destination_status, destination_digest)
    _assert_private_file_unchanged(temporary, temporary_status, temporary_digest)

    monkeypatch.setattr(os, "fsync", real_fsync)
    monkeypatch.setattr(os, "ftruncate", real_ftruncate)
    authority.delete(reference, operation_id=OPERATION_A)
    authority.delete(reference, operation_id=OPERATION_A)

    _assert_zero_length_private_files(directory, {DESTINATION_A, TEMPORARY_A, TOMBSTONE_A})


def test_delete_preserves_semantically_invalid_legacy_tombstone(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    reference = authority.store(OPERATION_A, _secrets())
    os.replace(
        DESTINATION_A,
        TOMBSTONE_A,
        src_dir_fd=authority._directory_descriptor,
        dst_dir_fd=authority._directory_descriptor,
    )
    os.fsync(authority._directory_descriptor)
    tombstone = directory / TOMBSTONE_A
    expected_status = tombstone.stat(follow_symlinks=False)
    expected_digest = hashlib.sha256(tombstone.read_bytes()).digest()

    with pytest.raises(WarehouseSecretStorageError) as failure:
        authority.delete(reference, operation_id=OPERATION_A)

    _assert_sanitized(failure.value)
    _assert_private_file_unchanged(tombstone, expected_status, expected_digest)


def test_delete_clears_verified_published_and_temporary_hard_link_residue(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    reference = authority.store(OPERATION_A, _secrets())
    os.link(
        DESTINATION_A,
        TEMPORARY_A,
        src_dir_fd=authority._directory_descriptor,
        dst_dir_fd=authority._directory_descriptor,
        follow_symlinks=False,
    )
    os.fsync(authority._directory_descriptor)

    authority.delete(reference, operation_id=OPERATION_A)
    authority.delete(reference, operation_id=OPERATION_A)

    expected_names = {DESTINATION_A, TEMPORARY_A, TOMBSTONE_A}
    _assert_zero_length_private_files(directory, expected_names)
    inodes = {
        (
            (directory / name).stat(follow_symlinks=False).st_dev,
            (directory / name).stat(follow_symlinks=False).st_ino,
        )
        for name in {DESTINATION_A, TEMPORARY_A}
    }
    assert len(inodes) == 1


def test_delete_never_calls_name_unlink_or_hard_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "private"
    authority = _authority(directory)
    reference = authority.store(OPERATION_A, _secrets())
    unlink_paths: list[str] = []
    link_paths: list[tuple[str, str]] = []

    def reject_name_unlink(path: str | bytes, *, dir_fd: int | None = None) -> None:
        del dir_fd
        unlink_paths.append(os.fsdecode(path))
        raise AssertionError("secret namespace deletion must remain descriptor-bound")

    def reject_hard_link(
        source: str | bytes,
        destination: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        del src_dir_fd, dst_dir_fd, follow_symlinks
        link_paths.append((os.fsdecode(source), os.fsdecode(destination)))
        raise AssertionError("secret deletion must not derive authority from a hard link")

    monkeypatch.setattr(os, "unlink", reject_name_unlink)
    monkeypatch.setattr(os, "link", reject_hard_link)

    authority.delete(reference, operation_id=OPERATION_A)

    assert unlink_paths == []
    assert link_paths == []


@pytest.mark.parametrize("fsync_before_crash", (False, True))
def test_directory_creation_replays_before_or_after_parent_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fsync_before_crash: bool,
) -> None:
    directory = tmp_path / "private"
    real_fsync = os.fsync
    crashed = False

    def crash_parent_fsync(descriptor: int) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            if fsync_before_crash:
                real_fsync(descriptor)
            raise SimulatedSecretCrash
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", crash_parent_fsync)
    with pytest.raises(SimulatedSecretCrash):
        _authority(directory)

    monkeypatch.setattr(os, "fsync", real_fsync)
    _authority(directory)

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_secret_failures_and_masked_results_never_expose_private_canaries(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "endpoint-private-path-canary"
    authority = _authority(directory)
    secrets = _secrets()
    reference = authority.store(OPERATION_A, secrets)
    resolved = authority.operation_capability(
        reference,
        operation_id=OPERATION_A,
        purpose="tls_private_key",
    ).resolve()

    with pytest.raises(WarehouseSecretStorageError) as failure:
        authority.operation_capability(
            reference,
            operation_id=OPERATION_B,
            purpose="tls_private_key",
        )

    rendered = "\n".join((str(failure.value), repr(failure.value), str(resolved), repr(resolved)))
    for canary in (
        "administration-secret-canary",
        "backup-secret-canary",
        "backup-encryption-secret-canary",
        "tls-private-key-secret-canary",
        "endpoint-private-path-canary",
    ):
        assert canary not in rendered
        assert canary not in caplog.text
