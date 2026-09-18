from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

import pytest
from heinzel_contract_model import ArtifactReference, digest
from heinzel_provider_openmetadata import (
    CatalogProviderError,
    OpenMetadataClient,
    OpenMetadataProductCatalogProvider,
    OpenMetadataSettings,
    ProviderHealth,
)
from heinzel_provider_openmetadata.client import (
    _description_metadata,
    _object_name,
    _OpenMetadataCredentials,
)
from heinzel_provider_openmetadata.models import CatalogFailureClassification
from heinzel_provider_sdk import (
    CatalogColumn,
    CatalogLineageSource,
    CatalogNativeTableDefinition,
    CatalogNativeTableObservation,
    CatalogProductDefinition,
    CatalogProductObservation,
    CatalogWarehouseHierarchyAuthority,
    ProviderError,
    ProviderErrorClassification,
    catalog_product_external_key,
    catalog_warehouse_service_external_key,
)
from httpx import Response
from pydantic import SecretStr


def _definition(**changes: object) -> CatalogProductDefinition:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "idempotency_key": "a" * 64,
        "stable_external_key": catalog_product_external_key(
            tenant_id="tenant-a", product_id="revenue", product_revision=2, generation=7
        ),
        "catalog_binding_id": "catalog-a",
        "catalog_revision": 3,
        "product_id": "revenue",
        "product_revision": 2,
        "generation": 7,
        "name": "Current revenue by region",
        "description": "Approved revenue grouped by region.",
        "owner_refs": ("owner:finance", "owner:revenue"),
        "namespace": "analytics",
        "relation_name": "revenue_by_region",
        "columns": (
            CatalogColumn(name="region", type_name="TEXT", nullable=False),
            CatalogColumn(name="revenue", type_name="NUMERIC", nullable=False),
        ),
        "lineage_sources": (
            CatalogLineageSource(
                source_ref="source:orders",
                freshness_observation_ref=ArtifactReference(
                    artifact_id="freshness:orders", version=8, digest="b" * 64
                ),
                freshness_observation_digest="b" * 64,
                watermark_at=datetime(2026, 9, 14, 11, tzinfo=UTC),
                observed_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
            ),
        ),
        "contract_digest": "c" * 64,
        "semantic_version_digest": "d" * 64,
        "materialization_receipt_ref": ArtifactReference(
            artifact_id="receipt:revenue", version=7, digest="e" * 64
        ),
        "materialization_receipt_digest": "e" * 64,
        "publication_authority_digest": "f" * 64,
    }
    values.update(changes)
    return CatalogProductDefinition.model_validate(values)


def _native_table_definition(**warehouse_changes: object) -> CatalogNativeTableDefinition:
    tenant_id = str(warehouse_changes.get("tenant_id", "tenant-a"))
    binding_id = str(warehouse_changes.get("warehouse_binding_id", "warehouse-a"))
    warehouse_values: dict[str, object] = {
        "tenant_id": tenant_id,
        "warehouse_binding_id": binding_id,
        "warehouse_binding_revision": 4,
        "warehouse_provider": "postgresql",
        "database_service_name": catalog_warehouse_service_external_key(
            tenant_id=tenant_id, warehouse_binding_id=binding_id
        ),
        "database_name": "heinzel",
        "schema_name": "analytics",
        "table_name": "revenue_by_region",
    }
    warehouse_values.update(warehouse_changes)
    return CatalogNativeTableDefinition(
        product=_definition(),
        warehouse=CatalogWarehouseHierarchyAuthority.model_validate(warehouse_values),
    )


