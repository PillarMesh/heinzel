# Plan 3B Architect Inbox Fulfillment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the tenant-scoped control-plane workflow that turns stakeholder questions and
data-access requests into immutable governed proposals, exact role-scoped approvals, typed
dependencies or denials, and replay-safe execution-ready handoffs without executing SQL or grants.

**Architecture:** Keep fulfillment inside `services/request-management` as focused model,
persistence, policy, orchestration, and read-view modules. Share one injected SQLite connection
between request and fulfillment repositories so every proposal, request revision, transition,
approval admission, denial, dependency, cancellation, and evidence receipt is atomic. Consume Plan
2 semantic and catalog authority through an adapter in `services/semantic-registry`, preserving the
existing dependency direction and treating immutable receipts rather than live OpenMetadata state
as authority.

**Tech Stack:** Python 3.13.5, frozen Pydantic v2 artifacts, standard-library `sqlite3`, `uv`,
pytest, Hypothesis, mutmut, Ruff, strict mypy.

**Spec:** `docs/superpowers/specs/2026-08-30-plan-3b-architect-inbox-fulfillment-design.md`

## Global Constraints

- Read the spec, root `AGENTS.md`, the managed-platform addendum, and the owning code before each
  task.
- `services/request-management` must not import `services/semantic-registry`; the adapter points
  from semantic-registry to request-management.
- Every public service and repository operation takes `tenant_id` and refuses cross-tenant IDs
  before reading or writing payload fields.
- All durable models are frozen, reject unknown fields, use UTC timestamps, canonical SHA-256
  digests, explicit enums, immutable tuples, and repository-sequence IDs.
- Candidate answer text, request purpose, field scope, classifications, policy observations, and
  tenant-private digests never enter public evidence, logs, exception messages, or requester views.
- An answer or access admission proves only `ready_for_execution`; it never proves SQL execution,
  a grant, delivery, freshness, verification, expiry, or revocation.
- A proposal cannot be created from `clarifying`; cancellation is terminal and defeats every open
  proposal and binding.
- Plan 2 `DecisionBinding` and Plan 3B `FulfillmentApprovalBinding` are disjoint authority systems.
- Admission matches every approval on tenant, authority, subject digest, proposal digest, proposal
  revision, decision, and current actor role; matching authority alone is illegal.
- An expired policy snapshot must be re-resolved. An equivalent re-resolution may admit; a changed
  disposition or scope supersedes the proposal and requires new approvals; failed resolution
  produces `No Valid Plan`.
- Use RED -> GREEN for every behavior and prove at least one denial or boundary test per task.
- End every task with focused verification, diff review, and one Conventional Commit.

---

### Task 1: Inject the request SQLite connection without changing behavior

**Files:**
- Modify: `services/request-management/src/pillarmesh_request_management/repository.py`
- Modify: all 42 `SQLiteRequestRepository(...)` construction sites named in spec section 6.1
- Test: `services/request-management/tests/test_service.py`
- Test: `services/request-management/tests/test_conversation.py`
- Test: `services/request-management/tests/test_semantic_review.py`

**Interfaces:**
- Produces: `SQLiteRequestRepository(connection: sqlite3.Connection)` and
  `SQLiteRequestRepository.open(database_path: str) -> SQLiteRequestRepository`.
- Preserves: every existing `RequestRepository` method and Plan 1/Plan 2 behavior.
- Ownership: `.open()` owns and closes its connection; an injected connection remains caller-owned.

- [ ] **Step 1: Add failing connection ownership and shared-transaction characterization tests**

```python
def test_request_repository_accepts_a_caller_owned_connection() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteRequestRepository(connection)
    repository.close()

    assert connection.execute("SELECT 1").fetchone() == (1,)


def test_opened_request_repository_owns_its_connection(tmp_path: Path) -> None:
    repository = SQLiteRequestRepository.open(str(tmp_path / "requests.sqlite"))
    repository.close()

    with pytest.raises(sqlite3.ProgrammingError):
        repository.list_inbox("tenant-a")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```sh
