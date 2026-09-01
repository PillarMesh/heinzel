# Plan 4A Managed Source Acquisition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a destination-neutral, immutable, replay-safe acquisition boundary for PostgreSQL
and Stripe that advances private source checkpoints only after an exact downstream acknowledgement.

**Architecture:** Add acquisition-specific contracts beside the existing M0 provider models so the
signed-graph path remains compatible. Providers capture bounded source facts; runtime validates and
publishes canonical artifacts; `services/state` owns prepared-batch and checkpoint authority; and
`services/connection-broker` owns opaque source bindings and credential capabilities. PostgreSQL
establishes the shared path in Milestone A, and Stripe proves source substitutability in Milestone B.

**Tech Stack:** Python 3.13, frozen Pydantic v2 models, SQLite transactions, psycopg 3, httpx,
canonical JSON Lines, SHA-256 canonical digests, cryptography, pytest, Hypothesis, mutmut, uv.

**Spec:** `docs/superpowers/specs/2026-08-31-plan-4a-managed-source-acquisition-design.md`

## Global Constraints

- Preserve the `Integration Contract -> Semantic IIR -> Physical Plan -> Execution Graph` boundary.
- Do not add scheduling, queues, destination writes, transformation, freshness, BI, or delivery.
- Keep the M0 `SourceBoundary` and `SourceProvider.read_snapshot()` path compatible while adding the
  acquisition boundary; migrate call sites explicitly rather than overloading their meanings.
- Every durable public model is frozen, rejects unknown fields, uses timezone-aware UTC timestamps,
  and derives identity from canonical bytes without timestamps.
- Provider-private cursors, identifiers, endpoints, SQL, request IDs, credentials, rows, and
  artifact paths never enter public evidence, logs, or exception text.
- Ceilings refuse the entire batch; they never truncate and advance a cursor.
- A checkpoint advances only in the same transaction that accepts the exact acknowledgement.
- PostgreSQL incrementals require a positive `max_write_transaction_duration` and use
  `lower < (updated_at, primary_key) <= lagged_upper` with native row-value comparison.
- Stripe incrementals require a positive event overlap window, prove continuity with the prior
  event, and produce `ResynchronizationRequired` when continuity cannot be proven.
- Pin Stripe request normalization to API version `2026-02-25.clover`. The official API reference
  verified on 2026-08-31 documents v1 cursor pagination, reverse chronological lists, `created`
  filters for all four approved resources, Events retrieval for up to 30 days, and event payloads
  rendered under their creation-time API version.
- Use dedicated least-privilege principals and prove both intended reads and denied mutation,
  administration, and unrelated-resource access.
- Use TDD for each behavior: observe a focused failure, implement the minimum change, then rerun the
  focused and affected suites. Critical authorization, cursor, ceiling, replay, and checkpoint
  predicates also require focused mutation testing.
- Milestone A must be independently mergeable with shared contracts, connection binding, state,
  runtime, artifact storage, evidence, PostgreSQL acquisition, and PostgreSQL acceptance.
- Milestone B begins from Milestone A's committed public contracts and is independently mergeable
  with Stripe acquisition and shared conformance acceptance.

## File and Responsibility Map

### Shared acquisition surface

- `packages/provider-sdk/src/pillarmesh_provider_sdk/acquisition_models.py`: strict provider-neutral
  intent, schema, record, boundary, segment, batch, receipt, acknowledgement, and governed-outcome
  models.
- `packages/provider-sdk/src/pillarmesh_provider_sdk/acquisition_protocols.py`: provider session,
  page/record iterator, private cursor codec, artifact-reader, and source-provider protocols.
- `packages/provider-sdk/src/pillarmesh_provider_sdk/acquisition_encoding.py`: canonical JSONL
  streaming and segment digest/count verification.
- `packages/provider-sdk/src/pillarmesh_provider_sdk/source_conformance.py`: reusable provider
  conformance assertions and fake-driver contracts used by PostgreSQL and Stripe.
- `packages/provider-sdk/src/pillarmesh_provider_sdk/errors.py`: precise provider failure classes
  without implementation leakage.

### Opaque source bindings

- `services/connection-broker/src/pillarmesh_connection_broker/models.py`: public immutable source
  binding and validation evidence.
- `services/connection-broker/src/pillarmesh_connection_broker/private_state.py`: encrypted/private
  endpoint and credential capability records.
- `services/connection-broker/src/pillarmesh_connection_broker/repository.py`: tenant-qualified,
  optimistic SQLite persistence.
- `services/connection-broker/src/pillarmesh_connection_broker/service.py`: lifecycle, validation,
  rotation, suspension, and retirement rules.
- `services/connection-broker/src/pillarmesh_connection_broker/protocols.py`: private resolver and
  provider capability-probe interfaces.

### Acquisition authority and artifacts

- `services/state/src/pillarmesh_state/models.py`: private checkpoint, prepared state, encrypted
  cursor envelope, and atomic checkpoint receipt records.
- `services/state/src/pillarmesh_state/crypto.py`: injected cursor encryption/decryption boundary.
- `services/state/src/pillarmesh_state/repository.py`: schema migration, exact prepared replay,
  acknowledgement compare-and-set, and governed outcomes.
- `services/state/src/pillarmesh_state/artifacts.py`: tenant-private, digest-addressed local
  streaming artifact store with fsync and atomic rename.

### Runtime and evidence

- `services/runtime/src/pillarmesh_runtime/acquisition.py`: admission, provider consumption,
  validation, ceiling refusal, artifact publication, preparation, replay, and acknowledgement.
- `services/runtime/src/pillarmesh_runtime/acquisition_errors.py`: sanitized runtime errors mapped
  from provider classifications.
- `services/evidence/src/pillarmesh_evidence/acquisition.py`: allowlisted public acquisition receipt
  and scanner surface.

### Providers and cross-component proof