class _Client:
    def __init__(self) -> None:
        self.snapshots: dict[tuple[str, str], CatalogProductObservation] = {}
        self.publish_calls = 0
        self.table_snapshots: dict[str, CatalogNativeTableObservation] = {}

    def health(self) -> ProviderHealth:
        return ProviderHealth()

    def ensure_product_catalog_snapshot(
        self, definition: CatalogProductDefinition
    ) -> CatalogProductObservation:
        self.publish_calls += 1
        key = (definition.tenant_id, definition.stable_external_key)
        candidate = CatalogProductObservation(
            tenant_id=definition.tenant_id,
            stable_external_key=definition.stable_external_key,
            definition=definition,
            definition_digest=digest(definition),
            provider_version="1.13.3",
        )
        existing = self.snapshots.setdefault(key, candidate)
        if existing != candidate:
            raise CatalogProviderError("immutable snapshot conflict", classification="conflict")
        return existing

    def get_product_catalog_snapshot(
        self, *, tenant_id: str, stable_external_key: str
    ) -> CatalogProductObservation:
        try:
            return self.snapshots[(tenant_id, stable_external_key)]
        except KeyError:
            raise CatalogProviderError("snapshot not found", classification="permanent") from None

    def ensure_native_table_snapshot(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        hierarchy = definition.warehouse
        candidate = CatalogNativeTableObservation(
            definition=definition,
            definition_digest=digest(definition),
            table_fully_qualified_name=".".join(
                (
                    hierarchy.database_service_name,
                    hierarchy.database_name,
                    hierarchy.schema_name,
                    hierarchy.table_name,
                )
            ),
            provider_version="1.13.3",
        )
        return self.table_snapshots.setdefault(candidate.table_fully_qualified_name, candidate)

    def get_native_table_snapshot(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        hierarchy = definition.warehouse
        return self.table_snapshots[
            ".".join(
                (
                    hierarchy.database_service_name,
                    hierarchy.database_name,
                    hierarchy.schema_name,
                    hierarchy.table_name,
                )
            )
        ]


def _response(status_code: int, payload: dict[str, object]) -> Response:
    return Response(status_code, json=payload)


class _ProductSnapshotTransport:
    def __init__(self) -> None:
        self.term_payload: dict[str, object] | None = None
        self.domain_payload: dict[str, object] | None = None
        self.data_product_payload: dict[str, object] | None = None
        self.post_count = 0
        self.put_count = 0

    def get(self, url: str, **kwargs: object) -> Response:
        if url.endswith("/api/v1/system/health"):
            return Response(200, text="OK")
        if "/api/v1/glossaries/name/" in url:
            name = _object_name("tenant-a", "namespace")
            return _response(
                200,
                {"id": "00000000-0000-4000-8000-000000000001", "name": name},
            )
        if "/api/v1/users/name/" in url:
            return _response(
                200,
                {
                    "id": "00000000-0000-4000-8000-000000000002",
                    "name": _object_name("tenant-a", "runtime"),
                },
            )
        if "/api/v1/domains/name/" in url and self.domain_payload is not None:
            return _response(200, self._domain_response())
        if "/api/v1/dataProducts/name/" in url and self.data_product_payload is not None:
            assert url.endswith(f"/name/{self.data_product_payload['name']}")
            return _response(200, self._data_product_response())
        if "/api/v1/glossaryTerms/name/" in url and self.term_payload is not None:
            return _response(200, self._term_response())
        return _response(404, {"message": "missing"})

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return _response(200, {"accessToken": "test-token"})
        if method == "PUT" and url.endswith("/api/v1/domains"):
            payload = kwargs["json"]
            assert isinstance(payload, dict)
            self.put_count += 1
            self.domain_payload = payload.copy()
            return _response(200, self._domain_response())
        if method == "PUT" and url.endswith("/api/v1/dataProducts"):
            payload = kwargs["json"]
            assert isinstance(payload, dict)
            self.put_count += 1
            self.data_product_payload = payload.copy()
            return _response(200, self._data_product_response())
        assert method == "POST"
        assert url.endswith("/api/v1/glossaryTerms")
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        self.post_count += 1
        self.term_payload = payload.copy()
        return _response(201, self._term_response())

    def _term_response(self) -> dict[str, object]:
        assert self.term_payload is not None
        namespace = _object_name("tenant-a", "namespace")
        return {
            "id": "00000000-0000-4000-8000-000000000003",
            "name": self.term_payload["name"],
            "fullyQualifiedName": f"{namespace}.{self.term_payload['name']}",
            "displayName": self.term_payload["displayName"],
            "description": self.term_payload["description"],
            "glossary": {
                "id": "00000000-0000-4000-8000-000000000001",
                "type": "glossary",
                "name": namespace,
            },
            "owners": self.term_payload["owners"],
        }

    def _domain_response(self) -> dict[str, object]:
        assert self.domain_payload is not None
        return {
            "id": "00000000-0000-4000-8000-000000000004",
            "name": self.domain_payload["name"],
            "fullyQualifiedName": self.domain_payload["name"],
            "displayName": self.domain_payload["displayName"],
            "description": self.domain_payload["description"],
            "domainType": self.domain_payload["domainType"],
            "owners": self.domain_payload["owners"],
        }

    def _data_product_response(self) -> dict[str, object]:
        assert self.data_product_payload is not None
        assert self.domain_payload is not None
        domain_name = str(self.domain_payload["name"])
        return {
            "id": "00000000-0000-4000-8000-000000000005",
            "name": self.data_product_payload["name"],
            "fullyQualifiedName": self.data_product_payload["name"],
            "displayName": self.data_product_payload["displayName"],
            "description": self.data_product_payload["description"],
            "owners": self.data_product_payload["owners"],
            "domains": [
                {
                    "id": "00000000-0000-4000-8000-000000000004",
                    "type": "domain",
                    "name": domain_name,
                    "fullyQualifiedName": domain_name,
                }
            ],
            "dataProductType": self.data_product_payload["dataProductType"],
            "visibility": self.data_product_payload["visibility"],
        }


class _NativeTableTransport:
    _IDS: ClassVar[dict[str, str]] = {
        "databaseServices": "00000000-0000-4000-8000-000000000010",
        "databases": "00000000-0000-4000-8000-000000000011",
        "databaseSchemas": "00000000-0000-4000-8000-000000000012",
        "tables": "00000000-0000-4000-8000-000000000013",
    }

    def __init__(self) -> None:
        self.payloads: dict[str, dict[str, object]] = {}
        self.put_collections: list[str] = []

    def get(self, url: str, **kwargs: object) -> Response:
        if url.endswith("/api/v1/system/health"):
            return Response(200, text="OK")
        if "/api/v1/users/name/" in url:
            return _response(
                200,
                {
                    "id": "00000000-0000-4000-8000-000000000002",
                    "name": _object_name("tenant-a", "runtime"),
                },
            )
        for collection in self._IDS:
            endpoint = (
                "services/databaseServices" if collection == "databaseServices" else collection
            )
            if f"/api/v1/{endpoint}/name/" in url and collection in self.payloads:
                return _response(200, self._entity_response(collection))
        return _response(404, {"message": "missing"})

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return _response(200, {"accessToken": "test-token"})
        assert method == "PUT"
        collection = url.rsplit("/", 1)[-1]
        assert collection in self._IDS
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        self.put_collections.append(collection)
        self.payloads[collection] = payload.copy()
        return _response(200, self._entity_response(collection))

    def _entity_response(self, collection: str) -> dict[str, object]:
        payload = self.payloads[collection]
        service_name = str(
            self.payloads.get("databaseServices", {}).get("name", "heinzel_postgresql")
        )
        database_name = str(self.payloads.get("databases", {}).get("name", "heinzel"))
        schema_name = str(self.payloads.get("databaseSchemas", {}).get("name", "analytics"))
        fqns = {
            "databaseServices": service_name,
            "databases": f"{service_name}.{database_name}",
            "databaseSchemas": f"{service_name}.{database_name}.{schema_name}",
            "tables": (
                f"{service_name}.{database_name}.{schema_name}."
                f"{payload.get('name', 'revenue_by_region')}"
            ),
        }
        response = {
            "id": self._IDS[collection],
            "name": payload["name"],
            "fullyQualifiedName": fqns[collection],
            "displayName": payload.get("displayName"),
            "description": payload.get("description"),
        }
        if collection == "databaseServices":
            response["serviceType"] = payload["serviceType"]
        elif collection == "databases":
            response["service"] = {
                "id": self._IDS["databaseServices"],
                "type": "databaseService",
                "name": service_name,
                "fullyQualifiedName": service_name,
            }
        elif collection == "databaseSchemas":
            response["database"] = {
                "id": self._IDS["databases"],
                "type": "database",
                "name": database_name,
                "fullyQualifiedName": f"{service_name}.{database_name}",
            }
        else:
            domain_name = _object_name("tenant-a", "product-domain")
            product_name = _object_name("tenant-a", _definition().stable_external_key)
            response.update(
                {
                    "tableType": payload["tableType"],
                    "columns": [
                        {
                            **column,
                            "fullyQualifiedName": f"{fqns['tables']}.{column['name']}",
                        }
                        for column in payload["columns"]
                    ],
                    "databaseSchema": {
                        "id": self._IDS["databaseSchemas"],
                        "type": "databaseSchema",
                        "name": schema_name,
                        "fullyQualifiedName": f"{service_name}.{database_name}.{schema_name}",
                    },
                    "dataProducts": [
                        {
                            "id": "00000000-0000-4000-8000-000000000015",
                            "type": "dataProduct",
                            "name": product_name,
                            "fullyQualifiedName": product_name,
                        }
                    ],
                    "domains": [
                        {
                            "id": "00000000-0000-4000-8000-000000000014",
                            "type": "domain",
                            "name": domain_name,
                            "fullyQualifiedName": domain_name,
                        }
                    ],
                    "owners": payload["owners"],
                }
            )
        return response


def _client(transport: object) -> OpenMetadataClient:
    return OpenMetadataClient(
        settings=OpenMetadataSettings(base_url="http://127.0.0.1:8585"),
        credentials=_OpenMetadataCredentials(
            username="admin@example.invalid",
            password=SecretStr("test-password"),
            runtime_password=SecretStr("test-runtime-password"),
            administrator_password=SecretStr("test-administrator-password"),
        ),
        transport=transport,
    )


def test_openmetadata_native_table_creates_exact_hierarchy_and_round_trips_columns() -> None:
    transport = _NativeTableTransport()
    client = _client(transport)
    definition = _native_table_definition()

    published = client.ensure_native_table_snapshot(definition)
    replayed = client.ensure_native_table_snapshot(definition)
    observed = client.get_native_table_snapshot(definition)

    assert published == replayed == observed
    assert observed.definition == definition
    assert observed.definition.product.columns == definition.product.columns
    assert transport.put_collections == [
        "databaseServices",
        "databases",
        "databaseSchemas",
        "tables",
    ]
    assert "connection" not in transport.payloads["databaseServices"]
    assert transport.payloads["databaseServices"]["serviceType"] == "Postgres"
    assert transport.payloads["tables"]["columns"] == [
        {
            "name": "region",
            "dataType": "TEXT",
            "dataTypeDisplay": "text",
            "constraint": "NOT_NULL",
        },
        {
            "name": "revenue",
            "dataType": "NUMERIC",
            "dataTypeDisplay": "numeric",
            "constraint": "NOT_NULL",
        },
    ]
    assert transport.payloads["tables"]["owners"] == [
        {"id": "00000000-0000-4000-8000-000000000002", "type": "user"}
    ]
    assert {resource.collection for resource in client.discovered_resources()} == {
        "databaseServices",
        "databases",
        "databaseSchemas",
        "tables",
    }


def test_openmetadata_native_table_rejects_conflicting_column_without_mutating() -> None:
    transport = _NativeTableTransport()
    client = _client(transport)
    definition = _native_table_definition()
    client.ensure_native_table_snapshot(definition)
    transport.payloads["tables"]["columns"] = [
        {
            "name": "region",
            "dataType": "BIGINT",
            "dataTypeDisplay": "BIGINT",
            "constraint": "NOT_NULL",
        }
    ]

    with pytest.raises(CatalogProviderError, match="native table conflicts") as captured:
        client.ensure_native_table_snapshot(definition)

    assert captured.value.classification == "conflict"
    assert transport.put_collections == [
        "databaseServices",
        "databases",
        "databaseSchemas",
        "tables",
    ]


def test_openmetadata_native_table_does_not_claim_preexisting_resources_for_cleanup() -> None:
    transport = _NativeTableTransport()
    definition = _native_table_definition()
    _client(transport).ensure_native_table_snapshot(definition)
    observing_client = _client(transport)

    observed = observing_client.ensure_native_table_snapshot(definition)

    assert observed.definition == definition
    assert observing_client.discovered_resources() == ()
    assert transport.put_collections == [
        "databaseServices",
        "databases",
        "databaseSchemas",
        "tables",
    ]


def test_openmetadata_native_table_records_created_resource_before_rejecting_readback() -> None:
    class CorruptingTransport(_NativeTableTransport):
        def _entity_response(self, collection: str) -> dict[str, object]:
            response = super()._entity_response(collection)
            if collection == "tables":
                response["description"] = "Provider changed the governed metadata."
            return response

    client = _client(CorruptingTransport())

    with pytest.raises(CatalogProviderError, match="native table conflicts"):
        client.ensure_native_table_snapshot(_native_table_definition())

    assert {resource.collection for resource in client.discovered_resources()} == {
        "databaseServices",
        "databases",
        "databaseSchemas",
        "tables",
    }


def test_openmetadata_product_snapshot_round_trips_every_governed_field() -> None:
    client = _Client()
    provider = OpenMetadataProductCatalogProvider(client)
    definition = _definition()

    published = provider.publish(definition)
    replayed = provider.publish(definition)
    observed = provider.observe(
        tenant_id=definition.tenant_id,
        stable_external_key=definition.stable_external_key,
    )
    native_definition = _native_table_definition()
    published_table = provider.publish_native_table(native_definition)
    observed_table = provider.observe_native_table(native_definition)

    assert provider.provider_kind == "openmetadata"
    assert published == replayed == observed
    assert observed.definition == definition
    assert observed.definition.columns == definition.columns
    assert observed.definition.owner_refs == definition.owner_refs
    assert observed.definition.lineage_sources == definition.lineage_sources
    assert observed.definition.catalog_revision == 3
    assert observed.definition.generation == 7
    assert observed.definition.materialization_receipt_ref.version == 7
    assert client.publish_calls == 2
    assert published_table == observed_table
    assert observed_table.definition.product == definition


def test_openmetadata_client_persists_and_reconstructs_the_snapshot_from_remote_metadata() -> None:
    transport = _ProductSnapshotTransport()
    client = OpenMetadataClient(
        settings=OpenMetadataSettings(base_url="http://127.0.0.1:8585"),
        credentials=_OpenMetadataCredentials(
            username="admin@example.invalid",
            password=SecretStr("test-password"),
            runtime_password=SecretStr("test-runtime-password"),
            administrator_password=SecretStr("test-administrator-password"),
        ),
        transport=transport,
    )
    definition = _definition()

    published = client.ensure_product_catalog_snapshot(definition)
    replayed = client.ensure_product_catalog_snapshot(definition)
    observed = client.get_product_catalog_snapshot(
        tenant_id=definition.tenant_id,
        stable_external_key=definition.stable_external_key,
    )

    assert published == replayed == observed
    assert observed.definition == definition
    assert transport.term_payload is not None
    assert transport.term_payload["displayName"] == definition.name
    assert transport.post_count == 1
    assert transport.domain_payload is not None
    assert transport.domain_payload["domainType"] == "Consumer-aligned"
    assert transport.data_product_payload is not None
    assert transport.data_product_payload["displayName"] == definition.name
    assert transport.data_product_payload["domains"] == [transport.domain_payload["name"]]
    _, native_metadata = _description_metadata(
        str(transport.data_product_payload["description"]),
        required=frozenset(
            {
                "definition_digest",
                "materialization_receipt_digest",
                "snapshot_external_key",
                "tenant_key",
            }
        ),
    )
    assert native_metadata["snapshot_external_key"] == definition.stable_external_key
    assert native_metadata["definition_digest"] == digest(definition)
    assert transport.put_count == 2
    assert {resource.collection for resource in client.discovered_resources()} >= {
        "domains",
        "dataProducts",
        "glossaryTerms",
    }


def test_openmetadata_client_rejects_conflicting_remote_snapshot_instead_of_overwriting() -> None:
    transport = _ProductSnapshotTransport()
    client = OpenMetadataClient(
        settings=OpenMetadataSettings(base_url="http://127.0.0.1:8585"),
        credentials=_OpenMetadataCredentials(
            username="admin@example.invalid",
            password=SecretStr("test-password"),
            runtime_password=SecretStr("test-runtime-password"),
            administrator_password=SecretStr("test-administrator-password"),
        ),
        transport=transport,
    )
    definition = _definition()
    client.ensure_product_catalog_snapshot(definition)
    assert transport.term_payload is not None
    transport.term_payload["displayName"] = "Conflicting remote title"

    with pytest.raises(CatalogProviderError, match="conflicts") as captured:
        client.ensure_product_catalog_snapshot(definition)

    assert captured.value.classification == "conflict"
    assert transport.term_payload["displayName"] == "Conflicting remote title"
    assert transport.post_count == 1


def test_openmetadata_client_rejects_conflicting_native_product_without_overwriting() -> None:
    transport = _ProductSnapshotTransport()
    client = OpenMetadataClient(
        settings=OpenMetadataSettings(base_url="http://127.0.0.1:8585"),
        credentials=_OpenMetadataCredentials(
            username="admin@example.invalid",
            password=SecretStr("test-password"),
            runtime_password=SecretStr("test-runtime-password"),
            administrator_password=SecretStr("test-administrator-password"),
        ),
        transport=transport,
    )
    definition = _definition()
    client.ensure_product_catalog_snapshot(definition)
    assert transport.data_product_payload is not None
    transport.data_product_payload["displayName"] = "Conflicting native title"

    with pytest.raises(CatalogProviderError, match="native data product conflicts") as captured:
        client.ensure_product_catalog_snapshot(definition)

    assert captured.value.classification == "conflict"
    assert transport.data_product_payload["displayName"] == "Conflicting native title"
    assert transport.put_count == 2


def test_openmetadata_product_snapshot_observation_is_tenant_scoped() -> None:
    provider = OpenMetadataProductCatalogProvider(_Client())
    definition = _definition()
    provider.publish(definition)

    with pytest.raises(ProviderError, match="observation failed") as captured:
        provider.observe(tenant_id="tenant-b", stable_external_key=definition.stable_external_key)

    assert captured.value.classification == "permanent"


@pytest.mark.parametrize(
    ("catalog_classification", "provider_classification"),
    (
        ("transient", "retryable"),
        ("throttled", "throttled"),
        ("authentication", "authorization"),
        ("authorization", "authorization"),
        ("conflict", "ambiguous"),
        ("invalid_request", "permanent"),
        ("permanent", "permanent"),
    ),
)
def test_openmetadata_product_snapshot_translates_provider_failures(
    catalog_classification: CatalogFailureClassification,
    provider_classification: ProviderErrorClassification,
) -> None:
    class FailingClient(_Client):
        def ensure_product_catalog_snapshot(
            self, definition: CatalogProductDefinition
        ) -> CatalogProductObservation:
            raise CatalogProviderError(
                "private provider failure",
                classification=catalog_classification,
            )

    with pytest.raises(ProviderError, match="publication failed") as captured:
        OpenMetadataProductCatalogProvider(FailingClient()).publish(_definition())

    assert captured.value.classification == provider_classification


@pytest.mark.parametrize(
    ("catalog_classification", "provider_classification"),
    (
        ("transient", "retryable"),
        ("throttled", "throttled"),
        ("authentication", "authorization"),
        ("authorization", "authorization"),
        ("conflict", "ambiguous"),
        ("invalid_request", "permanent"),
        ("permanent", "permanent"),
    ),
)
def test_openmetadata_native_table_translates_provider_failures(
    catalog_classification: CatalogFailureClassification,
    provider_classification: ProviderErrorClassification,
) -> None:
    class FailingClient(_Client):
        def ensure_native_table_snapshot(
            self, definition: CatalogNativeTableDefinition
        ) -> CatalogNativeTableObservation:
            raise CatalogProviderError(
                "private provider failure",
                classification=catalog_classification,
            )

    with pytest.raises(ProviderError, match="native table publication failed") as captured:
        OpenMetadataProductCatalogProvider(FailingClient()).publish_native_table(
            _native_table_definition()
        )

    assert captured.value.classification == provider_classification