uv run pytest services/request-management/tests/test_service.py \
  -k 'caller_owned_connection or opened_request_repository' -q
```

Expected: fail because the constructor still requires a path and `.open()` does not exist.

- [ ] **Step 3: Implement explicit connection ownership**

```python
class SQLiteRequestRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        _owns_connection: bool = False,
    ) -> None:
        self._connection = connection
        self._owns_connection = _owns_connection
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._initialize_schema()

    @classmethod
    def open(cls, database_path: str) -> Self:
        return cls(sqlite3.connect(database_path), _owns_connection=True)

    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()
```

Move schema initialization into `_initialize_schema()` without changing SQL. Do not expose the
connection as a public property.

- [ ] **Step 4: Update every path-taking caller**

Change every active path or `":memory:"` construction to:

```python
SQLiteRequestRepository.open(":memory:")
SQLiteRequestRepository.open(str(database_path))
```

Leave only the new injected-connection tests using `SQLiteRequestRepository(connection)`.

- [ ] **Step 5: Run all affected Plan 1 and Plan 2 tests**

```sh
uv run pytest services/request-management/tests \
  services/semantic-registry/tests/test_review.py \
  services/semantic-registry/tests/test_publication.py \
  tests/end-to-end/test_data_architect_foundation.py \
  tests/end-to-end/test_catalog_semantic_formation.py -q
uv run ruff check services/request-management services/semantic-registry tests
uv run mypy
```

Expected: all pass with no request or semantic behavior change.

- [ ] **Step 6: Commit**

```sh
git add services/request-management services/semantic-registry/tests \
  tests/acceptance/plan2_orchestration.py tests/acceptance/run_plan2.py \
  tests/end-to-end tests/integration/test_openmetadata_publication_live.py
git commit -m "refactor(requests): inject SQLite connection"
```

---

### Task 2: Ratify and implement strict fulfillment contracts

**Files:**
- Create: `services/request-management/src/pillarmesh_request_management/fulfillment_models.py`
- Modify: `services/request-management/src/pillarmesh_request_management/models.py`
- Modify: `services/request-management/src/pillarmesh_request_management/__init__.py`
- Create: `services/request-management/tests/test_fulfillment_models.py`
- Modify: `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md`
- Modify: `docs/superpowers/specs/2026-08-30-plan-3b-architect-inbox-fulfillment-design.md`
- Modify: `tests/conformance/test_specification_conformance.py`

**Interfaces:**
- Produces every artifact and enum named in spec section 7, plus
  `DataProductChangeRequest` in the `InboxRequest.payload` discriminator.
- Produces closed aliases `FulfillmentSubject`, `FulfillmentOutcome`, `DependencyKind`, and
  `FreshnessDisposition`.

- [ ] **Step 1: Write failing strict-model and conformance tests**

Cover the complete field shapes from spec section 7, including:

```python
def test_answer_requires_freshness_observation_for_current_claim() -> None:
    snapshot = grounding_snapshot(freshness_observation_ref=None)
    answer = answer_draft(freshness_disposition=FreshnessDisposition.CURRENT)

    with pytest.raises(ValueError, match="freshness observation"):
        FulfillmentProposal.create(
            request=request_in_state(RequestState.INVESTIGATING),
            clarified_outcome=clarified_outcome(),
            grounding=snapshot,
            policy=policy_snapshot(),
            subject=answer,
            required_approvals=answer_requirements(answer),
            revision=1,
            created_at=NOW,
        )


def test_unknown_fulfillment_fields_are_rejected() -> None:
    payload = FulfillmentEvidenceReceipt(...).model_dump(mode="json")
    payload["answer_text"] = "must not enter evidence"

    with pytest.raises(ValidationError):
        FulfillmentEvidenceReceipt.model_validate(payload)
