# Data Architect MVP Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the durable control-plane foundation for the data architect to create an immutable managed-warehouse binding, upload a versioned business-process package, and receive typed stakeholder and access requests in one inbox.

**Architecture:** Add substantive implementations to the already-governed `services/warehouse-control` and `services/request-management` boundaries, while keeping business-process versions beside Integration Contract lifecycle in `services/contract`. Each service owns strict frozen Pydantic contracts and an injected repository protocol; SQLite adapters provide deterministic local durability without coupling domain behavior to infrastructure. This plan stops before provider provisioning, catalog publication, semantic extraction, query execution, or grant application.

**Tech Stack:** Python 3.13, Pydantic 2.11, standard-library `sqlite3`, `uv`, pytest, Ruff, strict mypy

**Spec:** `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md`

## Global Constraints

- PostgreSQL and ClickHouse are the only MVP `engine_kind` values.
- A warehouse binding becomes immutable when it leaves `draft`; engine, region, deployment mode, and tenant must never change in place.
- The MVP process package contains one UTF-8 Markdown narrative and one strict JSON manifest; original bytes and extracted candidates remain distinct.
- Unknown fields fail validation at every durable boundary.
- Timestamps are timezone-aware UTC; deterministic artifacts exclude receipt timestamps. No durable identity is derived from a clock reading — identities derive from a tenant, a domain tag, and a repository-assigned sequence, so they are reproducible and cannot collide under a frozen test clock.
- **Every service and repository method that names a durable object takes `tenant_id` and refuses an object belonging to another tenant.** A bare identifier is never sufficient authority to read or mutate. Cross-tenant refusal raises before any field is read or written, and every task proves it. This boundary is set here, while there are ten call sites rather than several hundred.
- Durable state is append-only. Every mutation writes a new `(object_id, revision)` row; no row is updated in place, so lifecycle history survives. Every mutating call carries the caller's `expected_revision` and fails closed when the stored revision has moved.
- AI output is candidate material only and never approval, access, legality, or execution authority.
- Stakeholder questions and access requests enter the same typed inbox but retain distinct payloads and fail-closed fulfillment rules.
- No service in this plan provisions infrastructure, calls a catalog, executes SQL, grants access, or activates an Integration Contract.
- A SQLite adapter constructed with `":memory:"` must hold one connection for its lifetime; a per-call connection yields a fresh empty database and silently loses all state.
- Every task includes a focused success case and at least one denial, invalid input, immutable-state, or cross-tenant boundary case.
- Every task confirms its assertions fail for the intended reason before the rule exists, not merely that the module is absent (see "Red-step discipline").

## Program decomposition

This is Plan 1 of the architect-centered MVP. Later plans must consume the contracts created here in this order:

1. **Catalog and semantic formation:** OpenMetadata provisioning, process candidate extraction, authority resolution, ontology review, and Integration Contract formation.
2. **Managed data plane:** PostgreSQL and ClickHouse warehouse provisioning, PostgreSQL and Stripe source acquisition, transformations, reconciliation, and two-engine conformance.
3. **Architect inbox fulfillment:** grounded stakeholder answers, dependent-request creation, least-privilege access proposals, grant application, denial validation, expiry, and revocation.
4. **Scheduling and managed operations:** trigger policies, run intents, retry, replay, drift, incidents, backup, and witnessed restore.
5. **Superset and console:** governed datasets, dashboard/report compilation, architect inbox UI, decision previews, and evidence views.
6. **Witnessed acceptance:** the complete revenue-to-cash fixture on PostgreSQL and ClickHouse with independent reproduction.

Do not begin a later plan until the preceding plan's public contracts and acceptance tests are committed. This avoids parallel implementations inventing incompatible warehouse, process, request, or evidence identities.

## Red-step discipline

Each task's second step runs the new tests before any implementation exists, and the failure is a `ModuleNotFoundError` or a missing export. That proves the module is absent — it does not prove the invariant is unimplemented. The assertions that carry the weight here are the denials: `immutable after draft`, `transition ready -> retired is not allowed`, `request revision is stale`, and `belongs to another tenant`. A permissive transition table, an absent tenant check, or a differently worded error would never be observed failing.

So every task has a second red step: once the module imports, comment out or relax the rule under test, confirm the denial assertion itself fails for the intended reason, restore the rule, and confirm it passes. A test that has only ever been green is unverified.

## Spec alignment

Two points where this plan went beyond `managed-data-engineering-platform-addendum-v0.1.md`. Both are now ratified there, so Plan 2 may consume these contracts.

1. **Request terminal reachability.** Section 13.3 named the linear chain and listed terminal alternatives, but not which states reach which terminal. Ratified as addendum section 13.3.1.
2. **`WarehouseBinding` field shape.** Section 6.2 declared `capability_profile_digest` and `provisioned_at` only. Ratified: section 6.2 now carries the implemented shape, including `capacity_profile` and the append-only `revision`/`created_at`/`updated_at`, and records that `provisioned_at` stays null until provisioning succeeds.

Ratifying the warehouse binding also surfaced a third gap, now closed as addendum section 6.4.1: the section 6.4 diagram omitted `draft -> retired` and `failed -> retired`, both of which the implementation allows and neither of which passes through `retiring`.

## File map

