# Plan 3A Managed Warehouse Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision, validate, suspend, resume, back up, restore, retire, and exactly reconcile
dedicated PostgreSQL and ClickHouse warehouses through one tenant-scoped, provider-neutral,
evidence-gated lifecycle.

**Architecture:** `services/warehouse-control` owns immutable binding state, readiness policy,
operation claims, private resource records, and sanitized evidence. Engine mechanisms stay in
`providers/postgresql` and `providers/clickhouse`, while a characterized Docker Compose process
boundary moves into `packages/provider-sdk`. PostgreSQL reaches the common contract first;
ClickHouse then proves destination portability against the same observable outcomes.

**Tech Stack:** Python 3.13.5, frozen Pydantic v2 artifacts, SQLite, psycopg 3, httpx, cryptography,
Docker Compose, PostgreSQL 18.6, ClickHouse 25.8.32.4 LTS, pytest, Hypothesis, Ruff, strict mypy,
GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-24-plan-3a-managed-warehouse-lifecycle-design.md`

## Global Constraints

- Read `AGENTS.md`, the spec above, the managed-platform addendum, ADR-0003, and the owning code
  before each task.
- Transition a draft binding to `provisioning` before claiming an operation. Every claim carries
  the resulting `binding_revision`.
- Public artifacts contain no endpoint, credential, infrastructure identifier, backup path, raw
  canary, subprocess output, or driver text.
- Private resources are recorded as `planned` before creation, including every restore resource.
- IDs derive from a domain tag, tenant ID, and repository sequence; clocks and random values never
  contribute to durable identity.
- The default readiness policy is production and requires proven storage encryption. Only the
  acceptance harness may inject the local policy that admits `deferred_local_acceptance`.
- Plain lifecycle transitions never enter `ready` or `retired`; atomic evidence-admission methods
  own those revisions. The single exception is `draft -> retired`, which abandons a binding that
  was never provisioned: it has no operation, no resources, and therefore no retirement evidence to
  admit, so `abandon_draft` owns that revision instead.
- A transient, throttled, or ambiguous provider failure never records a terminal binding failure.
- A backup encryption key is retained for at least as long as every artifact it encrypts. Operation
  secrets are never deleted while any resource they protect is `retained`, and verified deletion
  removes the artifact and its key together.
- PostgreSQL and ClickHouse implement the same public protocols and conformance outcomes, not the
  same SQL or physical backup mechanism.
- The seven principal classes are administration, ingestion runtime, transformation runtime,
  backup and restore, customer SQL, catalog, and BI. Backup reads are constrained by private
  credential resolution because neither engine can distinguish backup reads from equivalent SQL.
- A test double proves orchestration only. Every engine, grant, TLS, monitoring, backup, restore,
  and cleanup claim remains unproven until the required live suite succeeds.
- Local Compose evidence is lifecycle-conformance evidence, never production-cloud readiness.
- Backup and restore payloads stream. No code path holds a whole dump, its ciphertext, or its
  plaintext in memory; the control plane moves bytes between the engine process and an encrypted
  file in bounded chunks.
- Use PostgreSQL image
  `postgres:18.6-bookworm@sha256:33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935`.
  PostgreSQL's [official version feed](https://www.postgresql.org/versions.json) marks 18.6
  current and supported through 2030-11-14; the
  [official image record](https://hub.docker.com/v2/repositories/library/postgres/tags/18.6-bookworm)
  supplies the manifest digest.
- Use ClickHouse image
  `clickhouse/clickhouse-server:25.8.32.4@sha256:7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0`.
  The [official release](https://github.com/ClickHouse/ClickHouse/releases/tag/v25.8.32.4-lts)
  is `v25.8.32.4-lts`; the
  [official image record](https://hub.docker.com/v2/repositories/clickhouse/clickhouse-server/tags/25.8.32.4)
  supplies the manifest digest.
- Preserve multi-architecture manifest pins. The required hosted gate runs Linux/amd64; local
  Apple Silicon resolves the same manifest to Linux/arm64.
- Follow RED → GREEN for every behavior. Prove each regression test against the relaxed or absent
  rule before accepting it.
- End every task with a focused review and one Conventional Commit. Do not combine review fixes
  from one task with the next task.

## File and ownership map

- `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md`:
  normative principal, artifact, lifecycle, readiness, retention, and conformance contracts.
- `packages/provider-sdk/src/pillarmesh_provider_sdk/compose.py`: reusable sanitized Compose
  process boundary only.
- `services/warehouse-control/src/pillarmesh_warehouse_control/models.py`: binding and closed
  public vocabularies.
- `services/warehouse-control/src/pillarmesh_warehouse_control/evidence.py`: public validation,
  resume, restore, and retirement artifacts.
- `services/warehouse-control/src/pillarmesh_warehouse_control/private_state.py`: private operation,
  claim, resource, phase, and cleanup models.
- `services/warehouse-control/src/pillarmesh_warehouse_control/errors.py`: persistence, admission,
  and provider-boundary error types.
- `services/warehouse-control/src/pillarmesh_warehouse_control/protocols.py`: six-operation
  provider-neutral lifecycle protocol and secret/resource recorder boundaries.
- `services/warehouse-control/src/pillarmesh_warehouse_control/readiness.py`: explicit production
  and local acceptance admission policies.
- `services/warehouse-control/src/pillarmesh_warehouse_control/repository.py`: append-only bindings,
  operation sequences and claims, exact private resources, and atomic evidence admission.
- `services/warehouse-control/src/pillarmesh_warehouse_control/service.py`: legal transitions and
  evidence-gated ready/retired revisions.
- `services/warehouse-control/src/pillarmesh_warehouse_control/orchestration.py`: state-to-provider
  sequencing, retry classification, and recovery.
- `services/warehouse-control/src/pillarmesh_warehouse_control/secrets.py`: encrypted private
  seven-principal operation-secret storage and command-boundary resolution.
- `providers/postgresql/src/pillarmesh_provider_postgresql/warehouse.py`: PostgreSQL lifecycle,
  grants, probes, backup, restore, and resource reconciliation.
- `providers/clickhouse/`: ClickHouse package implementing the same warehouse lifecycle contract.
- `tests/emulators/warehouses/`: pinned Compose files, TLS setup, readiness scripts, and local live
  entry points.
- `tests/conformance/warehouse_lifecycle.py`: reusable observable provider contract.
- `tests/acceptance/plan3a_orchestration.py` and `tests/acceptance/run_plan3a.py`: witnessed
  two-engine journey and sanitized evidence.
- `docs/plan3a/`: setup, acceptance, evidence, and teardown runbooks.
- `.github/workflows/warehouse-lifecycle.yml`: always-present required gate with path-filtered live
  execution.

---

### Task 1: Ratify public warehouse contracts and conformance locks

**Files:**
- Modify: `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md:181-238`
- Modify: `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md:848-859`
- Modify: `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md:1062-1080`
- Create: `services/warehouse-control/src/pillarmesh_warehouse_control/evidence.py`
- Modify: `services/warehouse-control/src/pillarmesh_warehouse_control/models.py:1-47`
- Modify: `services/warehouse-control/src/pillarmesh_warehouse_control/__init__.py:1-9`
- Modify: `services/warehouse-control/tests/test_models.py`
- Modify: `tests/conformance/test_specification_conformance.py:15-247`

**Interfaces:**
- Consumes: current `WarehouseBinding`, `WarehouseBindingState`, `EngineKind`, and addendum sections
  5.1, 6.2-6.5, 18, 20.6, 20.9, and 21.
- Produces: `WarehousePrincipalClass`, `WarehouseValidationProfile`,
  `EncryptionAtRestDisposition`, `WarehouseFailureClassification`,
  `WarehouseValidationEvidence`, `WarehouseResumeValidationEvidence`,
  `WarehouseRestoreVerification`, and `WarehouseRetirementEvidence`.

- [ ] **Step 1: Write conformance and strict-model failures**

Add tests that compare every documented field with `model_fields`, compare the seven documented
principal values with the enum, and reject naive timestamps, unknown fields, invalid digests,
negative retirement counts, a production profile with deferred encryption, and a local profile
claiming proven production encryption.

```python
def test_warehouse_principal_classes_match_section_18() -> None:
    documented = _fenced_fields("### 18.1")
    implemented = [member.value for member in WarehousePrincipalClass]
    assert documented == implemented


