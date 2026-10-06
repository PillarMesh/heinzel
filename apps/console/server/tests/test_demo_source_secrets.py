from __future__ import annotations

import stat
from datetime import timedelta
from pathlib import Path

import pytest
from heinzel_connection_broker import SourceSecretResolver
from heinzel_console.demo.source_secrets import (
    DEMO_SOURCE_SECRET_DIRNAME,
    DemoPostgreSQLSourceCapabilityAuthority,
    DemoSourceSecretError,
    DemoSourceSecretStore,
)
from heinzel_provider_postgresql import (
    PostgreSQLAcquisitionSettings,
    PostgreSQLSourceObjectDeclaration,
)
from pydantic import SecretStr

HANDLE = "demo-source"
OTHER_HANDLE = "another-source"
TENANT = "tenant-demo"
BINDING = "src-demo-orders"
DSN = "postgresql://acquisition_runtime:correct-horse@127.0.0.1:5432/source"


def _store(state_dir: Path) -> SourceSecretResolver:
    """The store, seen as `SourceBindingService` sees it.

    The annotation is the point: it makes the type checker, not a comment, answer for this class
    satisfying the protocol the service takes.
    """
    return DemoSourceSecretStore(state_dir)


def _enrolled(state_dir: Path, *, dsn: str = DSN) -> DemoSourceSecretStore:
    store = DemoSourceSecretStore(state_dir)
    store.enroll_connection(connection_handle=HANDLE, dsn=SecretStr(dsn))
    return store


def _settings(connection_handle: str, dsn: SecretStr) -> PostgreSQLAcquisitionSettings:
    return PostgreSQLAcquisitionSettings(
        dsn=dsn,
        connection_handle=connection_handle,
        objects=(
            PostgreSQLSourceObjectDeclaration(
                logical_object_ref="orders",
                schema_name="source_data",
                table_name="customer_orders",
                field_names=("order_id", "updated_at"),
                key_name="order_id",
                source_updated_at_field="updated_at",
            ),
        ),
        unrelated_schema_name="private_admin",
        max_write_transaction_duration=timedelta(minutes=5),
    )


def test_a_resolved_capability_carries_references_rather_than_the_connection_detail(
    tmp_path: Path,
) -> None:
    """A reference is what the broker persists, so none of it may be the secret it names."""
    store = _enrolled(tmp_path / "state")
    capability = store.resolve(
        tenant_id=TENANT,
        binding_id=BINDING,
        provider_kind="postgresql",
        connection_handle=HANDLE,
        account_mode="not_applicable",
        credential_revision=1,
    )
    assert capability.endpoint_reference.startswith("endpoint-ref:")
    assert capability.credential_reference.startswith("credential-ref:")
    assert DSN not in capability.model_dump_json()
    assert "correct-horse" not in capability.model_dump_json()
    assert "correct-horse" not in repr(capability)


def test_a_reference_is_not_derived_from_the_connection_detail_it_names(tmp_path: Path) -> None:
    """Two bindings over one enrolled connection get unrelated references.

    A reference derived from the secret would be an oracle for it: anyone holding one could test
    a guess at the credential by deriving the reference a guess would produce.
    """
    store = _enrolled(tmp_path / "state")
    first = store.resolve(
        tenant_id=TENANT,
        binding_id=BINDING,
        provider_kind="postgresql",
        connection_handle=HANDLE,
        account_mode="not_applicable",
        credential_revision=1,
    )
    second = store.resolve(
        tenant_id=TENANT,
        binding_id="src-demo-other",
        provider_kind="postgresql",
        connection_handle=HANDLE,
        account_mode="not_applicable",
        credential_revision=1,
    )
    assert first.endpoint_reference != second.endpoint_reference
    assert first.credential_reference != second.credential_reference


