from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_sdk import ProviderError
from heinzel_runtime import (
    AnswerExecutionAuthorization,
    AnswerExecutionAuthorizationError,
    AnswerExecutionConflict,
    AnswerExecutionIntegrityError,
    AnswerProductGenerationReference,
    AnswerQueryCeilings,
    AnswerQueryColumn,
    AnswerQueryCursor,
    AnswerQueryParameter,
    AnswerQueryPlan,
    AnswerQueryReference,
    AnswerQueryScan,
    AnswerQueryScanEstimate,
    AnswerQueryTimedOut,
    GovernedQueryExecutor,
    QueryGenerationState,
    QueryResultNotFound,
    ReadOnlyAnswerQuery,
    SQLiteAnswerResultStore,
)
from heinzel_runtime.answer_models import AnswerQueryValue

NOW = datetime(2026, 9, 11, 21, tzinfo=UTC)


class _Verifier:
    def verify(self, plan_digest: str, signature: str) -> bool:
        return signature == f"signed:{plan_digest}"


class _Authorizer:
    def __init__(
        self,
        *,
        tenant_id: str = "tenant-a",
        allowed: bool = True,
        row_ceiling: int = 2,
        byte_ceiling: int = 1_000,
        authority_updates: dict[str, object] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.allowed = allowed
        self.row_ceiling = row_ceiling
        self.byte_ceiling = byte_ceiling
        self.authority_updates = authority_updates or {}
        self.calls = 0

    def recheck(
        self, *, tenant_id: str, request_id: str, validation_digest: str, plan_digest: str
    ) -> AnswerExecutionAuthorization:
        self.calls += 1
        if not self.allowed:
            raise AnswerExecutionAuthorizationError("answer execution is not authorized")
        authorization = AnswerExecutionAuthorization(
            tenant_id=self.tenant_id,
            request_id=request_id,
            validation_digest=validation_digest,
            plan_digest=plan_digest,
            policy_revision=3,
            entitlement_digest="e" * 64,
            row_ceiling=self.row_ceiling,
            byte_ceiling=self.byte_ceiling,
            statement_timeout_seconds=15,
            result_retention_seconds=3600,
            freshness_observation_ref="freshness-1",
            quality_observation_ref="quality-1",
        )
        return authorization.model_copy(update=self.authority_updates)


class _Generations:
    def __init__(self, states: Iterable[QueryGenerationState] | None = None) -> None:
        self._states = tuple(
            states or (QueryGenerationState(addressable=True, current_generation=7),)
        )
        self.calls = 0

    def observe(self, _reference: object) -> QueryGenerationState:
        assert isinstance(_reference, AnswerProductGenerationReference)
        assert _reference.product_ref.artifact_id == "product-1"
        assert _reference.generation == 7
        index = min(self.calls, len(self._states) - 1)
        self.calls += 1
        return self._states[index]


class _Cursor:
    columns: tuple[AnswerQueryColumn, ...] = (
        AnswerQueryColumn(name="region", value_type="string"),
        AnswerQueryColumn(name="revenue", value_type="decimal"),
    )
    suppressed_group_count: int = 0

    def __init__(self, rows: Iterable[tuple[AnswerQueryValue, ...]]) -> None:
        self._rows = iter(rows)
        self.cancelled = False
        self.closed = False

    def fetchone(self) -> tuple[AnswerQueryValue, ...] | None:
        return next(self._rows, None)

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True


class _Provider:
    engine_kind: Literal["postgresql"] = "postgresql"

    def __init__(
        self,
        rows: Iterable[tuple[AnswerQueryValue, ...]] = (("west", "12.50"),),
        failures: Iterable[BaseException] = (),
    ) -> None:
        self.rows = tuple(rows)
        self._failures = iter(failures)
        self.requests: list[ReadOnlyAnswerQuery] = []
        self.cursors: list[_Cursor] = []

    def execute_read_only(self, request: ReadOnlyAnswerQuery) -> AnswerQueryCursor:
        self.requests.append(request)
        failure = next(self._failures, None)
        if failure is not None:
            raise failure
        cursor = _Cursor(self.rows)
        self.cursors.append(cursor)
        return cursor


def _plan(*, row_limit: int = 2, tenant_id: str = "tenant-a") -> AnswerQueryPlan:
    generation = AnswerProductGenerationReference(
        product_ref=AnswerQueryReference(artifact_id="product-1", version=4, digest="a" * 64),
        generation=7,
    )
    body: dict[str, object] = {
        "schema_version": "1",
        "plan_id": "query-plan-1",
        "tenant_id": tenant_id,
        "validation_digest": "b" * 64,
        "engine_kind": "postgresql",
        "compiler_version": "1",
        "allowlist_version": "governed-query-v1",
        "consumption_object_refs": (
            AnswerQueryReference(artifact_id="consumption-1", version=2, digest="c" * 64),
        ),
        "product_generation_refs": (generation,),
        "minimum_group_size": 5,
        "statement": (
            'SELECT "source"."region", SUM("source"."revenue") '
            'FROM "consumption"."sales" AS "source" GROUP BY "source"."region" '
            f'HAVING COUNT(DISTINCT "source"."customer_id") >= %s LIMIT {row_limit}'
        ),
        "parameters": (AnswerQueryParameter(name="p0", value_type="integer", value=5),),
        "statement_digest": "",
        "parameter_digest": "",
        "estimated_scan": AnswerQueryScanEstimate(rows=10, bytes=500, estimator_version="pg-1"),
        "ceilings": AnswerQueryCeilings(
            row_limit=row_limit,
            scan=AnswerQueryScan(rows=100, bytes=10_000),
            period_scan=AnswerQueryScan(rows=1_000, bytes=100_000),
        ),
        "routing": "policy_admitted",
    }
    body["statement_digest"] = digest(body["statement"])
    body["parameter_digest"] = digest(body["parameters"])
    plan_digest = digest(body)
    return AnswerQueryPlan.model_validate(
        {**body, "plan_digest": plan_digest, "signature": f"signed:{plan_digest}"}
    )


def _executor(
    store: SQLiteAnswerResultStore,
    provider: _Provider,
    *,
    authorizer: _Authorizer | None = None,
    generations: _Generations | None = None,
    is_cancelled: Callable[[str, str], bool] | None = None,
) -> GovernedQueryExecutor:
    return GovernedQueryExecutor(
        store=store,
        signature_verifier=_Verifier(),
        authorizer=authorizer or _Authorizer(),
        generation_reader=generations or _Generations(),
        provider_resolver=lambda engine_kind: provider,
        clock=lambda: NOW,
        sleeper=lambda _delay: None,
        is_cancelled=is_cancelled,
    )


def _store() -> SQLiteAnswerResultStore:
    return SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)


