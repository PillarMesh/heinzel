from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Literal, Protocol

from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_sdk import ProviderError
from pydantic import BaseModel, ValidationError

from .answer_errors import (
    AnswerExecutionAuthorizationError,
    AnswerExecutionConflict,
    AnswerExecutionIntegrityError,
    AnswerQueryAborted,
    AnswerQueryTimedOut,
)
from .answer_models import (
    AnswerExecutionAuthorization,
    AnswerExecutionOutcome,
    AnswerExecutionReceipt,
    AnswerProductGenerationReference,
    AnswerQueryColumn,
    AnswerQueryPlan,
    AnswerQueryValue,
    AnswerResultSnapshot,
    QueryGenerationState,
    ReadOnlyAnswerQuery,
)
from .result_store import SQLiteAnswerResultStore

_TRANSIENT_CLASSIFICATIONS = frozenset(
    ("retryable", "throttled", "transient_transport", "transient_unavailable")
)


class AnswerQueryPlanSignatureVerifier(Protocol):
    def verify(self, plan_digest: str, signature: str) -> bool: ...


class AnswerExecutionAuthorizer(Protocol):
    def recheck(
        self,
        *,
        tenant_id: str,
        request_id: str,
        validation_digest: str,
        plan_digest: str,
    ) -> AnswerExecutionAuthorization: ...


class AnswerGenerationReader(Protocol):
    def observe(self, reference: AnswerProductGenerationReference) -> QueryGenerationState: ...


class AnswerQueryCursor(Protocol):
    columns: tuple[AnswerQueryColumn, ...]
    suppressed_group_count: int

    def fetchone(self) -> tuple[AnswerQueryValue, ...] | None: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


class AnswerQueryProvider(Protocol):
    @property
    def engine_kind(self) -> Literal["postgresql", "clickhouse"]: ...

    def execute_read_only(self, request: ReadOnlyAnswerQuery) -> AnswerQueryCursor: ...