def test_resolving_one_binding_twice_returns_the_reference_pair_already_minted(
    tmp_path: Path,
) -> None:
    """The broker persisted the first pair, and a probe will present it back.

    The second store is the one the service holds, by its protocol rather than its class: the
    annotation on `_store` makes the type checker answer for this class satisfying it.
    """
    _enrolled(tmp_path / "state")
    resolver = _store(tmp_path / "state")
    first = resolver.resolve(
        tenant_id=TENANT,
        binding_id=BINDING,
        provider_kind="postgresql",
        connection_handle=HANDLE,
        account_mode="not_applicable",
        credential_revision=1,
    )
    assert (
        resolver.resolve(
            tenant_id=TENANT,
            binding_id=BINDING,
            provider_kind="postgresql",
            connection_handle=HANDLE,
            account_mode="not_applicable",
            credential_revision=1,
        )
        == first
    )
    reopened = _store(tmp_path / "state")
    assert (
        reopened.resolve(
            tenant_id=TENANT,
            binding_id=BINDING,
            provider_kind="postgresql",
            connection_handle=HANDLE,
            account_mode="not_applicable",
            credential_revision=1,
        )
        == first
    )


def test_a_rotated_credential_revision_mints_a_reference_the_previous_one_does_not_open(
    tmp_path: Path,
) -> None:
    """`rotate_credentials` asks at the next revision, and must not get the old pair back."""
    store = _enrolled(tmp_path / "state")
    first = store.resolve(
        tenant_id=TENANT,
        binding_id=BINDING,
        provider_kind="postgresql",
        connection_handle=HANDLE,
        account_mode="not_applicable",
        credential_revision=1,
    )
    second = store.resolve(
        tenant_id=TENANT,
        binding_id=BINDING,
        provider_kind="postgresql",
        connection_handle=HANDLE,
        account_mode="not_applicable",
        credential_revision=2,
    )
    assert second.credential_reference != first.credential_reference
    with pytest.raises(DemoSourceSecretError):
        store.resolve_connection(
            tenant_id=TENANT,
            binding_id=BINDING,
            credential_revision=2,
            endpoint_reference=first.endpoint_reference,
            credential_reference=first.credential_reference,
        )


def test_resolving_a_connection_that_was_never_enrolled_is_refused(tmp_path: Path) -> None:
    store = DemoSourceSecretStore(tmp_path / "state")
    with pytest.raises(DemoSourceSecretError) as refusal:
        store.resolve(
            tenant_id=TENANT,
            binding_id=BINDING,
            provider_kind="postgresql",
            connection_handle=OTHER_HANDLE,
            account_mode="not_applicable",
            credential_revision=1,
        )
    assert refusal.value.operation == "resolve a source capability"


def test_enrolling_one_handle_with_a_second_detail_is_refused(tmp_path: Path) -> None:
    """A binding validated against the first would be ready over a source nobody probed."""
    store = _enrolled(tmp_path / "state")
    store.enroll_connection(connection_handle=HANDLE, dsn=SecretStr(DSN))
    with pytest.raises(DemoSourceSecretError) as refusal:
        store.enroll_connection(
            connection_handle=HANDLE,
            dsn=SecretStr("postgresql://acquisition_runtime:other@127.0.0.1:5432/source"),
        )
    assert refusal.value.operation == "enrol a source connection"
    assert "other" not in str(refusal.value)


def test_a_half_matching_reference_pair_resolves_nothing(tmp_path: Path) -> None:
    """A reference is the only thing between a caller and the connection detail."""
    store = _enrolled(tmp_path / "state")
    capability = store.resolve(
        tenant_id=TENANT,
        binding_id=BINDING,
        provider_kind="postgresql",
        connection_handle=HANDLE,
        account_mode="not_applicable",
        credential_revision=1,
    )
    with pytest.raises(DemoSourceSecretError):
        store.resolve_connection(
            tenant_id=TENANT,
            binding_id=BINDING,
            credential_revision=1,
            endpoint_reference=capability.endpoint_reference,
            credential_reference="credential-ref:" + "0" * 64,
        )
    assert store.resolve_connection(
        tenant_id=TENANT,
        binding_id=BINDING,
        credential_revision=1,
        endpoint_reference=capability.endpoint_reference,
        credential_reference=capability.credential_reference,
    ) == (HANDLE, SecretStr(DSN))