def test_success_persists_exact_snapshot_and_replay_returns_identical_receipt() -> None:
    store = _store()
    provider = _Provider()
    executor = _executor(store, provider)

    first = executor.execute(request_id="request-1", plan=_plan())
    replay = executor.execute(request_id="request-1", plan=_plan())
    snapshot = store.read_result("tenant-a", first.result_ref or "")

    assert canonical_bytes(replay) == canonical_bytes(first)
    assert len(provider.requests) == 1
    assert first.outcome == "succeeded"
    assert first.principal_class == "answer_runtime"
    assert snapshot.rows == (("west", "12.50"),)
    assert snapshot.result_digest == first.result_digest
    request = provider.requests[0]
    assert request.read_only is True
    assert request.tenant_id == "tenant-a"
    assert request.consumption_object_refs == _plan().consumption_object_refs
    assert request.statement_timeout_seconds == 15
    assert "HAVING COUNT(DISTINCT" in request.statement


def test_result_reads_deny_cross_tenant_without_enumerating() -> None:
    store = _store()
    receipt = _executor(store, _Provider()).execute(request_id="request-1", plan=_plan())

    with pytest.raises(QueryResultNotFound, match="result is not available"):
        store.read_result("tenant-b", receipt.result_ref or "")
    with pytest.raises(QueryResultNotFound, match="result is not available"):
        store.read_result("tenant-a", "result-does-not-exist")


def test_result_retention_expiry_uses_the_same_non_enumerating_denial() -> None:
    current = [NOW]
    store = SQLiteAnswerResultStore.in_memory(clock=lambda: current[0])
    receipt = _executor(store, _Provider()).execute(request_id="request-1", plan=_plan())
    current[0] = NOW + timedelta(seconds=3601)

    with pytest.raises(QueryResultNotFound, match="result is not available"):
        store.read_result("tenant-a", receipt.result_ref or "")


