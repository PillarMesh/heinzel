# Plan 4A Managed Source Acquisition Design

**Date:** 2026-08-31
**Status:** Draft for review
**Scope:** Destination-neutral PostgreSQL and Stripe acquisition for the architect-centered MVP

## 1. Purpose

PillarMesh can provision a managed warehouse, establish approved catalog and semantic authority,
and admit governed stakeholder and access proposals. It does not yet have the source acquisition
boundary needed to move approved PostgreSQL and Stripe facts toward either managed destination.

Plan 4A creates that boundary. It turns an activated Integration Contract and an approved source
observation into an immutable, replay-safe acquisition batch without writing a destination,
running a transformation, scheduling work, or claiming consumer delivery.

The plan proves two source classes behind one contract:

1. PostgreSQL initial snapshot followed by bounded `(updated_at, primary_key)` acquisition; and
2. Stripe initial object snapshots followed by event-cursor acquisition and full reconciliation.

The providers are implemented sequentially. PostgreSQL establishes the shared contracts and state
lifecycle first. Stripe then demonstrates that the boundary is not a database-specific pipeline.

## 2. Position in the delivery sequence

The managed-platform delivery sequence requires source acquisition before destination movement,
transformation, scheduling, BI, report delivery, and full operations. Plan 4A therefore follows
Plan 3B and precedes:

1. destination conformance for PostgreSQL and ClickHouse;
2. managed transformation and reconciliation;
3. scheduling, incremental run intents, retry, replay, and recovery;
4. Superset compilation, stakeholder-answer execution, access application, and report delivery;
5. the complete two-engine witnessed acceptance journey.

Plan 4A may define the exact acknowledgement interface that a later destination consumer must use.
Its acceptance harness uses a strict test consumer. It does not interpret that acknowledgement as
a warehouse effect.

## 3. Approved design decisions

The following decisions were approved in the design conversation:

1. PostgreSQL and Stripe share one acquisition contract and conformance suite but are implemented
   sequentially, PostgreSQL first.
2. PostgreSQL incremental acquisition uses a compound `(updated_at, primary_key)` cursor. Logical
   CDC remains deferred.
3. PostgreSQL physical deletes are not observable in Plan 4A. A contract requiring delete capture
   receives `No Valid Plan` before activation.
4. Stripe uses bounded object snapshots for history and an overlapping Events cursor for changes.
   Event IDs are deduplicated and periodic full object reconciliation detects gaps or disagreement.
5. A Stripe cursor outside the retrievable Events window produces a typed resynchronization
   requirement. PillarMesh never assumes continuity across an unobservable interval.
6. The Stripe MVP object set is `Customer`, `Invoice`, `Charge`, and `Refund`. `Charge` supplies the
   settled-payment fact. PaymentIntents, subscriptions, disputes, balance transactions, and
   unrestricted metadata are deferred.
7. Acquisition ends at immutable source-aligned segments, a batch manifest, a candidate checkpoint,
   and evidence. Destination writes belong to the next plan.
8. Checkpoints advance only after an exact downstream acknowledgement. An unacknowledged batch is
   replayable but cannot move source state.
9. Canonical JSON Lines is the initial segment encoding. It keeps the first source boundary strict
   and deterministic without selecting Arrow or Parquet before a destination consumer exists.
10. AI has no acquisition authority. It may explain an error or propose a mapping, but it cannot
    select source objects, widen fields, advance a cursor, acknowledge a batch, or resolve drift.

## 4. Goals

Plan 4A must provide:

- strict destination-neutral acquisition contracts;
- tenant-qualified managed source connection bindings;
- one approved database source and one approved SaaS source;
- immutable, source-aligned segments with deterministic manifests;
- private, optimistic checkpoint state;
- exact prepared-batch replay and acknowledgement;
- bounded snapshot and incremental source semantics;
- explicit drift, cursor-gap, reconciliation, and authorization outcomes;
- provider error classification without driver or API exception leakage;
- public evidence that exposes no source rows, raw cursors, credentials, endpoints, or provider IDs;
- a shared provider conformance suite; and
- an offline acceptance journey that prepares and acknowledges batches without destination effects.

## 5. Non-goals

Plan 4A does not include:

- PostgreSQL logical replication or CDC;
- OAuth authorization flows or generic credential brokering;
- MySQL, Salesforce, files, event buses, or unrestricted connector authoring;
- arbitrary Stripe resources, metadata, expansions, or search queries;
- destination staging, merge, visibility, or warehouse reconciliation;
- transformation compilation or dbt execution;
- trigger policies, cron expressions, overlap scheduling, run queues, or backfill orchestration;
- automatic resynchronization after a material gap;
- requester answer execution, warehouse grant application, expiry, or revocation;
- Superset datasets, dashboards, reports, or delivery;
- cloud artifact-store selection;
- raw provider identifiers in public evidence; or
- a general workflow engine.

## 6. Architecture and ownership

The acquisition path is:

```text
activated Integration Contract + ready source connection binding
        |
        v
AcquisitionIntent
        |
        v
services/runtime acquisition runner
        |
        +------------------------+
        |                        |
        v                        v
providers/postgresql       providers/stripe
        |                        |
        +------------+-----------+
                     |
                     v
source-aligned canonical segments
+ AcquisitionBatchManifest
+ candidate private checkpoint
                     |
                     v
durable acquisition artifact store
                     |
                     v
AcquisitionPreparedReceipt
                     |
                     v
later destination acknowledgement
                     |
                     v
services/state checkpoint commit
+ services/evidence public receipt
```

### 6.1 `packages/provider-sdk`

The provider SDK owns:

- provider-neutral source observation and acquisition protocols;
- strict field, record, page, boundary, segment, and batch contract shapes;
- provider-error classifications;
- source-provider conformance utilities; and
- fixtures that every advertised source capability must pass.

It does not own Stripe or PostgreSQL fields, credentials, pagination calls, SQL, checkpoint storage,
or runtime orchestration.

### 6.2 `providers/postgresql`

The PostgreSQL provider owns:

- source capability observation and denial probes;
- repeatable-read, read-only initial snapshots;
- bounded compound-cursor incremental queries;
- exact schema and key validation;
- normalization of approved order rows into provider-neutral records; and
- translation of `psycopg` failures into `ProviderError` classifications.

### 6.3 `providers/stripe`

The Stripe provider owns:

- source capability observation and least-privilege probes;
- API-version and account-mode validation;
- bounded list pagination for approved resources;
- event pagination, overlap, canonical ordering, and event-ID deduplication;
- allowlisted field normalization;
- rate-limit and provider-failure classification;
- object reconciliation; and
- provider-private cursor serialization.

Provider-local IDs may appear in private source-aligned segment content when they are required for
identity and lineage. They never appear in public evidence, metrics, logs, exception messages, or
deterministic IDs exposed outside the tenant boundary.

### 6.4 `services/connection-broker`

The repository layout already declares `services/connection-broker` as the owner of opaque
connection handles and credential exchange. Plan 4A creates the minimum source-binding lifecycle
needed for PostgreSQL DSNs and Stripe restricted API keys.

The connection broker owns:

