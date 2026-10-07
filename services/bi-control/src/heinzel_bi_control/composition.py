from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol

from heinzel_contract_model import ArtifactReference, digest
from heinzel_provider_sdk.bi import BiVisualIntent
from heinzel_request_management import GovernedAnswerVerificationError
from heinzel_runtime import MaterializationAuthorityError, ProductMaterializationReceipt
from heinzel_semantic_registry import (
    ApprovedProductQueryBinding,
    ProductCatalogPublicationAuthorityError,
    ProductCatalogPublicationReceipt,
    ProductQueryBindingAuthorityError,
)
from pydantic import ValidationError

from .connection_repository import DashboardConnectionAuthorityError
from .contract_repository import DashboardContractAuthorityError, SQLiteDashboardContractRepository
from .models import (
    DashboardAnswerAuthority,
    DashboardDatasetConnectionBinding,
    DashboardDesiredState,
    DashboardProviderReceipt,
    PublishDashboardCommand,
)
from .service import DashboardControlService
from .signing import DashboardContractVerifier, InvalidDashboardContract


class DashboardCompositionError(RuntimeError):
    pass


class DashboardAuthorityUnavailable(DashboardCompositionError):
    """An authority this publication depends on answered nothing.

    Separate from the invalid case because the two refusals have different futures: evidence that
    failed verification will fail it again, while an authority that answered nothing may answer on a
    later attempt. A publication workflow classifies retries on that difference.
    """


class DashboardNoValidPlan(DashboardCompositionError):
    pass


class DashboardStaleRevision(DashboardCompositionError):
    pass


class DashboardAnswerAuthorityReader(Protocol):
    def read_exact(
        self, *, tenant_id: str, request_id: str, answer_id: str
    ) -> DashboardAnswerAuthority | None: ...


class DashboardQueryBindingReader(Protocol):
    def read_current(
        self, *, tenant_id: str, product_ref: ArtifactReference, generation: int
    ) -> ApprovedProductQueryBinding | None: ...


class DashboardMaterializationReader(Protocol):
    def read_receipt(
        self,
        *,
        tenant_id: str,
        product_id: str,
        product_revision: int,
        product_generation: int,
    ) -> ProductMaterializationReceipt | None: ...


class DashboardProductPublicationReader(Protocol):
    def read_for_product_generation(
        self, *, tenant_id: str, product_ref: ArtifactReference, generation: int
    ) -> ProductCatalogPublicationReceipt | None: ...


class DashboardConnectionAuthority(Protocol):
    def resolve(
        self,
        *,
        tenant_id: str,
        engine_kind: Literal["postgresql", "clickhouse"],
        consumption_object_ref: ArtifactReference,
    ) -> DashboardDatasetConnectionBinding | None: ...