def test_production_validation_cannot_defer_storage_encryption() -> None:
    payload = _validation_payload(
        validation_profile="production",
        encryption_at_rest_disposition="deferred_local_acceptance",
    )
    with pytest.raises(ValidationError):
        WarehouseValidationEvidence.model_validate(payload)
```

- [ ] **Step 2: Verify the conformance RED for the intended reason**

Run:

```bash
uv run pytest services/warehouse-control/tests/test_models.py \
  tests/conformance/test_specification_conformance.py -q
```

Expected: collection fails because the new enums and artifacts are absent. After adding temporary
empty definitions, relax one profile/encryption validator and require the production-denial test to
fail before restoring the rule.

- [ ] **Step 3: Add the closed vocabularies and public artifacts**

Implement strict frozen artifacts with these exact enum values and field order:

```python
class WarehousePrincipalClass(StrEnum):
    ADMINISTRATION = "administration"
    INGESTION_RUNTIME = "ingestion_runtime"
    TRANSFORMATION_RUNTIME = "transformation_runtime"
    BACKUP_RESTORE = "backup_restore"
    CUSTOMER_SQL = "customer_sql"
    CATALOG = "catalog"
    BI = "bi"


class WarehouseValidationProfile(StrEnum):
    LOCAL_ACCEPTANCE = "local_acceptance"
    PRODUCTION = "production"


class EncryptionAtRestDisposition(StrEnum):
    PROVEN = "proven"
    DEFERRED_LOCAL_ACCEPTANCE = "deferred_local_acceptance"


class WarehouseFailureClassification(StrEnum):
    TRANSIENT_TRANSPORT = "transient_transport"
    TRANSIENT_UNAVAILABLE = "transient_unavailable"
    THROTTLED = "throttled"
    AMBIGUOUS_OUTCOME = "ambiguous_outcome"
    AUTHORIZATION_DENIED = "authorization_denied"
    STATEMENT_REJECTED = "statement_rejected"
    INVALID_PROVIDER_RESPONSE = "invalid_provider_response"
    INTEGRITY_FAILURE = "integrity_failure"
    PERMANENT_CONFIGURATION = "permanent_configuration"
```

Add the four artifacts with exactly the fields in design sections 6.1-6.4. Use one
`_DIGEST_PATTERN`, UTC validators, `Field(ge=0)` for counts, and a model validator that permits only
`production/proven` or `local_acceptance/deferred_local_acceptance` pairings.

- [ ] **Step 4: Amend the addendum with exact tables and admission semantics**

Document:

- all artifact field blocks;
- the seven principal classes and enforceable grant/command boundaries, as a new `### 18.1`
  subsection whose fenced `text` block lists the seven values one per line in enum order, so
  `_fenced_fields` can compare the documented list with the enum by equality;
- the six provider operations;
- evidence-gated `ready` and `retired` transitions;
- retention-aware retirement;
- local versus production storage-encryption policy;
- fresh resume probes;
- monitoring and fixed-capacity alerts; and
- the explicit ClickHouse exclusions.

Do not change the existing `WarehouseBinding` fields or version-free capability digest.

- [ ] **Step 5: Lock every amended contract to code**

Extend conformance helpers to parse the new fenced blocks and tables. Assert exact field order,
enum values, lifecycle gates, provider operations, principal names, readiness conditions,
retirement meaning, and ClickHouse exclusions. Add a mutation check by changing one documented
field in a temporary copy and requiring the parser comparison to fail.

- [ ] **Step 6: Verify Task 1**

Run:

```bash
uv run pytest services/warehouse-control/tests/test_models.py \
  tests/conformance/test_specification_conformance.py -q
uv run ruff check services/warehouse-control tests/conformance
uv run ruff format --check services/warehouse-control tests/conformance
uv run mypy
```

Expected: all commands pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md \
  services/warehouse-control tests/conformance/test_specification_conformance.py
git commit -m "feat(warehouse-control): ratify managed lifecycle contracts"
```

---

### Task 2: Promote the characterized Docker Compose boundary

**Files:**
- Create: `packages/provider-sdk/src/pillarmesh_provider_sdk/compose.py`
- Create: `packages/provider-sdk/tests/test_compose.py`
- Modify: `packages/provider-sdk/src/pillarmesh_provider_sdk/__init__.py:1-26`
- Modify: `providers/openmetadata/pyproject.toml:1-19`
- Modify: `providers/openmetadata/src/pillarmesh_provider_openmetadata/provisioner.py:73-124`
- Modify: `providers/openmetadata/src/pillarmesh_provider_openmetadata/provisioner.py:342-635`
- Modify: `providers/openmetadata/tests/test_provisioner.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: existing OpenMetadata `DockerComposeController`, its injected `ComposeController`
  protocol, and current provisioner tests.
- Produces: `ComposeResourceKind`, `ComposeResource`, `ComposeErrorClassification`,
  `ComposeCommandError`, and `DockerComposeProcess` with `up`, `stop`, `start`, `down`,
  `planned_resources`, `discover_resources`, `remove_resource`, `resource_is_absent`, `exec`, and
  `inspect_container_image` methods.

- [ ] **Step 1: Characterize the existing OpenMetadata process behavior**

Before moving code, add tests around the existing controller for:

- exact `docker compose --project-name --file` command order;
- explicit Docker environment allowlist;
- no inherited provider secrets;
- project-label discovery for containers, volumes, and networks;
- absent-resource cleanup idempotency;
- unknown inspection outcome returning `None`;
- interrupt propagation; and
- errors containing a stable sanitized message without captured stdout, stderr, paths, or values.

Run the tests and require them to pass against the existing implementation.

- [ ] **Step 2: Write the provider-SDK export RED**

```python
def test_provider_sdk_exports_compose_process_boundary() -> None:
    from pillarmesh_provider_sdk import DockerComposeProcess

    assert DockerComposeProcess
```

Run `uv run pytest packages/provider-sdk/tests/test_compose.py -q`.

Expected: FAIL because `compose.py` and the export do not exist.

- [ ] **Step 3: Implement the minimal shared process boundary**

Use immutable models and an injected command runner:

```python
type ComposeResourceKind = Literal["container", "volume", "network"]


@dataclass(frozen=True, slots=True)
class ComposeResource:
    resource_kind: ComposeResourceKind
    identifier: str


class DockerComposeProcess:
    def __init__(
        self,
        *,
        compose_file: Path,
        run: CommandRunner = subprocess.run,
    ) -> None: ...

    def exec(
        self,
        *,
        project_name: str,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
        input_bytes: bytes | None = None,
    ) -> bytes: ...

    @contextmanager
    def exec_stream(
        self,
        *,
        project_name: str,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
        stdin: IO[bytes] | None = None,
    ) -> Iterator[IO[bytes]]: ...
```

`exec` is for bounded output only: readiness checks, inspection, and single-row probes. Anything
whose size scales with customer data uses `exec_stream`, which yields the child process's stdout as
a readable pipe and never accumulates it. Cap `exec` output explicitly and raise a closed
classification when a caller exceeds the cap, so a large payload cannot silently arrive through the
buffered path. Both forms propagate interrupts and reap the process on exit.

The class owns process invocation and resource inspection only. It does not own readiness,
database backup, database restore, search rebuild, or provider error policy. Map launch/daemon
unavailability, timeout, nonzero deterministic rejection, and post-launch ambiguity to a closed
`ComposeErrorClassification`; never include raw process output in the exception.

- [ ] **Step 4: Delegate OpenMetadata mechanisms to the shared class**

Keep the exported OpenMetadata `DockerComposeController` as a provider wrapper so existing callers
do not change. Replace its generic command, resource planning/discovery, exact removal, and image
inspection code with `DockerComposeProcess`. Retain MySQL dump/restore, readiness, and search rebuild
inside the OpenMetadata provider.

Add `pillarmesh-provider-sdk` as a workspace dependency and regenerate the lock with `uv lock`.

- [ ] **Step 5: Prove no behavior drift and no accidental abstraction**

Run:

```bash
uv run pytest packages/provider-sdk/tests/test_compose.py \
  providers/openmetadata/tests/test_provisioner.py -q
uv run ruff check packages/provider-sdk providers/openmetadata
uv run ruff format --check packages/provider-sdk providers/openmetadata
uv run mypy
```

Expected: all existing OpenMetadata lifecycle tests plus the new SDK tests pass. Review the shared
file and remove any method used only by OpenMetadata database semantics.

- [ ] **Step 6: Commit Task 2**

```bash
git add packages/provider-sdk providers/openmetadata uv.lock
git commit -m "refactor(provider-sdk): share compose process boundary"
```