- tenant-qualified `SourceConnectionBinding` records;
- provider-kind and account-mode binding;
- private endpoint and credential capabilities behind an opaque handle;
- credential revision and validation status;
- intended and denied capability-probe receipts; and
- suspension and retirement of unusable bindings.

It does not expose credentials, provider account IDs, endpoints, or private resource names to the
contract, runtime, state, or evidence models. OAuth attempts, callbacks, and token refresh remain a
later widening of the already-declared component.

### 6.5 `services/runtime`

The runtime owns an acquisition runner that:

- validates the admitted intent against the activated contract and source observation;
- loads the exact private checkpoint revision;
- invokes the selected provider capability;
- validates every provider record against the declared ordered field schema;
- writes canonical JSON Lines segments through an injected artifact store;
- verifies content and record-set digests before publication;
- records the prepared batch only after every artifact is durable; and
- returns exact replay or typed failure.

The existing M0 runtime remains compatible. Plan 4A should extract reusable source-acquisition
behavior incrementally rather than rewrite the signed-graph execution path.

### 6.6 `services/state`

The repository layout already declares `services/state` as the owner of checkpoints, epochs, leases,
and contract-scoped control loops. Plan 4A creates its first substantive implementation.

The state service owns:

- tenant-qualified private source checkpoints;
- monotonically increasing checkpoint revisions;
- provider-private cursor ciphertext and its canonical digest;
- one prepared batch per source and prior checkpoint revision;
- exact acknowledgement and checkpoint compare-and-set; and
- recovery metadata for incomplete preparation.

Neither component is new to the repository's governance. `services/connection-broker` and
`services/state` already have rows in `docs/architecture/repository-layout.md` and already appear in
the `validate_components services` allowlist in `tests/repository-structure/validate.sh`. The
Structural Change Rule fires when a governed component is first *declared*, not when a declared one
is first implemented, so this plan needs neither a new ADR nor a validator edit, and writing one
would imply a boundary change that is not happening.

What the implementation change does need, and what is easy to forget because the validator stays
silent about it:

- `[tool.uv.workspace] members` in the root `pyproject.toml` for each new package directory;
- `[tool.mypy] packages` entries for `pillarmesh_connection_broker` and `pillarmesh_state`; and
- `uv.lock` regenerated with `uv lock`.

`providers/stripe` needs the same workspace and typing entries. Provider directories are not
validated as components, so it needs no layout or validator change either.

### 6.7 `services/evidence`

The evidence service owns only append-only, allowlisted acquisition receipts. It may package public
receipts without opening private segment files or checkpoint payloads.

## 7. Contract model

All durable interface models are frozen, strict about unknown fields, canonically serializable, and
versioned.
Timestamps are timezone-aware UTC. Digests use canonical model bytes. Private cursor payloads are
never embedded in public artifacts.

### 7.0 Acquisition source observation

```text
AcquisitionSourceObservation
  schema_version = "1"
  tenant_id
  source_binding_ref
  provider_kind = postgresql | stripe
  object_observations

AcquisitionObjectObservation
  schema_version = "1"
  logical_object_ref
  provider_observation
```

`object_observations` contains exactly one entry for each requested logical object, ordered by
`logical_object_ref`. Each nested `ProviderObservation` must name the same provider, the activated
schema digest, the opaque connection handle, the admitted capability set, and a UTC observation time
no later than the intent's `admitted_at`. The digest of this complete envelope is the
`source_observation_digest` authority carried by the acquisition intent.

### 7.1 Source connection binding

```text
SourceConnectionBinding
  schema_version = "1"
  binding_id
  tenant_id
  provider_kind = postgresql | stripe
  connection_handle
  account_mode = not_applicable | test | live
  state = draft | validating | ready | suspended | failed | retired
  approved_object_refs
  capability_profile_digest | null
  source_observation_ref | null
  credential_revision
  revision
  created_at
  updated_at
```

The public binding carries only an opaque connection handle. Endpoint, database identity, Stripe
account ID, DSN, API key, and provider resource names remain private operational state.

The lifecycle is fixed:

```text
draft      -> validating, retired
validating -> ready, failed, retired
ready      -> validating, suspended, retired
suspended  -> validating, retired
failed     -> validating, retired
retired    ->
```

Tenant, provider kind, connection handle, and account mode are immutable after creation. Credential
rotation increments `credential_revision` behind the same handle and requires fresh validation
before the binding returns to `ready`. An activated source observation names `binding_id` as its
`source_ref`; acquisition requires exact agreement between the observation, binding, and tenant.

The activated acquisition projection pins `source_binding_revision`, `credential_revision`,
`capability_profile_digest`, `source_observation_ref`, and `source_observation_digest` alongside the
binding reference. It also pins the exact `acknowledgement_consumer_ref`; callers cannot supply or
override acknowledgement authority. A rotation or revalidation that changes any pinned authority
requires contract reactivation and produces `No Valid Plan` before provider resolution; an old
activated contract can never run with newly rotated credentials.

### 7.2 Acquisition intent

```text
AcquisitionIntent
  schema_version = "1"
  intent_key
  tenant_id
  run_intent_ref
  contract_ref
  contract_digest
  source_binding_ref
  source_observation_digest
  acquisition_mode = snapshot | incremental | reconciliation
  object_refs
  prior_checkpoint_revision
  prior_checkpoint_digest | null
  record_ceiling
  encoded_byte_ceiling
  admitted_at
```

`run_intent_ref` is an externally admitted identity, not a scheduler. For Plan 4A acceptance it is
created by the harness. A later trigger service supplies the same field for scheduled windows,
authorized `Run now`, and bounded backfills.

Its shape is fixed now to the identity the addendum already ratifies in section 15,
`digest(contract_version, trigger_policy_version, scheduled_window)`, so the trigger service can
supply it later without a migration or a second identity scheme. The acceptance harness synthesizes
one in manual mode rather than inventing an arbitrary reference. Plan 4A validates the shape and the
tenant-scoped uniqueness of the value; it does not evaluate trigger policy, windows, overlap, or
misfire behavior, all of which remain with the trigger plan.

`source_binding_ref` is the exact artifact reference for a ready `SourceConnectionBinding`, not a
connection handle or provider account ID.

The `intent_key` is the digest of a domain tag plus tenant, run-intent reference, contract digest,
source binding, mode, ordered object references, and prior checkpoint revision. `admitted_at` is not
part of that identity. The runtime rejects a supplied key that does not match the canonical inputs.

An intent may narrow the activated contract's object set and ceilings. It cannot add an object,
field, mode, or limit not admitted by the contract.

Ceilings are refusal limits, never truncation limits. A batch that would exceed `record_ceiling` or
`encoded_byte_ceiling` is abandoned: no segment is published, no prepared receipt is written, and no
checkpoint moves. The runtime returns `AcquisitionCeilingExceeded` naming the object and the limit
reached, so the operator can raise the ceiling or narrow the intent. Truncating to the ceiling would
silently drop records while advancing a cursor, which is the same permanent loss as skipping a row.

Where the count is knowable before streaming - PostgreSQL, which captures bounds and a count inside
the snapshot - the check runs first and no rows are read. Where it is not - Stripe, whose page count
is unknown until pagination ends - the runtime accumulates and refuses mid-stream, invoking provider
cleanup. Both paths reach the same outcome; only the cost differs. Conformance covers a batch that
crosses each ceiling on a later page and asserts no artifact, receipt, or checkpoint survives.