def test_row_ceiling_cancels_cursor_and_records_no_result() -> None:
    store = _store()
    provider = _Provider(rows=(("a", "1"), ("b", "2"), ("c", "3")))

    receipt = _executor(store, provider).execute(request_id="request-1", plan=_plan())

    assert receipt.outcome == "ceiling_exceeded"
    assert receipt.row_count == 3
    assert receipt.result_ref is None
    assert provider.cursors[0].cancelled is True


def test_byte_ceiling_cancels_cursor_using_canonical_result_bytes() -> None:
    store = _store()
    provider = _Provider(rows=(("west" * 20, "123456789.00"),))

    receipt = _executor(
        store,
        provider,
        authorizer=_Authorizer(byte_ceiling=16),
    ).execute(request_id="request-1", plan=_plan())

    assert receipt.outcome == "ceiling_exceeded"
    assert receipt.byte_count > 16
    assert provider.cursors[0].cancelled is True


def test_exact_row_and_byte_ceilings_are_admitted() -> None:
    store = _store()
    rows = (("a", "1"), ("b", "2"))
    provider = _Provider(rows=rows)

    receipt = _executor(
        store,
        provider,
        authorizer=_Authorizer(row_ceiling=2, byte_ceiling=len(canonical_bytes(rows))),
    ).execute(request_id="request-1", plan=_plan())

    assert receipt.outcome == "succeeded"
    assert receipt.row_count == 2
    assert receipt.byte_count == len(canonical_bytes(rows))


def test_unavailable_generation_records_failure_before_provider_execution() -> None:
    store = _store()
    provider = _Provider()
    generations = _Generations((QueryGenerationState(addressable=False, current_generation=8),))

    receipt = _executor(store, provider, generations=generations).execute(
        request_id="request-1", plan=_plan()
    )

    assert receipt.outcome == "generation_unavailable"
    assert provider.requests == []


def test_pointer_generation_change_after_query_discards_result() -> None:
    store = _store()
    provider = _Provider()
    generations = _Generations(
        (
            QueryGenerationState(addressable=False, current_generation=7),
            QueryGenerationState(addressable=False, current_generation=8),
        )
    )

    receipt = _executor(store, provider, generations=generations).execute(
        request_id="request-1", plan=_plan()
    )

    assert receipt.outcome == "generation_unavailable"
    assert provider.cursors[0].closed is True
    assert receipt.result_ref is None


def test_request_cancellation_cancels_the_active_cursor_and_records_abort() -> None:
    store = _store()
    provider = _Provider()
    checks = 0

    def is_cancelled(_tenant_id: str, _request_id: str) -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    receipt = _executor(store, provider, is_cancelled=is_cancelled).execute(
        request_id="request-1", plan=_plan()
    )

    assert receipt.outcome == "aborted"
    assert provider.cursors[0].cancelled is True


def test_transient_provider_failure_creates_a_new_attempt_under_the_same_plan() -> None:
    store = _store()
    provider = _Provider(failures=(ProviderError("temporary", "transient_unavailable"),))

    receipt = _executor(store, provider).execute(request_id="request-1", plan=_plan())
    attempts = store.list_attempts("tenant-a", "request-1")

    assert receipt.outcome == "succeeded"
    assert receipt.attempt == 2
    assert tuple(attempt.outcome for attempt in attempts) == ("provider_failed", "succeeded")
    assert attempts[0].provider_error_classification == "transient_unavailable"


def test_transient_provider_resolution_failure_is_recorded_and_retried() -> None:
    store = _store()
    resolutions = 0

    def unavailable_resolver(_engine_kind: str) -> _Provider:
        nonlocal resolutions
        resolutions += 1
        raise ProviderError("binding authority unavailable", "transient_unavailable")

    executor = GovernedQueryExecutor(
        store=store,
        signature_verifier=_Verifier(),
        authorizer=_Authorizer(),
        generation_reader=_Generations(),
        provider_resolver=unavailable_resolver,
        clock=lambda: NOW,
        sleeper=lambda _delay: None,
    )

    receipt = executor.execute(request_id="request-1", plan=_plan())

    assert receipt.outcome == "provider_failed"
    assert receipt.attempt == 3
    assert receipt.provider_error_classification == "transient_unavailable"
    assert resolutions == 3
    assert len(store.list_attempts("tenant-a", "request-1")) == 3


