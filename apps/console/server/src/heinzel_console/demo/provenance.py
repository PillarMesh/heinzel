"""How one answer was produced, read back from what each service wrote down.

Nothing here computes anything. Every value is a field of a receipt some service recorded while
doing its own work -- the acquisition contract the tenant activated, the landing run's receipt,
the compiled transform the compiler signed, the materialization receipt the transform provider
returned, the query plan the compiler signed, and the execution receipt the runtime wrote. The
console joins them and shows them; it never narrates a step that left no receipt.

A section is `None` when its step has not happened, so a request read before it is answered
shows the chain as far as it has got rather than a plausible account of the rest.
"""

from __future__ import annotations

from dataclasses import dataclass

from heinzel_compiler.query_repository import SQLiteQueryPlanRepository
from heinzel_contract_service.acquisition_lifecycle import (
    AcquisitionContractLifecycleNotFoundError,
    SQLiteAcquisitionContractLifecycleRepository,
)
from heinzel_runtime.generation_ledger import GenerationLedger
from heinzel_runtime.product_materialization import SQLiteProductMaterializationReceiptReader
from heinzel_runtime.result_store import SQLiteAnswerResultStore
from heinzel_warehouse_control.models import WarehouseBinding

from ..contracts import (
    ProvenanceExecutionView,
    ProvenanceFieldView,
    ProvenanceLandingView,
    ProvenanceMagnitudeCheckView,
    ProvenanceMaterializationView,
    ProvenanceParameterView,
    ProvenanceProductView,
    ProvenanceQualityTestView,
    ProvenanceQueryView,
    ProvenanceSourceView,
    ProvenanceView,
    ProvenanceWarehouseView,
)
from .model_authority import DemoSignedModelStore
from .stores import DemoLandedGenerationStore


@dataclass(frozen=True, slots=True)
class DemoProvenanceSubject:
    """The one product and acquisition contract this demonstration answers from."""

    acquisition_contract_ref: str
    acquisition_contract_revision: int
    product_id: str
    product_revision: int
    product_generation: int
    # The binding warehouse-control recorded, when this deployment provisioned its warehouse
    # through that service. `None` on the path that answers over a database no governing
    # service owns, where there is no binding to show and inventing one would be a fiction.
    warehouse: WarehouseBinding | None