### 7.3 Ordered field schema

```text
AcquisitionField
  name
  value_type = null | boolean | integer | decimal | string | timestamp
  nullable

AcquisitionObjectSchema
  logical_object_ref
  schema_digest
  fields
  record_key_fields
  source_updated_at_field | null
  operation_semantics = upsert_only
```

Plan 4A supports scalar values only. Arrays, arbitrary nested maps, binary values, and provider
expansions are not admitted. Stripe objects are flattened into the approved shape. A provider
response containing an unknown or mismatched field is invalid input, not an invitation to widen the
schema.

### 7.4 Provider-neutral record

```text
AcquisitionRecord
  schema_version = "1"
  logical_object_ref
  record_key
  source_created_at | null
  source_updated_at | null
  operation = upsert
  fields
```

`fields` is an ordered tuple of typed name/value entries, not an untyped dictionary. Its names,
order, nullability, and values must match the exact `AcquisitionObjectSchema`. `record_key` is a
private source-aligned identity and is excluded from public evidence.

The Stripe `Customer` schema may represent the allowlisted `customer.deleted` event as an upsert of
a minimal record carrying only its opaque ID, creation time when present, and `deleted = true`.
Other hard-delete semantics are not admitted in Plan 4A.

Records in a segment are canonically ordered by the provider's declared stable key:

- PostgreSQL: `(updated_at, primary_key)` for incremental batches and primary key for the initial
  snapshot; and
- Stripe: `(created, object_id)` for initial snapshots and `(event_created, event_id, object_kind,
  object_id)` for event acquisition before the resulting object records are grouped and sorted.

### 7.5 Provider boundary

```text
AcquisitionBoundary
  schema_version = "1"
  logical_object_ref
  acquisition_mode
  schema_digest
  lower_cursor_digest | null
  upper_cursor_digest
  query_shape_digest
  snapshot_identity_digest | null
  key_range_digest | null
  private_boundary_ref
  record_count
  opened_at
  closed_at
```

The boundary contains cursor digests, never raw cursor values. Provider-private boundary details -
the PostgreSQL snapshot identity, the primary-key bounds, the lag bound actually applied, and the
Stripe upper event timestamp - are stored through the state service and reached through
`private_boundary_ref`.

`snapshot_identity_digest` and `key_range_digest` exist because section 10.2 captures both and they
are what make a PostgreSQL snapshot provable rather than merely asserted; they are null for a source
class that has no equivalent. Without them the boundary could not carry the evidence its own
provider semantics require.

This model supersedes `SourceBoundary` (`schema_version = "2"` in `packages/provider-sdk`) for
acquisition. `SourceBoundary` already carries `snapshot_identity`, `key_range_digest`,
`query_shape_digest`, and `row_count` in the clear; `AcquisitionBoundary` keeps the same facts as
digests and adds the cursor, mode, and private-detail linkage that incremental acquisition needs.
The implementation extends the existing model rather than adding a parallel one where the shapes
allow it, and leaves the M0 signed-graph path on `SourceBoundary` until that path is retired. The
plan must state which of the two each call site uses; two boundary models with overlapping meaning
and no stated relationship is the outcome to avoid.

`ProviderObservation.provider` in `packages/provider-sdk` is currently
`Literal["postgresql", "snowflake"]` and must widen to admit `stripe` in the same change.

### 7.6 Segment manifest

```text
AcquisitionSegmentManifest
  schema_version = "1"
  segment_name
  logical_object_ref
  record_schema_digest
  boundary_digest
  encoding = canonical_jsonl_v1
  content_digest
  record_set_digest
  record_count
  encoded_bytes
```

Segment names are derived from a fixed ordinal and logical object digest, not a provider ID. JSON
Lines use one canonical `AcquisitionRecord` per line, a final newline, UTF-8, and no insignificant
whitespace. Decimal and timestamp forms follow the repository canonical serialization rules.

`content_digest` covers exact encoded bytes. `record_set_digest` covers the ordered canonical record
models. Both must match before the segment is admitted.

### 7.7 Batch manifest

```text
AcquisitionBatchManifest
  schema_version = "1"
  batch_id
  intent_key
  tenant_id
  contract_ref
  contract_digest
  source_binding_ref
  source_observation_digest
  acquisition_mode
  prior_checkpoint_revision
  prior_checkpoint_digest | null
  candidate_checkpoint_digest
  segment_manifests
  total_record_count
  total_encoded_bytes
  prepared_at
```

Segments are ordered by logical object reference. Totals must equal the exact sum of segment
manifests. `batch_id` is derived from a domain tag, intent key, prior checkpoint revision, candidate
checkpoint digest, and segment-manifest digests. `prepared_at` does not contribute to the batch ID.

### 7.8 Prepared receipt

```text
AcquisitionPreparedReceipt
  schema_version = "1"
  prepared_receipt_id
  tenant_id
  intent_key
  batch_id
  batch_manifest_digest
  prior_checkpoint_revision
  candidate_checkpoint_digest
  cursor_version
  prepared_at
```

The receipt is created only after every segment and the manifest are durable and digest-verified.
It does not mean the destination accepted, persisted, reconciled, or exposed the batch.

### 7.9 Downstream acknowledgement

```text
AcquisitionAcknowledgement
  schema_version = "1"
  acknowledgement_id
  tenant_id
  consumer_ref
  contract_digest
  source_binding_ref
  batch_id
  batch_manifest_digest
  prior_checkpoint_revision
  candidate_checkpoint_digest
  consumer_receipt_digest
  acknowledged_at
```

The consumer receipt remains opaque to Plan 4A. A later destination plan defines and validates its
warehouse effect. Plan 4A validates only exact identity and digest linkage.

### 7.10 Committed checkpoint receipt

```text
AcquisitionCheckpointReceipt
  schema_version = "1"
  checkpoint_receipt_id
  tenant_id
  contract_digest
  source_binding_ref
  previous_revision
  committed_revision
  cursor_digest
  batch_id
  acknowledgement_id
  committed_at
```

The acknowledgement, prepared-batch transition, and new checkpoint revision commit in one state
transaction. Replay returns the existing receipt only after exact canonical equality.

## 8. Private state

### 8.1 Source checkpoint

Private checkpoint state contains:

```text
SourceCheckpointState
  tenant_id
  contract_digest
  source_binding_ref
  provider_kind
  cursor_version
  revision
  encrypted_cursor_payload
  cursor_digest
  last_batch_id | null
  created_at
  updated_at
```

The encryption key is supplied through a private capability. The plaintext cursor cannot appear in
SQLite diagnostics, logs, evidence events, test snapshots, or exception messages. Decryption and
provider-specific validation occur only inside the provider invocation boundary.

### 8.2 Prepared batch state

```text
PreparedAcquisitionState
  tenant_id
  intent_key
  contract_digest
  source_binding_ref
  source_binding_revision
  credential_revision
  binding_authority_epoch
  contract_authority_epoch
  acknowledgement_consumer_ref
  provider_kind
  prior_checkpoint_revision
  batch_id
  batch_manifest_digest
  candidate_cursor_ciphertext
  candidate_checkpoint_digest
  cursor_version
  state = prepared | acknowledged
  acknowledgement_digest | null
  created_at
  updated_at
```