```

Add conformance assertions for all field tables, the full request transition table, the outcome
decision table, approval asymmetry, clarified-outcome requirement, Plan 2/Plan 3B binding split,
policy-expiry rule, cancellation, and the execution non-claims.

- [ ] **Step 2: Run the model and conformance tests and verify RED**

```sh
uv run pytest services/request-management/tests/test_fulfillment_models.py \
  tests/conformance/test_specification_conformance.py -q
```

Expected: import failure for the absent fulfillment models.

- [ ] **Step 3: Implement the strict models**

Use `ArtifactModel`, `ArtifactReference`, `Field(pattern=r"^[0-9a-f]{64}$")`, discriminated
unions, model validators, and enum values copied exactly from the spec. Implement pure constructors
for deterministic IDs and digests. `FulfillmentProposal.create(...)` validates citations, factual
freshness, requested/effective subset rules, and `prior_proposal_digest`/revision consistency.

Extend the request payload union:

```python
class DataProductChangeRequest(ArtifactModel):
    request_type: Literal["data_product_change"] = "data_product_change"
    purpose: str = Field(min_length=1, max_length=512)
    requested_outcome: str = Field(min_length=1, max_length=4000)
    missing_capability_refs: tuple[str, ...] = Field(min_length=1)
    source_request_id: str = Field(min_length=1)
    source_request_revision: int = Field(ge=1)
```

- [ ] **Step 4: Ratify the reviewed design in normative docs**

Set the design status to `approved`. Fix the malformed Definition-of-Done bullet formatting from
the review merge. Add exact addendum subsections for artifact shapes, visibility, approval and
admission predicates, dependency kinds, policy supersession, cancellation, and non-claims. Pin
them through conformance tests; do not merely test that phrases exist.

- [ ] **Step 5: Run focused verification**

```sh
uv run pytest services/request-management/tests/test_fulfillment_models.py \
  tests/conformance/test_specification_conformance.py -q
uv run ruff check services/request-management tests/conformance
uv run mypy
```

- [ ] **Step 6: Commit**

```sh
git add services/request-management docs/architecture/specifications \
  docs/superpowers/specs tests/conformance
git commit -m "feat(requests): define inbox fulfillment contracts"
```

---

### Task 3: Add append-only atomic fulfillment persistence

**Files:**
- Create: `services/request-management/src/pillarmesh_request_management/fulfillment_repository.py`
- Modify: `services/request-management/src/pillarmesh_request_management/repository.py`
- Modify: `services/request-management/src/pillarmesh_request_management/__init__.py`
- Create: `services/request-management/tests/test_fulfillment_repository.py`
- Create: `tests/fault-injection/test_inbox_fulfillment_repository.py`

**Interfaces:**
- Produces `FulfillmentRepository` protocol.
- Produces `SQLiteFulfillmentRepository(request_repository: SQLiteRequestRepository)` sharing the
  request repository's injected connection through package-private storage helpers.
- Produces atomic methods:

```python
store_clarified_outcome(..., expected_revision: int) -> ClarifiedOutcomeStatement
store_proposal(..., expected_revision: int) -> FulfillmentProposal
submit_proposal(..., expected_revision: int) -> InboxRequest
store_dependency(..., expected_revision: int) -> RequestDependency
store_no_valid_plan(..., expected_revision: int) -> RequestNoValidPlan
store_approval(...) -> FulfillmentApprovalBinding
admit(..., expected_revision: int) -> FulfillmentAdmissionReceipt
record_denial(..., expected_revision: int) -> DenialDispositionReceipt
record_cancellation(..., expected_revision: int) -> FulfillmentEvidenceReceipt
```

- [ ] **Step 1: Characterize shared request storage before refactoring helpers**

Add tests proving request revision, transition event, proposal, and evidence counts are all zero
after an injected failure at each write boundary. Add a concurrent stale-revision test with two
connections to a file-backed database.

- [ ] **Step 2: Run repository tests and verify RED**

```sh
uv run pytest services/request-management/tests/test_fulfillment_repository.py \
  tests/fault-injection/test_inbox_fulfillment_repository.py -q
