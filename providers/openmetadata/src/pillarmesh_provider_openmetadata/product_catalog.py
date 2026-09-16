from __future__ import annotations

from typing import Literal, Protocol

from pillarmesh_provider_sdk import (
    CatalogNativeTableDefinition,
    CatalogNativeTableObservation,
    CatalogProductDefinition,
    CatalogProductObservation,
    ProviderError,
    ProviderErrorClassification,
)
from pydantic import ValidationError

from .models import CatalogFailureClassification, CatalogProviderError, ProviderHealth


class _OpenMetadataProductCatalogClient(Protocol):
    def health(self) -> ProviderHealth: ...

    def ensure_product_catalog_snapshot(
        self, definition: CatalogProductDefinition
    ) -> CatalogProductObservation: ...

    def get_product_catalog_snapshot(
        self, *, tenant_id: str, stable_external_key: str
    ) -> CatalogProductObservation: ...

    def ensure_native_table_snapshot(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation: ...

    def get_native_table_snapshot(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation: ...


_PROVIDER_CLASSIFICATION: dict[CatalogFailureClassification, ProviderErrorClassification] = {
    "transient": "retryable",
    "throttled": "throttled",
    "authentication": "authorization",
    "authorization": "authorization",
    "conflict": "ambiguous",
    "invalid_request": "permanent",
    "permanent": "permanent",
}


class OpenMetadataProductCatalogProvider:
    """Publishes complete governed product snapshots through the OpenMetadata client."""

    def __init__(self, client: _OpenMetadataProductCatalogClient) -> None:
        self._client = client

    @property
    def provider_kind(self) -> Literal["openmetadata"]:
        return "openmetadata"

    def publish(self, definition: CatalogProductDefinition) -> CatalogProductObservation:
        definition = CatalogProductDefinition.model_validate(
            definition.model_dump(mode="python"), strict=True
        )
        try:
            observation = self._client.ensure_product_catalog_snapshot(definition)
            return self._validated_observation(observation, expected=definition)
        except CatalogProviderError as error:
            raise _provider_error("publication", error) from None
        except (ValidationError, ValueError):
            raise ProviderError(
                "OpenMetadata product catalog publication returned invalid authority",
                "permanent",
            ) from None

    def observe(self, *, tenant_id: str, stable_external_key: str) -> CatalogProductObservation:
        if not tenant_id or not stable_external_key:
            raise ProviderError("OpenMetadata product catalog observation is invalid", "permanent")
        try:
            observation = self._client.get_product_catalog_snapshot(
                tenant_id=tenant_id, stable_external_key=stable_external_key
            )
            validated = CatalogProductObservation.model_validate(
                observation.model_dump(mode="python"), strict=True
            )
        except CatalogProviderError as error:
            raise _provider_error("observation", error) from None
        except (ValidationError, ValueError):
            raise ProviderError(
                "OpenMetadata product catalog observation returned invalid authority",
                "permanent",
            ) from None
        if validated.tenant_id != tenant_id or validated.stable_external_key != stable_external_key:
            raise ProviderError(
                "OpenMetadata product catalog observation returned another identity",
                "permanent",
            )
        return validated

    def publish_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        definition = CatalogNativeTableDefinition.model_validate(
            definition.model_dump(mode="python"), strict=True
        )
        try:
            observation = self._client.ensure_native_table_snapshot(definition)
            return self._validated_table_observation(observation, expected=definition)
        except CatalogProviderError as error:
            raise _provider_error("native table publication", error) from None
        except (ValidationError, ValueError):
            raise ProviderError(
                "OpenMetadata native table publication returned invalid authority",
                "permanent",
            ) from None

    def observe_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        definition = CatalogNativeTableDefinition.model_validate(
            definition.model_dump(mode="python"), strict=True
        )
        try:
            observation = self._client.get_native_table_snapshot(definition)
            return self._validated_table_observation(observation, expected=definition)
        except CatalogProviderError as error:
            raise _provider_error("native table observation", error) from None
        except (ValidationError, ValueError):
            raise ProviderError(
                "OpenMetadata native table observation returned invalid authority",
                "permanent",
            ) from None

    def _validated_observation(
        self,
        observation: CatalogProductObservation,
        *,
        expected: CatalogProductDefinition,
    ) -> CatalogProductObservation:
        validated = CatalogProductObservation.model_validate(
            observation.model_dump(mode="python"), strict=True
        )
        if validated.definition != expected:
            raise ValueError("published catalog snapshot differs from approved definition")
        if validated.provider_version != self._client.health().provider_version:
            raise ValueError("published catalog snapshot names another provider version")
        return validated

    def _validated_table_observation(
        self,
        observation: CatalogNativeTableObservation,
        *,
        expected: CatalogNativeTableDefinition,
    ) -> CatalogNativeTableObservation:
        validated = CatalogNativeTableObservation.model_validate(
            observation.model_dump(mode="python"), strict=True
        )
        if validated.definition != expected:
            raise ValueError("published native table differs from approved definition")
        if validated.provider_version != self._client.health().provider_version:
            raise ValueError("published native table names another provider version")
        return validated


def _provider_error(operation: str, error: CatalogProviderError) -> ProviderError:
    classification = _PROVIDER_CLASSIFICATION[error.classification]
    return ProviderError(
        f"OpenMetadata product catalog {operation} failed",
        classification,
    )
