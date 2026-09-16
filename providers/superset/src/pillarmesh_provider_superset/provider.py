from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pillarmesh_provider_sdk import ProviderError
from pillarmesh_provider_sdk.bi import (
    BiApplyResult,
    BiDashboardDefinition,
    BiDataset,
    BiLifecycleState,
    dashboard_external_key,
)
from pillarmesh_provider_sdk.errors import ProviderErrorClassification


class SupersetClientError(RuntimeError):
    def __init__(self, *, classification: ProviderErrorClassification) -> None:
        self.classification = classification
        super().__init__("Superset client operation failed")


@dataclass(frozen=True, slots=True)
class SupersetDashboard:
    external_id: str
    stable_key: str
    managed_digest: str
    lifecycle_state: BiLifecycleState
    external_url: str


class SupersetClient(Protocol):
    def prepare_dashboard(self, *, definition: BiDashboardDefinition) -> None: ...

    def get_dataset(self, *, stable_key: str) -> BiDataset | None: ...

    def get_dashboard(self, *, stable_key: str) -> SupersetDashboard | None: ...

    def create_dashboard(self, *, definition: BiDashboardDefinition) -> SupersetDashboard: ...

    def update_dashboard(
        self, *, external_id: str, definition: BiDashboardDefinition
    ) -> SupersetDashboard: ...

    def archive_dashboard(
        self, *, external_id: str, definition: BiDashboardDefinition
    ) -> SupersetDashboard: ...

    def reconcile_dashboard_charts(
        self, *, external_id: str, definition: BiDashboardDefinition
    ) -> None: ...


class SupersetProvider:
    def __init__(self, client: SupersetClient, *, provider_version: str = "4.1.1") -> None:
        self._client = client
        self._provider_version = provider_version

    @property
    def provider_kind(self) -> Literal["superset"]:
        return "superset"

    def apply(self, definition: BiDashboardDefinition) -> BiApplyResult:
        expected_key = dashboard_external_key(
            tenant_id=definition.tenant_id,
            dashboard_id=definition.dashboard_id,
            version=definition.version,
        )
        if definition.stable_external_key != expected_key:
            raise ProviderError("Superset dashboard stable key is invalid", "integrity_failure")
        try:
            current = self._client.get_dashboard(stable_key=definition.stable_external_key)
            if current is None and definition.prior_desired_digest is not None:
                raise ProviderError(
                    "Superset dashboard disappeared after prior publication",
                    "integrity_failure",
                )
            if (
                current is not None
                and current.managed_digest != definition.desired_digest
                and current.managed_digest != definition.prior_desired_digest
            ):
                raise ProviderError(
                    "Superset dashboard has an unrecognized external mutation",
                    "integrity_failure",
                )
            if definition.lifecycle_state == "active":
                self._client.prepare_dashboard(definition=definition)
                dataset = self._client.get_dataset(stable_key=definition.dataset_stable_key)
                if dataset is None or dataset.generation != definition.dataset_generation:
                    raise ProviderError(
                        "Superset dashboard dataset is unavailable", "statement_rejected"
                    )
                if (
                    dataset.namespace != definition.dataset_namespace
                    or dataset.relation_name != definition.dataset_relation_name
                ):
                    raise ProviderError(
                        "Superset dashboard dataset address conflicts with authority",
                        "integrity_failure",
                    )
            if current is None:
                applied = self._client.create_dashboard(definition=definition)
            elif (
                current.managed_digest == definition.desired_digest
                and current.lifecycle_state == definition.lifecycle_state
            ):
                applied = current
            else:
                if definition.lifecycle_state == "archived":
                    applied = self._client.archive_dashboard(
                        external_id=current.external_id,
                        definition=definition,
                    )
                else:
                    applied = self._client.update_dashboard(
                        external_id=current.external_id,
                        definition=definition,
                    )
            self._client.reconcile_dashboard_charts(
                external_id=applied.external_id,
                definition=definition,
            )
        except ProviderError:
            raise
        except SupersetClientError as error:
            raise ProviderError(
                "Superset BI operation failed",
                error.classification,
            ) from None
        except (OSError, TimeoutError):
            raise ProviderError("Superset BI transport failed", "transient_transport") from None
        return self._result(definition, applied)

    def _result(
        self, definition: BiDashboardDefinition, applied: SupersetDashboard
    ) -> BiApplyResult:
        if (
            applied.stable_key != definition.stable_external_key
            or applied.managed_digest != definition.desired_digest
            or applied.lifecycle_state != definition.lifecycle_state
        ):
            raise ProviderError(
                "Superset returned invalid dashboard state", "invalid_provider_response"
            )
        return BiApplyResult(
            stable_external_key=applied.stable_key,
            desired_digest=applied.managed_digest,
            lifecycle_state=applied.lifecycle_state,
            external_url=applied.external_url,
            provider_version=self._provider_version,
        )