```

- [ ] **Step 3: Extract package-private SQLite helpers**

Move connection-scoped `_load_owned_request`, `_save_request_revision`,
`_allocate_artifact_sequence`, and transition-event insertion behind a package-private
`_SQLiteRequestStorage`. Both repositories receive the same instance. Existing request repository
tests must remain unchanged except construction.

- [ ] **Step 4: Create append-only fulfillment tables**

Create tenant-qualified tables for grounding snapshots, policy snapshots, clarified outcomes,
proposal revisions, dependencies, fulfillment approvals, admissions, denial dispositions,
no-valid-plan records, and evidence receipts. Every natural identity is unique with `tenant_id`;
proposal revisions use `(tenant_id, proposal_id, revision)`.

- [ ] **Step 5: Implement the atomic operations**

Each mutation runs one `_transaction(connection)` using `BEGIN IMMEDIATE`, verifies ownership and
expected revision before payload access, writes the outcome and request revision together, and
returns an existing artifact only after canonical equality. Use `suppress(OSError)` only for
best-effort cleanup; rollback errors must not replace the domain error.

- [ ] **Step 6: Prove replay and fault convergence**

Run the focused tests, then deliberately relax canonical replay equality and confirm its regression
test fails before restoring it.

```sh
uv run pytest services/request-management/tests/test_fulfillment_repository.py \
  tests/fault-injection/test_inbox_fulfillment_repository.py -q
```

- [ ] **Step 7: Commit**

```sh
git add services/request-management tests/fault-injection/test_inbox_fulfillment_repository.py
git commit -m "feat(requests): persist atomic fulfillment outcomes"
```

---

### Task 4: Compile deterministic policy outcomes and approval requirements

**Files:**
- Create: `services/request-management/src/pillarmesh_request_management/fulfillment_protocols.py`
- Create: `services/request-management/src/pillarmesh_request_management/fulfillment_policy.py`
- Create: `services/request-management/src/pillarmesh_request_management/fulfillment_errors.py`
- Modify: `services/request-management/src/pillarmesh_request_management/__init__.py`
- Create: `services/request-management/tests/test_fulfillment_policy.py`

**Interfaces:**

```python
class AuthorityRoleResolver(Protocol):
    def has_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> bool: ...

class FulfillmentSnapshotResolver(Protocol):
    def resolve(self, *, tenant_id: str, request: InboxRequest) \
        -> tuple[FulfillmentGroundingSnapshot, FulfillmentPolicySnapshot] | ResolutionFailure: ...

class AnswerCandidateProvider(Protocol):
    def propose(self, *, request: InboxRequest, grounding: FulfillmentGroundingSnapshot) \
        -> StakeholderAnswerDraft: ...

class FulfillmentPolicyCompiler:
    def compile_answer(...) -> ProposalCompilationResult: ...
    def compile_access(...) -> ProposalCompilationResult: ...