- `services/warehouse-control/src/pillarmesh_warehouse_control/models.py`: immutable warehouse binding and lifecycle transition contracts.
- `services/warehouse-control/src/pillarmesh_warehouse_control/repository.py`: repository protocol and SQLite adapter.
- `services/warehouse-control/src/pillarmesh_warehouse_control/service.py`: allowed transition and immutable-field enforcement.
- `services/contract/src/pillarmesh_contract_service/process_models.py`: process-package manifest, upload receipt, and approved process-version models.
- `services/contract/src/pillarmesh_contract_service/process_service.py`: upload validation, canonical digesting, version creation, and immutable storage.
- `services/request-management/src/pillarmesh_request_management/models.py`: inbox request payloads, lifecycle, conversations, and decision references.
- `services/request-management/src/pillarmesh_request_management/repository.py`: request repository protocol and SQLite adapter.
- `services/request-management/src/pillarmesh_request_management/service.py`: request creation, transition policy, and conversation append behavior.
- `tests/end-to-end/test_data_architect_foundation.py`: one architect-visible flow across the three service boundaries.

---

### Task 1: Immutable managed-warehouse binding

**Files:**
- Create: `services/warehouse-control/pyproject.toml`
- Create: `services/warehouse-control/src/pillarmesh_warehouse_control/__init__.py`
- Create: `services/warehouse-control/src/pillarmesh_warehouse_control/py.typed`
- Create: `services/warehouse-control/src/pillarmesh_warehouse_control/models.py`
- Create: `services/warehouse-control/src/pillarmesh_warehouse_control/repository.py`
- Create: `services/warehouse-control/src/pillarmesh_warehouse_control/service.py`
- Create: `services/warehouse-control/tests/test_service.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `pillarmesh_contract_model.ArtifactModel` and `digest(value: object) -> str`.
- Produces: `WarehouseBinding`, `WarehouseBindingState`, `EngineKind`, `WarehouseRepository`, `SQLiteWarehouseRepository`, `WarehouseControlService.create_draft()`, `.revise_draft()`, `.transition()`, and `.get()`.
- Every service method after `create_draft` takes `tenant_id` first and `expected_revision` last.

- [ ] **Step 1: Write failing model, lifecycle, tenant, and concurrency tests**

```python
from datetime import UTC, datetime

import pytest
from pillarmesh_warehouse_control import (
    EngineKind,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseControlService,
)
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository


NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def service() -> WarehouseControlService:
    return WarehouseControlService(SQLiteWarehouseRepository(":memory:"), clock=lambda: NOW)


def draft(control: WarehouseControlService, tenant_id: str = "tenant-a") -> WarehouseBinding:
    return control.create_draft(
        tenant_id=tenant_id,
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west",
        capacity_profile="mvp-fixed",
    )


def test_binding_becomes_immutable_when_provisioning_starts() -> None:
    control = service()
    binding = draft(control)

    provisioning = control.transition(
        "tenant-a",
        binding.binding_id,
        WarehouseBindingState.PROVISIONING,
        expected_revision=binding.revision,
    )

    assert provisioning.engine_kind is EngineKind.POSTGRESQL
    with pytest.raises(ValueError, match="immutable after draft"):
        control.revise_draft(
            "tenant-a",
            provisioning.binding_id,
            engine_kind=EngineKind.CLICKHOUSE,
            expected_revision=provisioning.revision,
        )


def test_ready_cannot_transition_directly_to_retired() -> None:
    control = service()
    binding = control.create_draft(
        tenant_id="tenant-a",
        engine_kind=EngineKind.CLICKHOUSE,
        region="us-west",
        capacity_profile="mvp-fixed",
    )
    for state in (
        WarehouseBindingState.PROVISIONING,
        WarehouseBindingState.VALIDATING,
        WarehouseBindingState.READY,
    ):
        binding = control.transition(
            "tenant-a", binding.binding_id, state, expected_revision=binding.revision
        )

    with pytest.raises(ValueError, match="transition ready -> retired is not allowed"):
        control.transition(
            "tenant-a",
            binding.binding_id,
            WarehouseBindingState.RETIRED,
            expected_revision=binding.revision,
        )


def test_one_tenant_cannot_read_or_move_another_tenants_binding() -> None:
    control = service()
    binding = draft(control, tenant_id="tenant-a")

    with pytest.raises(KeyError, match="belongs to another tenant"):
        control.get("tenant-b", binding.binding_id)
    with pytest.raises(KeyError, match="belongs to another tenant"):
        control.transition(
            "tenant-b",
            binding.binding_id,
            WarehouseBindingState.PROVISIONING,
            expected_revision=binding.revision,
        )
    assert control.get("tenant-a", binding.binding_id).lifecycle_state is (
        WarehouseBindingState.DRAFT
    )


def test_two_drafts_for_one_tenant_receive_distinct_identities() -> None:
    # Specification section 6.3 requires a migration to create a NEW binding for the
    # same tenant. Under the frozen clock these are created in the same instant.
    control = service()

    first = draft(control)
    second = draft(control)

    assert first.binding_id != second.binding_id
    assert control.get("tenant-a", first.binding_id).binding_id == first.binding_id


def test_stale_revision_loses_the_transition() -> None:
    control = service()
    binding = draft(control)
    control.transition(
        "tenant-a",
        binding.binding_id,
        WarehouseBindingState.PROVISIONING,
        expected_revision=binding.revision,
    )

    with pytest.raises(ValueError, match="binding revision is stale"):
        control.transition(
            "tenant-a",
            binding.binding_id,
            WarehouseBindingState.FAILED,
            expected_revision=binding.revision,
        )