---

### Task 3: Add replay-safe warehouse private state and atomic evidence persistence

**Files:**
- Create: `services/warehouse-control/src/pillarmesh_warehouse_control/private_state.py`
- Create: `services/warehouse-control/src/pillarmesh_warehouse_control/errors.py`
- Modify: `services/warehouse-control/src/pillarmesh_warehouse_control/repository.py:1-108`
- Modify: `services/warehouse-control/src/pillarmesh_warehouse_control/__init__.py`
- Create: `services/warehouse-control/tests/test_private_state.py`
- Modify: `services/warehouse-control/tests/test_repository.py`

**Interfaces:**
- Consumes: Task 1 public artifacts and current append-only `WarehouseBinding` storage.
- Produces: `WarehouseOperationKind`, `WarehouseOperationStatus`, `WarehouseOperationPhase`,
  `WarehouseResourceKind`, `WarehouseResourceCreationState`, `WarehouseResourceCleanupStatus`,
  `PrivateWarehouseOperation`, `PrivateWarehouseResource`, `WarehousePersistenceError`,
  `WarehouseOperationConflictError`, `WarehouseValidationConflictError`,
  `WarehouseAdmissionError`, and repository methods listed below.

- [ ] **Step 1: Write private-model and migration failures**

Test strict nonempty identities, UTC timestamps, binding revision ownership, closed resource kinds,
retention deadlines, cleanup failure requirements, schema creation with foreign keys enabled, and
reopening a database without losing earlier binding rows.

Define the resource vocabulary exactly as:

```python
class WarehouseResourceKind(StrEnum):
    COMPOSE_PROJECT = "compose_project"
    WAREHOUSE_CONTAINER = "warehouse_container"
    PRIVATE_NETWORK = "private_network"
    WAREHOUSE_DATA_VOLUME = "warehouse_data_volume"
    CREDENTIAL_FILE = "credential_file"
    TLS_PRIVATE_KEY = "tls_private_key"
    TLS_CERTIFICATE = "tls_certificate"
    BACKUP_ARTIFACT = "backup_artifact"
    RESTORE_COMPOSE_PROJECT = "restore_compose_project"
    RESTORE_CONTAINER = "restore_container"
    RESTORE_PRIVATE_NETWORK = "restore_private_network"
    RESTORE_DATA_VOLUME = "restore_data_volume"
```

Use these exact lifecycle vocabularies:

```python
class WarehouseOperationKind(StrEnum):
    PROVISION = "provision"
    SUSPEND = "suspend"
    RESUME = "resume"
    RETIRE = "retire"


class WarehouseOperationStatus(StrEnum):
    CLAIMED = "claimed"
    RUNNING = "running"
    RECONCILING = "reconciling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WarehouseOperationPhase(StrEnum):
    CLAIMED = "claimed"
    RESOURCES_PLANNED = "resources_planned"
    PROVIDER_CREATED = "provider_created"
    VALIDATING = "validating"
    BACKUP_PLANNED = "backup_planned"
    BACKUP_CREATED = "backup_created"
    RESTORE_PLANNED = "restore_planned"
    RESTORE_CREATED = "restore_created"
    RESTORE_VERIFIED = "restore_verified"
    RESTORE_CLEANED = "restore_cleaned"
    VALIDATED = "validated"
    SUSPENDED = "suspended"
    RESUMED = "resumed"
    RETIREMENT_DISPOSITION_RECORDED = "retirement_disposition_recorded"
    RETIRED = "retired"


class WarehouseResourceCreationState(StrEnum):
    PLANNED = "planned"
    CREATED = "created"
    AMBIGUOUS = "ambiguous"
    ABSENT = "absent"


class WarehouseResourceCleanupStatus(StrEnum):
    PENDING = "pending"
    RETAINED = "retained"
    COMPLETE = "complete"
    FAILED = "failed"
```

Define the control-plane errors before repository implementation so provider, persistence, and
admission failures cannot be flattened into one retry decision:

```python
class WarehousePersistenceError(RuntimeError):
    pass


class WarehouseOperationConflictError(WarehousePersistenceError):
    pass


class WarehouseValidationConflictError(WarehousePersistenceError):
    pass


class WarehouseAdmissionError(RuntimeError):
    pass
```

`WarehousePersistenceError` wraps storage and transaction failures without leaking SQLite driver
types. The two conflict subclasses represent durable competing claims rather than provider
failures. `WarehouseAdmissionError` represents deterministic evidence-policy denial and is never
retryable without changed evidence or policy.

- [ ] **Step 2: Verify private-state RED and denial RED**

Run `uv run pytest services/warehouse-control/tests/test_private_state.py
services/warehouse-control/tests/test_repository.py -q`.

Expected: collection fails for absent private models. After models import, temporarily omit the
binding revision from claim comparison and require the stale-claim test to fail.

- [ ] **Step 3: Create versioned SQLite private tables**

Add:

```text
warehouse_operation_sequences(tenant_id, next_sequence)
private_warehouse_operation_claims(tenant_id, binding_id, binding_revision, operation_id)
private_warehouse_operations(tenant_id, binding_id, binding_revision, operation_id, payload)
private_warehouse_resources(tenant_id, binding_id, binding_revision, operation_id, resource_id, payload)
warehouse_validation_evidence(tenant_id, binding_id, binding_revision, evidence_id, payload)
warehouse_resume_validation_evidence(tenant_id, binding_id, binding_revision, evidence_id, payload)
warehouse_restore_verifications(tenant_id, binding_id, binding_revision, verification_id, payload)
warehouse_retirement_evidence(tenant_id, binding_id, binding_revision, evidence_id, payload)
```

Every private table carries a composite foreign key to the exact `(tenant_id, binding_id, revision)`
that owns it. SQLite resolves a composite foreign key only against a parent key that has a unique
index, and the existing `warehouse_bindings` table is `PRIMARY KEY (binding_id, revision)` with no
`tenant_id`, so with `PRAGMA foreign_keys = ON` already enabled the first child insert would fail
with `foreign key mismatch`. Do not change that primary key; binding history must not be rewritten.
Instead the additive migration first runs:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS warehouse_bindings_tenant_revision
    ON warehouse_bindings (tenant_id, binding_id, revision);
```

Add a test that opens a database written by the current code, applies the migration, inserts a
claim, and asserts the foreign key is enforced rather than mismatched. Guard the whole migration by
a schema version and checksum, and preserve every existing row.

- [ ] **Step 4: Implement exact repository contracts**

Extend `WarehouseRepository` with:

```python
def next_operation_sequence(self, tenant_id: str) -> int: ...
def claim_operation(self, operation: PrivateWarehouseOperation) -> bool: ...
def load_live_operation(
    self, tenant_id: str, binding_id: str
) -> PrivateWarehouseOperation | None: ...
def load_operation(
    self, tenant_id: str, binding_id: str, operation_id: str
) -> PrivateWarehouseOperation: ...
def save_operation(self, operation: PrivateWarehouseOperation) -> None: ...
def record_resources(self, resources: tuple[PrivateWarehouseResource, ...]) -> None: ...
def save_resource(self, resource: PrivateWarehouseResource) -> None: ...
def load_resources(
    self, tenant_id: str, binding_id: str
) -> tuple[PrivateWarehouseResource, ...]: ...
def record_initial_validation(
    self,
    binding: WarehouseBinding,
    evidence: WarehouseValidationEvidence,
    restore: WarehouseRestoreVerification,
    *,
    expected_revision: int,
) -> None: ...
def record_resume_validation(
    self,
    binding: WarehouseBinding,
    evidence: WarehouseResumeValidationEvidence,
    *,
    expected_revision: int,
) -> None: ...
def record_retirement(
    self,
    binding: WarehouseBinding,
    evidence: WarehouseRetirementEvidence,
    *,
    expected_revision: int,
) -> None: ...
```

Each method starts with a tenant-qualified lookup. `claim_operation` is idempotent only for the same
operation and revision; a different live claim raises `WarehouseOperationConflictError`. Atomic
evidence methods insert the new binding revision and all evidence rows in one transaction.

`load_live_operation` is what makes the claim recoverable rather than fatal. A claim is durable
before any provider effect, so a process that dies immediately after claiming leaves a record whose
`operation_id` the retry cannot recompute: `next_operation_sequence` advances on every call, so a
fresh derivation would mint a different identifier, collide with the stored claim, and strand the
binding in `provisioning`, whose only exits are `validating` and `failed`. `load_live_operation`
returns the single operation for `(tenant_id, binding_id)` whose status is `claimed`, `running`, or
`reconciling`, or `None` when there is none. Enforce at most one such row per binding with a partial
unique index, so the invariant is held by the schema and not by caller discipline.

- [ ] **Step 5: Add transaction, replay, and cross-tenant tests**

Cover:

- two callers claiming the same binding with different operation IDs;
- `load_live_operation` returning the stored claim after a simulated crash, and the resumed
  orchestrator adopting it instead of allocating a new sequence;
- `load_live_operation` returning `None` once the operation reaches `succeeded` or `failed`;
- the partial unique index rejecting a second live operation for one binding;
- replaying the same operation;
- an old revision attempting to claim after the binding advances;
- evidence ID, binding, tenant, revision, and digest mismatch;
- injected failures after the binding insert and after each evidence insert;
- another tenant loading an operation, resource, or evidence;
- a restore resource with no recorded parent operation; and
- rollback/close failures not replacing the primary persistence error.

- [ ] **Step 6: Verify Task 3**

```bash
uv run pytest services/warehouse-control/tests/test_private_state.py \
  services/warehouse-control/tests/test_repository.py -q