- `providers/postgresql/src/pillarmesh_provider_postgresql/acquisition.py`: generic approved-table
  snapshot, compound cursor, lagged upper bound, and reconciliation.
- `providers/postgresql/src/pillarmesh_provider_postgresql/acquisition_settings.py`: strict source
  object declaration and positive write-transaction-duration constraint.
- `providers/stripe/src/pillarmesh_provider_stripe/`: strict settings, HTTP client boundary,
  creation-version codecs, object snapshots, overlapping Events acquisition, and reconciliation.
- `tests/conformance/source_acquisition.py`: reusable fake source and consumer harness.
- `tests/conformance/test_source_acquisition.py`: shared provider and lifecycle conformance.
- `tests/end-to-end/test_plan4a_postgresql_acquisition.py`: Milestone A journey.
- `tests/end-to-end/test_plan4a_stripe_acquisition.py`: Milestone B journey.
- `tests/fault-injection/test_source_acquisition_recovery.py`: write-boundary and cleanup faults.
- `tests/acceptance/run_plan4a.py`: deterministic witnessed source-only acceptance runner.
- `docs/plan4a/`: setup, acceptance, teardown, evidence, limits, and recovery runbooks.

---

## Milestone A: Shared Contracts, State, Runtime, and PostgreSQL

### Task 1: Ratify and implement the provider-neutral acquisition contracts

**Files:**
- Create: `packages/provider-sdk/src/pillarmesh_provider_sdk/acquisition_models.py`
- Modify: `packages/provider-sdk/src/pillarmesh_provider_sdk/models.py`
- Modify: `packages/provider-sdk/src/pillarmesh_provider_sdk/__init__.py`
- Modify: `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md`
- Modify: `tests/conformance/test_specification_conformance.py`
- Test: `packages/provider-sdk/tests/test_acquisition_models.py`

**Interfaces:**
- Consumes: `ArtifactModel`, `canonical_bytes`, and `digest` from `pillarmesh_contract_model`.
- Produces: `AcquisitionIntent`, `AcquisitionObjectSchema`, `AcquisitionRecord`,
  `AcquisitionBoundary`, `AcquisitionSegmentManifest`, `AcquisitionBatchManifest`,
  `AcquisitionPreparedReceipt`, `AcquisitionAcknowledgement`, `AcquisitionCheckpointReceipt`,
  `AcquisitionNoValidPlan`, and `ResynchronizationRequired`.

- [ ] **Step 1: Write strict-model and identity failures first**

  Add tests that reject unknown fields, naive timestamps, unordered/duplicate fields, mismatched
  supplied `intent_key`, unsupported scalar values, duplicate object references, non-positive
  ceilings, and non-canonical totals. Prove that changing `admitted_at` or `prepared_at` does not
  change the deterministic key while changing tenant, contract, binding, mode, object order, prior
  revision, candidate checkpoint, or segment digest does.

  ```python
  def test_intent_key_excludes_admission_time_but_binds_authority() -> None:
      first = acquisition_intent(admitted_at=UTC_A)
      replay = acquisition_intent(admitted_at=UTC_B, intent_key=first.intent_key)
      other_tenant = first.model_copy(update={"tenant_id": "tenant-b"})

      assert replay.intent_key == first.intent_key
      with pytest.raises(ValidationError, match="intent_key"):
          AcquisitionIntent.model_validate(other_tenant.model_dump())
  ```

- [ ] **Step 2: Run the focused tests and capture the expected import failure**

  Run: `uv run pytest packages/provider-sdk/tests/test_acquisition_models.py -q`

  Expected: FAIL because `pillarmesh_provider_sdk.acquisition_models` does not exist.

- [ ] **Step 3: Add the frozen acquisition models and canonical validators**

  Define closed vocabularies as PEP 695 aliases and use a private base model for UTC validation.
  Keep `fields` as an ordered tuple of typed entries rather than a dictionary.

  ```python
  type AcquisitionMode = Literal["snapshot", "incremental", "reconciliation"]
  type AcquisitionScalar = None | bool | int | Decimal | str | datetime


  class AcquisitionFieldValue(ProviderModel):
      name: str = Field(min_length=1)
      value: AcquisitionScalar


  class AcquisitionRecord(ProviderModel):
      schema_version: Literal["1"] = "1"
      logical_object_ref: str = Field(min_length=1)
      record_key: str = Field(min_length=1)
      source_created_at: datetime | None
      source_updated_at: datetime | None
      operation: Literal["upsert"] = "upsert"
      fields: tuple[AcquisitionFieldValue, ...]
  ```

  Implement model-level validators for deterministic IDs, ordered uniqueness, exact manifest
  totals, UTC timestamps, and digest shapes. Widen only `ProviderObservation.provider` to include
  `stripe`; leave the M0 `SourceBoundary` unchanged.

- [ ] **Step 4: Pin the exact model fields and Plan 4A non-claims in the addendum**

  Add dedicated Plan 4A sections containing fenced model shapes, prepared/acknowledged transitions,
  PostgreSQL lag semantics, Stripe overlap semantics, source ownership for Subscription and Account
  Segment, refusal ceilings, privacy allowlists, and explicit exclusions for destination effects,
  scheduling, transformation, freshness, and delivery. Extend specification-conformance tests to
  compare every documented field list with `model_fields`.

- [ ] **Step 5: Run model and specification conformance tests**

  Run: `uv run pytest packages/provider-sdk/tests/test_acquisition_models.py tests/conformance/test_specification_conformance.py -q`

  Expected: PASS.

- [ ] **Step 6: Commit the contract boundary**

  ```bash
  git add packages/provider-sdk docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md tests/conformance/test_specification_conformance.py
  git commit -m "feat(acquisition): add provider-neutral contracts"
  ```

### Task 2: Add provider sessions, canonical encoding, and shared conformance