The unique authority key is `(tenant_id, contract_digest, source_binding_ref,
prior_checkpoint_revision)`. A second preparation under that key must be canonically identical.
Concurrent different output or a different authority snapshot is an integrity failure. Binding
cancellation, suspension, retirement, credential rotation, or contract retirement increments its
state-owned authority epoch before the lifecycle change becomes externally effective. A pending
preparation or acknowledgement holding an earlier epoch then loses its compare-and-set. Exact
acknowledgement replay may return the already committed receipt after later retirement because it
reconstructs no new effect and revalidates the complete committed fact set. Source-binding
invalidation is an exact revision compare-and-set: retrying the same invalidation is idempotent,
while a delayed request cannot revoke an already admitted newer revision. The contract service owns
contract retirement through the same fail-closed authority boundary. Retirement loads a
tenant-qualified activated lifecycle record, checks its exact expected revision, invalidates state
authority, and only then persists the retired revision. Unknown, cross-tenant, inactive, and stale
requests have no authority effect; an unknown state authority cannot be invalidated into existence.
Contract lifecycle activation is the only operation that may create contract state authority;
runtime admission rejects an unknown contract rather than implicitly activating it.

### 8.3 Artifact durability

Runtime depends on a narrow `AcquisitionArtifactStore`:

```text
put_if_absent(artifact_digest, reader)
open_verified(artifact_digest) -> reader
exists_verified(artifact_digest)
```

The store takes and returns readers rather than `bytes`. A segment is bounded by
`encoded_byte_ceiling`, so whole-segment buffering would not be unbounded, but the ceiling exists to
bound a *batch*, not to promise that one segment fits comfortably in the control plane's memory: an
initial snapshot of a large approved table is a single segment. Streaming in bounded chunks keeps
the interface honest at the ceilings an operator is likely to set, and the digest is computed over
the same chunks as they are written, so verification does not require a second full read.

The initial local adapter writes to a tenant-private temporary path, flushes and synchronizes the
file, atomically renames it to a digest-addressed path, and synchronizes the directory before state
records may reference it. An existing path is reused only after byte-for-byte digest verification.

There is no distributed transaction across files and SQLite. Ordering makes recovery safe:

1. durable artifact first;
2. prepared state second; and
3. acknowledgement and checkpoint transaction last.

A crash after artifact publication but before prepared state leaves an unreferenced digest-addressed
artifact. Replaying the same intent verifies and reuses it. Cleanup of unreferenced artifacts is a
later maintenance concern and can never delete a referenced digest.

## 9. Acquisition lifecycle

```text
intent admitted
  -> provider boundary opened
  -> records validated and encoded
  -> segments durable
  -> batch manifest durable
  -> prepared receipt recorded
  -> downstream acknowledgement recorded
  -> checkpoint committed
```

### 9.1 Admission

The runtime verifies:

- tenant, contract, source binding, and observation identity;
- ready source-binding state, credential revision, and exact observation linkage;
- activated contract status;
- source observation freshness and exact schema digest;
- acquisition mode and object subset;
- prior checkpoint revision and digest;
- record and byte ceilings; and
- deterministic intent key.

An unactivated contract, stale mandatory authority, unsupported delete requirement, absent stable
cursor, or incompatible source observation yields `No Valid Plan` at compilation or admission. It
does not reach provider execution.

### 9.2 Preparation

The provider captures a private upper boundary before returning records. The runtime consumes every
record, validates it, writes segments, obtains completed boundaries, and verifies all counts and
digests. Abandoning iteration invokes provider cleanup and cannot create a prepared receipt.

Zero-record batches are valid. They retain exact lower and upper cursor digests, produce empty
segments or a manifest-declared empty object set according to the provider contract, and may be
acknowledged. The checkpoint revision advances even when the cursor digest is unchanged so a later
run intent has an unambiguous predecessor.

### 9.3 Replay

Replaying the same intent:

- returns the same prepared receipt when the stored batch is exact;
- resumes deterministic artifact publication when artifacts exist but prepared state does not;
- rejects a different candidate checkpoint, segment digest, count, or manifest; and
- never invokes a destination or advances a checkpoint implicitly.

### 9.4 Acknowledgement

An acknowledgement is accepted only when every field binds the exact prepared batch and current
checkpoint revision. The state service atomically marks the batch acknowledged, writes the next
checkpoint revision, and records its receipt. A stale, cross-tenant, wrong-consumer, wrong-contract,
or digest-mismatched acknowledgement has no durable effect.

## 10. PostgreSQL source semantics

### 10.1 Capability admission

The source must be a declared base table with:

- an exact approved column projection;
- a single non-null primary or unique B-tree key;
- a timezone-aware `updated_at` column;
- a contract assertion that `updated_at` is nondecreasing for every mutation;
- SELECT access through a dedicated read-only principal; and
- denied INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, schema administration, role
  administration, and unrelated-schema reads.

Failure to prove any mandatory capability yields `No Valid Plan` or authorization failure according
to whether the problem is contract legality or live credential state.

### 10.2 Initial snapshot

The provider reuses the established M0 guarantees and generalizes the record projection:

1. open `REPEATABLE READ READ ONLY`;
2. validate identity, schema, key, and privileges inside that transaction;
3. capture the PostgreSQL snapshot identity;
4. capture primary-key bounds and count;
5. reject contract ceilings before streaming rows;
6. stream exact approved fields in primary-key order;
7. commit only after complete consumption; and
8. return the completed boundary.

Partial consumption rolls back and closes the transaction. It cannot claim a completed boundary.

### 10.3 Incremental acquisition

The private cursor is:

```text
PostgreSQLIncrementalCursor
  schema_version = "1"
  updated_at
  primary_key
```

Inside one repeatable-read, read-only transaction, the provider captures the greatest visible
`(updated_at, primary_key)`. That value is not yet the upper cursor: it must first be held back by a
safety lag, for the reason in section 10.3.1. It then evaluates:

```text
lower_cursor < (updated_at, primary_key) <= captured_upper_cursor
ORDER BY updated_at, primary_key
```

Both comparisons use PostgreSQL row-value semantics over the exact timestamp and key types admitted
by the source observation. The provider does not truncate timestamps or cast keys through text.

Multiple rows with the same timestamp are neither skipped nor duplicated because the primary key is
the deterministic tie-breaker. The next committed checkpoint is the lagged upper cursor, not the
greatest visible value and not the last row returned by an interrupted iterator.

An application that moves `updated_at` backward violates the activated contract. Plan 4A cannot
observe all such violations incrementally; full reconciliation is the detection boundary. Physical
deletes are likewise not observable. The compiler refuses contracts that claim either guarantee.

#### 10.3.1 Why the upper cursor lags

`updated_at` is assigned by the writer - at statement time, or by `now()`, which in PostgreSQL is
the transaction start time. Row visibility is governed by commit order instead. The two disagree
whenever a write transaction outlives the snapshot that follows it:

1. transaction T sets `updated_at = 10:00:00` and stays open;
2. an acquisition snapshot opens at 10:00:02 and cannot see T's row;
3. it captures a greatest visible value of, say, `10:00:01` and commits that as the checkpoint;
4. T commits at 10:00:05;
5. the next run's lower bound is `10:00:01`, T's row sits below it, and the row is never acquired.

No application misbehaves here, so the contractual monotonicity assertion above does not cover it,
and section 10.4 defers detection to a reconciliation whose comparison belongs to later plans. Left
unaddressed, Plan 4A would lose rows silently and permanently. This is the single most common defect
in timestamp-cursor acquisition and it must be closed in the mechanism, not in a later plan.

The upper cursor is therefore the greatest visible `(updated_at, primary_key)` whose timestamp is at
or below a *lag bound*:

```text
lag_bound = snapshot_time - max_write_transaction_duration
upper_cursor = greatest visible (updated_at, primary_key) with updated_at <= lag_bound
```

`max_write_transaction_duration` is an activated-contract parameter, not a provider default, because
only the source owner can bound it. The contract must state it, and the compiler refuses a contract
that asserts incremental acquisition without one. A source whose write transactions can exceed the
declared bound is outside the contract exactly as a source with a decreasing `updated_at` is.

Where the least-privilege principal can read `xact_start` for concurrent backends, the provider
tightens the bound to the oldest concurrently running transaction's start time and records which
bound it used in the private boundary detail. That is a refinement; the declared parameter remains
mandatory, because the capability probe may deny the statistics view.

Two consequences are deliberate. A row committed inside the lag window is acquired by the *next*
run, so the design trades a bounded freshness delay for the guarantee that no row is skipped. And a
zero lag is not permitted: it would reintroduce the defect above.

Conformance must include the sequence exactly as numbered: open a write transaction, take a
snapshot that cannot see it, commit the writer afterwards, run the next incremental batch, and
assert the row appears. A mutation that removes the lag from the upper-cursor predicate must fail
that test.

### 10.4 PostgreSQL reconciliation

An explicit reconciliation intent opens a fresh bounded snapshot and emits source-aligned records
and a complete record-set digest. It does not advance the incremental cursor unless a downstream
plan accepts the reconciliation result under an exact acknowledgement. A disagreement between
provider-declared bounds, counts, and encoded records is an integrity failure. Comparison with
destination state belongs to the destination and transformation plans.

## 11. Stripe source semantics

### 11.0 What Stripe does not supply

Addendum section 20.5 requires Customer, Order, Invoice, Payment, Refund, Subscription, and Account
Segment entities, and revenue *and churn* metric definitions. Decision 6 defers Stripe
subscriptions, so the source of the last two entities must be stated rather than left to the
implementation to discover.

Subscription and Account Segment facts come from the application's PostgreSQL database, alongside
Order. They are ordinary approved base tables under section 10 and need no new source class: the
same snapshot and compound-cursor semantics apply. Nothing about them requires Stripe subscriptions,
which remain deferred.

Two consequences follow. The PostgreSQL acceptance fixture in section 18.1 is not orders alone; it
must include a subscription table with a lifecycle status and a segment table, so the churn metric
has facts to stand on before a later plan tries to compute it. And a tenant whose subscriptions live
only in Stripe cannot satisfy section 20.5 within Plan 4A - that is a real limitation, and the
runbooks must say so rather than let it surface during acceptance.

Deletion semantics are similarly bounded and equally explicit. Decision 3 refuses PostgreSQL
physical-delete capture, and section 11.3 admits only the `customer.deleted` tombstone. Section 20.5
asks for explicit deletion semantics, and refusing to claim unobservable deletes is an explicit
semantic - but the contract must say so in words, so a data product does not silently assume a
delete it will never see.

### 11.1 Approved object set

The initial Stripe source admits exactly four logical resources:

1. `Customer` for approved cross-source identity;
2. `Invoice` for issued revenue;
3. `Charge` for settled payment; and
4. `Refund` for returned value.

The activated contract specifies exact fields. The initial normalized subset is limited to:

- opaque object and relationship IDs;
- source creation timestamps;
- currency and integer minor-unit amounts;
- lifecycle status and payment/refund booleans needed by the approved process;
- the `Customer` deleted lifecycle marker needed for a private tombstone;
- invoice paid timestamp when present; and
- one explicitly approved metadata key for cross-source customer identity when that identity rule is
  separately approved.

Names, email addresses, postal addresses, phone numbers, card details, bank details, descriptions,
receipts, unrestricted metadata, expanded nested resources, and secrets are excluded.

### 11.2 Account and API-version admission

The provider observes and binds:

- account identity as a private digest;
- live mode versus test mode;
- the requested API version;
- object-list and Events capabilities;
- least-privilege credential behavior;
- denied mutation and unrelated-resource probes; and
- the approved object and event-type allowlists.

An acquisition cannot mix live and test objects. Every event carries its creation-time API version.
The provider accepts only versions with a tested normalization codec. An unsupported event version
produces `ResynchronizationRequired` or `No Valid Plan` according to whether a complete bounded
reconstruction is still possible.

### 11.3 Initial object snapshots

At preparation start the provider captures an upper source timestamp. For each approved resource it:

1. issues a list request constrained to objects created at or before the upper timestamp; absence
   of that capability is `No Valid Plan` for the resource;
2. follows only official cursor pagination;
3. validates list URL, object kind, account mode, and every payload;
4. rejects unexpected expansions or fields outside the normalization codec;
5. normalizes allowlisted fields;
6. sorts the resulting records canonically by `(created, object_id)`; and
7. emits one segment and boundary per logical resource.

Provider pagination cursors are private. Page order is not used as canonical segment order.

### 11.4 Event cursor

The private incremental cursor is:

```text
StripeEventCursor
  schema_version = "1"
  last_event_created
  last_event_id
  api_version_set_digest
```

Each incremental run uses an inclusive overlap before `last_event_created`, captures an upper event
creation time, retrieves approved event types through that upper bound, and paginates all pages.

The overlap has a declared width. `event_overlap_window` is an activated-contract parameter with a
stated minimum, chosen to exceed the largest event-ordering skew the account can exhibit, and the
compiler refuses an incremental Stripe contract without one. The run reads from
`last_event_created - event_overlap_window` so that an event created before the previous upper
bound, but not listable until after it, is still seen. A zero overlap is not permitted, for the same
reason a zero PostgreSQL lag is not.

The overlap is only correct if it is provably wide enough, so continuity is checked rather than
assumed. If the first page of the overlap does not contain the exact `last_event_id` at
`last_event_created`, the run cannot prove it saw everything between the two cursors and returns
`ResynchronizationRequired(reason_code="stripe_event_overlap_gap")`. An event whose `created`
precedes the overlap start is the same failure, not a record to drop quietly.
The provider then:

- validates account mode and event type;
- deduplicates by event ID;
- rejects two different payloads under one event ID;
- orders events by `(created, event_id)`;
- discards already committed events at or below the exact prior cursor after deduplication;
- normalizes the event snapshot through its creation-time API-version codec; and
- derives the candidate cursor from the greatest fully processed event.

The overlap is a correctness mechanism, not a heuristic checkpoint. Repeated events produce the
same record-set and batch digests.

