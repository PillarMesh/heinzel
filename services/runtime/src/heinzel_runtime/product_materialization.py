from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from heinzel_contract_model import ArtifactModel, canonical_bytes, digest
from heinzel_execution_graph import (
    InvalidProductExecutionAuthorization,
    ProductExecutionAuthorizationVerifier,
    ProductPhysicalPlan,
    SignedProductExecutionAuthorization,
)
from heinzel_state import ExternalEffectRecoveryUnavailableError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .product_input_cardinality import (
    ProductInputCardinalityEvidence,
    ProductInputCardinalityEvidenceCorruptError,
    ProductInputCardinalityEvidenceReader,
    ProductInputCardinalityEvidenceUnavailableError,
)

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_MATERIALIZATION_TABLE = "product_materializations_v4"
_LEGACY_MATERIALIZATION_TABLES = (
    "product_materializations",
    "product_materializations_v2",
    "product_materializations_v3",
)
type MaterializationFaultHook = Callable[[str], None]


def _noop_fault_hook(checkpoint: str) -> None:
    del checkpoint


class CatalogPublicationError(RuntimeError):
    pass


class MaterializationAuthorityError(RuntimeError):
    pass


class PublicationRecoveryNotAllowedError(ValueError):
    pass


class StalePublicationRevisionError(ValueError):
    pass


class MaterializationRequest(ArtifactModel):
    run_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    product_revision: int = Field(ge=1)
    product_generation: int = Field(ge=1)
    retention_seconds: int = Field(gt=0)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    physical_plan: ProductPhysicalPlan
    physical_plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    compiled_model_digest: str = Field(pattern=_DIGEST_PATTERN)
    input_generation_digests: tuple[str, ...] = Field(min_length=1)
    input_cardinality_evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    expected_output_schema_digest: str = Field(pattern=_DIGEST_PATTERN)

    @field_validator("input_generation_digests")
    @classmethod
    def generation_digests_are_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("input generation digests must be unique")
        if any(
            len(item) != 64 or any(char not in "0123456789abcdef" for char in item)
            for item in value
        ):
            raise ValueError("input generation digest must be lowercase hexadecimal")
        return value

    @model_validator(mode="after")
    def physical_plan_is_bound(self) -> MaterializationRequest:
        if self.physical_plan_digest != digest(self.physical_plan):
            raise ValueError("materialization physical plan digest does not match")
        return self

    @property
    def materialization_key(self) -> str:
        return _materialization_key(
            tenant_id=self.tenant_id,
            product_id=self.product_id,
            product_revision=self.product_revision,
            product_generation=self.product_generation,
        )


class ProductMaterializationAdmission(ArtifactModel):
    legality_decision_digest: str = Field(pattern=_DIGEST_PATTERN)
    cardinality_evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    signed_execution_authorization: SignedProductExecutionAuthorization


class MaterializationObservation(ArtifactModel):
    provider_commit_reference: str = Field(min_length=1)
    output_schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    output_row_count: int = Field(ge=0)
    dbt_manifest_digest: str = Field(pattern=_DIGEST_PATTERN)
    dbt_run_results_digest: str = Field(pattern=_DIGEST_PATTERN)
    lineage_digest: str = Field(pattern=_DIGEST_PATTERN)
    quality_assertion_count: int = Field(ge=0)
    quality_disposition: Literal["not_asserted", "passed", "limited"]
    # Which output columns the engine actually asserted the checked Decimal(57,9) magnitude on.
    # Naming them, rather than counting them, is what lets the runner refuse an observation that
    # asserted some of the plan's checked columns and not others. It defaults to empty so that a
    # warehouse which attests nothing is refused rather than unable to report, which is the
    # direction this check is meant to fail in.
    magnitude_asserted_columns: tuple[str, ...] = ()

    @field_validator("magnitude_asserted_columns")
    @classmethod
    def magnitude_asserted_columns_are_distinct(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not column.strip() for column in value):
            raise ValueError("magnitude asserted column must not be blank")
        if len(value) != len(set(value)):
            raise ValueError("magnitude asserted columns must be unique")
        return value