**Files:**
- Create: `packages/provider-sdk/src/pillarmesh_provider_sdk/acquisition_protocols.py`
- Create: `packages/provider-sdk/src/pillarmesh_provider_sdk/acquisition_encoding.py`
- Create: `packages/provider-sdk/src/pillarmesh_provider_sdk/source_conformance.py`
- Modify: `packages/provider-sdk/src/pillarmesh_provider_sdk/errors.py`
- Modify: `packages/provider-sdk/src/pillarmesh_provider_sdk/__init__.py`
- Test: `packages/provider-sdk/tests/test_acquisition_encoding.py`
- Test: `packages/provider-sdk/tests/test_source_conformance.py`

**Interfaces:**
- Consumes: Task 1 acquisition models.
- Produces: `AcquisitionProvider`, `AcquisitionSession`, `CompletedAcquisition`,
  `AcquisitionArtifactReader`, `encode_canonical_jsonl()`, and `SourceConformanceScenario`.

- [ ] **Step 1: Write protocol and canonical-byte tests**

  Test final-newline encoding, decimal/timestamp normalization, chunked reads, byte and record-set
  digests, abandoned-session cleanup, malformed records, contradictory duplicate identities, and
  that exception strings contain only the stable provider class and safe reason code.

- [ ] **Step 2: Confirm the tests fail before implementation**

  Run: `uv run pytest packages/provider-sdk/tests/test_acquisition_encoding.py packages/provider-sdk/tests/test_source_conformance.py -q`

  Expected: FAIL on missing imports.

- [ ] **Step 3: Define the narrow provider session**

  ```python
  class AcquisitionSession(Protocol):
      def __iter__(self) -> Iterator[AcquisitionRecord]: ...
      def complete(self) -> CompletedAcquisition: ...
      def abort(self) -> None: ...


  class AcquisitionProvider(Protocol):
      def observe_source(
          self,
          request: SourceObservationRequest,
      ) -> AcquisitionSourceObservation: ...
      def open_acquisition(
          self,
          intent: AcquisitionIntent,
          schemas: tuple[AcquisitionObjectSchema, ...],
          private_cursor: bytes | None,
      ) -> AcquisitionSession: ...
  ```

  `complete()` must fail before full consumption. `abort()` is idempotent, and cleanup failures are
  suppressed while preserving the original error. `AcquisitionSourceObservation` binds tenant,
  source binding, provider kind, and one canonically ordered observation for every requested
  logical object. Provider-owned model instances are reparsed at the boundary rather than trusted.

- [ ] **Step 4: Implement streaming canonical JSONL and conformance helpers**

  Encode one `canonical_bytes(record)` payload per line, update SHA-256 incrementally over the exact
  chunks, and compute the record-set digest from ordered model digests. Shared helpers must assert
  ordering, pagination completeness through expected identities, ceiling refusal, replay equality,
  cross-tenant denial probes, cursor privacy, and prepared-versus-acknowledged artifact linkage
  without knowing PostgreSQL or Stripe types. Cursor containment covers raw values and common
  reversible encodings, and typed provider failures are reconstructed from allowlisted fields so a
  provider subclass cannot alter public error text. Task 9 extends these helpers over the real state
  and runtime to prove tenant isolation and the durable prepared-versus-acknowledged transition.

- [ ] **Step 5: Run provider SDK tests and mutation checks for encoding predicates**

  Run: `uv run pytest packages/provider-sdk/tests -q`

  Run from `packages/provider-sdk`: `uv run mutmut run`

  Expected: tests PASS and no critical digest, ordering, or ceiling mutation survives.

- [ ] **Step 6: Commit the reusable provider boundary**

  ```bash
  git add packages/provider-sdk
  git commit -m "feat(acquisition): add source provider protocol"
  ```

### Task 3: Implement opaque managed source bindings

**Files:**
- Create: `services/connection-broker/pyproject.toml`
- Create: `services/connection-broker/src/pillarmesh_connection_broker/{__init__,models,private_state,protocols,repository,service}.py`
- Create: `services/connection-broker/src/pillarmesh_connection_broker/py.typed`
- Test: `services/connection-broker/tests/test_models.py`
- Test: `services/connection-broker/tests/test_repository.py`
- Test: `services/connection-broker/tests/test_service.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: provider capability probes and injected secret resolver.
- Produces: `SourceConnectionBinding`, `SourceBindingValidationEvidence`,
  `PrivateSourceCapability`, `SourceBindingRepository`, and `SourceBindingService`.

- [ ] **Step 1: Write lifecycle, immutability, tenant, and credential-rotation failures**

  Pin the exact transition table from the spec. Test that tenant, provider, connection handle, and
  account mode never change; `ready` requires matching positive and denied probes; a credential
  revision increment returns to `validating`; stale and cross-tenant revisions disclose no object;
  and retired records are terminal.

- [ ] **Step 2: Verify the tests fail because the package is absent**

  Run: `uv run pytest services/connection-broker/tests -q`

  Expected: FAIL on missing package.

- [ ] **Step 3: Add package metadata and strict binding models**

  ```python
  class SourceConnectionBinding(ArtifactModel):
      schema_version: Literal["1"] = "1"
      binding_id: str
      tenant_id: str
      provider_kind: Literal["postgresql", "stripe"]
      connection_handle: str
      account_mode: Literal["not_applicable", "test", "live"]
      lifecycle_state: SourceConnectionBindingState
      approved_object_refs: tuple[str, ...]
      capability_profile_digest: Digest | None
      source_observation_ref: str | None
      credential_revision: int = Field(ge=1)
      revision: int = Field(ge=1)
      created_at: datetime
      updated_at: datetime
  ```

  Add the package to uv workspace members and mypy packages; regenerate `uv.lock` with `uv lock`.

- [ ] **Step 4: Implement transactional repository and service transitions**

  Store the public binding and the private capability in one tenant-qualified transaction. Validation
  evidence binds tenant, binding revision, credential revision, provider kind, positive probe,
  denied probe, observation reference, and capability digest. Map stale revisions and ownership to
  typed generic errors.

- [ ] **Step 5: Run focused tests and structure validation**

  Run: `uv run pytest services/connection-broker/tests -q`

  Run: `./tests/repository-structure/test.sh`

  Expected: PASS.

- [ ] **Step 6: Commit the source-binding lifecycle**

  ```bash
  git add services/connection-broker pyproject.toml uv.lock
  git commit -m "feat(connections): add managed source bindings"
  ```

### Task 4: Implement private checkpoint and prepared-batch authority

**Files:**
- Create: `services/state/pyproject.toml`
- Create: `services/state/src/pillarmesh_state/{__init__,models,crypto,repository}.py`
- Create: `services/state/src/pillarmesh_state/py.typed`
- Test: `services/state/tests/test_models.py`
- Test: `services/state/tests/test_repository.py`
- Test: `services/state/tests/test_crypto.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: Task 1 receipts and acknowledgements; injected `CursorCipher`.
- Produces: `SourceCheckpointState`, `PreparedAcquisitionState`, `AcquisitionStateRepository`,
  `prepare_exact()`, `acknowledge_exact()`, and governed outcome persistence.