Stripe documents a finite Events retrieval window. If the prior cursor is outside the retrievable
window, or the first page cannot prove continuity with the overlap, the provider returns
`ResynchronizationRequired(reason_code="stripe_event_cursor_expired")`. It never begins from the
oldest still-visible event and calls that complete.

### 11.5 Stripe reconciliation

An explicit reconciliation intent repeats bounded object snapshots for all four resources and
produces a complete, destination-neutral reconciliation batch. Plan 4A verifies list completeness,
canonical object identity, normalization, and internal digest consistency. It does not claim that
the batch matches a warehouse or the prior incrementally materialized state; the destination and
transformation plans own that comparison.

Plan 4A produces either a prepared reconciliation batch, `ResynchronizationRequired` when source
continuity cannot be reconstructed, or integrity failure for contradictory provider responses.

Automatic cadence and authorization for resynchronization belong to the scheduling and managed
operations plan. Plan 4A records the exact required scope and never performs an unbounded resync.

## 12. Error and outcome model

Public acquisition methods expose typed, sanitized failures:

```text
AcquisitionStaleRevision
AcquisitionOwnershipError
AcquisitionContractError
AcquisitionAuthorizationError
AcquisitionThrottledError
AcquisitionTransientError
AcquisitionDriftError
AcquisitionCursorExpiredError
AcquisitionCeilingExceeded
AcquisitionIntegrityError
```

Provider entry points raise `ProviderError` with a precise classification. PostgreSQL driver types,
Stripe client/HTTP types, response bodies, request IDs, account IDs, cursors, SQL, credentials, and
source values cannot cross that boundary.

Stripe cursor expiry and overlap discontinuity use the closed
`resynchronization_required` provider classification with the allowlisted
`stripe_event_cursor_expired` and `stripe_event_overlap_gap` reasons. Unclassified provider
exceptions are integrity failures, not retryable transport failures.

### 12.1 Durable governed outcomes

Expected governed conditions are artifacts, not exceptions:

```text
AcquisitionNoValidPlan
  reason_codes
  failed_constraints

ResynchronizationRequired
  reason_code
  source_binding_ref
  affected_object_refs
  last_proven_checkpoint_digest
  required_scope
  created_at

```

Both artifacts are tenant-private. `ResynchronizationRequired` carries
`last_proven_checkpoint_digest`, which section 14 forbids in public evidence, so it is stored with
the private state and never exported. What reaches the public receipt is
`outcome = resynchronization_required` and the allowlisted `reason_code`; an operator recovers the
scope from the architect-facing projection, not from evidence. `AcquisitionNoValidPlan` follows the
same rule: reason codes are public, failed constraints are not.

Rules:

- missing capability or illegal contract semantics produce `No Valid Plan`;
- transient and throttled failures advance no checkpoint and may be retried later under policy;
- live credential denial is authorization failure, not provider non-conformance;
- source drift blocks preparation until exact authority is restored;
- an expired or discontinuous cursor creates `ResynchronizationRequired`;
- malformed bytes, duplicate identity with different content, canonical mismatch, transaction
  failure, or impossible state is integrity failure; and
- no runtime failure is converted into a denial, dependency, successful empty batch, or checkpoint.

## 13. Security and privacy

- Every method takes `tenant_id`; bare intent, batch, source, and checkpoint IDs are never authority.
- Source credentials are resolved through private capabilities and never persisted in artifacts.
- PostgreSQL and Stripe use dedicated least-privilege principals.
- Positive access and denied mutation/administration probes are both required.
- Private cursor payloads are encrypted at rest and structurally absent from public models.
- Segment paths, raw object IDs, provider request IDs, endpoints, SQL, and source rows are private.
- Logs and exception text contain only stable error classes and allowlisted reason codes.
- Public evidence uses logical object references and independently generated opaque receipt
  references, never content digests, low-entropy source values, or raw provider identifiers.
- Stripe metadata is denied by default; one key may be admitted only by the approved identity rule.
- Artifact paths are tenant-private, digest-addressed, and protected from traversal and symlink
  substitution.
- Provider responses, persisted cursors, segment bytes, acknowledgements, and contract references
  are revalidated as untrusted input on every read.
- Cleanup failure cannot replace the original acquisition failure.

## 14. Public evidence

```text
AcquisitionEvidenceReceipt
  schema_version = "1"
  evidence_id
  tenant_id
  run_intent_ref
  contract_ref
  source_binding_ref
  acquisition_mode
  logical_object_refs
  prepared_receipt_ref | null
  checkpoint_receipt_ref | null
  prior_checkpoint_revision
  resulting_checkpoint_revision | null
  reason_codes
  outcome = prepared | acknowledged | no_valid_plan |
            resynchronization_required | failed
  created_at
```

Prepared and checkpoint receipt references are independently generated opaque identifiers; they are
not content digests or deterministic hashes of low-entropy source facts. The evidence receipt
contains no private batch ID, manifest digest, row or segment count, segment content, record key,
source field, raw cursor, cursor digest, provider account ID, endpoint, credential, SQL, HTTP
response, or private artifact path.

Preparation and acknowledgement are separate receipts. Only acknowledgement may name a resulting
checkpoint revision. Neither receipt claims destination persistence, source-to-destination
reconciliation, warehouse visibility, transformation success, freshness, or consumer delivery.

`reason_codes` is a closed public vocabulary. Runtime-only diagnostics and dependency text are
mapped to stable public categories before receipt construction; arbitrary provider or exception text
is rejected by the receipt model.

## 15. Concurrency, transaction, and recovery rules

1. Intent admission reads the exact current checkpoint revision.
2. Only one canonical prepared batch may exist for a source and prior revision.
3. Competing preparation may return the existing exact batch; different output fails integrity.
4. Artifact writes precede prepared-state references.
5. Prepared state and its evidence event commit together.
6. Acknowledgement, prepared-state transition, new checkpoint revision, checkpoint receipt, and
   evidence event commit in one transaction.
7. Every state mutation uses tenant predicates and expected revisions.
8. Rollback and close are best-effort and suppressed so they cannot replace the original exception.
9. Restart after provider iteration but before durable segments repeats acquisition from the same
   prior checkpoint.
10. Restart after segment publication reuses only digest-identical artifacts.
11. Restart after acknowledgement returns the existing exact checkpoint receipt.
12. An acknowledgement racing cancellation, contract retirement, or a newer checkpoint loses by
    compare-and-set and has no partial effect.

Plan 4A does not add global locks, a run queue, or a scheduler. Later trigger and state plans may add
contract-scoped leases without changing the intent, batch, or acknowledgement identities.

## 16. Provider conformance

Every source provider passes the same observable suite:

- strict capability declaration and fresh observation;
- intended read plus denied mutation and administration;
- exact object and field allowlist;
- stable boundary capture;
- deterministic record ordering;
- complete pagination;
- exact record and byte ceilings, refused rather than truncated, with no artifact, receipt, or
  checkpoint surviving the refusal;
- empty acquisition behavior;
- same-intent replay;
- abandoned-iterator cleanup;
- drift before and during acquisition;
- malformed provider payload rejection;
- error classification without implementation leakage;
- cross-tenant denial;
- private cursor containment;
- prepared-versus-acknowledged checkpoint behavior;
- reconciliation or explicit unsupported semantics; and
- sanitized public evidence.