```

`ProposalCompilationResult` is a strict discriminated result: `proposal`, `dependency`, `denial`,
or `no_valid_plan`. It is impossible to carry two outcomes.

- [ ] **Step 1: Write failing policy matrix and property tests**

Cover authorized answer, classified answer, narrowed access, finance policy authority, missing
semantic/data capability, unentitled disclosure, expired/contradictory policy, duplicate
requirements, effective-scope widening, and reordered-input determinism.

- [ ] **Step 2: Verify RED**

```sh
uv run pytest services/request-management/tests/test_fulfillment_policy.py -q
```

- [ ] **Step 3: Implement pure closed-outcome compilation**

Compile requirements in sorted order. Every proposal includes requester acceptance of
`ClarifiedOutcomeStatement`; answer adds architect and conditional policy authority; access adds
the exact product owner and conditional policy authority. Missing capability returns dependency,
insufficient entitlement returns denial, and invalid authority returns `No Valid Plan`.

- [ ] **Step 4: Prove critical branches by mutation**

Run focused mutation testing over effective-field subset, role inclusion, tenant equality,
freshness derivation, and closed result construction. Inspect every survivor.

- [ ] **Step 5: Commit**

```sh
git add services/request-management
git commit -m "feat(requests): compile governed fulfillment policy"
```

---

### Task 5: Resolve immutable Plan 2 grounding snapshots

**Files:**
- Create: `services/semantic-registry/src/pillarmesh_semantic_registry/fulfillment_adapter.py`
- Modify: `services/semantic-registry/src/pillarmesh_semantic_registry/publication.py`
- Modify: `services/semantic-registry/src/pillarmesh_semantic_registry/__init__.py`
- Create: `services/semantic-registry/tests/test_fulfillment_adapter.py`
- Modify: `services/semantic-registry/pyproject.toml` only if an existing workspace edge is absent

**Interfaces:**
- Produces `SemanticFulfillmentSnapshotAdapter` implementing `FulfillmentSnapshotResolver`.
- Consumes exact `CatalogPublicationIntent`, `CatalogPublicationReceipt`,
  `ApprovedSemanticVersion`, `ManagedIntegrationContract`, persisted observations, and injected
  entitlement observations.

- [ ] **Step 1: Write failing snapshot reachability and drift tests**

Test successful round-trip binding, publication/semantic/contract mismatch, unknown and
cross-tenant references, stale authority, private provider-ID exclusion, and changed live catalog
observations leaving an existing immutable snapshot unchanged.

- [ ] **Step 2: Verify RED**

```sh
uv run pytest services/semantic-registry/tests/test_fulfillment_adapter.py -q
```

- [ ] **Step 3: Implement the adapter**

Load persisted Plan 2 records by tenant and exact IDs. Recompute every digest, verify publication
round trip and contract semantic reference, derive only public `ArtifactReference` tuples, and
construct policy snapshots from the approved contract and injected current entitlement
observations. Return closed resolution failures rather than provider exceptions.

- [ ] **Step 4: Run affected Plan 2 and adapter tests**

```sh
uv run pytest services/semantic-registry/tests \
  tests/end-to-end/test_catalog_semantic_formation.py -q
uv run mypy
```

- [ ] **Step 5: Commit**

```sh
git add services/semantic-registry
git commit -m "feat(semantic): resolve fulfillment snapshots"
```

---

### Task 6: Orchestrate clarified outcomes, answer proposals, and dependencies

**Files:**
- Create: `services/request-management/src/pillarmesh_request_management/fulfillment_service.py`
- Modify: `services/request-management/src/pillarmesh_request_management/service.py`
- Modify: `services/request-management/src/pillarmesh_request_management/__init__.py`
- Create: `services/request-management/tests/test_answer_fulfillment.py`

**Interfaces:**

```python
class FulfillmentService:
    def clarify_outcome(..., expected_revision: int) -> ClarifiedOutcomeStatement: ...
    def propose_answer(..., expected_revision: int) -> FulfillmentOutcomeResult: ...
    def submit_proposal(..., expected_revision: int) -> InboxRequest: ...
    def revise_proposal(..., expected_revision: int) -> FulfillmentProposal: ...