- [ ] **Step 1: Write red tests for exact replay and atomic acknowledgement**

  Cover initial revision zero, one prepared batch per tenant/contract/source/prior revision,
  byte-identical replay, contradictory replay, stale acknowledgement, wrong tenant/consumer/contract/
  manifest/candidate digest, acknowledgement replay, zero-record revision advancement, evidence-write
  failure, compare-and-set failure, rollback failure, and close failure.

- [ ] **Step 2: Confirm missing-package failure**

  Run: `uv run pytest services/state/tests -q`

  Expected: FAIL on missing package.

- [ ] **Step 3: Implement strict private models and encryption boundary**

  ```python
  class CursorCipher(Protocol):
      def encrypt(self, *, tenant_id: str, plaintext: bytes) -> bytes: ...
      def decrypt(self, *, tenant_id: str, ciphertext: bytes) -> bytes: ...


  class SourceCheckpointState(StateModel):
      tenant_id: str
      contract_digest: Digest
      source_binding_ref: str
      provider_kind: Literal["postgresql", "stripe"]
      cursor_version: str
      revision: int = Field(ge=0)
      encrypted_cursor_payload: bytes
      cursor_digest: Digest
      last_batch_id: str | None
      created_at: datetime
      updated_at: datetime
  ```

  Plain cursor bytes must never appear in SQLite payloads, model representations, errors, or logs.

- [ ] **Step 4: Implement one-transaction acknowledgement compare-and-set**

  Begin `IMMEDIATE`, load by all tenant-qualified authority fields, validate exact acknowledgement,
  mark prepared state acknowledged, insert revision `prior + 1`, insert checkpoint receipt, and append
  public evidence before commit. Suppress rollback and close errors while re-raising the original.

- [ ] **Step 5: Run tests and critical predicate mutations**

  Run: `uv run pytest services/state/tests -q`

  Run: `uv run mutmut run --paths-to-mutate services/state/src/pillarmesh_state/repository.py`

  Expected: tests PASS; no tenant, revision, digest, or atomicity mutation survives.

- [ ] **Step 6: Commit state authority**

  ```bash
  git add services/state pyproject.toml uv.lock
  git commit -m "feat(state): add acquisition checkpoints"
  ```

### Task 5: Add the digest-addressed streaming artifact store

**Files:**
- Create: `services/state/src/pillarmesh_state/artifacts.py`
- Test: `services/state/tests/test_artifacts.py`

**Interfaces:**
- Consumes: tenant ID, expected SHA-256 digest, and a binary reader.
- Produces: `LocalAcquisitionArtifactStore.put_if_absent()`, `open_verified()`, and
  `exists_verified()`.

- [ ] **Step 1: Write durability and path-safety tests**

  Cover chunked writes, partial write, digest mismatch, existing identical content, existing
  contradictory content, crash before rename, traversal digest, symlink substitution, tenant
  collision, fsync failure, rename failure, and cleanup failure preserving the primary error.

- [ ] **Step 2: Verify the focused tests fail**

  Run: `uv run pytest services/state/tests/test_artifacts.py -q`

  Expected: FAIL on missing `pillarmesh_state.artifacts`.

- [ ] **Step 3: Implement temp-write, fsync, rename, and directory-sync ordering**

  ```python
  def put_if_absent(
      self,
      *,
      tenant_id: str,
      artifact_digest: str,
      reader: BinaryIO,
  ) -> None:
      target = self._validated_target(tenant_id, artifact_digest)
      # Stream to a same-directory temporary file, verify exact digest, fsync,
      # atomically rename, then fsync the directory before returning.
  ```

  Never trust a pre-existing path without reopening it using no-follow semantics and verifying all
  bytes against the requested digest.

- [ ] **Step 4: Run artifact and state suites**

  Run: `uv run pytest services/state/tests -q`

  Expected: PASS.

- [ ] **Step 5: Commit artifact durability**

  ```bash
  git add services/state
  git commit -m "feat(state): add acquisition artifact store"
  ```

### Task 6: Implement acquisition evidence and runtime preparation