class GovernedQueryExecutor:
    def __init__(
        self,
        *,
        store: SQLiteAnswerResultStore,
        signature_verifier: AnswerQueryPlanSignatureVerifier,
        authorizer: AnswerExecutionAuthorizer,
        generation_reader: AnswerGenerationReader,
        provider_resolver: Callable[[str], AnswerQueryProvider],
        clock: Callable[[], datetime],
        sleeper: Callable[[float], None],
        is_cancelled: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._store = store
        self._signature_verifier = signature_verifier
        self._authorizer = authorizer
        self._generation_reader = generation_reader
        self._provider_resolver = provider_resolver
        self._clock = clock
        self._sleeper = sleeper
        self._is_cancelled = is_cancelled or (lambda _tenant_id, _request_id: False)

    def execute(self, *, request_id: str, plan: BaseModel) -> AnswerExecutionReceipt:
        query_plan = self._validate_plan(plan)
        if not self._signature_verifier.verify(query_plan.plan_digest, query_plan.signature):
            raise AnswerExecutionIntegrityError("query plan signature is invalid")
        input_digest = digest(
            {
                "domain": "heinzel-answer-execution-command-v1",
                "tenant_id": query_plan.tenant_id,
                "request_id": request_id,
                "plan_digest": query_plan.plan_digest,
            }
        )
        existing = self._store.load_execution(query_plan.tenant_id, request_id)
        if existing is not None:
            recorded_digest, receipt = existing
            if recorded_digest != input_digest:
                raise AnswerExecutionConflict(
                    "request replay conflicts with the recorded execution authority"
                )
            return receipt

        authorization = self._authorizer.recheck(
            tenant_id=query_plan.tenant_id,
            request_id=request_id,
            validation_digest=query_plan.validation_digest,
            plan_digest=query_plan.plan_digest,
        )
        self._validate_authorization(query_plan, request_id, authorization)
        request = ReadOnlyAnswerQuery(
            tenant_id=query_plan.tenant_id,
            engine_kind=query_plan.engine_kind,
            statement=query_plan.statement,
            parameters=query_plan.parameters,
            statement_timeout_seconds=authorization.statement_timeout_seconds,
            row_ceiling=min(authorization.row_ceiling, query_plan.ceilings.row_limit),
            byte_ceiling=authorization.byte_ceiling,
            consumption_object_refs=query_plan.consumption_object_refs,
            product_generation_refs=query_plan.product_generation_refs,
        )
        delays = (1.0, 5.0)
        provider: AnswerQueryProvider | None = None
        for attempt in range(1, 4):
            started_at = self._clock()
            pointer_references = self._check_generations(query_plan.product_generation_refs)
            if pointer_references is None:
                return self._record_failure(
                    query_plan,
                    request_id,
                    authorization,
                    input_digest,
                    attempt,
                    started_at,
                    "generation_unavailable",
                )
            if self._is_cancelled(query_plan.tenant_id, request_id):
                return self._record_failure(
                    query_plan,
                    request_id,
                    authorization,
                    input_digest,
                    attempt,
                    started_at,
                    "aborted",
                )
            try:
                if provider is None:
                    provider = self._provider_resolver(query_plan.engine_kind)
                cursor = provider.execute_read_only(request)
                try:
                    rows, observed_row_count, observed_byte_count = self._read_rows(
                        cursor,
                        query_plan.tenant_id,
                        request_id,
                        request.row_ceiling,
                        request.byte_ceiling,
                    )
                finally:
                    with suppress(Exception):
                        cursor.close()
            except _CeilingExceeded as error:
                return self._record_failure(
                    query_plan,
                    request_id,
                    authorization,
                    input_digest,
                    attempt,
                    started_at,
                    "ceiling_exceeded",
                    row_count=error.row_count,
                    byte_count=error.byte_count,
                )
            except AnswerQueryTimedOut:
                return self._record_failure(
                    query_plan,
                    request_id,
                    authorization,
                    input_digest,
                    attempt,
                    started_at,
                    "timed_out",
                )
            except AnswerQueryAborted:
                return self._record_failure(
                    query_plan,
                    request_id,
                    authorization,
                    input_digest,
                    attempt,
                    started_at,
                    "aborted",
                )
            except ProviderError as error:
                receipt = self._record_failure(
                    query_plan,
                    request_id,
                    authorization,
                    input_digest,
                    attempt,
                    started_at,
                    "provider_failed",
                    provider_error_classification=error.classification,
                )
                if error.classification not in _TRANSIENT_CLASSIFICATIONS or attempt == 3:
                    return receipt
                self._sleeper(delays[attempt - 1])
                continue

            if not self._recheck_pointer_generations(pointer_references):
                return self._record_failure(
                    query_plan,
                    request_id,
                    authorization,
                    input_digest,
                    attempt,
                    started_at,
                    "generation_unavailable",
                    row_count=observed_row_count,
                    byte_count=observed_byte_count,
                )
            return self._record_success(
                query_plan,
                request_id,
                authorization,
                input_digest,
                attempt,
                started_at,
                cursor.columns,
                rows,
                cursor.suppressed_group_count,
            )
        raise AssertionError("unreachable")

    @staticmethod
    def _validate_plan(plan: BaseModel) -> AnswerQueryPlan:
        try:
            return AnswerQueryPlan.model_validate(plan.model_dump(mode="python"), strict=True)
        except ValidationError as error:
            raise AnswerExecutionIntegrityError(
                "query plan digest or structure is invalid"
            ) from error

    @staticmethod
    def _validate_authorization(
        plan: AnswerQueryPlan,
        request_id: str,
        authorization: AnswerExecutionAuthorization,
    ) -> None:
        if (
            authorization.tenant_id != plan.tenant_id
            or authorization.request_id != request_id
            or authorization.validation_digest != plan.validation_digest
            or authorization.plan_digest != plan.plan_digest
        ):
            raise AnswerExecutionAuthorizationError(
                "policy or entitlement recheck returned mismatched authority"
            )

    def _check_generations(
        self, references: tuple[AnswerProductGenerationReference, ...]
    ) -> tuple[AnswerProductGenerationReference, ...] | None:
        pointer_references: list[AnswerProductGenerationReference] = []
        for reference in references:
            state = self._generation_reader.observe(reference)
            if state.addressable:
                continue
            if state.current_generation != reference.generation:
                return None
            pointer_references.append(reference)
        return tuple(pointer_references)

    def _recheck_pointer_generations(
        self, references: tuple[AnswerProductGenerationReference, ...]
    ) -> bool:
        return all(
            not (state := self._generation_reader.observe(reference)).addressable
            and state.current_generation == reference.generation
            for reference in references
        )

    def _read_rows(
        self,
        cursor: AnswerQueryCursor,
        tenant_id: str,
        request_id: str,
        row_ceiling: int,
        byte_ceiling: int,
    ) -> tuple[tuple[tuple[AnswerQueryValue, ...], ...], int, int]:
        rows: list[tuple[AnswerQueryValue, ...]] = []
        while True:
            if self._is_cancelled(tenant_id, request_id):
                with suppress(Exception):
                    cursor.cancel()
                raise AnswerQueryAborted
            row = cursor.fetchone()
            if row is None:
                break
            rows.append(row)
            row_count = len(rows)
            byte_count = len(canonical_bytes(tuple(rows)))
            if row_count > row_ceiling or byte_count > byte_ceiling:
                with suppress(Exception):
                    cursor.cancel()
                raise _CeilingExceeded(row_count=row_count, byte_count=byte_count)
        return tuple(rows), len(rows), len(canonical_bytes(tuple(rows)))

    def _record_success(
        self,
        plan: AnswerQueryPlan,
        request_id: str,
        authorization: AnswerExecutionAuthorization,
        input_digest: str,
        attempt: int,
        started_at: datetime,
        columns: tuple[AnswerQueryColumn, ...],
        rows: tuple[tuple[AnswerQueryValue, ...], ...],
        suppressed_group_count: int,
    ) -> AnswerExecutionReceipt:
        completed_at = self._clock()
        row_count = len(rows)
        byte_count = len(canonical_bytes(rows))
        result_schema_digest = digest(columns)
        result_digest = digest({"columns": columns, "rows": rows})
        result_ref = (
            "answer-result-"
            + digest(
                {
                    "tenant_id": plan.tenant_id,
                    "request_id": request_id,
                    "plan_digest": plan.plan_digest,
                    "attempt": attempt,
                }
            )[:24]
        )
        snapshot = AnswerResultSnapshot(
            result_ref=result_ref,
            tenant_id=plan.tenant_id,
            request_id=request_id,
            plan_digest=plan.plan_digest,
            product_generation_refs=plan.product_generation_refs,
            columns=columns,
            rows=rows,
            row_count=row_count,
            byte_count=byte_count,
            result_schema_digest=result_schema_digest,
            result_digest=result_digest,
            created_at=completed_at,
            expires_at=completed_at + timedelta(seconds=authorization.result_retention_seconds),
        )
        receipt = self._receipt(
            plan,
            request_id,
            authorization,
            attempt,
            started_at,
            completed_at,
            "succeeded",
            row_count=row_count,
            byte_count=byte_count,
            suppressed_group_count=suppressed_group_count,
            result_schema_digest=result_schema_digest,
            result_digest=result_digest,
            result_ref=result_ref,
        )
        self._store.record_attempt(
            input_digest=input_digest,
            receipt=receipt,
            snapshot=snapshot,
        )
        return receipt

    def _record_failure(
        self,
        plan: AnswerQueryPlan,
        request_id: str,
        authorization: AnswerExecutionAuthorization,
        input_digest: str,
        attempt: int,
        started_at: datetime,
        outcome: AnswerExecutionOutcome,
        *,
        provider_error_classification: str | None = None,
        row_count: int = 0,
        byte_count: int = 0,
    ) -> AnswerExecutionReceipt:
        receipt = self._receipt(
            plan,
            request_id,
            authorization,
            attempt,
            started_at,
            self._clock(),
            outcome,
            provider_error_classification=provider_error_classification,
            row_count=row_count,
            byte_count=byte_count,
        )
        self._store.record_attempt(input_digest=input_digest, receipt=receipt, snapshot=None)
        return receipt

    @staticmethod
    def _receipt(
        plan: AnswerQueryPlan,
        request_id: str,
        authorization: AnswerExecutionAuthorization,
        attempt: int,
        started_at: datetime,
        completed_at: datetime,
        outcome: AnswerExecutionOutcome,
        *,
        provider_error_classification: str | None = None,
        row_count: int = 0,
        byte_count: int = 0,
        suppressed_group_count: int = 0,
        result_schema_digest: str | None = None,
        result_digest: str | None = None,
        result_ref: str | None = None,
    ) -> AnswerExecutionReceipt:
        return AnswerExecutionReceipt.model_validate(
            {
                "receipt_id": "answer-execution-"
                + digest(
                    {
                        "tenant_id": plan.tenant_id,
                        "request_id": request_id,
                        "plan_digest": plan.plan_digest,
                        "attempt": attempt,
                    }
                )[:24],
                "tenant_id": plan.tenant_id,
                "request_id": request_id,
                "plan_digest": plan.plan_digest,
                "product_generation_refs": plan.product_generation_refs,
                "attempt": attempt,
                "started_at": started_at,
                "completed_at": completed_at,
                "outcome": outcome,
                "provider_error_classification": provider_error_classification,
                "row_count": row_count,
                "byte_count": byte_count,
                "suppressed_group_count": suppressed_group_count,
                "result_schema_digest": result_schema_digest,
                "result_digest": result_digest,
                "result_ref": result_ref,
                "freshness_observation_ref": authorization.freshness_observation_ref,
                "quality_observation_ref": authorization.quality_observation_ref,
            }
        )


class _CeilingExceeded(RuntimeError):
    def __init__(self, *, row_count: int, byte_count: int) -> None:
        super().__init__("query result ceiling exceeded")
        self.row_count = row_count
        self.byte_count = byte_count