```

- [ ] **Step 1: Write failing lifecycle and visibility-independent orchestration tests**

Cover direct submitted-to-investigating clarification, explicit `clarifying`, no proposal from
`clarifying`, valid semantic-definition proposal, factual answer freshness, semantic dependency,
data-product dependency, unadmitted authority `No Valid Plan`, material proposal revision, and
changed clarified outcome requiring fresh requester acceptance.

- [ ] **Step 2: Verify RED**

```sh
uv run pytest services/request-management/tests/test_answer_fulfillment.py -q
```

- [ ] **Step 3: Implement minimal orchestration**

The service invokes resolver, candidate, and policy boundaries but independently validates every
citation. It never accepts caller-selected outcome kinds. Dependency creation is atomic and leaves
the parent in `investigating`; proposal creation atomically moves to `proposed`.

- [ ] **Step 4: Guard Plan 2 decisions**

Modify `RequestManagementService.record_decision` to refuse a current request revision that has a
fulfillment proposal. Add the inverse test that fulfillment admission ignores existing
`DecisionBinding` rows.

- [ ] **Step 5: Commit**

```sh
git add services/request-management
git commit -m "feat(requests): prepare governed answer proposals"
```

---

### Task 7: Orchestrate access previews and policy-grounded denials

**Files:**
- Modify: `services/request-management/src/pillarmesh_request_management/fulfillment_service.py`
- Create: `services/request-management/tests/test_access_fulfillment.py`

**Interfaces:**

```python
def propose_access(
    self,
    *,
    tenant_id: str,
    request_id: str,
    expected_revision: int,
) -> FulfillmentOutcomeResult: ...
```

- [ ] **Step 1: Write failing access and denial tests**

Cover exact requested scope, narrowed fields, excluded governed surfaces, expiry cap, mode denial,
classification authority, finance denial, no automatic wider-access dependency, private role/SQL
absence, and cross-tenant refusal before payload reads.

- [ ] **Step 2: Verify RED**

```sh
uv run pytest services/request-management/tests/test_access_fulfillment.py -q
```

- [ ] **Step 3: Implement preview and denial orchestration**

Use only compiler output. Validate effective fields are a subset, objects belong to the requested
product, expiry does not exceed request/policy, and exclusions name all denied surfaces. A denial is
a private proposal subject until approved; it creates no admission or dependency.

- [ ] **Step 4: Commit**

```sh
git add services/request-management
git commit -m "feat(requests): prepare access scope previews"
```

---

### Task 8: Bind approvals, supersede expired policy, and admit exact proposals

**Files:**
- Modify: `services/request-management/src/pillarmesh_request_management/fulfillment_service.py`
- Modify: `services/request-management/src/pillarmesh_request_management/fulfillment_repository.py`
- Create: `services/request-management/tests/test_fulfillment_approval.py`
- Create: `tests/fault-injection/test_fulfillment_admission.py`

**Interfaces:**

```python
record_approval(..., authority_ref: str, subject_digest: str,
                decision: FulfillmentDecision, expected_revision: int)
    -> FulfillmentApprovalBinding
admit(..., expected_revision: int) -> FulfillmentAdmissionReceipt | FulfillmentProposal
dispose_denial(..., expected_revision: int) -> DenialDispositionReceipt
cancel(..., expected_revision: int) -> FulfillmentEvidenceReceipt
```

- [ ] **Step 1: Write the exact admission predicate tests**

Independently mutate tenant, authority, subject digest, proposal digest, proposal revision,
decision, role membership, snapshot digest, policy expiry, request state, and cancellation. Every
mutation must refuse admission.

- [ ] **Step 2: Write policy re-resolution tests**

Equivalent policy except observation window admits against the fresh snapshot. Changed entitlement,
effective scope, required authority, or denial disposition supersedes the proposal, returns to
`investigating`, and invalidates old approvals. Resolution failure stores `No Valid Plan`.

- [ ] **Step 3: Verify RED**

```sh
uv run pytest services/request-management/tests/test_fulfillment_approval.py \
  tests/fault-injection/test_fulfillment_admission.py -q