def test_execution_usage_reader_lists_only_exact_tenant_receipts() -> None:
    store = _store()
    tenant_a = _executor(store, _Provider()).execute(
        request_id="request-a", plan=_plan(tenant_id="tenant-a")
    )
    tenant_b = _executor(
        store,
        _Provider(),
        authorizer=_Authorizer(tenant_id="tenant-b"),
    ).execute(request_id="request-b", plan=_plan(tenant_id="tenant-b"))

    assert store.list_for_tenant(tenant_id="tenant-a") == (tenant_a,)
    assert store.list_for_tenant(tenant_id="tenant-b") == (tenant_b,)


def test_reused_request_with_a_different_signed_plan_is_a_conflict() -> None:
    store = _store()
    executor = _executor(store, _Provider())
    executor.execute(request_id="request-1", plan=_plan())

    with pytest.raises(AnswerExecutionConflict, match="replay conflicts"):
        executor.execute(request_id="request-1", plan=_plan(row_limit=1))


def test_permanent_provider_outcome_retains_its_classification() -> None:
    store = _store()
    provider = _Provider(failures=(ProviderError("denied", "authorization_denied"),))

    receipt = _executor(store, provider).execute(request_id="request-1", plan=_plan())

    assert receipt.outcome == "provider_failed"
    assert receipt.provider_error_classification == "authorization_denied"
    assert len(provider.requests) == 1


def test_plan_digest_mismatch_fails_before_authority_or_provider_resolution() -> None:
    store = _store()
    provider = _Provider()
    authorizer = _Authorizer()
    changed = _plan().model_copy(update={"statement": "SELECT 1"})

    with pytest.raises(AnswerExecutionIntegrityError, match="digest"):
        _executor(store, provider, authorizer=authorizer).execute(
            request_id="request-1", plan=changed
        )

    assert authorizer.calls == 0
    assert provider.requests == []


def test_invalid_plan_signature_fails_before_authority_or_provider_resolution() -> None:
    store = _store()
    provider = _Provider()
    authorizer = _Authorizer()
    changed = _plan().model_copy(update={"signature": "wrong"})

    with pytest.raises(AnswerExecutionIntegrityError, match="signature"):
        _executor(store, provider, authorizer=authorizer).execute(
            request_id="request-1", plan=changed
        )

    assert authorizer.calls == 0
    assert provider.requests == []


def test_policy_or_entitlement_denial_precedes_generation_and_provider_access() -> None:
    store = _store()
    provider = _Provider()
    generations = _Generations()

    with pytest.raises(AnswerExecutionAuthorizationError):
        _executor(
            store,
            provider,
            authorizer=_Authorizer(allowed=False),
            generations=generations,
        ).execute(request_id="request-1", plan=_plan())

    assert generations.calls == 0
    assert provider.requests == []


@pytest.mark.parametrize(
    "authority_updates",
    (
        {"tenant_id": "tenant-b"},
        {"request_id": "request-other"},
        {"validation_digest": "d" * 64},
        {"plan_digest": "f" * 64},
    ),
)
def test_mismatched_recheck_authority_is_denied_before_generation_access(
    authority_updates: dict[str, object],
) -> None:
    store = _store()
    provider = _Provider()
    generations = _Generations()

    with pytest.raises(AnswerExecutionAuthorizationError, match="mismatched authority"):
        _executor(
            store,
            provider,
            authorizer=_Authorizer(authority_updates=authority_updates),
            generations=generations,
        ).execute(request_id="request-1", plan=_plan())

    assert generations.calls == 0
    assert provider.requests == []


def test_timeout_is_terminal_and_replay_does_not_execute_again() -> None:
    store = _store()
    provider = _Provider(failures=(AnswerQueryTimedOut(),))
    executor = _executor(store, provider)

    first = executor.execute(request_id="request-1", plan=_plan())
    replay = executor.execute(request_id="request-1", plan=_plan())

    assert first.outcome == "timed_out"
    assert replay == first
    assert len(provider.requests) == 1
