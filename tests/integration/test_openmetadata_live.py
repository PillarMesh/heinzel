from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
from heinzel_catalog_control import CatalogBindingState

from tests.integration.openmetadata_live_harness import LocalOpenMetadata

_EXPECTED_PRIVATE_RESOURCE_COUNTS = (
    ("backup_artifact", 1),
    ("catalog_classification", 1),
    ("catalog_glossary_term", 2),
    ("catalog_lineage", 1),
    ("catalog_policy", 1),
    ("catalog_role", 1),
    ("catalog_tag", 1),
    ("compose_container", 6),
    ("compose_network", 1),
    ("compose_volume", 2),
    ("service_account", 3),
    ("tenant_namespace", 2),
)


@pytest.fixture
def local_openmetadata(tmp_path: Path) -> Generator[LocalOpenMetadata]:
    local = LocalOpenMetadata(tmp_path)
    try:
        yield local
    finally:
        local.cleanup()


@dataclass(frozen=True, slots=True)
class _TrackedResource:
    collection: str
    identifier: str


class _ResidualClient:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    def discovered_resources(self) -> tuple[_TrackedResource, ...]:
        return (
            _TrackedResource(collection="domains", identifier="domain-a"),
            _TrackedResource(collection="dataProducts", identifier="product-a"),
        )

    def delete_recorded_resource(self, *, collection: str, identifier: str) -> None:
        self.deleted.append((collection, identifier))

    def recorded_resource_is_absent(self, *, collection: str, identifier: str) -> bool:
        return identifier != "domain-a"


def test_supplemental_cleanup_rejects_a_residual_provider_object() -> None:
    client = _ResidualClient()
    local = LocalOpenMetadata.__new__(LocalOpenMetadata)

    with pytest.raises(RuntimeError, match="cleanup is not terminal"):
        local.cleanup_client_resources(client)

    assert client.deleted == [
        ("dataProducts", "product-a"),
        ("domains", "domain-a"),
    ]


def test_live_harness_skips_safely_when_local_credentials_are_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HEINZEL_OPENMETADATA_SECRET_STORE_KEY", raising=False)
    monkeypatch.delenv("HEINZEL_OPENMETADATA_BOOTSTRAP_ADMIN_PASSWORD", raising=False)

    with pytest.raises(pytest.skip.Exception, match="OpenMetadata live credentials are required"):
        LocalOpenMetadata(tmp_path)


@pytest.mark.live
@pytest.mark.emulator
def test_real_catalog_lifecycle_round_trip(local_openmetadata: LocalOpenMetadata) -> None:
    binding = local_openmetadata.provision_and_validate("tenant-a")
    assert binding.binding.lifecycle_state is CatalogBindingState.READY
    assert binding.validation_evidence is not None
    assert len(binding.validation_evidence.positive_probe_digest) == 64
    assert len(binding.validation_evidence.denial_probe_digest) == 64
    ledger = local_openmetadata.resource_ledger(binding)
    assert ledger.resource_kind_counts == _EXPECTED_PRIVATE_RESOURCE_COUNTS
    assert ledger.all_exact_resources_created
    assert len(ledger.provider_ids) == sum(count for _, count in _EXPECTED_PRIVATE_RESOURCE_COUNTS)

    binding = local_openmetadata.suspend(binding)
    assert binding.binding.lifecycle_state is CatalogBindingState.SUSPENDED
    binding = local_openmetadata.resume(binding)
    assert binding.binding.lifecycle_state is CatalogBindingState.READY
    restored = local_openmetadata.backup_restore_and_probe(binding)
    assert restored.representative_objects_verified is True

    binding = local_openmetadata.retire(binding)
    assert binding.binding.lifecycle_state is CatalogBindingState.RETIRED
    ledger = local_openmetadata.resource_ledger(binding)
    assert ledger.all_cleaned