```

- [ ] **Step 2: Run the tests and confirm the missing package failure**

Run: `uv run pytest services/warehouse-control/tests/test_service.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'pillarmesh_warehouse_control'`.

- [ ] **Step 3: Add the workspace package and strict warehouse contracts**

Add `services/warehouse-control` to `[tool.uv.workspace].members`, add `pillarmesh_warehouse_control` to mypy packages, and create the package with:

```python
class EngineKind(StrEnum):
    POSTGRESQL = "postgresql"
    CLICKHOUSE = "clickhouse"


class WarehouseBindingState(StrEnum):
    DRAFT = "draft"
    PROVISIONING = "provisioning"
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"
    SUSPENDED = "suspended"
    RETIRING = "retiring"
    RETIRED = "retired"


class WarehouseBinding(ArtifactModel):
    schema_version: Literal["1"] = "1"
    binding_id: str
    tenant_id: str
    engine_kind: EngineKind
    deployment_mode: Literal["pillarmesh_cloud"] = "pillarmesh_cloud"
    region: str
    capacity_profile: Literal["mvp-fixed"] = "mvp-fixed"
    capability_profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lifecycle_state: WarehouseBindingState
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    provisioned_at: datetime | None = None
```

`capability_profile_digest` is `digest({"capacity_profile": capacity_profile, "engine_kind": engine_kind, "deployment_mode": deployment_mode})`, satisfying the specification section 6.2 field. `provisioned_at` stays `None` for the whole of this plan; Plan 2 sets it when provisioning succeeds.

**Identity.** The repository assigns a per-tenant sequence and the identity derives from it, never from a clock:

```python
sequence = repository.next_sequence(tenant_id)
binding_id = (
    "whb-"
    + digest(
        {
            "domain": "pillarmesh-warehouse-binding-v1",
            "tenant_id": tenant_id,
            "sequence": sequence,
        }
    )[:24]
)
```

A timestamp-derived identity collides for two drafts created in the same instant, which is exactly what specification section 6.3 requires for a migration, and collides on every draft under the frozen test clock.

**Transition table.** Copy specification section 6.4 exactly, and reject everything else:

```text
draft       → provisioning, retired
provisioning → validating, failed
validating  → ready, failed
ready       → suspended, retiring
suspended   → ready, retiring
retiring    → retired
failed      → retired
retired     → (terminal)
```

`draft → retired` covers abandoning a binding that was never provisioned. `ready → retired` is absent by construction; the test above pins it.

**Tenant scoping.** Every method except `create_draft` takes `tenant_id` first. The service loads the binding, raises `KeyError("binding <id> belongs to another tenant")` when `tenant_id` differs, and only then reads or writes any field. The repository protocol carries `tenant_id` on every method too, so a future adapter cannot silently widen the read.

**Concurrency.** `transition` and `revise_draft` take `expected_revision` and raise `ValueError("binding revision is stale")` when the stored revision differs. `revise_draft` rejects every non-draft binding before applying any field change.

**Durability.** Append-only, matching Task 3: `(binding_id, revision)` primary key, `tenant_id TEXT NOT NULL`, `payload BLOB NOT NULL`, plus a `warehouse_sequences` table of `(tenant_id TEXT PRIMARY KEY, next_sequence INTEGER NOT NULL)`. Never update a row in place; `get` returns the highest revision for the binding. A binding whose lifecycle history is overwritten cannot evidence when it became immutable.

Define `WarehouseRepository` with exact `next_sequence(tenant_id: str) -> int`, `save(binding: WarehouseBinding) -> None`, and `load(tenant_id: str, binding_id: str) -> WarehouseBinding | None` methods. `SQLiteWarehouseRepository` holds one `sqlite3.Connection` for its lifetime so `":memory:"` retains state across calls.

- [ ] **Step 4: Run focused tests**

Run: `uv lock && uv run pytest services/warehouse-control/tests/test_service.py -q`

Expected: `5 passed`.

- [ ] **Step 5: Prove each denial fails for its own reason**

For each of the four denial assertions in turn — immutability after draft, `ready -> retired`, cross-tenant access, and the stale revision — relax only that rule in `service.py`, re-run the file, and confirm that test alone fails on its own assertion rather than on an import or an unrelated error. Restore the rule and confirm `5 passed` again.

Expected: each denial observed failing once, and a green run afterwards.

- [ ] **Step 6: Run package static checks**

Run: `uv run ruff check services/warehouse-control && uv run mypy -p pillarmesh_warehouse_control`

Expected: both commands exit 0.

- [ ] **Step 7: Commit the warehouse boundary**

```bash
git add pyproject.toml uv.lock services/warehouse-control
git commit -m "feat(warehouse-control): add immutable warehouse bindings"
```

### Task 2: Immutable business-process package intake

**Files:**
- Create: `services/contract/src/pillarmesh_contract_service/process_models.py`
- Create: `services/contract/src/pillarmesh_contract_service/process_service.py`
- Create: `services/contract/tests/test_process_service.py`
- Modify: `services/contract/src/pillarmesh_contract_service/__init__.py`

**Interfaces:**
- Consumes: `pillarmesh_contract_model.canonical_bytes()` and `digest()`; injected `ProcessPackageRepository` protocol.
- Produces: `BusinessProcessManifest`, `ProcessPackageReceipt`, `ProcessPackageRepository`, `SQLiteProcessPackageRepository`, `ProcessPackageService.upload()`, and `ProcessPackageService.get_original()`.
- `ProcessPackageService(repository, *, clock)` takes an injected clock exactly like the Task 1 and Task 3 services; `ProcessPackageReceipt.received_at` cannot be produced without one.
- `get_original` takes `tenant_id` first.

- [ ] **Step 1: Write failing upload, immutability, and tenant tests**

```python
from datetime import UTC, datetime