uv run ruff check services/warehouse-control
uv run ruff format --check services/warehouse-control
uv run mypy
```

Expected: all commands pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add services/warehouse-control
git commit -m "feat(warehouse-control): persist managed lifecycle state"
```

---

### Task 4: Gate readiness, resume, failure, and retirement in warehouse control

**Files:**
- Create: `services/warehouse-control/src/pillarmesh_warehouse_control/readiness.py`
- Modify: `services/warehouse-control/src/pillarmesh_warehouse_control/service.py:10-185`
- Modify: `services/warehouse-control/src/pillarmesh_warehouse_control/__init__.py`
- Modify: `services/warehouse-control/tests/test_service.py`
- Create: `services/warehouse-control/tests/test_readiness.py`

**Interfaces:**
- Consumes: Task 1 public evidence and Task 3 atomic repository methods.
- Produces: `WarehouseReadinessPolicy`, `ProductionWarehouseReadinessPolicy`,
  `LocalAcceptanceWarehouseReadinessPolicy`, `record_validation`,
  `record_resume_validation`, `record_retirement`, `abandon_draft`, and
  `record_terminal_failure`.

- [ ] **Step 1: Write the four ungated-transition failures**

```python
@pytest.mark.parametrize(
    ("source", "target"),
    [
        (WarehouseBindingState.VALIDATING, WarehouseBindingState.READY),
        (WarehouseBindingState.SUSPENDED, WarehouseBindingState.READY),
        (WarehouseBindingState.RETIRING, WarehouseBindingState.RETIRED),
        (WarehouseBindingState.FAILED, WarehouseBindingState.RETIRED),
    ],
)
def test_plain_transition_refuses_evidence_gated_state(source, target): ...


def test_draft_is_abandoned_without_retirement_evidence() -> None:
    binding = service.create_draft(...)
    retired = service.abandon_draft(
        binding.tenant_id, binding.binding_id, expected_revision=binding.revision
    )
    assert retired.lifecycle_state is WarehouseBindingState.RETIRED
    assert retired.provisioned_at is None


def test_abandon_draft_refuses_a_binding_that_left_draft() -> None: ...


def test_abandon_draft_refuses_a_draft_with_a_live_operation() -> None: ...
```

`draft -> retired` is ratified in addendum section 6.4.1 and abandons a binding that was never
provisioned. It is deliberately absent from the parametrisation above: there is no operation, no
resource, and no cleanup to attest, so demanding retirement evidence for it would make the
transition unreachable. `abandon_draft` is its own narrow admission method rather than a hole in the
plain transition path, and it refuses any binding that is not in `draft` or that has a live
operation.

Add tests that initial evidence must match the current validating revision, resume evidence must
match the current suspended revision, retirement evidence must match the current retiring or failed
revision, and `provisioned_at` is set once on initial admission and preserved on resume.

- [ ] **Step 2: Verify RED against the current permissive service**

Run `uv run pytest services/warehouse-control/tests/test_service.py
services/warehouse-control/tests/test_readiness.py -q`.

Expected: the four plain transitions are accepted or the admission methods are absent. Confirm the
test fails because of the rule, not merely import failure.

- [ ] **Step 3: Implement explicit readiness policies**

```python
class WarehouseReadinessPolicy(Protocol):
    def admit(self, evidence: WarehouseValidationEvidence) -> None: ...


class ProductionWarehouseReadinessPolicy:
    def admit(self, evidence: WarehouseValidationEvidence) -> None:
        if (
            evidence.validation_profile is not WarehouseValidationProfile.PRODUCTION
            or evidence.encryption_at_rest_disposition is not EncryptionAtRestDisposition.PROVEN
        ):
            raise WarehouseAdmissionError(
                "production warehouse validation evidence is insufficient"
            )
```

The local policy admits only the exact local/deferred pair. `WarehouseControlService` defaults to
the production policy; only an explicit constructor dependency selects local acceptance.

- [ ] **Step 4: Implement atomic admission methods**

`record_validation` checks state, revision, tenant, binding, engine, profile policy, every digest,
and restore parent linkage before constructing the `ready` revision with
`provisioned_at=self._clock()`. `record_resume_validation` requires fresh evidence and preserves
the original provisioned time. `record_retirement` checks cleanup counts and the resource-inventory
digest against private repository dispositions before entering `retired`.

`abandon_draft` asserts the state is `draft`, asserts `load_live_operation` returns `None`, and
writes the `retired` revision with `provisioned_at` left null. It admits no evidence because none
exists, and it is the only path into `retired` that does not.

`record_terminal_failure` accepts only authorization, statement, invalid-response, integrity, or
permanent-configuration classifications. It rejects transient, unavailable, throttled, and
ambiguous classifications. A transient classification therefore leaves the operation resumable
rather than terminal, so an operation that cannot make progress must remain visibly unresolved:
never reclassify a transient failure as terminal to unwedge a binding.

- [ ] **Step 5: Prove admission atomicity and failure classification**

Inject repository failures and require no visible advanced binding. Parameterize all nine failure
classifications and assert exactly five may produce `failed`. Prove cross-profile, stale revision,
wrong engine, wrong tenant, wrong restore parent, reused evidence, negative cleanup count, and
resource-inventory mismatch denials.

- [ ] **Step 6: Verify Task 4**

```bash
uv run pytest services/warehouse-control/tests/test_service.py \
  services/warehouse-control/tests/test_readiness.py -q
uv run ruff check services/warehouse-control
uv run ruff format --check services/warehouse-control
uv run mypy
```

Expected: all commands pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add services/warehouse-control
git commit -m "feat(warehouse-control): gate managed lifecycle evidence"
```

---

### Task 5: Orchestrate provider operations and protect private credentials

**Files:**
- Create: `services/warehouse-control/src/pillarmesh_warehouse_control/protocols.py`
- Create: `services/warehouse-control/src/pillarmesh_warehouse_control/orchestration.py`
- Create: `services/warehouse-control/src/pillarmesh_warehouse_control/secrets.py`
- Modify: `services/warehouse-control/src/pillarmesh_warehouse_control/errors.py`
- Modify: `services/warehouse-control/src/pillarmesh_warehouse_control/__init__.py`
- Modify: `services/warehouse-control/pyproject.toml:1-15`
- Create: `services/warehouse-control/tests/test_orchestration.py`
- Create: `services/warehouse-control/tests/test_secrets.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: Tasks 3-4 repository and admission services.
- Produces: `WarehouseProvider`, `WarehouseProviderError`, `WarehouseProvisionResult`,
  `WarehouseOperationSecrets`, `WarehouseSecretStore`, `WarehouseResourceRecorder`,
  `EncryptedDirectoryWarehouseSecretStore`, and `WarehouseLifecycleOrchestrator`.

- [ ] **Step 1: Write protocol-level orchestration failures**

Use a strict fake provider that validates all arguments against real models. Test the exact order:

```text
transition provisioning
claim operation for provisioning revision
provider provision
transition validating
provider validate initial
record validation
```

Stop the fake after every edge and prove replay resumes from durable state. Reproduce the original
review failure: stop after claim, try to revise engine, resume, and require revision to remain
immutable and the provider engine to match the binding.

- [ ] **Step 2: Verify RED**

Run `uv run pytest services/warehouse-control/tests/test_orchestration.py
services/warehouse-control/tests/test_secrets.py -q`.