**Files:**
- Create: `services/evidence/src/pillarmesh_evidence/acquisition.py`
- Modify: `services/evidence/src/pillarmesh_evidence/__init__.py`
- Test: `services/evidence/tests/test_acquisition.py`
- Create: `services/runtime/src/pillarmesh_runtime/acquisition_errors.py`
- Create: `services/runtime/src/pillarmesh_runtime/acquisition.py`
- Modify: `services/runtime/src/pillarmesh_runtime/__init__.py`
- Modify: `services/runtime/pyproject.toml`
- Test: `services/runtime/tests/test_acquisition.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: ready binding resolver, activated contract projection, source observation, exact state
  revision, provider resolver, cursor cipher, artifact store, and evidence writer.
- Produces: `AcquisitionRunner.prepare()` and `AcquisitionEvidenceReceipt` with prepared, no-valid-
  plan, resynchronization-required, or failed outcomes.

The activated projection pins binding revision, credential revision, capability profile digest,
source observation reference, and source observation digest. Prepared authority persists the exact
provider cursor version, and public failure reasons use a closed allowlist.

- [ ] **Step 1: Write admission, preparation, ceiling, and privacy failures**

  Assert exact binding/observation/tenant agreement, activated contract, intent narrowing, positive
  ceilings, deterministic key, exact checkpoint, provider classification mapping, record/schema
  matching, deterministic ordering, zero records, later-page ceiling crossing, abandoned iterator,
  incomplete boundary, artifact fault, prepared-state fault, and public evidence canary exclusion.

- [ ] **Step 2: Verify the tests fail before production code**

  Run: `uv run pytest services/evidence/tests/test_acquisition.py services/runtime/tests/test_acquisition.py -q`

  Expected: FAIL on missing acquisition modules.

- [ ] **Step 3: Add the structurally allowlisted evidence receipt**

  ```python
  class AcquisitionEvidenceReceipt(ArtifactModel):
      schema_version: Literal["1"] = "1"
      evidence_id: str
      tenant_id: str
      run_intent_ref: str
      contract_ref: str
      source_binding_ref: str
      acquisition_mode: AcquisitionMode
      logical_object_refs: tuple[str, ...]
      prepared_receipt_ref: str | None
      checkpoint_receipt_ref: str | None
      prior_checkpoint_revision: int
      resulting_checkpoint_revision: int | None
      reason_codes: tuple[AcquisitionPublicReasonCode, ...]
      outcome: AcquisitionEvidenceOutcome
      created_at: datetime
  ```

  Use independent opaque receipt references. Do not derive public IDs from source facts or content
  digests.

- [ ] **Step 4: Implement runtime admission and preparation**

  Validate authority before resolving credentials or invoking a provider. Stream records through the
  exact schema validator and temporary encoders, refuse ceilings before publication, then publish
  segment artifacts, manifest, and prepared state in that order. A provider/private failure maps to
  a typed sanitized runtime error or governed outcome; it never becomes an empty batch. Provider-
  declared cursor expiry or overlap discontinuity becomes durable `ResynchronizationRequired`, while
  an unclassified exception fails integrity rather than becoming retryable.

- [ ] **Step 5: Run affected component suites and inspect public serialization**

  Run: `uv run pytest services/evidence/tests services/runtime/tests packages/provider-sdk/tests -q`

  Expected: PASS and privacy canary scan finds no forbidden field or value.

- [ ] **Step 6: Commit preparation lifecycle**

  ```bash
  git add services/evidence services/runtime uv.lock
  git commit -m "feat(runtime): prepare acquisition batches"
  ```

### Task 7: Implement exact replay and acknowledgement

**Files:**
- Modify: `services/runtime/src/pillarmesh_runtime/acquisition.py`
- Modify: `services/state/src/pillarmesh_state/repository.py`
- Test: `services/runtime/tests/test_acquisition_acknowledgement.py`
- Test: `tests/fault-injection/test_source_acquisition_recovery.py`

**Interfaces:**
- Consumes: exact prepared state and `AcquisitionAcknowledgement`.
- Produces: `AcquisitionRunner.acknowledge()` returning `AcquisitionCheckpointReceipt` and exact
  replay on duplicate acknowledgement.

- [ ] **Step 1: Write replay, race, and write-boundary fault tests**

  Inject faults before/after every artifact, prepared-state, acknowledgement, checkpoint, and
  evidence write. Test a competing different batch, cancellation/retirement race, newer checkpoint,
  stale acknowledgement, exact replay, and cleanup failures. Every failure must leave either safe
  orphan artifacts or a complete transaction, never a moved checkpoint without its receipt.

- [ ] **Step 2: Confirm focused failures**

  Run: `uv run pytest services/runtime/tests/test_acquisition_acknowledgement.py tests/fault-injection/test_source_acquisition_recovery.py -q`

  Expected: FAIL on missing acknowledgement behavior.

- [ ] **Step 3: Add replay-first preparation and acknowledgement orchestration**

  Before provider invocation, look for the authority-key prepared state. Return only an exact
  receipt. During acknowledgement, delegate the complete compare-and-set transaction to state and
  append an acknowledged evidence receipt that names only the new revision and opaque receipt ref.

- [ ] **Step 4: Run runtime, state, and fault suites**

  Run: `uv run pytest services/runtime/tests services/state/tests tests/fault-injection/test_source_acquisition_recovery.py -q`

  Expected: PASS.

- [ ] **Step 5: Commit replay and acknowledgement**

  ```bash
  git add services/runtime services/state tests/fault-injection/test_source_acquisition_recovery.py
  git commit -m "feat(acquisition): commit exact acknowledgements"
  ```

### Task 8: Generalize PostgreSQL source snapshots

**Files:**
- Create: `providers/postgresql/src/pillarmesh_provider_postgresql/acquisition_settings.py`
- Create: `providers/postgresql/src/pillarmesh_provider_postgresql/acquisition.py`
- Modify: `providers/postgresql/src/pillarmesh_provider_postgresql/__init__.py`
- Test: `providers/postgresql/tests/test_acquisition_snapshot.py`

**Interfaces:**
- Consumes: approved logical object schemas, opaque connection capability, and snapshot intent.
- Produces: `PostgreSQLAcquisitionProvider.open_acquisition()` for generic approved base tables.

- [ ] **Step 1: Write generic snapshot and least-privilege failures**

  Test exact projection, single non-null primary/unique B-tree key, timezone-aware updated timestamp,
  base-table-only admission, unrelated-schema denial, all mutation/administration denials, count
  ceiling before row read, primary-key order, exact bounds/snapshot/query digests, drift, malformed
  scalar, partial consumption rollback, and cleanup failure suppression.

- [ ] **Step 2: Confirm the tests fail**

  Run: `uv run pytest providers/postgresql/tests/test_acquisition_snapshot.py -q`

  Expected: FAIL on missing provider.

- [ ] **Step 3: Implement identifier-safe generic projection inside repeatable read**

  Build every identifier with `psycopg.sql.Identifier`; bind values as parameters. Capture metadata,
  snapshot identity, exact key bounds/count, and a private boundary detail before opening the named
  cursor. Convert scalar values according to the approved schema, never inferred provider output.

- [ ] **Step 4: Prove the test fails without rollback, then pass it**

  Temporarily disable the provider's abort rollback and confirm the partial-consumption test fails;
  restore the implementation and rerun the full PostgreSQL provider suite.

  Run: `uv run pytest providers/postgresql/tests -q`

  Expected: PASS.

- [ ] **Step 5: Commit generic snapshots**

  ```bash
  git add providers/postgresql
  git commit -m "feat(postgresql): acquire approved table snapshots"
  ```

### Task 9: Add PostgreSQL lagged incrementals, reconciliation, and Milestone A acceptance

**Files:**
- Modify: `providers/postgresql/src/pillarmesh_provider_postgresql/acquisition.py`
- Test: `providers/postgresql/tests/test_acquisition_incremental.py`
- Create: `tests/conformance/source_acquisition.py`
- Create: `tests/conformance/test_source_acquisition.py`
- Create: `tests/end-to-end/test_plan4a_postgresql_acquisition.py`
- Create: `tests/acceptance/run_plan4a.py`
- Create: `tests/acceptance/test_run_plan4a.py`

**Interfaces:**
- Consumes: `PostgreSQLIncrementalCursor(updated_at, primary_key)` and positive contract lag.
- Produces: deterministic snapshot/incremental/reconciliation sessions passing shared conformance.

- [ ] **Step 1: Write row-value, lag, late-commit, and reconciliation failures**

  Cover strict lower/inclusive upper bounds, timestamp ties, native key types, exact upper-bound
  update, zero/missing lag refusal, lagged upper cursor rather than greatest visible/last returned,
  xact-start tightening, an unavailable statistics view, backward timestamp/delete limitations,
  ceiling preflight, empty incremental, and complete reconciliation digest.

  The late-commit test must perform this real sequence in the fake transaction model: open writer;
  assign an earlier `updated_at`; open acquisition snapshot; commit writer; acknowledge the first
  batch; run the next incremental; assert the row appears.

- [ ] **Step 2: Run and observe predicate failures**

  Run: `uv run pytest providers/postgresql/tests/test_acquisition_incremental.py -q`

  Expected: FAIL because incremental mode is unsupported.

- [ ] **Step 3: Implement lagged bounded SQL**

  ```sql
  WHERE (updated_at, primary_key) > (%s, %s)
    AND (updated_at, primary_key) <= (%s, %s)
  ORDER BY updated_at, primary_key
  ```

  Compute `lag_bound = snapshot_time - max_write_transaction_duration`, select the greatest visible
  row at or below it, and commit that boundary even for an empty batch. Record whether the declared
  lag or the oldest visible `xact_start` tightened the bound in private detail only.

- [ ] **Step 4: Build the shared conformance harness and PostgreSQL end-to-end journey**

  Use the real state repository, runtime, evidence model, local artifact store, and strict consumer.
  Include orders, subscriptions with lifecycle status, and account segments; two tenants with
  colliding provider IDs; drift; denied writes; replay; exact acknowledgement; stale/cross-tenant
  denial; late commit; refusal ceilings; and assertion that no destination provider is resolved.

- [ ] **Step 5: Run Milestone A gates and cursor mutation tests**

  Run: `uv run pytest providers/postgresql/tests tests/conformance/test_source_acquisition.py tests/end-to-end/test_plan4a_postgresql_acquisition.py tests/acceptance/test_run_plan4a.py -q`

  Run: `uv run mutmut run --paths-to-mutate providers/postgresql/src/pillarmesh_provider_postgresql/acquisition.py`

  Expected: PASS and no critical lower/upper/lag/order/tenant/ceiling mutation survives.

- [ ] **Step 6: Commit Milestone A**

  ```bash
  git add providers/postgresql tests/conformance tests/end-to-end/test_plan4a_postgresql_acquisition.py tests/acceptance
  git commit -m "feat(postgresql): add bounded incremental acquisition"
  ```

---

## Milestone B: Stripe on the Shared Acquisition Boundary

### Task 10: Add strict Stripe settings, client boundary, and versioned normalization

**Files:**
- Create: `providers/stripe/pyproject.toml`
- Create: `providers/stripe/src/pillarmesh_provider_stripe/{__init__,settings,client,models,codecs}.py`
- Create: `providers/stripe/src/pillarmesh_provider_stripe/py.typed`
- Test: `providers/stripe/tests/test_settings.py`
- Test: `providers/stripe/tests/test_client.py`
- Test: `providers/stripe/tests/test_codecs.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: opaque restricted-key capability and exact approved object/field schemas.
- Produces: `StripeSettings`, `StripeClient` protocol, `HttpxStripeClient`, and creation-version
  codecs for Customer, Invoice, Charge, Refund, and approved event types.