import pytest
from pillarmesh_contract_service import BusinessProcessManifest, ProcessPackageService
from pillarmesh_contract_service.process_service import SQLiteProcessPackageRepository


NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
MARKDOWN = "text/markdown; charset=utf-8"


def service() -> ProcessPackageService:
    return ProcessPackageService(SQLiteProcessPackageRepository(":memory:"), clock=lambda: NOW)


def manifest() -> BusinessProcessManifest:
    return BusinessProcessManifest(
        process_name="revenue-to-cash",
        owner="finance-data-owner",
        participants=("customer", "finance"),
        outcomes=("recognized-revenue",),
        entities=("Customer", "Invoice", "Payment", "Refund"),
        events=("invoice-issued", "payment-settled", "refund-issued"),
        states=("invoice-open", "invoice-paid", "invoice-refunded"),
        rules=("refund does not exceed settled payment",),
        source_references=("postgresql.billing", "stripe"),
        unresolved_questions=("refund exception owner",),
    )


def test_upload_preserves_original_and_creates_new_version() -> None:
    packages = service()
    first = packages.upload("tenant-a", b"# Revenue to cash\n", MARKDOWN, manifest(), "architect-a")
    second = packages.upload(
        "tenant-a", b"# Revenue to cash v2\n", MARKDOWN, manifest(), "architect-a"
    )

    assert first.version == 1
    assert second.version == 2
    assert first.original_digest != second.original_digest
    assert first.media_type == MARKDOWN
    assert packages.get_original("tenant-a", first.package_id, 1) == b"# Revenue to cash\n"


def test_upload_rejects_non_utf8_narrative() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        service().upload("tenant-a", b"\xff", MARKDOWN, manifest(), "architect-a")


def test_upload_rejects_an_unsupported_media_type() -> None:
    with pytest.raises(ValueError, match="media type"):
        service().upload("tenant-a", b"# ok\n", "application/pdf", manifest(), "architect-a")


def test_one_tenant_cannot_read_another_tenants_original() -> None:
    packages = service()
    receipt = packages.upload("tenant-a", b"# Revenue to cash\n", MARKDOWN, manifest(), "arch-a")

    with pytest.raises(KeyError, match="belongs to another tenant"):
        packages.get_original("tenant-b", receipt.package_id, receipt.version)
```

- [ ] **Step 2: Run the tests and confirm the missing model failure**

Run: `uv run pytest services/contract/tests/test_process_service.py -q`

Expected: import fails because `BusinessProcessManifest` is not exported.

- [ ] **Step 3: Implement the strict process package contracts**

```python
class BusinessProcessManifest(ArtifactModel):
    schema_version: Literal["1"] = "1"
    process_name: str = Field(min_length=1, max_length=128)
    owner: str = Field(min_length=1, max_length=128)
    participants: tuple[str, ...]
    outcomes: tuple[str, ...]
    entities: tuple[str, ...]
    events: tuple[str, ...]
    states: tuple[str, ...]
    rules: tuple[str, ...]
    source_references: tuple[str, ...]
    unresolved_questions: tuple[str, ...]


class ProcessPackageReceipt(ArtifactModel):
    package_id: str
    tenant_id: str
    version: int = Field(ge=1)
    media_type: Literal["text/markdown; charset=utf-8"]
    original_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    uploader_id: str
    received_at: datetime
```

`media_type` satisfies specification section 8.4, which requires each uploaded artifact to retain "original bytes, media type, digest, uploader, and receipt time". The MVP accepts exactly one value; `upload` rejects any other with a message naming the media type, so widening the format later is a deliberate contract change rather than an accident.

Validate UTF-8 and media type before creating a receipt. Derive `package_id` from tenant and process name, increment versions per package, store original bytes separately from canonical manifest bytes, and reject replacement of an existing `(package_id, version)`. Do not create extracted candidates or an approved process during upload.

**Tenant scoping.** `get_original` takes `tenant_id` first and raises `KeyError("package <id> belongs to another tenant")` before returning any bytes. `package_id` is derived from the tenant, so a cross-tenant identifier can only arrive by guess or leak — which is exactly the case the check exists for.

Define `ProcessPackageRepository` with exact `next_version(tenant_id: str, package_id: str) -> int`, `save(receipt: ProcessPackageReceipt, original: bytes, manifest: bytes) -> None`, and `load_original(tenant_id: str, package_id: str, version: int) -> bytes | None` methods. Implement them in SQLite with `(package_id, version)` as the package primary key, `tenant_id TEXT NOT NULL`, and artifact digests as separately unique columns. `SQLiteProcessPackageRepository` holds one connection for its lifetime so `":memory:"` retains state.

- [ ] **Step 4: Run focused tests and contract regressions**

Run: `uv run pytest services/contract/tests/test_process_service.py services/contract/tests/test_service.py -q`

Expected: all tests pass, including the four new process-package tests.

- [ ] **Step 5: Prove each denial fails for its own reason**

Relax the UTF-8 check, the media-type check, and the tenant check in turn; confirm the matching test fails on its own assertion each time; restore and confirm green.

Expected: three denials observed failing once, and a green run afterwards.

- [ ] **Step 6: Run package static checks**

Run: `uv run ruff check services/contract && uv run mypy -p pillarmesh_contract_service`

Expected: both commands exit 0.

- [ ] **Step 7: Commit process intake**

```bash
git add services/contract
git commit -m "feat(contract): add immutable process package intake"
```

### Task 3: Typed architect inbox and lifecycle

**Files:**
- Create: `services/request-management/pyproject.toml`
- Create: `services/request-management/src/pillarmesh_request_management/__init__.py`
- Create: `services/request-management/src/pillarmesh_request_management/py.typed`
- Create: `services/request-management/src/pillarmesh_request_management/models.py`
- Create: `services/request-management/src/pillarmesh_request_management/repository.py`
- Create: `services/request-management/src/pillarmesh_request_management/service.py`
- Create: `services/request-management/tests/test_service.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `pillarmesh_contract_model.ArtifactModel` and `digest()`.
- Produces: `InboxRequest`, `StakeholderQuestion`, `DataAccessRequest`, `RequestState`, `RequestRepository`, `SQLiteRequestRepository`, `RequestManagementService.submit_question()`, `.submit_access_request()`, `.transition()`, `.get()`, and `.list_inbox()`.
- Conversation and decision APIs belong to Task 4 and must not appear here.
- Every method after the two `submit_*` calls takes `tenant_id` first; `transition` also takes `expected_revision`.