class DemoRequestProvenanceReader:
    """Assemble the chain for a request from the stores the demonstration already keeps."""

    def __init__(
        self,
        *,
        acquisition_lifecycle: SQLiteAcquisitionContractLifecycleRepository,
        generations: GenerationLedger,
        landed_generations: DemoLandedGenerationStore,
        materializations: SQLiteProductMaterializationReceiptReader,
        plans: SQLiteQueryPlanRepository,
        results: SQLiteAnswerResultStore,
        signed_models: DemoSignedModelStore,
        subject: DemoProvenanceSubject,
    ) -> None:
        self._acquisition_lifecycle = acquisition_lifecycle
        self._generations = generations
        self._landed_generations = landed_generations
        self._materializations = materializations
        self._plans = plans
        self._results = results
        self._signed_models = signed_models
        self._subject = subject

    def provenance(self, *, tenant_id: str, request_id: str) -> ProvenanceView | None:
        return ProvenanceView(
            request_id=request_id,
            warehouse=self._warehouse(),
            source=self._source(tenant_id),
            landing=self._landing(tenant_id),
            product=self._product(tenant_id),
            materialization=self._materialization(tenant_id),
            query=self._query(tenant_id, request_id),
            execution=self._execution(tenant_id, request_id),
        )

    def _warehouse(self) -> ProvenanceWarehouseView | None:
        """The warehouse the binding describes, as warehouse-control holds it."""
        binding = self._subject.warehouse
        if binding is None:
            return None
        return ProvenanceWarehouseView(
            binding_ref=binding.binding_id,
            engine_kind=str(binding.engine_kind),
            deployment_mode=binding.deployment_mode,
            region=binding.region,
            lifecycle_state=str(binding.lifecycle_state),
            capability_profile_digest=binding.capability_profile_digest,
            provisioned_at=binding.provisioned_at,
        )

    def _source(self, tenant_id: str) -> ProvenanceSourceView | None:
        """The object shape the tenant activated a contract to read, and the source it names."""
        try:
            record = self._acquisition_lifecycle.get_contract(
                tenant_id,
                self._subject.acquisition_contract_ref,
                self._subject.acquisition_contract_revision,
            )
        except AcquisitionContractLifecycleNotFoundError:
            return None
        contract = record.contract
        schemas = tuple(contract.object_schemas)
        if not schemas:
            return None
        # One logical object in this demonstration; a deployment reading several would show each.
        schema = schemas[0]
        return ProvenanceSourceView(
            source_binding_ref=contract.source_binding_ref,
            logical_object_ref=schema.logical_object_ref,
            source_observation_ref=contract.source_observation_ref,
            capability_profile_digest=contract.capability_profile_digest,
            acquisition_modes=tuple(str(mode) for mode in contract.acquisition_modes),
            operation_semantics=str(schema.operation_semantics),
            record_key_fields=tuple(schema.record_key_fields),
            source_updated_at_field=schema.source_updated_at_field,
            schema_digest=schema.schema_digest,
            fields=tuple(
                ProvenanceFieldView(
                    name=field.name,
                    value_type=str(field.value_type),
                    nullable=field.nullable,
                )
                for field in schema.fields
            ),
            validated_at=contract.activated_at,
        )

    def _landing(self, tenant_id: str) -> ProvenanceLandingView | None:
        """The receipt of the run that put records in the warehouse."""
        landed = self._landed_generations.read(
            tenant_id=tenant_id, contract_ref=self._subject.acquisition_contract_ref
        )
        if landed is None:
            return None
        receipt = self._generations.load(landed.generation_key)
        if receipt is None:
            return None
        return ProvenanceLandingView(
            target_table_ref=receipt.target_table_ref,
            trigger_window=receipt.trigger_window,
            record_count=receipt.record_count,
            schema_digest=receipt.schema_digest,
            segment_digest=receipt.segment_digest,
            committed_at=receipt.committed_at,
        )

    def _product(self, tenant_id: str) -> ProvenanceProductView | None:
        """The statement the compiler emitted to build the product, as it signed it."""
        authority = self._signed_models.read(
            tenant_id=tenant_id,
            product_id=self._subject.product_id,
            product_revision=self._subject.product_revision,
            generation=self._subject.product_generation,
        )
        if authority is None:
            return None
        model = authority.signed_model.model
        return ProvenanceProductView(
            product_id=self._subject.product_id,
            product_revision=self._subject.product_revision,
            generation=self._subject.product_generation,
            model_name=model.model_name,
            target_schema=model.target_schema,
            output_columns=tuple(model.output_columns),
            quality_tests=tuple(
                ProvenanceQualityTestView(column_name=test.column_name, kind=str(test.kind))
                for test in model.quality_tests
            ),
            magnitude_checks=tuple(
                ProvenanceMagnitudeCheckView(
                    column_name=check.column_name,
                    precision=check.precision,
                    scale=check.scale,
                )
                for check in model.output_magnitude_checks
            ),
            compiled_sql=model.compiled_sql,
            model_digest=authority.signed_model.model_digest,
        )

    def _materialization(self, tenant_id: str) -> ProvenanceMaterializationView | None:
        """What the transform provider reported about the run that built the product."""
        receipt = self._materializations.read_receipt(
            tenant_id=tenant_id,
            product_id=self._subject.product_id,
            product_revision=self._subject.product_revision,
            product_generation=self._subject.product_generation,
        )
        if receipt is None:
            return None
        return ProvenanceMaterializationView(
            output_row_count=receipt.output_row_count,
            quality_assertion_count=receipt.quality_assertion_count,
            quality_disposition=str(receipt.quality_disposition),
            lineage_digest=receipt.lineage_digest,
            dbt_manifest_digest=receipt.dbt_manifest_digest,
            dbt_run_results_digest=receipt.dbt_run_results_digest,
            committed_at=receipt.committed_at,
        )

    def _query(self, tenant_id: str, request_id: str) -> ProvenanceQueryView | None:
        """The statement the compiler emitted to answer the question, and its ceilings."""
        execution = self._results.load_execution(tenant_id, request_id)
        if execution is None:
            return None
        plan = self._plans.read(tenant_id, execution[1].plan_digest)
        if plan is None:
            return None
        estimate = plan.estimated_scan
        return ProvenanceQueryView(
            engine_kind=str(plan.engine_kind),
            compiler_version=plan.compiler_version,
            allowlist_version=str(plan.allowlist_version),
            statement=plan.statement,
            parameters=tuple(
                ProvenanceParameterView(name=parameter.name, value_type=str(parameter.value_type))
                for parameter in plan.parameters
            ),
            minimum_group_size=plan.minimum_group_size,
            row_limit=plan.ceilings.row_limit,
            scan_row_ceiling=plan.ceilings.scan.rows,
            scan_byte_ceiling=plan.ceilings.scan.bytes,
            estimated_rows=None if estimate is None else estimate.rows,
            estimated_bytes=None if estimate is None else estimate.bytes,
            routing=str(plan.routing),
            plan_digest=plan.plan_digest,
            statement_digest=plan.statement_digest,
            # The key that signed it, never the signature: who vouched for the plan is the fact
            # a reader needs, and the signature itself is for the verifier.
            signing_key_id=plan.signature.split(":", 1)[0],
        )

    def _execution(self, tenant_id: str, request_id: str) -> ProvenanceExecutionView | None:
        """The runtime's receipt for the run that produced the rows."""
        execution = self._results.load_execution(tenant_id, request_id)
        if execution is None:
            return None
        receipt = execution[1]
        # A receipt the runtime wrote for a run that did not produce rows carries no digest of
        # them. There is then no execution to show, rather than one to show with gaps.
        if (
            receipt.outcome != "succeeded"
            or receipt.result_digest is None
            or receipt.result_schema_digest is None
        ):
            return None
        return ProvenanceExecutionView(
            execution_receipt_id=receipt.receipt_id,
            result_digest=receipt.result_digest,
            result_schema_digest=receipt.result_schema_digest,
            row_count=receipt.row_count,
        )