Expected: collection fails because protocols, orchestrator, and secret store are absent.

- [ ] **Step 3: Define the six-operation provider protocol**

```python
class WarehouseProviderError(RuntimeError):
    def __init__(
        self,
        *,
        operation: Literal["provision", "reconcile", "validate", "suspend", "resume", "retire"],
        classification: WarehouseFailureClassification,
    ) -> None:
        super().__init__(f"warehouse provider {operation} failed: {classification.value}")
        self.operation = operation
        self.classification = classification


@dataclass(frozen=True, slots=True)
class WarehouseProvisionResult:
    tenant_id: str
    binding_id: str
    binding_revision: int
    operation_id: str
    engine_kind: EngineKind
    private_resource_handle: str
    provider_build_digest: str
    resource_inventory_digest: str


class InitialWarehouseValidationResult(ArtifactModel):
    result_kind: Literal["initial"] = "initial"
    evidence: WarehouseValidationEvidence
    restore_verification: WarehouseRestoreVerification


class ResumeWarehouseValidationResult(ArtifactModel):
    result_kind: Literal["resume"] = "resume"
    evidence: WarehouseResumeValidationEvidence


type WarehouseValidationResult = Annotated[
    InitialWarehouseValidationResult | ResumeWarehouseValidationResult,
    Field(discriminator="result_kind"),
]


class WarehouseResourceRecorder(Protocol):
    def record_planned(self, resource: PrivateWarehouseResource) -> None: ...
    def mark_created(
        self,
        tenant_id: str,
        resource_id: str,
        provider_resource_handle: str,
    ) -> None: ...
    def mark_ambiguous(self, tenant_id: str, resource_id: str) -> None: ...
    def record_cleanup(
        self,
        tenant_id: str,
        resource_id: str,
        status: WarehouseResourceCleanupStatus,
        classification: WarehouseFailureClassification | None,
    ) -> None: ...


class WarehouseProvider(Protocol):
    engine_kind: EngineKind

    def provision(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult: ...
    def reconcile(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult: ...
    def validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> WarehouseValidationResult: ...
    def suspend(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None: ...
    def resume(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None: ...
    def retire(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseRetirementEvidence: ...
```

Provider calls raise only `WarehouseProviderError` with the closed classification from Task 1.

- [ ] **Step 4: Implement deterministic operation identity and orchestration**

Derive IDs as:

```python
existing = repository.load_live_operation(tenant_id, binding_id)
if existing is not None:
    operation = existing
else:
    operation_id = (
        "wop-"
        + digest(
            {
                "domain": "pillarmesh-warehouse-operation-v1",
                "tenant_id": tenant_id,
                "sequence": repository.next_operation_sequence(tenant_id),
            }
        )[:24]
    )
```

Allocate a sequence only after `load_live_operation` reports no live operation. Because the sequence
advances on every call, deriving an identifier first and discovering the stored claim second would
guarantee a mismatch on exactly the crash the claim exists to survive.

The orchestrator transitions first, then claims. A live operation for the current binding revision
is adopted and resumed from its durable phase; a live operation recorded against an older revision
is reconciled and closed before a new one is claimed; a missing claim on an already-provisioning
revision is recoverable. Same-operation replay calls
`reconcile` before any repeated effect. Transient failures leave the operation resumable; terminal
classifications go through `record_terminal_failure`; ambiguity always reconciles.

- [ ] **Step 5: Implement encrypted private operation secrets**

Define this exact strict frozen model:

```python
class WarehouseOperationSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    administration_password: SecretStr
    ingestion_runtime_password: SecretStr
    transformation_runtime_password: SecretStr
    backup_restore_password: SecretStr
    customer_sql_probe_password: SecretStr
    catalog_password: SecretStr
    bi_password: SecretStr
    tls_private_key_pem: SecretStr
    tls_certificate_pem: SecretStr
    backup_encryption_key_b64: SecretStr
```

The encrypted directory is `0700`; files are `0600`; names derive only from `operation_id`; the
Fernet key is injected and never persisted beside ciphertext. Reject symlinks, traversal, wrong
permissions, malformed ciphertext, unknown fields, and cross-operation handles. Expose separate
resolution methods so only the backup command boundary can request the backup credential.

Use this exact protocol:

```python
type WarehouseSecretPurpose = Literal[
    "administration",
    "ingestion_runtime",
    "transformation_runtime",
    "backup_restore",
    "customer_sql",
    "catalog",
    "bi",
    "tls_private_key",
    "tls_certificate",
    "backup_encryption",
]


class WarehouseSecretStore(Protocol):
    def store(self, operation_id: str, secrets: WarehouseOperationSecrets) -> str: ...
    def resolve(
        self,
        secret_reference: str,
        *,
        operation_id: str,
        purpose: WarehouseSecretPurpose,
    ) -> SecretStr: ...
    def delete(self, secret_reference: str, *, operation_id: str) -> None: ...
```

Engine-provider constructors receive only `WarehouseResourceRecorder`, `WarehouseSecretStore`,
and `DockerComposeProcess` boundaries plus injected clocks/entropy. They do not receive the SQLite
connection or the full control service.

- [ ] **Step 6: Prove command-boundary and error privacy**

Assert ingestion, transformation, catalog, BI, customer SQL, diagnostics, and generic provider paths
cannot resolve the backup handle. Inject every credential, TLS key fragment, endpoint, path, and raw
provider diagnostic as a canary and scan captured logs/exceptions/results for absence.

- [ ] **Step 7: Verify Task 5**

```bash
uv lock
uv run pytest services/warehouse-control/tests/test_orchestration.py \
  services/warehouse-control/tests/test_secrets.py -q
uv run ruff check services/warehouse-control
uv run ruff format --check services/warehouse-control
uv run mypy
```

Expected: all commands pass and `uv lock --check` succeeds.

- [ ] **Step 8: Commit Task 5**

```bash
git add services/warehouse-control uv.lock
git commit -m "feat(warehouse-control): orchestrate provider lifecycle"
```

---

### Task 6: Implement the PostgreSQL reference warehouse lifecycle

**Files:**
- Create: `providers/postgresql/src/pillarmesh_provider_postgresql/warehouse.py`
- Create: `providers/postgresql/src/pillarmesh_provider_postgresql/warehouse_settings.py`
- Modify: `providers/postgresql/src/pillarmesh_provider_postgresql/__init__.py`
- Modify: `providers/postgresql/pyproject.toml:1-20`
- Create: `providers/postgresql/tests/test_warehouse.py`
- Create: `tests/emulators/warehouses/postgresql/compose.yaml`
- Create: `tests/emulators/warehouses/postgresql/init_tls.py`
- Create: `tests/emulators/warehouses/postgresql/wait_ready.py`
- Create: `tests/integration/test_postgresql_warehouse_live.py`
- Create: `tests/conformance/warehouse_lifecycle.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: shared `DockerComposeProcess`, `WarehouseProvider`, repository recorder, secret store,
  artifacts, and orchestrator.
- Produces: `PostgreSQLWarehouseProvider` and the first green implementation of
  `assert_warehouse_lifecycle_contract(provider_factory)`.

- [ ] **Step 1: Pin the real fixture and write provider-shape RED**

Use this exact Compose image value:

```yaml
image: postgres:18.6-bookworm@sha256:33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935
```

Bind TLS only to `127.0.0.1` on an ephemeral host port, use an internal Compose network, declare an
explicit named data volume, and generate server/client certificates outside the repository into the
private operation directory. The fixture must not contain a default password or floating image tag.

Add an import/protocol test and run it. Expected: FAIL because `PostgreSQLWarehouseProvider` is
absent.

- [ ] **Step 2: Define the canonical PostgreSQL grant plan**

Generate identifiers from the private binding handle using `psycopg.sql.Identifier`. Never format
tenant or caller text into SQL. Create non-login roles for all seven classes and temporary login
probe roles that inherit exactly one class.

Implement the matrix:

```text
administration: database/schema/role administration
ingestion_runtime: USAGE raw/control; CREATE/INSERT/UPDATE on raw probe and append-only ledger probe
transformation_runtime: SELECT raw; CREATE/WRITE conformed/product/quarantine/consumption probes
backup_restore: CONNECT plus membership in pg_read_all_data for pg_dump; no writes, CREATE,
                role, or grant administration on the primary warehouse