- [ ] **Step 1: Write failing request-shape, transition, and tenant tests**

```python
from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_request_management import (
    InboxRequest,
    RequestManagementService,
    RequestState,
)
from pillarmesh_request_management.repository import SQLiteRequestRepository


NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def test_question_and_access_request_share_ordered_inbox() -> None:
    service = RequestManagementService(SQLiteRequestRepository(":memory:"), clock=lambda: NOW)
    question = service.submit_question(
        tenant_id="tenant-a",
        requester_id="finance-user",
        purpose="monthly close",
        question="What were net refunds yesterday?",
    )
    access = service.submit_access_request(
        tenant_id="tenant-a",
        requester_id="analyst-a",
        purpose="refund investigation",
        data_product_id="finance-revenue",
        requested_fields=("invoice_id", "refund_amount"),
        access_mode="query",
        expires_at=NOW + timedelta(days=7),
    )

    assert [item.request_id for item in service.list_inbox("tenant-a")] == [
        question.request_id,
        access.request_id,
    ]


def access_request(service: RequestManagementService) -> InboxRequest:
    return service.submit_access_request(
        tenant_id="tenant-a",
        requester_id="analyst-a",
        purpose="refund investigation",
        data_product_id="finance-revenue",
        requested_fields=("refund_amount",),
        access_mode="query",
        expires_at=NOW + timedelta(days=1),
    )


def test_request_cannot_skip_approval_state() -> None:
    service = RequestManagementService(SQLiteRequestRepository(":memory:"), clock=lambda: NOW)
    request = access_request(service)

    with pytest.raises(ValueError, match="submitted -> executing"):
        service.transition(
            "tenant-a",
            request.request_id,
            RequestState.EXECUTING,
            actor_id="architect-a",
            expected_revision=request.revision,
        )


def test_one_tenant_cannot_see_or_move_another_tenants_request() -> None:
    service = RequestManagementService(SQLiteRequestRepository(":memory:"), clock=lambda: NOW)
    request = access_request(service)

    assert service.list_inbox("tenant-b") == ()
    with pytest.raises(KeyError, match="belongs to another tenant"):
        service.get("tenant-b", request.request_id)
    with pytest.raises(KeyError, match="belongs to another tenant"):
        service.transition(
            "tenant-b",
            request.request_id,
            RequestState.CLARIFYING,
            actor_id="attacker",
            expected_revision=request.revision,
        )


def test_access_expiry_must_be_aware_and_after_submission() -> None:
    service = RequestManagementService(SQLiteRequestRepository(":memory:"), clock=lambda: NOW)

    with pytest.raises(ValueError, match="expires_at"):
        service.submit_access_request(
            tenant_id="tenant-a",
            requester_id="analyst-a",
            purpose="refund investigation",
            data_product_id="finance-revenue",
            requested_fields=("refund_amount",),
            access_mode="query",
            expires_at=NOW - timedelta(days=1),
        )
```

- [ ] **Step 2: Run the tests and confirm the package is absent**

Run: `uv run pytest services/request-management/tests/test_service.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'pillarmesh_request_management'`.

- [ ] **Step 3: Implement discriminated request payloads and lifecycle**

```python
class StakeholderQuestion(ArtifactModel):
    request_type: Literal["stakeholder_question"] = "stakeholder_question"
    purpose: str = Field(min_length=1, max_length=512)
    question: str = Field(min_length=1, max_length=4000)


class DataAccessRequest(ArtifactModel):
    request_type: Literal["data_access"] = "data_access"
    purpose: str = Field(min_length=1, max_length=512)
    data_product_id: str
    requested_fields: tuple[str, ...]
    access_mode: Literal["query", "dashboard", "export"]
    expires_at: datetime


class InboxRequest(ArtifactModel):
    request_id: str
    tenant_id: str
    requester_id: str
    payload: Annotated[StakeholderQuestion | DataAccessRequest, Field(discriminator="request_type")]
    state: RequestState
    revision: int = Field(ge=1)
    submitted_at: datetime
    updated_at: datetime
```