```

- [ ] **Step 4: Implement approval and admission**

Revalidate role at decision and admission. One binding satisfies one requirement. Store admission
or denial plus resulting request revision, transition event, and public evidence in one
transaction. Replay identity is `(tenant_id, request_id, source_request_revision)` and requires
canonical equality.

- [ ] **Step 5: Prove critical authorization mutations**

Run mutmut over the admission predicate and inspect survivors. Restore each deliberately relaxed
term only after its focused test fails.

- [ ] **Step 6: Commit**

```sh
git add services/request-management tests/fault-injection/test_fulfillment_admission.py
git commit -m "feat(requests): admit exact fulfillment approvals"
```

---

### Task 9: Add fail-closed requester and reviewer projections

**Files:**
- Create: `services/request-management/src/pillarmesh_request_management/requester_view.py`
- Modify: `services/request-management/src/pillarmesh_request_management/__init__.py`
- Create: `services/request-management/tests/test_requester_view.py`

**Interfaces:**

```python
class FulfillmentReadService:
    def requester_view(
        *, tenant_id: str, request_id: str, actor_id: str
    ) -> RequesterRequestView: ...
    def reviewer_view(
        *, tenant_id: str, request_id: str, actor_id: str, authority_ref: str
    ) -> ReviewerRequestView: ...
    def architect_view(
        *, tenant_id: str, request_id: str, actor_id: str
    ) -> ArchitectRequestView: ...
```

- [ ] **Step 1: Write structural privacy tests**

Serialize each role view and assert forbidden candidate text, field scope, classifications,
private digests, provider IDs, policy observations, and other-actor decisions are absent as keys and
values. Verify requester sees clarified outcome and own labelled Plan 2/Plan 3B decisions, but no
candidate before a future verified-delivery fixture. Verify approved denial exposes only its safe
explanation.

- [ ] **Step 2: Verify RED**

```sh
uv run pytest services/request-management/tests/test_requester_view.py -q
```

- [ ] **Step 3: Implement allowlist view models and service**

Build distinct frozen projection models; never serialize a private model and redact it. Role checks
are injected and tenant-qualified. Unrelated actors receive `FulfillmentNotVisible` with a generic
message.

- [ ] **Step 4: Commit**

```sh
git add services/request-management
git commit -m "feat(requests): expose governed inbox views"
```

---

### Task 10: Package privacy-safe fulfillment evidence

**Files:**
- Modify: `services/evidence/pyproject.toml`
- Modify: `uv.lock`
- Create: `services/evidence/src/pillarmesh_evidence/fulfillment.py`
- Modify: `services/evidence/src/pillarmesh_evidence/__init__.py`
- Create: `services/evidence/tests/test_fulfillment.py`

**Interfaces:**

```python
def package_fulfillment_receipts(
    receipts: tuple[FulfillmentEvidenceReceipt, ...],
) -> bytes: ...
```

- [ ] **Step 1: Write failing evidence allowlist and scanner tests**

Use canaries in answer, purpose, fields, classification details, private digests, provider IDs, and
policy observations. Assert the packaged canonical bytes contain none of them and reject malformed
or duplicate receipts.

- [ ] **Step 2: Verify RED**

```sh
uv run pytest services/evidence/tests/test_fulfillment.py -q
```

- [ ] **Step 3: Add the declared workspace dependency and packager**

Add `pillarmesh-request-management` to project dependencies and `[tool.uv.sources]`, run
`uv lock`, strictly reload every receipt, canonicalize its allowlisted model, and run the existing
privacy scanner before returning bytes.

- [ ] **Step 4: Commit**

```sh
git add services/evidence uv.lock
git commit -m "feat(evidence): package fulfillment receipts"
```

---

### Task 11: Prove the integrated architect inbox acceptance slice

**Files:**
- Create: `tests/end-to-end/test_architect_inbox_fulfillment.py`
- Create: `tests/fault-injection/test_architect_inbox_fulfillment.py`
- Create: `tests/acceptance/run_plan3b.py`
- Create: `tests/acceptance/test_run_plan3b.py`
- Create: `docs/plan3b/setup.md`
- Create: `docs/plan3b/acceptance-run.md`
- Create: `docs/plan3b/evidence-package.md`
- Create: `docs/plan3b/teardown.md`

**Interfaces:**
- Produces a deterministic offline Plan 3B harness over the real request, semantic, publication,
  policy, persistence, read-view, and evidence models.
- Emits only a sanitized evidence digest and terminal control-plane status.

- [ ] **Step 1: Write the failing revenue-to-cash end-to-end journey**

Prove the six acceptance cases in spec section 14.4, plus clarified outcome acceptance, Plan 2
decision separation, policy-expiry supersession, cancellation, cross-tenant refusal, replay, and
absence of SQL/provider/grant/message effects.

- [ ] **Step 2: Write the fault matrix**

Inject failure before and after every atomic write, stale revisions, malformed persisted payloads,
role revocation, policy expiry, changed policy, and cancellation races. Every replay converges to
one outcome with no partial record.

- [ ] **Step 3: Verify RED, then add the minimal harness**

```sh
uv run pytest tests/end-to-end/test_architect_inbox_fulfillment.py \
  tests/fault-injection/test_architect_inbox_fulfillment.py \
  tests/acceptance/test_run_plan3b.py -q