Provider-specific additions are:

### 16.1 PostgreSQL

- repeatable-read snapshot consistency;
- compound-cursor lower and upper inclusivity;
- timestamp ties resolved by primary key;
- a write transaction opened before the snapshot and committed after it, whose row carries an
  earlier timestamp than the greatest visible value, acquired by the next run;
- the upper cursor held at the lag bound rather than the greatest visible value;
- refusal of an incremental contract that declares no maximum write-transaction duration, and of a
  zero duration;
- update at the exact upper boundary;
- backward timestamp and delete limitations;
- read-only role verification; and
- transaction rollback on partial consumption.

### 16.2 Stripe

- reverse-chronological list pagination normalized to canonical order;
- duplicate pages and duplicate events;
- same event ID with contradictory content;
- creation-time API-version normalization;
- live/test mode separation;
- HTTP 429 classification and retry metadata containment;
- retrievable-window expiry;
- overlap continuity, including a first overlap page that does not contain the prior event ID;
- refusal of an incremental contract that declares no event overlap window, and of a zero window;
- an event created before the overlap start treated as a continuity failure, never dropped;
- unexpected object or expansion rejection; and
- object reconciliation disagreement.

## 17. Test strategy

### 17.1 Unit and property tests

- strict model validation, immutability, canonical digests, and unknown-field rejection;
- deterministic intent and batch identities independent of timestamps;
- field-schema and record validation;
- segment byte and record-set digests;
- cursor ordering and boundary predicates;
- intent narrowing without widening;
- evidence allowlist serialization; and
- error sanitization.

Property tests generate timestamp ties, page partitions, duplicate events, zero-record batches,
random restart points, and canonical-equivalent replays.

### 17.2 State and fault-injection tests

Inject failure:

- before and after each artifact write;
- before and after prepared-state commit;
- during cursor encryption and decryption;
- before and after acknowledgement insertion;
- before and after checkpoint compare-and-set;
- during evidence write; and
- during rollback, close, and temporary-file cleanup.

Every failure proves no partial checkpoint advancement and deterministic retry convergence.

### 17.3 Mutation tests

Focused mutation testing is mandatory for:

- tenant qualification;
- intent-key inputs;
- contract/object/field subset checks;
- PostgreSQL lower and upper cursor operators;
- the PostgreSQL lag bound on the upper cursor;
- timestamp/primary-key ordering;
- Stripe overlap width and continuity proof;
- Stripe overlap and event deduplication;
- ceiling refusal rather than truncation;
- checkpoint revision equality;
- batch and acknowledgement digest equality;
- provider error classification; and
- evidence field exclusion.

No surviving critical mutation may weaken an authorization, boundary, replay, or checkpoint rule.

### 17.4 Offline acceptance

The deterministic acceptance journey uses:

- one PostgreSQL fake with an initial order snapshot, an approved subscription table with a
  lifecycle status, an approved account-segment table, equal-timestamp rows, one update, one empty
  incremental, a write transaction that commits after a snapshot it predates, a batch that crosses
  a ceiling on a later page, drift, and denied writes;
- one Stripe fake with all four approved resources, reverse pages, duplicate events, a supported and
  unsupported event API version, rate limiting, a cursor gap, an overlap page missing the prior
  event ID, and reconciliation disagreement;
- two tenants with colliding private provider IDs;
- a digest-addressed temporary artifact store;
- the real state repository and evidence models; and
- a strict acknowledgement consumer that validates the manifest but performs no destination write.

The acceptance run proves:

1. initial PostgreSQL and Stripe batches are deterministic;
2. incremental batches use exact prior checkpoints;
3. replay returns exact prepared receipts;
4. unacknowledged batches advance nothing;
5. exact acknowledgements advance one revision atomically;
6. stale and cross-tenant acknowledgements fail closed;
7. cursor expiry and overlap discontinuity each create `ResynchronizationRequired`;
8. a row written by a transaction that commits after the snapshot it predates is acquired by the
   next incremental batch rather than skipped;
9. a ceiling refuses the batch and leaves no artifact, receipt, or checkpoint;
10. private values are absent from evidence and diagnostics; and
11. no PostgreSQL or ClickHouse destination operation occurs.

### 17.5 Live verification

Live verification is opt-in and requires dedicated fixtures:

- an isolated PostgreSQL source with a least-privilege principal and verified denied access; and
- a Stripe test-mode account or isolated test clock with dedicated restricted credentials.

Each live run creates new facts, traces them to prepared and acknowledged source checkpoints, and
retains sanitized evidence. Existing Stripe objects, prior source rows, successful authentication,
or list responses alone do not prove acquisition correctness.

Live teardown revokes credentials and deletes only exact test fixtures using provider-approved
cleanup. A live test-mode acknowledgement still proves no destination effect.

## 18. Acceptance scenarios

### 18.1 PostgreSQL initial and incremental

0. Activate approved subscription and account-segment sources alongside orders, so the entities
   addendum section 20.5 requires have facts before a later plan computes churn.
1. Activate an approved orders source with a stable key, a monotonic `updated_at` contract, and a
   declared maximum write-transaction duration.
2. Prepare and acknowledge the initial snapshot.
3. Insert two rows sharing a timestamp and update one existing row at a later timestamp.
4. Prepare the bounded incremental batch.
5. Verify exact ordering, no skipped tie, no duplicate, and candidate upper cursor.
6. Open a write transaction whose row timestamp precedes the next snapshot, snapshot while it is
   still open, commit it afterwards, and verify the row arrives in the following batch.
7. Crash before acknowledgement and replay the same batch.
8. Acknowledge once and prove a second acknowledgement is exact replay.
9. Run an empty incremental and advance only the checkpoint revision.

### 18.2 PostgreSQL unsupported delete guarantee

1. Form a contract that requires physical delete capture.
2. Verify compilation or admission returns `No Valid Plan`.
3. Verify no provider query, segment, batch, or checkpoint is created.

### 18.3 Stripe initial and incremental

1. Observe a Stripe test-mode source and approve the four-resource field set.
2. Prepare initial Customer, Invoice, Charge, and Refund segments across multiple pages.
3. Acknowledge the initial checkpoint.
4. Introduce invoice, charge, and refund events, including duplicate delivery and timestamp ties.
5. Prepare the incremental batch through an inclusive overlap.
6. Verify canonical ordering, event-ID deduplication, exact cursor, and allowlisted fields.
7. Replay before acknowledgement and verify the same batch.
8. Acknowledge and commit one checkpoint revision.

### 18.4 Stripe gap and reconciliation

1. Advance the stored cursor beyond the provider's retrievable Events window.
2. Verify a sanitized `ResynchronizationRequired` with no cursor advancement.
3. Run a bounded reconciliation fixture with contradictory content under one object ID.
4. Verify integrity failure rather than overwrite.
5. Run a valid reconciliation and prepare its complete source batch without claiming destination
   equality.

### 18.5 Tenant isolation

1. Prepare two tenants whose private source IDs and timestamps collide.
2. Attempt to load, replay, acknowledge, or inspect each batch using the other tenant ID.
3. Verify a generic ownership error, no existence disclosure, no durable mutation, and no private
   value in diagnostics.

## 19. Documentation and operational artifacts