**Lifecycle.** Specification section 13.3 gives the linear chain and names the terminals but not which states reach them, so this plan fixes the table. Every value is snake_case; `no_valid_plan` never appears hyphenated in a persisted value.

```text
submitted        → clarifying, investigating
clarifying       → investigating, submitted
investigating    → proposed, no_valid_plan
proposed         → awaiting_approval, investigating
awaiting_approval → executing, rejected, investigating
executing        → verifying, failed
verifying        → delivered, failed
delivered        → monitoring, retired
monitoring       → retired
```

Any non-terminal state may also move to `cancelled`. `rejected`, `no_valid_plan`, `cancelled`, `failed`, and `retired` are terminal and have no outgoing transitions. `transition` raises `ValueError("transition <from> -> <to> is not allowed")`, which is the message the skip test matches.

This table is a plan decision, not a quotation. Record it in the addendum before Plan 3 builds fulfillment on it.

**Validation.** Access expiry must be timezone-aware and strictly later than submission; the service checks it against its own clock rather than the payload alone, because the payload cannot see the submission time. The error names `expires_at`.

**Tenant scoping.** `get` and `transition` take `tenant_id` first and raise `KeyError("request <id> belongs to another tenant")` before reading or writing any field. `list_inbox(tenant_id)` filters by tenant and returns an empty tuple for a tenant with no requests rather than raising.

**Durability.** Store each revision append-only in SQLite with `(request_id, revision)` as the primary key and `tenant_id TEXT NOT NULL`; maintain no mutable JSON row. `list_inbox` returns the latest revision per request ordered by `submitted_at, request_id`. `SQLiteRequestRepository` holds one connection for its lifetime so `":memory:"` retains state.

**Forward compatibility.** Section 13.2 names ten request types; this plan implements two. Later plans widen the discriminated union, which is backward compatible for reading rows written now. Do not collapse the discriminator into a single permissive payload to avoid the widening.

- [ ] **Step 4: Run focused tests**

Run: `uv lock && uv run pytest services/request-management/tests/test_service.py -q`

Expected: `4 passed`.

- [ ] **Step 5: Prove each denial fails for its own reason**

Relax the transition table, the tenant check, and the expiry validation in turn; confirm the matching test fails on its own assertion each time; restore and confirm green.

Expected: three denials observed failing once, and a green run afterwards.

- [ ] **Step 6: Run package static checks**

Run: `uv run ruff check services/request-management && uv run mypy -p pillarmesh_request_management`

Expected: both commands exit 0.

- [ ] **Step 7: Commit the inbox boundary**

```bash
git add pyproject.toml uv.lock services/request-management
git commit -m "feat(request-management): add typed architect inbox"
```

### Task 4: Attributable conversation and exact decision binding

**Files:**
- Modify: `services/request-management/src/pillarmesh_request_management/models.py`
- Modify: `services/request-management/src/pillarmesh_request_management/repository.py`
- Modify: `services/request-management/src/pillarmesh_request_management/service.py`
- Modify: `services/request-management/src/pillarmesh_request_management/__init__.py`
- Create: `services/request-management/tests/test_conversation.py`

**Interfaces:**
- Consumes: Task 3 `InboxRequest` and repository revisions.
- Produces: `DecisionKind`, immutable `ConversationEntry`, `DecisionBinding`, `append_conversation()`, and `record_decision()`. These are introduced here, not in Task 3.
- Both new methods take `tenant_id` first, matching every other method in the service.

- [ ] **Step 1: Write failing attribution, stale-decision, and tenant tests**

```python
from datetime import UTC, datetime

import pytest
from pillarmesh_request_management import (
    DecisionKind,
    InboxRequest,
    RequestManagementService,
)
from pillarmesh_request_management.repository import SQLiteRequestRepository


NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


@pytest.fixture
def service() -> RequestManagementService:
    return RequestManagementService(SQLiteRequestRepository(":memory:"), clock=lambda: NOW)


def question(service: RequestManagementService, tenant_id: str = "tenant-a") -> InboxRequest:
    return service.submit_question(
        tenant_id=tenant_id,
        requester_id="finance-user",
        purpose="monthly close",
        question="What were net refunds yesterday?",
    )


def test_decision_rejects_stale_request_revision(service: RequestManagementService) -> None:
    request = question(service)
    service.append_conversation(
        "tenant-a", request.request_id, "architect-a", "Investigating governed metrics"
    )

    with pytest.raises(ValueError, match="request revision is stale"):
        service.record_decision(
            tenant_id="tenant-a",
            request_id=request.request_id,
            request_revision=request.revision,
            actor_id="architect-a",
            kind=DecisionKind.APPROVE,
            subject_digest="0" * 64,
        )


def test_conversation_and_decision_refuse_another_tenant(
    service: RequestManagementService,
) -> None:
    request = question(service)

    with pytest.raises(KeyError, match="belongs to another tenant"):
        service.append_conversation("tenant-b", request.request_id, "attacker", "note")
    with pytest.raises(KeyError, match="belongs to another tenant"):
        service.record_decision(
            tenant_id="tenant-b",
            request_id=request.request_id,
            request_revision=request.revision,
            actor_id="attacker",
            kind=DecisionKind.APPROVE,
            subject_digest="0" * 64,
        )
    assert service.get("tenant-a", request.request_id).revision == request.revision
```

- [ ] **Step 2: Run the test and verify the missing decision API**

Run: `uv run pytest services/request-management/tests/test_conversation.py -q`

Expected: import fails because `DecisionKind` is not exported.