class DashboardCompositionService:
    def __init__(
        self,
        *,
        contracts: SQLiteDashboardContractRepository,
        contract_verifier: DashboardContractVerifier,
        answers: DashboardAnswerAuthorityReader,
        query_bindings: DashboardQueryBindingReader,
        materializations: DashboardMaterializationReader,
        product_publications: DashboardProductPublicationReader,
        connections: DashboardConnectionAuthority,
        dashboard_control: DashboardControlService,
        clock: Callable[[], datetime],
    ) -> None:
        self._contracts = contracts
        self._contract_verifier = contract_verifier
        self._answers = answers
        self._query_bindings = query_bindings
        self._materializations = materializations
        self._product_publications = product_publications
        self._connections = connections
        self._dashboard_control = dashboard_control
        self._clock = clock

    def publish(self, command: PublishDashboardCommand) -> DashboardProviderReceipt:
        command = PublishDashboardCommand.model_validate(
            command.model_dump(mode="python"), strict=True
        )
        try:
            signed = self._contracts.read_exact(
                tenant_id=command.tenant_id,
                dashboard_id=command.dashboard_id,
                version=command.dashboard_version,
            )
        except DashboardContractAuthorityError as error:
            raise DashboardCompositionError("dashboard contract authority is invalid") from error
        if signed is None:
            raise DashboardAuthorityUnavailable("dashboard contract authority is unavailable")
        try:
            contract = self._contract_verifier.verify(signed)
        except InvalidDashboardContract as error:
            raise DashboardCompositionError("dashboard contract authority is invalid") from error
        if (
            signed.tenant_id != command.tenant_id
            or contract.dashboard_id != command.dashboard_id
            or contract.version != command.dashboard_version
        ):
            raise DashboardCompositionError("dashboard contract identity does not match command")
        if contract.lifecycle_state != "certified":
            raise DashboardNoValidPlan("dashboard publication requires a certified contract")
        if len(contract.data_product_versions) != 1:
            raise DashboardNoValidPlan("dashboard publication supports exactly one data product")
        if not contract.visual_intents:
            raise DashboardNoValidPlan("dashboard publication requires a visual intent")

        answer = self._read_answer(command)
        if len(answer.product_generation_refs) != 1:
            raise DashboardNoValidPlan("dashboard publication requires one product generation")
        generation = answer.product_generation_refs[0]
        product_ref = contract.data_product_versions[0]
        if generation.product_ref != product_ref:
            raise DashboardCompositionError("answer product identity does not match dashboard")
        if answer.metric_version_refs != contract.metric_versions:
            raise DashboardCompositionError("answer metric authority does not match dashboard")
        observed_at = self._clock()
        if (
            answer.freshness_disposition != "current"
            or answer.as_of > observed_at
            or (observed_at - answer.as_of).total_seconds()
            > contract.freshness_requirement.maximum_age_seconds
        ):
            raise DashboardCompositionError("dashboard answer freshness requirement failed")

        binding = self._read_binding(command.tenant_id, product_ref, generation.generation)
        self._validate_binding(
            tenant_id=command.tenant_id,
            product_ref=product_ref,
            generation=generation.generation,
            contract_metrics=contract.metric_versions,
            contract_dimensions=contract.dimensions,
            drill_dimensions=tuple(item for path in contract.drill_paths for item in path),
            binding=binding,
        )
        receipt = self._read_materialization(command.tenant_id, product_ref, generation.generation)
        self._validate_materialization(command.tenant_id, product_ref, binding, receipt)
        product_publication = self._read_product_publication(
            command.tenant_id, product_ref, generation.generation
        )
        connection = self._read_connection(command.tenant_id, binding)
        self._validate_connection(command.tenant_id, binding, connection)

        current = self._dashboard_control.get_current_desired(
            command.tenant_id, command.dashboard_id, command.dashboard_version
        )
        desired = self._desired(
            command=command,
            signed_key_id=signed.key_id,
            signed_digest=signed.contract_digest,
            signed_signature=signed.signature,
            answer=answer,
            product_ref=product_ref,
            binding=binding,
            product_publication=product_publication,
            connection=connection,
            metric_refs=contract.metric_versions,
            dimension_refs=contract.dimensions,
            filter_refs=contract.filters,
            visual_intents=contract.visual_intents,
            current=current,
        )
        return self._dashboard_control.apply(desired)

    def _read_answer(self, command: PublishDashboardCommand) -> DashboardAnswerAuthority:
        try:
            raw = self._answers.read_exact(
                tenant_id=command.tenant_id,
                request_id=command.request_id,
                answer_id=command.answer_id,
            )
        except GovernedAnswerVerificationError as error:
            raise DashboardCompositionError("dashboard answer authority is invalid") from error
        if raw is None:
            raise DashboardAuthorityUnavailable("dashboard answer authority is unavailable")
        try:
            answer = DashboardAnswerAuthority.model_validate(
                raw.model_dump(mode="python"), strict=True
            )
        except (AttributeError, ValidationError):
            raise DashboardCompositionError("dashboard answer authority is invalid") from None
        if (
            answer.tenant_id != command.tenant_id
            or answer.request_id != command.request_id
            or answer.answer_id != command.answer_id
        ):
            raise DashboardCompositionError("dashboard answer identity does not match command")
        return answer

    def _read_binding(
        self, tenant_id: str, product_ref: ArtifactReference, generation: int
    ) -> ApprovedProductQueryBinding:
        try:
            raw = self._query_bindings.read_current(
                tenant_id=tenant_id, product_ref=product_ref, generation=generation
            )
        except ProductQueryBindingAuthorityError as error:
            raise DashboardCompositionError(
                "dashboard query binding authority is invalid"
            ) from error
        if raw is None:
            raise DashboardAuthorityUnavailable("dashboard query binding authority is unavailable")
        try:
            return ApprovedProductQueryBinding.model_validate(
                raw.model_dump(mode="python"), strict=True
            )
        except (AttributeError, ValidationError, ValueError, TypeError):
            raise DashboardCompositionError(
                "dashboard query binding authority is invalid"
            ) from None

    def _read_materialization(
        self, tenant_id: str, product_ref: ArtifactReference, generation: int
    ) -> ProductMaterializationReceipt:
        try:
            raw = self._materializations.read_receipt(
                tenant_id=tenant_id,
                product_id=product_ref.artifact_id,
                product_revision=product_ref.version,
                product_generation=generation,
            )
        except MaterializationAuthorityError as error:
            raise DashboardCompositionError(
                "dashboard materialization authority is invalid"
            ) from error
        if raw is None:
            raise DashboardAuthorityUnavailable(
                "dashboard materialization authority is unavailable"
            )
        try:
            return ProductMaterializationReceipt.model_validate(
                raw.model_dump(mode="python"), strict=True
            )
        except (AttributeError, ValidationError, ValueError, TypeError):
            raise DashboardCompositionError(
                "dashboard materialization authority is invalid"
            ) from None

    def _read_connection(
        self, tenant_id: str, binding: ApprovedProductQueryBinding
    ) -> DashboardDatasetConnectionBinding:
        try:
            raw = self._connections.resolve(
                tenant_id=tenant_id,
                engine_kind=binding.engine_kind,
                consumption_object_ref=binding.consumption_object_ref,
            )
        except DashboardConnectionAuthorityError as error:
            raise DashboardCompositionError("dashboard connection authority is invalid") from error
        if raw is None:
            raise DashboardAuthorityUnavailable("dashboard connection authority is unavailable")
        try:
            return DashboardDatasetConnectionBinding.model_validate(
                raw.model_dump(mode="python"), strict=True
            )
        except (AttributeError, ValidationError, ValueError, TypeError):
            raise DashboardCompositionError("dashboard connection authority is invalid") from None

    def _read_product_publication(
        self, tenant_id: str, product_ref: ArtifactReference, generation: int
    ) -> ProductCatalogPublicationReceipt:
        try:
            raw = self._product_publications.read_for_product_generation(
                tenant_id=tenant_id, product_ref=product_ref, generation=generation
            )
        except ProductCatalogPublicationAuthorityError as error:
            raise DashboardCompositionError(
                "dashboard product publication authority is invalid"
            ) from error
        if raw is None:
            raise DashboardAuthorityUnavailable(
                "dashboard product publication authority is unavailable"
            )
        try:
            publication = ProductCatalogPublicationReceipt.model_validate(
                raw.model_dump(mode="python"), strict=True
            )
        except (AttributeError, ValidationError, ValueError, TypeError):
            raise DashboardCompositionError(
                "dashboard product publication authority is invalid"
            ) from None
        if (
            publication.tenant_id != tenant_id
            or publication.product_ref != product_ref
            or publication.generation != generation
            or not publication.round_trip_verified
        ):
            raise DashboardCompositionError(
                "dashboard product publication authority does not match"
            )
        return publication

    @staticmethod
    def _validate_binding(
        *,
        tenant_id: str,
        product_ref: ArtifactReference,
        generation: int,
        contract_metrics: tuple[ArtifactReference, ...],
        contract_dimensions: tuple[ArtifactReference, ...],
        drill_dimensions: tuple[ArtifactReference, ...],
        binding: ApprovedProductQueryBinding,
    ) -> None:
        if (
            binding.tenant_id != tenant_id
            or binding.product_ref != product_ref
            or binding.generation != generation
        ):
            raise DashboardCompositionError("dashboard query binding identity does not match")
        bound_metrics = {item.semantic_ref for item in binding.metric_bindings}
        if any(reference not in bound_metrics for reference in contract_metrics):
            raise DashboardCompositionError("dashboard metric is absent from query binding")
        bound_dimensions = {item.semantic_ref for item in binding.dimension_bindings}
        if any(
            reference not in bound_dimensions
            for reference in (*contract_dimensions, *drill_dimensions)
        ):
            raise DashboardCompositionError("dashboard dimension is absent from query binding")

    def _validate_materialization(
        self,
        tenant_id: str,
        product_ref: ArtifactReference,
        binding: ApprovedProductQueryBinding,
        receipt: ProductMaterializationReceipt,
    ) -> None:
        expected_receipt_ref = ArtifactReference(
            artifact_id=receipt.run_id,
            version=receipt.product_generation,
            digest=digest(receipt),
        )
        if (
            receipt.tenant_id != tenant_id
            or receipt.product_id != product_ref.artifact_id
            or receipt.product_revision != product_ref.version
            or receipt.product_generation != binding.generation
            or binding.materialization_receipt_ref != expected_receipt_ref
            or binding.contract_ref.digest != receipt.contract_digest
            or binding.lineage_digest != receipt.lineage_digest
        ):
            raise DashboardCompositionError(
                "dashboard materialization identity, contract, or lineage does not match"
            )
        if receipt.retained_until <= self._clock():
            raise DashboardCompositionError("dashboard materialization retention has expired")

    @staticmethod
    def _validate_connection(
        tenant_id: str,
        binding: ApprovedProductQueryBinding,
        connection: DashboardDatasetConnectionBinding,
    ) -> None:
        if (
            connection.tenant_id != tenant_id
            or connection.engine_kind != binding.engine_kind
            or connection.consumption_object_ref != binding.consumption_object_ref
            or connection.namespace != binding.namespace
            or connection.relation_name != binding.relation_name
        ):
            raise DashboardCompositionError("dashboard connection authority does not match")

    @staticmethod
    def _desired(
        *,
        command: PublishDashboardCommand,
        signed_key_id: str,
        signed_digest: str,
        signed_signature: str,
        answer: DashboardAnswerAuthority,
        product_ref: ArtifactReference,
        binding: ApprovedProductQueryBinding,
        product_publication: ProductCatalogPublicationReceipt,
        connection: DashboardDatasetConnectionBinding,
        metric_refs: tuple[ArtifactReference, ...],
        dimension_refs: tuple[ArtifactReference, ...],
        filter_refs: tuple[ArtifactReference, ...],
        visual_intents: tuple[BiVisualIntent, ...],
        current: DashboardDesiredState | None,
    ) -> DashboardDesiredState:
        if current is None:
            if command.expected_revision != 1:
                raise DashboardStaleRevision("dashboard desired revision is stale")
            prior_digest = None
        elif command.expected_revision == current.revision:
            prior_digest = current.prior_desired_digest
        elif command.expected_revision == current.revision + 1:
            prior_digest = current.desired_digest
        else:
            raise DashboardStaleRevision("dashboard desired revision is stale")
        desired = DashboardDesiredState(
            tenant_id=command.tenant_id,
            dashboard_id=command.dashboard_id,
            version=command.dashboard_version,
            revision=command.expected_revision,
            prior_desired_digest=prior_digest,
            title=answer.title,
            contract_digest=signed_digest,
            contract_key_id=signed_key_id,
            contract_signature=signed_signature,
            source_answer=answer,
            dataset_product_ref=product_ref,
            dataset_generation=binding.generation,
            consumption_object_ref=binding.consumption_object_ref,
            materialization_receipt_ref=binding.materialization_receipt_ref,
            product_publication_ref=ArtifactReference(
                artifact_id=product_publication.publication_id,
                version=product_publication.generation,
                digest=digest(product_publication),
            ),
            dataset_namespace=binding.namespace,
            dataset_relation_name=binding.relation_name,
            warehouse_binding_id=connection.warehouse_binding_id,
            warehouse_binding_revision=connection.warehouse_binding_revision,
            warehouse_binding_digest=connection.warehouse_binding_digest,
            connection_secret_ref=connection.connection_secret_ref,
            metric_refs=metric_refs,
            dimension_refs=dimension_refs,
            filter_refs=filter_refs,
            visual_intents=visual_intents,
            lifecycle_state="active",
        )
        if (
            current is not None
            and command.expected_revision == current.revision
            and desired != current
        ):
            raise DashboardStaleRevision("dashboard desired revision is stale")
        return desired