Implementation must add:

- Plan 4A setup, acceptance-run, teardown, and evidence-package runbooks;
- source capability and credential requirements;
- PostgreSQL monotonic timestamp and delete limitations;
- Stripe object, event, API-version, and retention constraints;
- checkpoint recovery and resynchronization procedures;
- artifact retention and orphan-cleanup policy boundaries;
- provider conformance fixtures; and
- addendum and specification-conformance amendments.

The runbooks distinguish offline, emulator, and live claims. They never present a test consumer
acknowledgement as a destination or warehouse effect.

## 20. Addendum and conformance amendments

The implementation plan must update the managed-platform addendum and conformance suite to pin:

1. the acquisition intent, field schema, record, boundary, segment, batch, prepared receipt,
   acknowledgement, and checkpoint receipt shapes;
2. the destination-neutral Plan 4A boundary and explicit non-claims;
3. private versus public cursor and provider-identity treatment;
4. PostgreSQL compound-cursor semantics, the lagged upper cursor and its contract parameter, and
   the unsupported delete guarantee;
5. Stripe approved resources, the declared event overlap window and its continuity proof,
   API-version handling, finite retrieval window, and reconciliation behavior;
5a. the sources of the Subscription and Account Segment entities, and the limitation that a tenant
    whose subscriptions live only in Stripe is outside Plan 4A;
5b. ceilings as refusal limits rather than truncation limits;
6. prepared-versus-acknowledged checkpoint transitions;
7. `No Valid Plan`, resynchronization, provider failure, and integrity distinctions;
8. shared provider conformance requirements;
9. public evidence allowlists; and
10. the rule that scheduling, destination effects, transformation, freshness, and delivery remain
    later stages.

No amendment may imply that acquisition preparation or acknowledgement proves destination
visibility or consumer correctness.

## 21. Delivery decomposition

Implementation should proceed in reviewable stages:

1. ratify Plan 4A contracts and conformance requirements in the addendum;
2. add provider-neutral acquisition models and conformance tooling;
3. create the declared `services/connection-broker` component and managed source-binding lifecycle;
4. create the declared `services/state` component and strict private checkpoint repository;
5. add digest-addressed local acquisition artifact storage;
6. add runtime preparation, replay, acknowledgement, and evidence integration;
7. generalize the PostgreSQL provider from M0 snapshot rows to approved acquisition records;
8. add PostgreSQL compound-cursor incremental and reconciliation behavior;
9. add PostgreSQL provider and end-to-end acceptance;
10. add the Stripe provider with strict settings, client boundary, and object normalization;
11. add Stripe bounded snapshots, Events cursor, and reconciliation;
12. add Stripe provider and end-to-end acceptance;
13. add fault-injection, mutation, privacy scanning, runbooks, and complete offline gates; and
14. request independent review with explicit source-only non-claims.

PostgreSQL establishes the shared interfaces first. Stripe may not copy the PostgreSQL runtime or
state lifecycle into provider-local code. It must pass the same contracts and conformance suite.
The implementation plan must expose two mergeable milestones: Milestone A ends with shared
contracts, state, runtime, and PostgreSQL acceptance; Milestone B begins from those committed public
contracts and ends with Stripe acceptance. Plan 4A is complete only after both milestones pass.

## 22. Risks and mitigations

**Risk: a prepared batch is mistaken for source-to-warehouse success.**
: Prepared and checkpoint receipts explicitly disclaim destination persistence, visibility,
  reconciliation, transformation, freshness, and delivery.

**Risk: generic connector abstractions are designed before evidence exists.**
: The shared surface supports only capabilities demonstrated by PostgreSQL and Stripe. Future
  providers widen it only after conformance evidence.

**Risk: PostgreSQL updates are skipped.**
: The exact compound cursor handles timestamp ties; monotonic timestamps are contractual; full
  reconciliation creates the complete batch needed for later comparison; delete capture is refused.

**Risk: Stripe Events are treated as infinite durable history.**
: The finite retrieval window is explicit. Cursor gaps produce resynchronization requirements and
  never silently restart.

**Risk: Stripe schema or API-version drift changes meaning.**
: Exact API-version codecs and strict field allowlists fail closed. Unsupported versions cannot be
  normalized optimistically.

**Risk: an acknowledgement advances the wrong checkpoint.**
: Exact tenant, contract, source, batch, manifest, prior revision, and candidate digest equality is
  enforced in one transaction and mutation-tested.

**Risk: private source identity leaks through evidence or diagnostics.**
: Public receipts are structural allowlists. Privacy scanners use row, cursor, account, endpoint,
  and credential canaries.

**Risk: filesystem and database writes split.**
: Durable content-addressed artifacts are written first. State references them only after digest
  verification, and replay repairs the safe orphan direction.

## 23. Definition of done

Plan 4A is complete only when:

- addendum, public models, state transitions, and conformance fixtures agree;
- source connection bindings are tenant-qualified, opaque, capability-validated, and lifecycle-safe;
- PostgreSQL and Stripe pass one destination-neutral source-provider suite;
- PostgreSQL initial and compound-cursor incremental acquisition are deterministic, and a row
  committed after a snapshot it predates is acquired rather than skipped;
- every ceiling refuses its batch without publishing an artifact, receipt, or checkpoint;
- Stripe initial snapshots, overlapping Events acquisition, deduplication, API-version validation,
  finite-window handling, and reconciliation are deterministic;
- every provider uses intended and denied least-privilege probes;
- every record matches an approved strict field schema;
- batch replay is exact and a contradictory replay fails integrity;
- no unacknowledged batch advances a checkpoint;
- acknowledgement and checkpoint commit are atomic and replay-safe;
- cross-tenant, stale, malformed, drifted, and contradictory inputs fail closed;
- public evidence contains no source rows, raw cursors, provider IDs, request IDs, endpoints,
  credentials, SQL, or private artifact paths;
- fault injection at every write boundary converges without partial state;
- focused mutation tests leave no surviving critical boundary or checkpoint mutation;
- offline, structure, and acceptance gates pass;
- opt-in live claims are backed by new isolated source facts and denied-access proof;
- no Plan 4A code schedules work, writes a destination, runs transformation, or claims delivery; and
- independent review has no unresolved blocking finding.

Plan 4A then supplies the stable source-side handoff required for PostgreSQL and ClickHouse
destination conformance, managed transformation, scheduling, Superset delivery, and full witnessed
acceptance.

## 24. External provider constraints

The Stripe design relies on the official API properties documented at review time:

- list endpoints use cursor pagination with `starting_after` and `ending_before` and return objects
  in reverse chronological order: <https://docs.stripe.com/api/pagination>;
- Events can be listed with cursor and creation-time filters: <https://docs.stripe.com/api/events/list>;
- event objects are rendered according to the API version at event creation and are retrievable only
  for a finite documented window: <https://docs.stripe.com/api/events>;
- Charges and Refunds expose list endpoints with creation filters and cursor pagination:
  <https://docs.stripe.com/api/charges/list> and <https://docs.stripe.com/api/refunds/list>.

Implementation planning must revalidate these provider constraints against the then-current official
documentation and pin the tested Stripe API version. Documentation is an input to conformance, not
proof that a dedicated account behaves correctly.