customer_sql: SELECT only on allowlisted consumption probe
catalog: catalog metadata inspection; no table SELECT or writes
bi: SELECT only on certified consumption probe
```

Every probe also attempts role creation, grant changes, control-object deletion, and unrelated
namespace access where the class must be denied.

- [ ] **Step 3: Implement record-before-create provisioning and reconciliation**

Before Compose `up`, record project, container, network, data volume, credential file, TLS key, and
certificate resources as `planned`. Reconcile by inspecting only those stable identities. After
readiness, rotate the bootstrap password, create roles/namespaces, remove inherited/default access,
and mark each resource `created`. A second provision call with the same operation returns the same
resource result; another operation cannot adopt the project.

- [ ] **Step 4: Implement PostgreSQL validation and monitoring probes**

Observe `server_version_num`, the inspected image digest, TLS protocol/cipher, host binding, exact
roles/grants, control ledger replay, `pg_stat_database`, and `pg_database_size`. Feed a bounded
synthetic capacity observation through the deterministic `mvp-fixed` threshold evaluator and prove
one alert opens and clears without creating a general monitoring system.

Build canonical positive and denial summaries and expose only their digests.

- [ ] **Step 5: Implement encrypted backup and isolated restore**

Record the backup resource before invoking `pg_dump --format=custom` through the private backup
credential boundary. Stream the dump: read `exec_stream` stdout in bounded chunks, encrypt each
chunk with the injected acceptance encryption key, and append to a `0600` private temporary file
that is
renamed into place only after the child process exits zero. Restore reverses it, decrypting chunk by
chunk into the restore process's stdin. Neither the plaintext dump nor its ciphertext is ever fully
resident. Use a streaming AEAD construction with a per-chunk nonce and an authenticated chunk index;
do not use Fernet here, which requires the whole payload in memory and base64-expands it by about a
third. Record restore project/container/network/volume resources before creating them.

Add a test that backs up and restores a fixture larger than the chunk size and asserts peak process
memory stays bounded, so a future change cannot silently reintroduce whole-payload buffering. Prove
truncation and chunk reordering are rejected rather than restored.

`backup_restore` is deliberately read-only, so it cannot be the identity that replays a dump:
`pg_restore` issues `CREATE SCHEMA`, `CREATE TABLE`, `COPY`, and `ALTER ... OWNER TO`, every one of
which that class is denied. The restore instance is a throwaway with its own bootstrap administrator
whose credential is minted for this operation, exists only inside the isolated project, and is
destroyed with it. That administrator has no grant on the primary warehouse, so the read-only
guarantee on `backup_restore` is unweakened and the primary's administration path never sees the
backup credential or the decrypted stream. The decrypt-and-feed step runs inside the backup command
boundary, which writes plaintext only to the restore process's stdin.

For restore:

1. create an isolated project with no route to the original warehouse;
2. create canonical roles without login credentials;
3. decrypt through stdin and run `pg_restore` as the restore instance's own bootstrap
   administrator, never as `backup_restore`;
4. verify representative row-set, schema, grant-profile, integrity-marker, and query-behavior
   digests;
5. remove exact restore resources; and
6. verify every recorded restore identifier is absent before returning evidence.

- [ ] **Step 6: Implement suspend, resume, and retention-aware retirement**

Suspend uses Compose `stop` and proves the data volume still exists. Resume uses `start`, rechecks
TLS/version/image/metrics/integrity, and returns fresh resume evidence. Retirement stops active work,
deletes expired non-data resources exactly, and marks customer-data or backup resources `retained`
until their deadline. The operation secret holding `backup_encryption_key_b64` is itself a retained
resource whenever any backup artifact it encrypted is retained: retirement must not call
`WarehouseSecretStore.delete` while such an artifact survives, or the retained backup becomes
ciphertext with no key and the addendum section 19 exit right cannot be honored. The later
deadline-authorized cleanup deletes the artifact and its key together and records both in the same
verified-deletion evidence. Failed cleanup records a classification without replacing the lifecycle
error.

- [ ] **Step 7: Run PostgreSQL live RED then GREEN**

Before implementation is complete, run:

```bash
uv run pytest tests/integration/test_postgresql_warehouse_live.py -m live -q
```

Require failures for absent grants/lifecycle. Then run the completed lifecycle twice and require the
second run to create no duplicate role, namespace, project, backup, or restore resources.

- [ ] **Step 8: Verify Task 6**

```bash
uv lock
uv run pytest providers/postgresql/tests/test_warehouse.py -q
uv run pytest tests/integration/test_postgresql_warehouse_live.py -m live -q
uv run ruff check providers/postgresql tests/conformance tests/integration/test_postgresql_warehouse_live.py
uv run ruff format --check providers/postgresql tests/conformance tests/integration/test_postgresql_warehouse_live.py
uv run mypy
```

Expected: all commands pass, the live test leaves no restore resources, and retained primary data is
named only in the private ledger.

- [ ] **Step 9: Commit Task 6**

```bash
git add providers/postgresql tests/emulators/warehouses/postgresql \
  tests/integration/test_postgresql_warehouse_live.py tests/conformance/warehouse_lifecycle.py \
  uv.lock
git commit -m "feat(postgresql): provision managed warehouse lifecycle"
```

---

### Task 7: Implement the ClickHouse portability lifecycle

**Files:**
- Create: `providers/clickhouse/pyproject.toml`
- Create: `providers/clickhouse/src/pillarmesh_provider_clickhouse/__init__.py`
- Create: `providers/clickhouse/src/pillarmesh_provider_clickhouse/py.typed`
- Create: `providers/clickhouse/src/pillarmesh_provider_clickhouse/warehouse.py`
- Create: `providers/clickhouse/src/pillarmesh_provider_clickhouse/settings.py`
- Create: `providers/clickhouse/tests/test_warehouse.py`
- Modify: `pyproject.toml:16-72`
- Create: `tests/emulators/warehouses/clickhouse/compose.yaml`
- Create: `tests/emulators/warehouses/clickhouse/config.xml`
- Create: `tests/emulators/warehouses/clickhouse/wait_ready.py`
- Create: `tests/integration/test_clickhouse_warehouse_live.py`
- Modify: `tests/conformance/warehouse_lifecycle.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: the exact Task 6 provider protocol and conformance function; no PostgreSQL-specific
  helper or expected SQL.
- Produces: `ClickHouseWarehouseProvider`, passing the same observable lifecycle contract.

- [ ] **Step 1: Scaffold the package and pin the LTS fixture**

Use:

```yaml
image: clickhouse/clickhouse-server:25.8.32.4@sha256:7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0
```

Add the workspace member and mypy package `pillarmesh_provider_clickhouse`. Depend only on
`pillarmesh-contract-model`, `pillarmesh-provider-sdk`, `pillarmesh-warehouse-control`,
`httpx>=0.28,<1`, and Pydantic. Bind the HTTPS interface to loopback only and disable the default
user's network access after bootstrap rotation.

- [ ] **Step 2: Write ClickHouse conformance RED**

Parameterize the Task 6 conformance contract with a ClickHouse factory and run:

```bash
uv run pytest providers/clickhouse/tests/test_warehouse.py \
  tests/integration/test_clickhouse_warehouse_live.py -m live -q
```

Expected: import or protocol failure. Once the class exists, keep at least one grant denial red
until the role matrix is enforced.

- [ ] **Step 3: Translate the seven principal classes without widening the subset**

Create databases equivalent to the six governed namespaces and ClickHouse roles matching the Task 6
outcomes. Use `GRANT` only for explicit databases, tables, and `BACKUP`/`RESTORE` capabilities.
Prove that no non-administration role can manage users/roles, change grants, access system secrets,
or cross binding databases.

Do not implement PostgreSQL transaction emulation, transactional DDL rollback, deferred foreign
keys, or row-by-row update/delete semantics. Any requested operation outside the documented subset
raises `WarehouseProviderError(classification=PERMANENT_CONFIGURATION)` and later compilation maps
that to `No Valid Plan`.

- [ ] **Step 4: Implement provision, reconcile, validate, suspend, and resume**

Mirror the PostgreSQL record-before-create order using ClickHouse stable resource identities. Query
`version()`, inspect the immutable image, verify TLS and host binding, inspect roles/grants through
system tables, write/read the bounded ledger probe, query asynchronous metrics, and run the same
capacity threshold evaluator.

Suspend and resume preserve the data volume. Fresh resume evidence must include version, image, TLS,
network, monitoring, positive, denial, and storage-integrity digests.

- [ ] **Step 5: Implement ClickHouse-native backup and isolated restore**