- [ ] **Step 1: Write settings, transport, and payload-denial tests**

  Reject live/test mismatch, absent explicit API version, unsupported creation versions, wrong list
  URL/object kind, unexpected expansions, unapproved fields after normalization, unrestricted
  metadata, sensitive fields, malformed pages, unknown event types, request/body leakage, and wrong
  account mode. Assert 429 is throttled while transport, availability, authorization, statement, and
  malformed-response classes remain distinct.

- [ ] **Step 2: Confirm the package is absent**

  Run: `uv run pytest providers/stripe/tests/test_settings.py providers/stripe/tests/test_client.py providers/stripe/tests/test_codecs.py -q`

  Expected: FAIL on missing package.

- [ ] **Step 3: Add package and pinned HTTP request boundary**

  Declare direct `httpx>=0.28,<1`, provider SDK, and contract-model dependencies. Send
  `Stripe-Version: 2026-02-25.clover` on every request. Keep Authorization, account identity,
  request ID, response bodies, pagination cursors, and URLs behind the client boundary.

- [ ] **Step 4: Add explicit allowlist codecs**

  Each codec consumes an untrusted mapping, verifies the exact object discriminator and live mode,
  extracts only approved scalar fields, rejects expansions, and returns `AcquisitionRecord`.
  Support one separately approved customer-identity metadata key; deny every other metadata key.