class ProductMaterializationReceipt(ArtifactModel):
    run_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    product_revision: int = Field(ge=1)
    product_generation: int = Field(ge=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    input_generation_digests: tuple[str, ...] = Field(min_length=1)
    input_cardinality_evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    execution_authorization_digest: str = Field(pattern=_DIGEST_PATTERN)
    legality_decision_digest: str = Field(pattern=_DIGEST_PATTERN)
    physical_plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    compiled_model_digest: str = Field(pattern=_DIGEST_PATTERN)
    output_schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    output_row_count: int = Field(ge=0)
    provider_commit_reference: str = Field(min_length=1)
    dbt_manifest_digest: str = Field(pattern=_DIGEST_PATTERN)
    dbt_run_results_digest: str = Field(pattern=_DIGEST_PATTERN)
    lineage_digest: str = Field(pattern=_DIGEST_PATTERN)
    quality_assertion_count: int = Field(ge=0)
    quality_disposition: Literal["not_asserted", "passed", "limited"]
    # Carried into the receipt so the evidence a reader reconstructs shows which columns the engine
    # was held to, not merely that a plan declared them.
    magnitude_asserted_columns: tuple[str, ...] = ()
    committed_at: datetime
    retained_until: datetime

    @field_validator("committed_at", "retained_until")
    @classmethod
    def committed_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("committed_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def retention_follows_commit(self) -> ProductMaterializationReceipt:
        if self.retained_until <= self.committed_at:
            raise ValueError("product generation retention must follow its commit")
        return self


class PublicationRecoveryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    materialization_key: str = Field(pattern=_DIGEST_PATTERN)
    expected_revision: int = Field(ge=1)
    actor_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def reason_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("publication recovery reason cannot be blank")
        return value


class PublicationRecoveryStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    materialization_key: str = Field(pattern=_DIGEST_PATTERN)
    tenant_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    product_revision: int = Field(ge=1)
    product_generation: int = Field(ge=1)
    revision: int = Field(ge=1)
    state: Literal["publication_pending", "published"]
    publication_ref: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def publication_matches_state(self) -> PublicationRecoveryStatus:
        if (self.publication_ref is not None) != (self.state == "published"):
            raise ValueError("published recovery status requires its publication reference")
        return self


class PublicationRecoveryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    materialization_key: str = Field(pattern=_DIGEST_PATTERN)
    materialization_revision: int = Field(ge=1)
    actor_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    publication_ref: str = Field(min_length=1)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("recorded_at must be timezone-aware UTC")
        return value.astimezone(UTC)


class MaterializationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt: ProductMaterializationReceipt
    publication_ref: str | None
    publication_pending: bool


class MaterializationWarehouse(Protocol):
    def execute(self, request: MaterializationRequest) -> MaterializationObservation: ...

    def switch_consumption_view(
        self, request: MaterializationRequest, observation: MaterializationObservation
    ) -> None: ...


class MaterializationCatalog(Protocol):
    def publish(
        self, request: MaterializationRequest, receipt: ProductMaterializationReceipt
    ) -> str: ...


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_digest: str
    request: MaterializationRequest | None = None
    admission: ProductMaterializationAdmission | None = None
    observation: MaterializationObservation
    receipt: ProductMaterializationReceipt
    view_switched: bool
    publication_ref: str | None = None
    publication_revision: int = Field(default=1, ge=1)
    publication_recovery_evidence: tuple[PublicationRecoveryEvidence, ...] = ()

    @model_validator(mode="after")
    def recovery_authority_is_consistent(self) -> _Record:
        request = self.request
        if request is None:
            if self.admission is not None or self.publication_recovery_evidence:
                raise ValueError("legacy materialization cannot carry publication recovery")
            return self
        if self.admission is None:
            raise ValueError("materialization record is missing its execution admission")
        if digest(request) != self.request_digest:
            raise ValueError("stored materialization request does not match its digest")
        authorization = self.admission.signed_execution_authorization.authorization
        if (
            authorization.physical_plan_digest != request.physical_plan_digest
            or authorization.legality_decision_digest != self.admission.legality_decision_digest
            or authorization.cardinality_evidence_digest
            != self.admission.cardinality_evidence_digest
            or self.admission.cardinality_evidence_digest
            != request.input_cardinality_evidence_digest
        ):
            raise ValueError("stored execution admission conflicts with materialization")
        receipt_authority = (
            self.receipt.run_id,
            self.receipt.tenant_id,
            self.receipt.product_id,
            self.receipt.product_revision,
            self.receipt.product_generation,
            self.receipt.contract_digest,
            self.receipt.input_generation_digests,
            self.receipt.input_cardinality_evidence_digest,
            self.receipt.execution_authorization_digest,
            self.receipt.legality_decision_digest,
            self.receipt.physical_plan_digest,
            self.receipt.compiled_model_digest,
        )
        request_authority = (
            request.run_id,
            request.tenant_id,
            request.product_id,
            request.product_revision,
            request.product_generation,
            request.contract_digest,
            request.input_generation_digests,
            request.input_cardinality_evidence_digest,
            digest(self.admission.signed_execution_authorization),
            self.admission.legality_decision_digest,
            request.physical_plan_digest,
            request.compiled_model_digest,
        )
        if receipt_authority != request_authority:
            raise ValueError("stored materialization request conflicts with its receipt")
        if len({item.command_id for item in self.publication_recovery_evidence}) != len(
            self.publication_recovery_evidence
        ):
            raise ValueError("publication recovery command identifiers must be unique")
        if any(
            item.tenant_id != request.tenant_id
            or item.materialization_key != request.materialization_key
            for item in self.publication_recovery_evidence
        ):
            raise ValueError("publication recovery evidence conflicts with materialization")
        return self


class ProductMaterializationRunner:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        warehouse: MaterializationWarehouse,
        catalog: MaterializationCatalog,
        cardinality_evidence_reader: ProductInputCardinalityEvidenceReader,
        execution_authorization_verifier: ProductExecutionAuthorizationVerifier,
        clock: Callable[[], datetime],
        fault_hook: MaterializationFaultHook = _noop_fault_hook,
    ) -> None:
        self._connection = connection
        self._warehouse = warehouse
        self._catalog = catalog
        self._cardinality_evidence_reader = cardinality_evidence_reader
        self._execution_authorization_verifier = execution_authorization_verifier
        self._clock = clock
        self._fault_hook = fault_hook
        for legacy_table_name in _LEGACY_MATERIALIZATION_TABLES:
            legacy_table = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (legacy_table_name,),
            ).fetchone()
            if (
                legacy_table is not None
                and self._connection.execute(
                    f"SELECT 1 FROM {legacy_table_name} LIMIT 1"
                ).fetchone()
                is not None
            ):
                raise ValueError("legacy materialization ledger requires an explicit migration")
        self._connection.execute(
            f"CREATE TABLE IF NOT EXISTS {_MATERIALIZATION_TABLE} ("
            "materialization_key TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
            "product_id TEXT NOT NULL, product_revision INTEGER NOT NULL, "
            "product_generation INTEGER NOT NULL, "
            "payload BLOB NOT NULL, "
            "UNIQUE (tenant_id, product_id, product_revision, product_generation))"
        )

    @classmethod
    def in_memory(
        cls,
        *,
        warehouse: MaterializationWarehouse,
        catalog: MaterializationCatalog,
        cardinality_evidence_reader: ProductInputCardinalityEvidenceReader,
        execution_authorization_verifier: ProductExecutionAuthorizationVerifier,
        clock: Callable[[], datetime],
        fault_hook: MaterializationFaultHook = _noop_fault_hook,
    ) -> ProductMaterializationRunner:
        return cls(
            sqlite3.connect(":memory:"),
            warehouse=warehouse,
            catalog=catalog,
            cardinality_evidence_reader=cardinality_evidence_reader,
            execution_authorization_verifier=execution_authorization_verifier,
            clock=clock,
            fault_hook=fault_hook,
        )

    def materialize(
        self,
        request: MaterializationRequest,
        *,
        admission: ProductMaterializationAdmission,
    ) -> MaterializationResult:
        request_digest = digest(request)
        record = self._load(request.materialization_key)
        if record is None:
            self._require_execution_authorization(request, admission, require_active=True)
            self._require_cardinality_evidence(request)
            observation = self._warehouse.execute(request)
            self._validate_observation(request, observation)
            receipt = ProductMaterializationReceipt(
                run_id=request.run_id,
                tenant_id=request.tenant_id,
                product_id=request.product_id,
                product_revision=request.product_revision,
                product_generation=request.product_generation,
                contract_digest=request.contract_digest,
                input_generation_digests=request.input_generation_digests,
                input_cardinality_evidence_digest=(request.input_cardinality_evidence_digest),
                execution_authorization_digest=digest(admission.signed_execution_authorization),
                legality_decision_digest=admission.legality_decision_digest,
                physical_plan_digest=request.physical_plan_digest,
                compiled_model_digest=request.compiled_model_digest,
                output_schema_digest=observation.output_schema_digest,
                output_row_count=observation.output_row_count,
                provider_commit_reference=observation.provider_commit_reference,
                dbt_manifest_digest=observation.dbt_manifest_digest,
                dbt_run_results_digest=observation.dbt_run_results_digest,
                lineage_digest=observation.lineage_digest,
                quality_assertion_count=observation.quality_assertion_count,
                quality_disposition=observation.quality_disposition,
                magnitude_asserted_columns=observation.magnitude_asserted_columns,
                committed_at=self._clock(),
                retained_until=self._clock() + timedelta(seconds=request.retention_seconds),
            )
            record = _Record(
                request_digest=request_digest,
                request=request,
                admission=admission,
                observation=observation,
                receipt=receipt,
                view_switched=False,
            )
            self._insert(request.materialization_key, request, record)
            self._fault_hook("after_commit_receipt")
        elif record.request_digest != request_digest:
            raise ValueError("materialization replay conflicts with the recorded authority")
        else:
            self._require_execution_authorization(request, admission, require_active=False)
            if record.admission is None or digest(record.admission) != digest(admission):
                raise ValueError(
                    "materialization replay conflicts with the recorded execution admission"
                )
            self._require_cardinality_evidence(request)

        if not record.view_switched:
            self._warehouse.switch_consumption_view(request, record.observation)
            self._fault_hook("after_consumption_view_switch")
            record = record.model_copy(update={"view_switched": True})
            self._update(request.materialization_key, record)

        if record.publication_ref is None:
            try:
                publication_ref = self._catalog.publish(request, record.receipt)
            except CatalogPublicationError:
                return MaterializationResult(
                    receipt=record.receipt,
                    publication_ref=None,
                    publication_pending=True,
                )
            record = record.model_copy(
                update={
                    "publication_ref": publication_ref,
                    "publication_revision": record.publication_revision + 1,
                }
            )
            self._update(request.materialization_key, record)

        return MaterializationResult(
            receipt=record.receipt,
            publication_ref=record.publication_ref,
            publication_pending=False,
        )

    def read_publication_recovery(
        self, *, tenant_id: str, materialization_key: str
    ) -> PublicationRecoveryStatus:
        try:
            record = self._load(materialization_key)
        except (sqlite3.Error, ValidationError) as error:
            raise MaterializationAuthorityError(
                "materialization authority is invalid or unavailable"
            ) from error
        if record is None or record.receipt.tenant_id != tenant_id:
            raise LookupError("materialization publication is unavailable")
        if not record.view_switched:
            raise PublicationRecoveryNotAllowedError(
                "materialization has not reached the publication boundary"
            )
        return PublicationRecoveryStatus(
            materialization_key=materialization_key,
            tenant_id=tenant_id,
            product_id=record.receipt.product_id,
            product_revision=record.receipt.product_revision,
            product_generation=record.receipt.product_generation,
            revision=record.publication_revision,
            state=("published" if record.publication_ref is not None else "publication_pending"),
            publication_ref=record.publication_ref,
        )

    def retry_publication(self, command: PublicationRecoveryCommand) -> PublicationRecoveryEvidence:
        try:
            record = self._load(command.materialization_key)
        except (sqlite3.Error, ValidationError) as error:
            raise MaterializationAuthorityError(
                "materialization authority is invalid or unavailable"
            ) from error
        if record is None or record.receipt.tenant_id != command.tenant_id:
            raise LookupError("materialization publication is unavailable")
        replay = next(
            (
                evidence
                for evidence in record.publication_recovery_evidence
                if evidence.command_id == command.command_id
            ),
            None,
        )
        if replay is not None:
            if _publication_evidence_matches_command(replay, command):
                return replay
            raise PublicationRecoveryNotAllowedError(
                "publication recovery command conflicts with recorded evidence"
            )
        if record.publication_revision != command.expected_revision:
            raise StalePublicationRevisionError("materialization publication revision is stale")
        if record.publication_ref is not None:
            raise PublicationRecoveryNotAllowedError("materialization publication is not pending")
        if not record.view_switched or record.request is None:
            raise PublicationRecoveryNotAllowedError(
                "materialization has not reached a recoverable publication boundary"
            )
        if record.admission is None:
            raise MaterializationAuthorityError(
                "materialization execution admission is invalid or unavailable"
            )
        self._require_execution_authorization(
            record.request,
            record.admission,
            require_active=False,
        )
        self._require_cardinality_evidence(record.request)
        publication_ref = self._catalog.publish(record.request, record.receipt)
        evidence = PublicationRecoveryEvidence(
            command_id=command.command_id,
            tenant_id=command.tenant_id,
            materialization_key=command.materialization_key,
            materialization_revision=record.publication_revision,
            actor_id=command.actor_id,
            reason=command.reason,
            publication_ref=publication_ref,
            recorded_at=self._clock(),
        )
        updated = record.model_copy(
            update={
                "publication_ref": publication_ref,
                "publication_revision": record.publication_revision + 1,
                "publication_recovery_evidence": (*record.publication_recovery_evidence, evidence),
            }
        )
        self._update(command.materialization_key, updated)
        return evidence

    def reconcile_external_effect(
        self,
        *,
        tenant_id: str,
        source_record_ref: str,
        expected_revision: int,
        command_id: str,
        actor_id: str,
        reason: str,
    ) -> str:
        try:
            evidence = self.retry_publication(
                PublicationRecoveryCommand(
                    command_id=command_id,
                    tenant_id=tenant_id,
                    materialization_key=source_record_ref,
                    expected_revision=expected_revision,
                    actor_id=actor_id,
                    reason=reason,
                )
            )
        except CatalogPublicationError as error:
            raise ExternalEffectRecoveryUnavailableError(
                "catalog publication recovery is temporarily unavailable"
            ) from error
        return evidence.publication_ref

    def read_receipt(
        self,
        *,
        tenant_id: str,
        product_id: str,
        product_revision: int,
        product_generation: int,
    ) -> ProductMaterializationReceipt | None:
        try:
            record = self._load(
                _materialization_key(
                    tenant_id=tenant_id,
                    product_id=product_id,
                    product_revision=product_revision,
                    product_generation=product_generation,
                )
            )
        except (sqlite3.Error, ValidationError) as error:
            raise MaterializationAuthorityError(
                "materialization authority is invalid or unavailable"
            ) from error
        if record is None or not record.view_switched:
            return None
        receipt = record.receipt
        if (
            receipt.tenant_id != tenant_id
            or receipt.product_id != product_id
            or receipt.product_revision != product_revision
            or receipt.product_generation != product_generation
        ):
            raise MaterializationAuthorityError(
                "materialization authority index does not match its payload"
            )
        return receipt

    @staticmethod
    def _validate_observation(
        request: MaterializationRequest, observation: MaterializationObservation
    ) -> None:
        if observation.output_schema_digest != request.expected_output_schema_digest:
            raise ValueError("observed output schema does not match the approved schema")
        ProductMaterializationRunner._require_magnitude_enforcement(request, observation)

    @staticmethod
    def _require_magnitude_enforcement(
        request: MaterializationRequest, observation: MaterializationObservation
    ) -> None:
        """Refuse a result the engine did not hold to the plan's declared decimal magnitude.

        The plan declares one `Decimal57OutputCheck` per aggregate measure, and the emitted
        statement is what enforces the bound on the engine. Nothing tied the two together: a
        warehouse could execute a different statement, or materialise without the checked cast, and
        the only observation the runner compared was the output schema digest -- which a wrong but
        correctly shaped result satisfies. Requiring the observation to name the columns it asserted
        closes that, and closes it in the refusing direction: an observation that attests nothing is
        rejected rather than assumed compliant.

        Declared and asserted must match exactly. An observation naming a column the plan does not
        check is as wrong as one omitting a column it does: it means the observation describes some
        other plan, and treating the extra as harmless would accept that confusion.
        """
        declared = frozenset(
            check.column_name for check in request.physical_plan.decimal_output_checks
        )
        asserted = frozenset(observation.magnitude_asserted_columns)
        if declared == asserted:
            return
        unasserted = sorted(declared - asserted)
        if unasserted:
            raise ValueError(
                "observed materialization did not assert the checked decimal magnitude on "
                f"{', '.join(unasserted)}"
            )
        raise ValueError(
            "observed materialization asserted a decimal magnitude the physical plan does not "
            f"check: {', '.join(sorted(asserted - declared))}"
        )

    def _require_cardinality_evidence(self, request: MaterializationRequest) -> None:
        try:
            evidence = self._cardinality_evidence_reader.read(
                tenant_id=request.tenant_id,
                evidence_digest=request.input_cardinality_evidence_digest,
            )
        except (
            ProductInputCardinalityEvidenceCorruptError,
            ProductInputCardinalityEvidenceUnavailableError,
        ) as error:
            raise MaterializationAuthorityError(
                "input cardinality evidence is invalid or unavailable"
            ) from error
        if evidence is None:
            raise MaterializationAuthorityError(
                "input cardinality evidence is invalid or unavailable"
            )
        try:
            evidence = ProductInputCardinalityEvidence.model_validate(evidence.model_dump())
        except ValidationError as error:
            raise MaterializationAuthorityError(
                "input cardinality evidence is invalid or unavailable"
            ) from error

        input_receipt_digests = tuple(item.receipt_digest for item in evidence.receipts)
        if (
            digest(evidence) != request.input_cardinality_evidence_digest
            or evidence.tenant_id != request.tenant_id
            or evidence.contract_digest != request.contract_digest
            or evidence.product_plan_digest != request.physical_plan_digest
            or input_receipt_digests != request.input_generation_digests
            or evidence.total_contributing_row_ceiling > evidence.policy_maximum_contributing_rows
            or evidence.policy_maximum_contributing_rows > 2**63 - 1
            or evidence.decimal_input_precision != 38
            or evidence.decimal_input_scale != 9
        ):
            raise MaterializationAuthorityError(
                "input cardinality evidence does not match materialization authority"
            )

    def _require_execution_authorization(
        self,
        request: MaterializationRequest,
        admission: ProductMaterializationAdmission,
        *,
        require_active: bool,
    ) -> None:
        try:
            if type(admission) is not ProductMaterializationAdmission:
                raise TypeError("execution admission has an unexpected model type")
            undeclared_fields = set(vars(admission)) - set(
                ProductMaterializationAdmission.model_fields
            )
            if undeclared_fields or getattr(admission, "__pydantic_extra__", None):
                raise ValueError("execution admission contains undeclared fields")
            validated_admission = ProductMaterializationAdmission.model_validate(
                admission.model_dump(mode="python"), strict=True
            )
            verification_time = (
                self._clock()
                if require_active
                else validated_admission.signed_execution_authorization.authorization.issued_at
            )
            authorization = self._execution_authorization_verifier.verify(
                validated_admission.signed_execution_authorization,
                physical_plan=request.physical_plan,
                now=verification_time,
            )
        except (
            AttributeError,
            InvalidProductExecutionAuthorization,
            TypeError,
            ValidationError,
            ValueError,
        ) as error:
            raise MaterializationAuthorityError(
                "product execution authorization is invalid or unavailable"
            ) from error

        plan = request.physical_plan
        if (
            authorization.physical_plan_digest != request.physical_plan_digest
            or authorization.legality_decision_digest
            != validated_admission.legality_decision_digest
            or authorization.cardinality_evidence_digest
            != validated_admission.cardinality_evidence_digest
            or validated_admission.cardinality_evidence_digest
            != request.input_cardinality_evidence_digest
            or plan.tenant_id != request.tenant_id
            or plan.product_id != request.product_id
            or plan.product_revision != request.product_revision
            or plan.contract_digest != request.contract_digest
            or plan.expected_output_schema_digest != request.expected_output_schema_digest
        ):
            raise MaterializationAuthorityError(
                "product execution authorization does not match materialization authority"
            )

    def _load(self, materialization_key: str) -> _Record | None:
        row = self._connection.execute(
            f"SELECT payload FROM {_MATERIALIZATION_TABLE} WHERE materialization_key = ?",
            (materialization_key,),
        ).fetchone()
        if row is None:
            return None
        return _Record.model_validate_json(bytes(row[0]))

    def _insert(
        self, materialization_key: str, request: MaterializationRequest, record: _Record
    ) -> None:
        with self._connection:
            self._connection.execute(
                f"INSERT INTO {_MATERIALIZATION_TABLE} "
                "(materialization_key, tenant_id, product_id, product_revision, "
                "product_generation, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    materialization_key,
                    request.tenant_id,
                    request.product_id,
                    request.product_revision,
                    request.product_generation,
                    canonical_bytes(record),
                ),
            )

    def _update(self, materialization_key: str, record: _Record) -> None:
        with self._connection:
            cursor = self._connection.execute(
                f"UPDATE {_MATERIALIZATION_TABLE} SET payload = ? WHERE materialization_key = ?",
                (canonical_bytes(record), materialization_key),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("materialization record disappeared during update")


def _materialization_key(
    *,
    tenant_id: str,
    product_id: str,
    product_revision: int,
    product_generation: int,
) -> str:
    return digest(
        {
            "domain": "heinzel-product-materialization-v2",
            "tenant_id": tenant_id,
            "product_id": product_id,
            "product_revision": product_revision,
            "product_generation": product_generation,
        }
    )


def _publication_evidence_matches_command(
    evidence: PublicationRecoveryEvidence, command: PublicationRecoveryCommand
) -> bool:
    return (
        evidence.command_id,
        evidence.tenant_id,
        evidence.materialization_key,
        evidence.materialization_revision,
        evidence.actor_id,
        evidence.reason,
    ) == (
        command.command_id,
        command.tenant_id,
        command.materialization_key,
        command.expected_revision,
        command.actor_id,
        command.reason,
    )