Configure a private backup disk and execute `BACKUP`/`RESTORE` through the backup principal command
boundary. Encrypt the artifact with the same streaming construction and chunk discipline as
PostgreSQL before it leaves the private operation directory; never load a whole backup into memory.
As with PostgreSQL, the isolated target is restored by its own throwaway bootstrap administrator,
not by the read-only backup principal. Record every restore project/container/network/volume before
creation. Reapply the canonical role plan in the isolated target, restore the data and metadata
subset, verify the same six restore digests, then remove exact restore resources and prove absence.

- [ ] **Step 6: Implement retention-aware retirement and error translation**

Translate HTTP transport, server unavailable, throttling, access denial, statement rejection,
malformed response, checksum mismatch, and ambiguous Compose outcomes into the exact closed enum.
Retain customer data until deadline and use the same retirement evidence shape and count rules as
PostgreSQL.

- [ ] **Step 7: Verify Task 7 and the first portability gate**

```bash
uv lock
uv run pytest providers/clickhouse/tests/test_warehouse.py -q
uv run pytest tests/integration/test_clickhouse_warehouse_live.py -m live -q
uv run pytest tests/integration/test_postgresql_warehouse_live.py \
  tests/integration/test_clickhouse_warehouse_live.py -m live -q
uv run ruff check providers/clickhouse tests/conformance tests/integration
uv run ruff format --check providers/clickhouse tests/conformance tests/integration
uv run mypy
```

Expected: both engine factories pass the same conformance assertions and no ClickHouse exclusion is
silently simulated.

- [ ] **Step 8: Commit Task 7**

```bash
git add providers/clickhouse pyproject.toml tests/emulators/warehouses/clickhouse \
  tests/integration/test_clickhouse_warehouse_live.py tests/conformance/warehouse_lifecycle.py \
  uv.lock
git commit -m "feat(clickhouse): provision managed warehouse lifecycle"
```

---

### Task 8: Prove two-engine replay, fault recovery, and retention semantics

**Files:**
- Create: `tests/fault-injection/test_warehouse_lifecycle_fault_matrix.py`
- Create: `tests/end-to-end/test_managed_warehouse_lifecycle.py`
- Modify: `tests/conformance/warehouse_lifecycle.py`
- Modify: `services/warehouse-control/src/pillarmesh_warehouse_control/orchestration.py`
- Modify: `providers/postgresql/src/pillarmesh_provider_postgresql/warehouse.py`
- Modify: `providers/clickhouse/src/pillarmesh_provider_clickhouse/warehouse.py`

**Interfaces:**
- Consumes: complete Tasks 5-7 orchestrator and engine providers.
- Produces: the closed `WarehouseLifecycleCheckpoint` enum, a deterministic
  `WarehouseFaultHook = Callable[[WarehouseLifecycleCheckpoint], None]`, and the final offline/live
  fault matrix.

- [ ] **Step 1: Define stable fault checkpoints and write RED**

Define the checkpoints as a closed `StrEnum`, not bare strings, and type the hook against it. A
stringly-typed hook makes a typo a silent no-op and lets a later refactor drop a checkpoint without
any test failing. Add a no-op hook at each member of:

```text
after_provisioning_transition
after_operation_claim
after_resource_plan
after_provider_create
after_validating_transition
after_backup_recorded
after_backup_created
after_restore_plan
after_restore_created
after_restore_verified
after_restore_cleanup
before_validation_admission
after_suspend_effect
after_resume_effect
before_resume_admission
after_retirement_disposition
before_retirement_admission
```

Parameterize process-stop simulation at every checkpoint. Expected RED: the orchestrator does not
accept a fault hook and selected replays leak or duplicate effects.

Add a test that runs one complete lifecycle per engine with a recording hook and asserts the set of
checkpoints fired equals the full enum. Merging, reordering, or dropping a phase then fails loudly
instead of quietly shrinking the crash windows the fault matrix covers.

- [ ] **Step 2: Implement the no-op hook and replay convergence**

Interruptions propagate outside ordinary provider failure handling. Replay uses the durable phase,
claim, and exact resource ledger to choose reconcile, validate, cleanup, or admission without
repeating a completed effect. Never catch `BaseException` as an ordinary failure.

- [ ] **Step 3: Add classification and ambiguous-outcome mutation tests**

For each provider operation, mutate a transient classification to terminal and require the test to
fail. Simulate create success with lost response, backup success with lost receipt, restore create
success with lost response, cleanup success with lost response, and inspection unavailable. Require
convergence or an explicit unresolved operation, never resource adoption or false failure.

- [ ] **Step 4: Add retention and later-deletion tests**

Use an injected clock. Before deadline, retirement records `retained` and leaves the exact data and
backup resources. At deadline, an explicitly authorized cleanup pass removes only those recorded
identifiers. Prove unrelated Compose projects and another tenant's resources remain. Record verified
deletion separately from the already-terminal binding.

- [ ] **Step 5: Add the data-architect end-to-end test**

For both engine kinds: create draft, freeze provisioning, provision, validate, reach ready, suspend,
resume with fresh evidence, retire with retention, advance the clock, perform authorized deletion,
and verify all public artifacts while scanning for private canaries. Include wrong-tenant, stale
revision, competing operation, and production-policy denial paths.

- [ ] **Step 6: Verify Task 8**

```bash
uv run pytest tests/fault-injection/test_warehouse_lifecycle_fault_matrix.py \
  tests/end-to-end/test_managed_warehouse_lifecycle.py -q
uv run pytest providers/postgresql/tests providers/clickhouse/tests \
  services/warehouse-control/tests tests/conformance -q
uv run ruff check services/warehouse-control providers/postgresql providers/clickhouse tests
uv run ruff format --check services/warehouse-control providers/postgresql providers/clickhouse tests
uv run mypy
```

Expected: all commands pass.

- [ ] **Step 7: Commit Task 8**

```bash
git add services/warehouse-control providers/postgresql providers/clickhouse \
  tests/conformance tests/end-to-end/test_managed_warehouse_lifecycle.py \
  tests/fault-injection/test_warehouse_lifecycle_fault_matrix.py
git commit -m "test(warehouse): prove lifecycle recovery boundaries"
```

---

### Task 9: Build witnessed Plan 3A acceptance and evidence

**Files:**
- Create: `tests/acceptance/plan3a_orchestration.py`
- Create: `tests/acceptance/run_plan3a.py`
- Create: `tests/acceptance/test_run_plan3a.py`
- Create: `docs/plan3a/setup.md`
- Create: `docs/plan3a/acceptance-run.md`
- Create: `docs/plan3a/evidence-package.md`
- Create: `docs/plan3a/teardown.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: complete two-engine lifecycle and fault checkpoints.
- Produces: `Plan3AConfig`, `Plan3AEvidence`, `Plan3ACleanupEvidence`,
  `run_plan3a(config) -> Plan3AEvidence`, and a CLI that prints only terminal status and evidence
  digest.

- [ ] **Step 1: Write the acceptance RED and privacy scanner**

Create these strict evidence contracts:

```text
Plan3AConfig
  state_path
  secret_directory
  backup_directory
  evidence_directory
  reservation_path
  source_commit
  postgres_image
  clickhouse_image
  retention_deadline
  credential_canary_digests

Plan3AEvidence
schema_version
run_id
source_commit
engine_results
cross_engine_conformance_digest
tenant_isolation_digest
failure_matrix_digest
privacy_scan_digest
local_encryption_limitation
cleanup_digest
started_at
completed_at

Plan3ACleanupEvidence
  schema_version
  run_id
  resource_inventory_digest
  completed_resource_count
  retained_resource_count
  failed_resource_count
  zero_residual_resources
  verified_at