def test_nothing_the_store_writes_is_readable_by_anyone_but_its_owner(tmp_path: Path) -> None:
    """A connection detail written world readable is a connection detail given away."""
    store = _enrolled(tmp_path / "state")
    store.resolve(
        tenant_id=TENANT,
        binding_id=BINDING,
        provider_kind="postgresql",
        connection_handle=HANDLE,
        account_mode="not_applicable",
        credential_revision=1,
    )
    directory = tmp_path / "state" / DEMO_SOURCE_SECRET_DIRNAME
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    written = sorted(directory.iterdir())
    assert len(written) == 2
    for path in written:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_no_stored_file_name_carries_the_connection_detail(tmp_path: Path) -> None:
    store = _enrolled(tmp_path / "state")
    store.resolve(
        tenant_id=TENANT,
        binding_id=BINDING,
        provider_kind="postgresql",
        connection_handle=HANDLE,
        account_mode="not_applicable",
        credential_revision=1,
    )
    directory = tmp_path / "state" / DEMO_SOURCE_SECRET_DIRNAME
    for path in directory.iterdir():
        assert "correct-horse" not in path.name


def test_a_record_that_is_no_longer_one_is_refused_rather_than_dialled(tmp_path: Path) -> None:
    """A truncated or replaced file must fail where the failure names the store."""
    store = _enrolled(tmp_path / "state")
    directory = tmp_path / "state" / DEMO_SOURCE_SECRET_DIRNAME
    (connection_file,) = list(directory.iterdir())
    connection_file.write_text("{}", encoding="utf-8")
    with pytest.raises(DemoSourceSecretError) as refusal:
        store.resolve(
            tenant_id=TENANT,
            binding_id=BINDING,
            provider_kind="postgresql",
            connection_handle=HANDLE,
            account_mode="not_applicable",
            credential_revision=1,
        )
    assert "not readable as one" in str(refusal.value)


def test_the_probe_authority_composes_the_settings_the_resolved_detail_names(
    tmp_path: Path,
) -> None:
    """What the probe receives is the deployment's own declaration over the resolved DSN."""
    store = _enrolled(tmp_path / "state")
    capability = store.resolve(
        tenant_id=TENANT,
        binding_id=BINDING,
        provider_kind="postgresql",
        connection_handle=HANDLE,
        account_mode="not_applicable",
        credential_revision=1,
    )
    authority = DemoPostgreSQLSourceCapabilityAuthority(store, _settings)
    settings = authority.resolve_source_acquisition(
        tenant_id=TENANT,
        binding_id=BINDING,
        credential_revision=1,
        endpoint_reference=capability.endpoint_reference,
        credential_reference=capability.credential_reference,
    )
    assert settings.connection_handle == HANDLE
    assert settings.dsn.get_secret_value() == DSN
    assert tuple(item.logical_object_ref for item in settings.objects) == ("orders",)


def test_the_probe_authority_refuses_a_reference_pair_it_was_not_minted(tmp_path: Path) -> None:
    store = _enrolled(tmp_path / "state")
    store.resolve(
        tenant_id=TENANT,
        binding_id=BINDING,
        provider_kind="postgresql",
        connection_handle=HANDLE,
        account_mode="not_applicable",
        credential_revision=1,
    )
    authority = DemoPostgreSQLSourceCapabilityAuthority(store, _settings)
    with pytest.raises(DemoSourceSecretError):
        authority.resolve_source_acquisition(
            tenant_id=TENANT,
            binding_id=BINDING,
            credential_revision=1,
            endpoint_reference="endpoint-ref:" + "0" * 64,
            credential_reference="credential-ref:" + "0" * 64,
        )