```

- [ ] **Step 4: Add operator documentation**

Document setup, exact command, evidence claims and non-claims, interruption recovery, and teardown.
Do not claim real source, query, grant, or delivery effects.

- [ ] **Step 5: Run focused integrated verification**

```sh
uv run pytest services/request-management/tests \
  services/semantic-registry/tests \
  services/evidence/tests \
  tests/end-to-end/test_architect_inbox_fulfillment.py \
  tests/fault-injection/test_architect_inbox_fulfillment.py \
  tests/acceptance/test_run_plan3b.py -q
```

- [ ] **Step 6: Commit**

```sh
git add tests/end-to-end/test_architect_inbox_fulfillment.py \
  tests/fault-injection/test_architect_inbox_fulfillment.py \
  tests/acceptance/run_plan3b.py tests/acceptance/test_run_plan3b.py docs/plan3b
git commit -m "test: witness Plan 3B inbox fulfillment"
```

---

### Task 12: Complete verification and independent review

**Files:**
- Modify only files required by confirmed review findings.

**Interfaces:**
- Verifies all Plan 3B public contracts and repository boundaries.

- [ ] **Step 1: Run complete offline gates**

```sh
uv sync --locked --all-packages
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
./tests/repository-structure/test.sh
git diff --check origin/main...HEAD
```

- [ ] **Step 2: Run focused mutation tests**

Mutate tenant qualification, requester visibility, outcome compilation, freshness derivation,
effective-scope subset, role checks, approval completeness, subject/proposal/revision equality,
policy expiry, and cancellation. Inspect and eliminate every surviving critical mutation.

- [ ] **Step 3: Review the complete diff**

Check architecture direction, exact field/table conformance, transaction boundaries, tenant
qualification, replay, failure classification, private/public separation, dependency restrictions,
absence of SQL/grant/provider effects, docs, and accidental files.

- [ ] **Step 4: Request independent review**

Give the reviewer the spec, this plan, fresh `origin/main` diff, focused/full test counts, mutation
evidence, and explicit control-plane non-claims. Address confirmed findings one logical commit at a
time and rerun affected plus complete gates.

- [ ] **Step 5: Record completion status**

Update the plan header only after every required gate and review passes. Do not mark source
acquisition, query execution, grants, delivery, expiry, or revocation complete.

## Final Completion Gate

Plan 3B is complete only when every Definition-of-Done item in the reviewed spec has a passing
focused test, the addendum and conformance suite agree with the implementation, all atomic and
privacy boundaries survive mutation testing, the complete offline and structure gates pass, and
independent review has no unresolved blocking finding.