```

Each engine result carries only binding/evidence digests, terminal states, version/image values,
named check dispositions, duration, and residual-resource counts. Inject credential, endpoint, path,
tenant, canary-row, TLS-key, and raw diagnostic markers and require absence from JSON, stdout,
stderr, and logs.

Run `uv run pytest tests/acceptance/test_run_plan3a.py -q` and require import failure.

- [ ] **Step 2: Implement secure configuration and reservation**

Require absolute owner-only paths outside the repository for state, secret storage, encrypted
backups, evidence, and the shared-environment reservation. Reject symlinks, group/world access,
repository descendants, duplicate paths, nonempty output, floating image values, and missing
encryption/signing keys. Secrets arrive through inherited environment or stdin, never arguments.

- [ ] **Step 3: Implement the witnessed two-engine journey**

Run PostgreSQL then ClickHouse with unique operation/resource identities. For each engine, execute
provision, initial validation, backup, isolated restore, suspend, gated resume, retention-aware
retirement, deadline-authorized cleanup, and absence verification. Then compare canonical outcomes,
run cross-tenant denials, validate every public artifact through the real model, and scan evidence.

The acceptance report must state:

```text
storage_encryption = deferred_local_acceptance
production_readiness = not_proven
lifecycle_conformance = proven
```

- [ ] **Step 4: Implement exact teardown and interrupted-run recovery**

Teardown reads only the private resource ledger, checks the reservation/run identity, applies
retention authorization, removes exact recorded resources, verifies absence, and records a cleanup
package. Private secret files and encrypted backups are deleted together and only once retention
authorization covers the backup artifact; a secret file whose artifact is still retained is left in
place and reported. A second teardown is idempotent. Missing or corrupt ledger state refuses broad
Docker cleanup.

- [ ] **Step 5: Write operator runbooks**

Document prerequisites, exact commands, expected durations, private environment names, terminal
success fields, live-only limitations, interruption recovery, evidence verification, retention
authorization, and exact teardown. State that offline tests, an HTTP success, a running container,
or a backup file do not prove Plan 3A.

- [ ] **Step 6: Run offline acceptance tests**

```bash
uv run pytest tests/acceptance/test_run_plan3a.py -q
uv run ruff check tests/acceptance
uv run ruff format --check tests/acceptance
uv run mypy
```

Expected: all commands pass.

- [ ] **Step 7: Run a fresh witnessed acceptance**

Follow `docs/plan3a/setup.md`, then run the exact CLI in `docs/plan3a/acceptance-run.md`. Require both
engine results terminal, every live check `passed`, local encryption limitation present,
`production_readiness=not_proven`, and cleanup with zero post-deadline residual resources. Preserve
only sanitized evidence; remove the ephemeral shell environment after teardown.

- [ ] **Step 8: Commit Task 9**

```bash
git add README.md docs/plan3a tests/acceptance
git commit -m "test: prove managed warehouse lifecycle"
```

---

### Task 10: Add the required path-filtered live gate and complete verification

**Files:**
- Create: `.github/workflows/warehouse-lifecycle.yml`
- Create: `tests/ci/test_warehouse_lifecycle_workflow.py`
- Modify: `docs/plan3a/acceptance-run.md`
- Modify: `docs/plan3a/evidence-package.md`

**Interfaces:**
- Consumes: Task 9 witnessed command and all offline gates.
- Produces: the stable `Warehouse lifecycle / gate` check with a fail-closed detector guard, a
  path-filtered live job, manual dispatch, a duration artifact, an owner-approval handover for
  making the check required, and final implementation evidence.

- [ ] **Step 1: Write workflow contract tests before YAML**

Parse the workflow and assert:

- `pull_request`, `push`, and `workflow_dispatch` triggers;
- `permissions: contents: read` only;
- a change detector covers warehouse control, both engine providers, provider SDK Compose code,
  warehouse emulators/acceptance, and the workflow;
- a live job runs only when an affected path changes or the workflow is manually dispatched;
- an always-running final `gate` job fails when affected live execution is not successful;
- unrelated changes yield a successful gate without starting the live lifecycle;
- a failed, cancelled, or skipped `changes` job fails the gate rather than passing it;
- an empty or unexpected `AFFECTED` value fails the gate;
- image tags and digests match the spec; and
- the gate runs exact teardown under `if: always()`.

Run `uv run pytest tests/ci/test_warehouse_lifecycle_workflow.py -q`.

Expected: FAIL because the workflow is absent.

- [ ] **Step 2: Implement an always-present required gate**

Do not put a workflow-level `paths` filter on a required check; an unmatched pull request would
leave the required check pending forever. Trigger the workflow for every pull request. Use a
`changes` job to calculate an output from the base/head diff, condition the expensive `lifecycle`
job on that output, and make `gate` run with `if: always()`.

The gate logic is:

```bash
test "$CHANGES_RESULT" = success || exit 1
case "$AFFECTED" in
  true)  test "$LIFECYCLE_RESULT" = success ;;
  false) test "$LIFECYCLE_RESULT" = skipped ;;
  *)     exit 1 ;;
esac
```

`CHANGES_RESULT` is `needs.changes.result` and `AFFECTED` is its output. Both guards are
load-bearing. Without them a `changes` job that fails or is cancelled leaves `AFFECTED` empty, the
empty value
falls through to the not-affected branch, the `lifecycle` job never started so its result is
`skipped`, and the required check reports success on a pull request that does modify the warehouse.
Fail closed instead: an undecided detector is a failed gate, never a passed one.

Use `actions/checkout@v6` and the repository-pinned `astral-sh/setup-uv` SHA. Do not add an unpinned
path-filter action.

- [ ] **Step 3: Measure hosted-runner lifecycle cost**

Run the workflow on the branch, record wall time and peak Docker disk/memory observations as
sanitized numeric fields, and upload the Plan 3A evidence artifact. The target is under ten minutes.
If it exceeds ten minutes or flakes on two consecutive fresh runs, stop for design review; do not
make the gate optional.

- [ ] **Step 4: Prepare the required-check change and hand it to the owner**

Do not mutate branch protection from inside this plan. Adding a required check is outward-facing,
shared, and hard to reverse, the GitHub endpoint replaces the whole protection object rather than
patching it, and a plan cannot authorize its own repository administration: approval has to come
from the repository owner at the time of the change.

Instead, prepare the change and hand it over. Using GitHub account `ks2002119`, read current branch
protection, render the exact resulting required-check list with `Warehouse lifecycle / gate` added
and every existing context preserved, and record both the before and after in
`docs/plan3a/acceptance-run.md` together with the command that would apply it. Stop there and ask
the repository owner to approve.

If and only if the owner approves in that conversation, apply it as a read-modify-write inside one
shell invocation: re-read protection, confirm it still matches the snapshot the owner approved,
apply, re-read, and diff the required-check list. Abort on any drift, because a concurrent session
that changed protection between the read and the write would otherwise have its checks silently
dropped. Record the verified result as evidence.

- [ ] **Step 5: Run complete local verification**

```bash
uv sync --locked --all-packages
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
uv run pytest tests/integration/test_postgresql_warehouse_live.py \
  tests/integration/test_clickhouse_warehouse_live.py -m live -q
./tests/repository-structure/test.sh
git diff --check
```

Expected: all commands pass. Record exact pass counts and live durations; skipped or unrun live tests
remain explicit gaps.

- [ ] **Step 6: Review the complete diff**

Check architecture placement, dependency direction, lifecycle admission, transaction boundaries,
tenant qualification, failure classification, command and SQL construction, secret paths, evidence
allowlists, retention behavior, ClickHouse exclusions, CI guards, documentation, and accidental
files. Confirm the branch adds no source/destination directory split and no production-readiness
claim.

- [ ] **Step 7: Request independent review**

Provide the reviewer with the spec, plan, full diff from the fresh base, offline results, two-engine
live evidence digest, CI duration, and known local encryption limitation. The author must not fill
the independent-review disposition. Address confirmed findings one logical commit at a time and
rerun affected plus complete gates.

- [ ] **Step 8: Commit Task 10**

```bash
git add .github/workflows/warehouse-lifecycle.yml tests/ci \
  docs/plan3a/acceptance-run.md docs/plan3a/evidence-package.md
git commit -m "ci: require managed warehouse lifecycle"
```

## Final completion gate

Plan 3A is complete only when:

- all ten task commits and review-fix commits are present on a branch based on freshly fetched
  `origin/main`;
- the addendum, public artifacts, transition tables, principal classes, provider operations,
  failure classifications, and conformance tests agree exactly;
- PostgreSQL and ClickHouse pass the same supported observable subset;
- initial and resume readiness plus retirement are evidence-gated and atomic;
- every crash point converges without duplicate or adopted resources;
- private backup credentials are inaccessible to non-backup command paths;
- both real engine backups restore usable data, metadata, grants, integrity markers, and queries;
- restore resources are recorded before creation and exactly absent afterward;
- retirement honors retention and later deletion touches only exact recorded identifiers;
- the hosted live gate succeeds within the measured cost envelope, fails closed when its change
  detector does not report success, and its promotion to a required check is either owner-approved
  and verified or recorded as an open handover;
- a fresh witnessed evidence package passes privacy scanning and explicitly withholds production
  storage-encryption/readiness claims;
- the complete offline suite and repository structure validation pass; and
- independent review has no unresolved blocking finding.