- [ ] **Step 5: Run Stripe foundation tests and lock checks**

  Run: `uv lock --check`

  Run: `uv run pytest providers/stripe/tests/test_settings.py providers/stripe/tests/test_client.py providers/stripe/tests/test_codecs.py -q`

  Expected: PASS.

- [ ] **Step 6: Commit Stripe foundation**

  ```bash
  git add providers/stripe pyproject.toml uv.lock
  git commit -m "feat(stripe): add versioned source client"
  ```

### Task 11: Implement Stripe snapshots, overlapping Events, and reconciliation

**Files:**
- Create: `providers/stripe/src/pillarmesh_provider_stripe/acquisition.py`
- Modify: `providers/stripe/src/pillarmesh_provider_stripe/__init__.py`
- Test: `providers/stripe/tests/test_snapshot.py`
- Test: `providers/stripe/tests/test_events.py`
- Test: `providers/stripe/tests/test_reconciliation.py`

**Interfaces:**
- Consumes: `StripeEventCursor(last_event_created, last_event_id, api_version_set_digest)`, positive
  overlap window, approved object schemas, and `StripeClient`.
- Produces: `StripeAcquisitionProvider` passing the same `AcquisitionProvider` protocol as PostgreSQL.

- [ ] **Step 1: Write complete-pagination and deterministic-snapshot tests**

  Cover all four resources, reverse pages, same-created ties, upper `created` filter, cursor loops,
  duplicate pages, object contradictions, page-size boundaries, later-page ceilings, empty lists,
  cleanup, stable logical ordering, and one segment per resource.

- [ ] **Step 2: Write overlap continuity and cursor-gap tests**

  Cover missing/zero overlap refusal, inclusive overlap, prior event present, first overlap page
  missing prior event, event older than overlap start, 30-day expiry, duplicate event equality,
  duplicate event contradiction, timestamp ties, already-committed discard after dedupe, unsupported
  creation version, and candidate cursor from the greatest fully processed event.

- [ ] **Step 3: Confirm snapshot and event tests fail**

  Run: `uv run pytest providers/stripe/tests/test_snapshot.py providers/stripe/tests/test_events.py providers/stripe/tests/test_reconciliation.py -q`

  Expected: FAIL because the acquisition provider is missing.

- [ ] **Step 4: Implement bounded list snapshots**

  Capture one upper source timestamp, request each resource with `created[lte]`, paginate exclusively
  with `starting_after`, validate `has_more` and URL, deduplicate only byte-identical objects, normalize
  under the pinned codec, and sort by `(created, object_id)` independent of page order.

- [ ] **Step 5: Implement overlapping event acquisition and reconciliation**

  Read from `last_event_created - event_overlap_window` through captured upper time, prove the exact
  prior event appears before accepting continuity, deduplicate by event ID before discarding committed
  events, normalize under each event's `api_version`, sort by the specified compound event key, and
  return private `ResynchronizationRequired` for expiry or discontinuity. Reconciliation reruns all
  four snapshots and checks internal identity/digest consistency without destination claims.

- [ ] **Step 6: Run Stripe tests and critical mutations**

  Run: `uv run pytest providers/stripe/tests -q`

  Run: `uv run mutmut run --paths-to-mutate providers/stripe/src/pillarmesh_provider_stripe/acquisition.py`

  Expected: PASS and no critical overlap/continuity/dedupe/order/ceiling mutation survives.

- [ ] **Step 7: Commit Stripe acquisition**

  ```bash
  git add providers/stripe
  git commit -m "feat(stripe): add bounded source acquisition"
  ```

### Task 12: Prove Stripe against shared conformance and complete Milestone B acceptance

**Files:**
- Modify: `tests/conformance/source_acquisition.py`
- Modify: `tests/conformance/test_source_acquisition.py`
- Create: `tests/end-to-end/test_plan4a_stripe_acquisition.py`
- Modify: `tests/acceptance/run_plan4a.py`
- Modify: `tests/acceptance/test_run_plan4a.py`

**Interfaces:**
- Consumes: Milestone A runtime/state/artifact/evidence and Task 11 Stripe provider.
- Produces: one destination-neutral conformance result and complete source-only Plan 4A acceptance.

- [ ] **Step 1: Add Stripe to the exact shared provider scenarios**

  Parameterize provider-neutral cases for observation, denied capability, stable bounds, ordering,
  complete pagination, later-page ceiling refusal, empty batch, replay, cleanup, drift, malformed
  payload, error classification, tenant isolation, cursor containment, prepare/acknowledge, and
  reconciliation. Keep Stripe-only gap/version cases in provider tests.

- [ ] **Step 2: Add the full Stripe end-to-end journey**

  Prepare and acknowledge Customer, Invoice, Charge, and Refund snapshots; process duplicate and tied
  events; replay before acknowledgement; accept exactly once; prove expired cursor and overlap gap
  create governed resynchronization outcomes; prove contradictory reconciliation fails integrity;
  and scan all public diagnostics/evidence for canaries.

- [ ] **Step 3: Extend witnessed acceptance without destination effects**

  The strict consumer opens and verifies the manifest and segment digests but performs no warehouse
  operation. Instrument the harness so resolving PostgreSQL or ClickHouse destination capabilities is
  an immediate test failure.