- [ ] **Step 3: Implement append-only entries and revision-bound decisions**

```python
class ConversationEntry(ArtifactModel):
    entry_id: str
    request_id: str
    request_revision: int = Field(ge=1)
    actor_id: str
    body: str = Field(min_length=1, max_length=8000)
    created_at: datetime


class DecisionBinding(ArtifactModel):
    decision_id: str
    request_id: str
    request_revision: int = Field(ge=1)
    actor_id: str
    kind: DecisionKind
    subject_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
```

Define `DecisionKind` explicitly; the MVP needs `APPROVE = "approve"`, `REJECT = "reject"`, and `REQUEST_CHANGES = "request_changes"`. Later plans widen it rather than reusing a free-form string.

Appending a conversation increments the request revision and binds the entry to the new revision. `record_decision` first loads the latest request and rejects a stale supplied revision. Both methods resolve the request through the same tenant-scoped load as Task 3 and refuse another tenant before appending or deciding anything. Decision recording does not transition or execute the request; later fulfillment plans must consume the exact decision and subject digest.

Conversation entries and decisions are append-only alongside request revisions; no entry is edited or removed once written.

- [ ] **Step 4: Prove each denial fails for its own reason**

Relax the stale-revision check and the tenant check in turn; confirm the matching test fails on its own assertion each time; restore and confirm green.

Expected: both denials observed failing once, and a green run afterwards.

- [ ] **Step 5: Run request-management tests and static checks**

Run: `uv run pytest services/request-management/tests -q && uv run ruff check services/request-management && uv run mypy -p pillarmesh_request_management`

Expected: all commands exit 0.

- [ ] **Step 6: Commit attributable inbox decisions**

```bash
git add services/request-management
git commit -m "feat(request-management): bind inbox decisions to revisions"
```

### Task 5: Cross-service architect foundation acceptance

**Files:**
- Create: `tests/end-to-end/test_data_architect_foundation.py`
- Modify: `docs/architecture/decisions/ADR-0003-managed-data-engineering-platform.md`
- Modify: `docs/architecture/repository-layout.md`

**Interfaces:**
- Consumes: Tasks 1-4 public service interfaces.
- Produces: executable proof that warehouse setup, process upload, and both inbox request types share stable tenant and artifact identities without activating external effects.

- [ ] **Step 1: Write the failing cross-service journey**

```python
from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_contract_service import (
    BusinessProcessManifest,
    ProcessPackageReceipt,
    ProcessPackageService,
)
from pillarmesh_contract_service.process_service import SQLiteProcessPackageRepository
from pillarmesh_request_management import RequestManagementService
from pillarmesh_request_management.repository import SQLiteRequestRepository
from pillarmesh_warehouse_control import (
    EngineKind,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseControlService,
)
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository


NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


@pytest.fixture
def warehouse_service() -> WarehouseControlService:
    return WarehouseControlService(SQLiteWarehouseRepository(":memory:"), clock=lambda: NOW)


@pytest.fixture
def process_service() -> ProcessPackageService:
    return ProcessPackageService(SQLiteProcessPackageRepository(":memory:"), clock=lambda: NOW)


MARKDOWN = "text/markdown; charset=utf-8"


@pytest.fixture
def request_service() -> RequestManagementService:
    return RequestManagementService(SQLiteRequestRepository(":memory:"), clock=lambda: NOW)


def test_architect_establishes_control_plane_and_receives_work(
    warehouse_service: WarehouseControlService,
    process_service: ProcessPackageService,
    request_service: RequestManagementService,
) -> None:
    binding = create_ready_clickhouse_binding(warehouse_service, tenant_id="tenant-a")
    package = upload_revenue_process(process_service, tenant_id="tenant-a")
    question = request_service.submit_question(
        tenant_id="tenant-a",
        requester_id="finance-user",
        purpose="monthly close",
        question="What were net refunds yesterday?",
    )
    access = request_service.submit_access_request(
        tenant_id="tenant-a",
        requester_id="analyst-a",
        purpose="refund investigation",
        data_product_id="finance-revenue",
        requested_fields=("invoice_id", "refund_amount"),
        access_mode="query",
        expires_at=NOW + timedelta(days=7),
    )

    assert binding.lifecycle_state is WarehouseBindingState.READY
    assert package.tenant_id == question.tenant_id == access.tenant_id == "tenant-a"
    assert [item.payload.request_type for item in request_service.list_inbox("tenant-a")] == [
        "stakeholder_question",
        "data_access",
    ]


def test_a_second_tenant_sees_none_of_the_first_tenants_control_plane(
    warehouse_service: WarehouseControlService,
    process_service: ProcessPackageService,
    request_service: RequestManagementService,
) -> None:
    binding = create_ready_clickhouse_binding(warehouse_service, tenant_id="tenant-a")
    package = upload_revenue_process(process_service, tenant_id="tenant-a")
    question = request_service.submit_question(
        tenant_id="tenant-a",
        requester_id="finance-user",
        purpose="monthly close",
        question="What were net refunds yesterday?",
    )

    # Holding a valid identifier is not authority. Every boundary refuses on tenant.
    assert request_service.list_inbox("tenant-b") == ()
    with pytest.raises(KeyError, match="belongs to another tenant"):
        warehouse_service.get("tenant-b", binding.binding_id)
    with pytest.raises(KeyError, match="belongs to another tenant"):
        process_service.get_original("tenant-b", package.package_id, package.version)
    with pytest.raises(KeyError, match="belongs to another tenant"):
        request_service.get("tenant-b", question.request_id)
```