- [ ] **Step 4: Run both provider journeys together**

  Run: `uv run pytest tests/conformance/test_source_acquisition.py tests/end-to-end/test_plan4a_postgresql_acquisition.py tests/end-to-end/test_plan4a_stripe_acquisition.py tests/acceptance/test_run_plan4a.py -q`

  Expected: PASS for both source classes against one lifecycle.

- [ ] **Step 5: Commit Milestone B acceptance**

  ```bash
  git add tests/conformance tests/end-to-end/test_plan4a_stripe_acquisition.py tests/acceptance
  git commit -m "test(acquisition): witness PostgreSQL and Stripe sources"
  ```

### Task 13: Add operations, privacy, and live-verification runbooks

**Files:**
- Create: `docs/plan4a/setup.md`
- Create: `docs/plan4a/acceptance-run.md`
- Create: `docs/plan4a/teardown.md`
- Create: `docs/plan4a/evidence-package.md`
- Create: `docs/plan4a/recovery.md`
- Test: `services/evidence/tests/test_acquisition_privacy.py`
- Test: `tests/integration/test_postgresql_acquisition_live.py`
- Test: `tests/integration/test_stripe_acquisition_live.py`

**Interfaces:**
- Consumes: completed offline acceptance and opt-in dedicated credentials.
- Produces: operator procedures and sanitized live evidence boundaries.

- [ ] **Step 1: Write privacy scanner failures using source canaries**

  Insert canaries for row values, keys, cursors, cursor digests, account IDs, endpoint, DSN, API key,
  Stripe request ID, SQL, response body, and artifact path. Assert every public model, package,
  diagnostic, and exception is clean while preserving opaque correlation references.

- [ ] **Step 2: Write runbooks with exact claim boundaries**

  Document source credential requirements, positive and denied probes, PostgreSQL monotonic timestamp/
  maximum transaction/delete constraints, Stripe resources/version/30-day retention/overlap limits,
  subscription-only-in-Stripe limitation, checkpoint recovery, explicit resynchronization approval,
  safe orphan-artifact retention, and fixture-scoped teardown. Distinguish offline, emulator, and live
  proof and state that acknowledgement proves no destination effect.

- [ ] **Step 3: Add opt-in live tests that create new facts**

  PostgreSQL live proof creates isolated rows including a transaction that commits after a snapshot;
  Stripe live proof creates test-mode Customer/Invoice/Charge/Refund facts or uses an isolated test
  clock. Trace each to prepared and acknowledged checkpoints, prove denied access, retain sanitized
  evidence, and clean up only exact fixtures. Mark both `live` and skip without complete credentials.

- [ ] **Step 4: Run privacy and offline documentation-adjacent tests**

  Run: `uv run pytest services/evidence/tests/test_acquisition_privacy.py tests/acceptance/test_run_plan4a.py -q`

  Expected: PASS. Report live tests as not run unless dedicated credentials are supplied.

- [ ] **Step 5: Commit operations material**

  ```bash
  git add docs/plan4a services/evidence/tests/test_acquisition_privacy.py tests/integration/test_postgresql_acquisition_live.py tests/integration/test_stripe_acquisition_live.py
  git commit -m "docs(acquisition): add Plan 4A runbooks"
  ```

### Task 14: Run complete verification and request independent review

**Files:**
- Modify only files required to fix a demonstrated Plan 4A defect.

**Interfaces:**
- Consumes: both complete milestones.
- Produces: a reviewable branch with explicit evidence and remaining live/independent-review gaps.

- [ ] **Step 1: Run formatting, lint, typing, lock, offline, and structure gates**

  ```bash
  uv sync --locked --all-packages
  uv lock --check
  uv run ruff check .
  uv run ruff format --check .
  uv run mypy
  uv run pytest -m "not live"
  ./tests/repository-structure/test.sh
  ```

  Expected: every command PASS with no skipped required gate. Live tests remain explicit opt-in gaps.

- [ ] **Step 2: Review the complete diff against fresh main**

  Run: `git diff --stat origin/main...HEAD`

  Run: `git diff --check origin/main...HEAD`

  Confirm the branch contains only Plan 4A additions, preserves M0 behavior, introduces no scheduler
  or destination call, exposes no private values, and does not revert work already in main.

- [ ] **Step 3: Run independent architecture, security, and correctness review**

  Require a reviewer other than the implementing agent to inspect contract/addendum agreement,
  provider substitutability, PostgreSQL lag proof, Stripe continuity proof, tenant predicates,
  checkpoint atomicity, refusal ceilings, evidence privacy, dependency license/maintenance posture,
  and explicit source-only non-claims. Resolve every blocking finding and rerun affected gates.

- [ ] **Step 4: Commit only verified review fixes**

  ```bash
  git add <explicit-reviewed-paths>
  git commit -m "fix(acquisition): close Plan 4A review findings"
  ```

  Omit this commit when review finds no defects. Do not push, open a pull request, merge, deploy, or
  run live credentials without separate user authorization.

## Plan Self-Review

- Spec coverage: all 14 delivery-decomposition items map to Tasks 1-14; Milestone A and Milestone B
  are independently testable and commit at Tasks 9 and 12.
- Boundary coverage: models, private cursor state, exact acknowledgement, refusal ceilings, provider
  conformance, late PostgreSQL commit, Stripe finite-window continuity, evidence privacy, and explicit
  non-claims each have focused negative tests.
- Type consistency: `AcquisitionProvider -> AcquisitionSession -> CompletedAcquisition` feeds
  `AcquisitionRunner.prepare`; prepared state consumes the resulting manifest and candidate cursor;
  `AcquisitionRunner.acknowledge` consumes the Task 1 acknowledgement and returns the Task 1
  checkpoint receipt.
- Placeholder scan: no task delegates unspecified error handling or generic tests; each behavior,
  interface, command, expected result, and commit boundary is explicit.