- [ ] **Step 2: Run the cross-service test and confirm fixture helpers are absent**

Run: `uv run pytest tests/end-to-end/test_data_architect_foundation.py -q`

Expected: test execution fails with `NameError` because the local acceptance helpers have not been added.

- [ ] **Step 3: Add explicit local fixtures and architecture decision detail**

Implement the helpers in the test using only the public APIs defined in Tasks 1-3:

```python
def create_ready_clickhouse_binding(
    service: WarehouseControlService, tenant_id: str
) -> WarehouseBinding:
    binding = service.create_draft(
        tenant_id=tenant_id,
        engine_kind=EngineKind.CLICKHOUSE,
        region="us-west",
        capacity_profile="mvp-fixed",
    )
    for state in (
        WarehouseBindingState.PROVISIONING,
        WarehouseBindingState.VALIDATING,
        WarehouseBindingState.READY,
    ):
        binding = service.transition(
            tenant_id, binding.binding_id, state, expected_revision=binding.revision
        )
    return binding


def upload_revenue_process(service: ProcessPackageService, tenant_id: str) -> ProcessPackageReceipt:
    manifest = BusinessProcessManifest(
        process_name="revenue-to-cash",
        owner="finance-data-owner",
        participants=("customer", "finance"),
        outcomes=("recognized-revenue",),
        entities=("Customer", "Invoice", "Payment", "Refund"),
        events=("invoice-issued", "payment-settled", "refund-issued"),
        states=("invoice-open", "invoice-paid", "invoice-refunded"),
        rules=("refund does not exceed settled payment",),
        source_references=("postgresql.billing", "stripe"),
        unresolved_questions=("refund exception owner",),
    )
    return service.upload(tenant_id, b"# Revenue to cash\n", MARKDOWN, manifest, "architect-a")
```

Use the injected service fixtures from Step 1 with their `:memory:` SQLite adapters and fixed `NOW` clock. Update ADR-0003 to record three decisions: domain services use injected repositories with standard-library SQLite reference adapters; every service and repository method that names a durable object takes `tenant_id` and refuses another tenant's object; and durable state is append-only per `(object_id, revision)` with caller-supplied `expected_revision` on every mutation. Update the repository layout descriptions to name process-package intake, stakeholder questions, access requests, and warehouse-binding lifecycle explicitly.

`services/warehouse-control` and `services/request-management` are already allowlisted in `tests/repository-structure/validate.sh` and described in `docs/architecture/repository-layout.md`, so this plan adds no top-level or governed component and must not edit the validator's component lists.

- [ ] **Step 4: Run focused and complete offline verification**

Run:

```bash
uv sync --locked --all-packages
uv lock --check
uv run pytest tests/end-to-end/test_data_architect_foundation.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
./tests/repository-structure/test.sh
```

Expected: every command exits 0; no test is skipped or xfailed as evidence for this plan.

- [ ] **Step 5: Review external-effect boundaries**

Match imports and calls, not vocabulary. A bare `clickhouse` matches `EngineKind.CLICKHOUSE = "clickhouse"`, which is the very contract this plan is supposed to define, and a bare `grant` matches request-payload nouns:

```bash
rg -n --pcre2 \
  '^\s*(import|from)\s+(psycopg|clickhouse\w*|requests|httpx|urllib|socket|boto3|subprocess)\b|\b(subprocess|httpx|requests)\.\w+\(|\b(CREATE|DROP|ALTER)\s+(ROLE|DATABASE|USER|SCHEMA)\b|\bGRANT\s+\w+\s+ON\b' \
  services/warehouse-control services/request-management \
  services/contract/src/pillarmesh_contract_service/process_*.py
```

Expected: no match. `sqlite3` is the only permitted durable-storage import, and `EngineKind`, `access_mode`, and request-type nouns remain free to name deferred behavior.

Then confirm the storage import is the expected one:

Run:

```bash
rg -n '^\s*import sqlite3' \
  services/warehouse-control/src \
  services/request-management/src \
  services/contract/src/pillarmesh_contract_service/process_service.py
```

Expected: exactly the three repository modules.

- [ ] **Step 6: Commit the foundation acceptance slice**

```bash
git add tests/end-to-end/test_data_architect_foundation.py docs/architecture/decisions/ADR-0003-managed-data-engineering-platform.md docs/architecture/repository-layout.md
git commit -m "test: prove data architect control-plane foundation"
```

## Plan completion gate

Plan 1 is complete only when all of the following hold:

- the cross-service test demonstrates a ready logical binding, immutable process-package receipt, typed stakeholder question, and typed access request for the same tenant;
- a second tenant holding valid identifiers is refused by all three boundaries, proven in the same cross-service test;
- every denial assertion in Tasks 1 through 4 has been observed failing for its own reason before its rule existed, not merely as an import error;
- two drafts for one tenant receive distinct binding identities under the frozen clock;
- every mutation is append-only and every stale `expected_revision` is refused;
- all offline gates pass with no test skipped or xfailed as evidence;
- the external-effect review matches nothing, and `sqlite3` is the only durable-storage import; and
- the decisions recorded under "Spec alignment" — request terminal reachability, the `WarehouseBinding` field shape, and the warehouse transition table — are ratified into `managed-data-engineering-platform-addendum-v0.1.md` before Plan 2 begins. **Done:** addendum sections 13.3.1, 6.2, and 6.4.1.
