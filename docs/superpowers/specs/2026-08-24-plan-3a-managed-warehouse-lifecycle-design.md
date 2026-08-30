# Plan 3A Managed Warehouse Lifecycle Design

**Status:** Proposed for final review

**Date:** 2026-08-24

**Review input:** `docs/superpowers/reviews/2026-08-24-plan-3a-design-review.md`

## 1. Purpose

Plan 3A proves that PillarMesh can create, secure, validate, suspend, resume, back up,
restore, retire, and reconcile a dedicated managed analytical warehouse through one
provider-neutral lifecycle. The customer chooses PostgreSQL or ClickHouse before
provisioning; the lifecycle contract and observable evidence remain the same after that
choice.

The plan is deliberately narrower than the managed-data-plane program. It establishes a
real local acceptance substrate and the durable contracts that later cloud provisioners
must implement. It does not ingest a source, compile transformations, activate an
Integration Contract, schedule a run, grant customer access, or claim production-cloud
readiness.

## 2. Approved decisions

The design records these user-approved decisions:

1. Both PostgreSQL and ClickHouse are required Plan 3A targets.
2. Real acceptance provisioning uses dedicated Docker Compose projects. Production-cloud
   and customer-cloud provisioners are deferred behind the same interfaces.
3. Initial readiness includes an engine-native backup and an isolated restore that proves
   usable data and metadata, not merely archive presence.
4. Seven principal classes are separate: administration, ingestion runtime,
   transformation runtime, backup and restore, customer SQL, catalog, and BI.
5. Provisioning creates the empty `raw`, `conformed`, `product`, `consumption`, private
   control/ledger, and quarantine namespaces with their grant matrix. It creates no
   business tables or transformations.
6. Resuming a suspended binding requires fresh positive and denial evidence.
7. The real two-engine lifecycle is a required, path-filtered pull-request check.
8. The existing OpenMetadata Docker Compose process boundary is promoted into
   `packages/provider-sdk` before warehouse providers consume it.

## 3. Scope and non-goals

### 3.1 In scope

- Ratify the warehouse principal vocabulary, private ledger, evidence artifacts, and gated
  lifecycle in the managed-platform addendum.
- Extend `services/warehouse-control` with provider-neutral protocols, validation admission,
  private operation claims, exact resource records, and replay-safe lifecycle orchestration.
- Promote only the stable Docker Compose process boundary into `packages/provider-sdk` and
  preserve OpenMetadata behavior through characterization tests.
- Add PostgreSQL warehouse provisioning to `providers/postgresql` beside its existing data
  provider.
- Add `providers/clickhouse` with the same public warehouse lifecycle contract.
- Provision one dedicated local Compose project per binding, with host-private connectivity,
  generated credentials, isolated networks, separate volumes, and pinned image digests.
- Create and validate the governed namespaces and seven principal classes.
- Produce real backup, isolated restore, restore verification, and exact restore cleanup.
- Prove initial readiness, gated resume, retention-aware retirement, crash recovery, tenant
  isolation, and engine portability.
- Add required path-filtered CI and a manually reproducible witnessed run.

### 3.2 Non-goals

- Production AWS, Azure, GCP, Kubernetes, customer-cloud, or on-premises provisioning.
- A shared multi-tenant warehouse cluster.
- Warehouse-engine migration or an in-place version upgrade lifecycle.
- Capacity profiles beyond `mvp-fixed`, high availability, or cross-region recovery.
- PostgreSQL or Stripe source acquisition.
- Snapshot, cursor, CDC, scheduling, replay, or resynchronization of source data.
- Transformation compilation, dbt execution, business schemas, or data-product tables.
- Superset provisioning, customer access fulfillment, or catalog publication changes.
- General SQL portability or emulation of transactional behavior ClickHouse cannot prove.
- Immediate physical deletion of data whose retention deadline has not elapsed.
- A production-readiness claim from a local Compose lifecycle.

## 4. Authority and component boundaries

### 4.1 Warehouse control

`services/warehouse-control` is authoritative for:

- `WarehouseBinding` lifecycle and immutable revisions;
- initial and resume validation admission;
- tenant-scoped operation claims;
- private operation and resource-ledger persistence;
- sanitized lifecycle evidence and failure decisions; and
- enforcing that only validated bindings enter or re-enter `ready`.

It depends only on provider-neutral protocols. It never imports PostgreSQL, ClickHouse,
Docker, or driver response types.

### 4.2 Provider SDK

`packages/provider-sdk` owns the reusable Compose process boundary after two real consumers
exist. The promoted component is limited to:

- sanitized subprocess invocation;
- explicit environment construction rather than ambient-environment inheritance;
- project, container, network, and volume inspection;
- timeout, interruption, and process-result handling;
- reconciliation of ambiguous create and remove outcomes; and
- provider-neutral process failure classification.

It does not own manifests, credentials, resource ledgers, lifecycle policy, database SQL,
backup semantics, or validation rules. OpenMetadata, PostgreSQL, and ClickHouse retain those
responsibilities in their provider packages.

### 4.3 Engine providers

`providers/postgresql` and `providers/clickhouse` own:

- exact image and version pins;
- Compose manifests and engine startup configuration;
- engine-specific role and namespace translation;
- engine identity, capability, monitoring, and grant probes;
- backup and restore mechanisms;
- stable provider-resource identity mapping; and
- translation of every provider failure into the closed warehouse classification.

No engine response model or infrastructure identifier crosses into a public warehouse
artifact.

### 4.4 Tests and acceptance

- Offline unit and conformance tests remain under component tests and `tests/conformance/`.
- Real local lifecycle harnesses live under `tests/emulators/warehouses/`.
- Cross-engine observable outcomes live under `tests/conformance/`, not in either provider.
- A witnessed runner and operator documentation live under `tests/acceptance/` and
  `docs/plan3a/`.

No new top-level repository area is required.

## 5. Canonical principal classes and namespaces

### 5.1 Principal classes

The addendum section 18 vocabulary is amended to define these seven separate,
least-privilege classes:

| Principal class | Required access | Required denials |
| --- | --- | --- |
| Administration | Provision namespaces, roles, and engine configuration | May not be used by ingestion, transformation, catalog, BI, or customer queries |
| Ingestion runtime | Write source-aligned generations in `raw`; append its bounded operational ledger records | No role administration; no writes to `conformed`, `product`, or `consumption`; no reads through customer or BI paths |
| Transformation runtime | Read `raw`; write `conformed`, `product`, and `quarantine`; publish approved `consumption` objects | No role administration, backup operation, or control-ledger mutation outside its allowlist |
| Backup and restore | Read the objects required by engine-native backup on the primary warehouse, and drive approved backup and restore operations through the private backup command boundary | No writes, schema authoring, role administration, customer access, or use outside that boundary, on the primary warehouse |
| Customer SQL | Read only explicitly granted `consumption` objects | No shared credential; no `raw`, `conformed`, `product`, quarantine, ledger, role, or backup access |
| Catalog | Inspect approved schemas, object metadata, and lineage-supporting metadata | No row-data reads, warehouse writes, role administration, or backup access |
| BI | Read approved `consumption` objects required by certified datasets | No `raw`, `conformed`, `product`, quarantine, ledger, role, or backup access |

Because the class is read-only, it is not the identity that replays a dump: `pg_restore` and the
ClickHouse `RESTORE` path both author schema and write rows. The isolated restore target is created
with its own throwaway bootstrap administrator, minted per operation, confined to the isolated
project, and destroyed with it. It holds no grant on the primary warehouse, so the read-only
guarantee above is unweakened and the primary's administration identity never handles the backup
credential or the decrypted stream.

These are capability classes, not shared credentials. Customer SQL identities are created
later by access fulfillment. Plan 3A creates a non-login template where the engine supports
one and uses temporary, uniquely named probe identities that are removed after validation.

PostgreSQL cannot distinguish the reads performed by `pg_dump` from an equivalent `SELECT`
issued with the same database credential. ClickHouse backup privileges also permit the engine
to read protected data for backup. The backup credential is therefore non-interactive,
resolved only inside the private backup worker, and absent from customer SQL, BI, catalog,
general provider, and diagnostic paths. Database grants deny writes and administration;
command-boundary tests deny every non-backup consumer from resolving or using the handle.

### 5.2 Governed namespaces

Provisioning creates empty engine-equivalent namespaces for:

- `raw`;
- `conformed`;
- `product`;
- `consumption`;
- `quarantine`; and
- a private PillarMesh control/ledger area.

Names are derived from a stable private binding handle, not raw tenant text. Provider probes
create only bounded canary objects and remove them before readiness evidence is admitted.

## 6. Durable artifacts and private state

All public artifacts are frozen, strict about unknown input, tenant-scoped, canonically
serializable, and digest-addressable. Operational identifiers and secrets remain private.

### 6.1 Initial validation evidence

```text
WarehouseValidationEvidence
  schema_version                    1
  evidence_id
  tenant_id
  binding_id
  binding_revision
  validation_profile                local_acceptance | production
  engine_kind                       postgresql | clickhouse
  engine_version
  engine_build_digest
  engine_image_digest
  principal_profile_digest
  namespace_grant_matrix_digest
  tls_probe_digest
  network_isolation_probe_digest
  encryption_at_rest_evidence_digest
  encryption_at_rest_disposition    proven | deferred_local_acceptance
  positive_probe_digest
  denial_probe_digest
  ledger_probe_digest
  monitoring_probe_digest
  capacity_alert_probe_digest
  backup_artifact_digest
  restore_verification_digest
  restore_cleanup_digest
  observed_at
```

`capability_profile_digest` remains version-free and continues to cover only capacity
profile, engine kind, and deployment mode. Exact observed engine version, build, and image
identity live in validation evidence and must match immutable provider pins.

The local Compose adapter cannot establish production-grade storage encryption for a cloud
warehouse volume. It must still emit an attributable storage-encryption observation and use
`deferred_local_acceptance`; it may never emit `proven`. Consequently, a local witnessed run
proves lifecycle conformance but is not production-readiness evidence. A future production
adapter must emit `proven` from a substrate-native storage-encryption check before a real
customer binding is admitted to `ready`. TLS and encrypted backup artifacts are required and
live-proven in Plan 3A; they do not substitute for storage encryption.

`WarehouseControlService` receives an explicit `WarehouseReadinessPolicy`. Its production
policy is the default and admits only `validation_profile=production` with
`encryption_at_rest_disposition=proven`. The acceptance harness must inject the distinct local
policy, which admits only `validation_profile=local_acceptance` with
`deferred_local_acceptance`. No environment-name branch or caller-supplied Boolean can weaken
the production policy. Conformance tests prove both cross-profile denials.

### 6.2 Resume validation evidence

```text
WarehouseResumeValidationEvidence
  schema_version                  1
  evidence_id
  tenant_id
  binding_id
  binding_revision
  engine_kind
  engine_version
  engine_build_digest
  engine_image_digest
  tls_probe_digest
  network_isolation_probe_digest
  monitoring_probe_digest
  positive_probe_digest
  denial_probe_digest
  storage_integrity_probe_digest
  observed_at
```

Resume does not overwrite or replace initial evidence. It appends evidence bound to the
current suspended binding revision. A full backup and restore is repeated only when the
suspension reason or observed drift concerns storage, corruption, backup, restore, or engine
version. That conditional reason and the resulting evidence digests remain private plus a
sanitized disposition in the resume evidence history.

### 6.3 Restore verification

```text
WarehouseRestoreVerification
  schema_version                  1
  verification_id
  tenant_id
  binding_id
  binding_revision
  engine_kind
  source_backup_artifact_digest
  representative_data_digest
  schema_metadata_digest
  principal_profile_digest
  integrity_marker_digest
  query_behavior_digest
  verified_at
```

The digests bind canonical, privacy-safe probe summaries. Raw representative rows, query
results, credentials, endpoints, backup paths, and engine diagnostics remain private. The
`restore_verification_digest` in initial validation evidence is the canonical digest of this
artifact.

### 6.4 Retirement evidence

```text
WarehouseRetirementEvidence
  schema_version                  1
  evidence_id
  tenant_id
  binding_id
  binding_revision
  resource_inventory_digest
  cleanup_disposition_digest
  retention_policy_digest
  completed_resource_count
  retained_resource_count
  cleanup_failed_resource_count
  observed_at
```

This evidence contains counts and digests, not resource identities or paths. It gates both
`retiring → retired` and `failed → retired`. A retained or cleanup-failed resource is visible
as a disposition without exposing its private identifier.

### 6.5 Private operation

```text
PrivateWarehouseOperation
  tenant_id
  binding_id
  binding_revision
  operation_id
  operation_kind              provision | suspend | resume | retire
  engine_kind
  status                      claimed | running | reconciling | succeeded | failed
  phase
  provider_resource_handle
  failure_classification
  started_at
  updated_at
```

The caller obtains `operation_id` from a repository sequence and derives it from
`{domain, tenant_id, sequence}`. Time, process identifiers, endpoints, and random values do
not contribute to identity. A unique live claim on `(tenant_id, binding_id)` prevents a
different operation from proceeding concurrently. Replay with the same operation identifier
returns or reconciles the existing operation; a second identifier fails closed.

The claim carries `binding_revision`. A claim created for an older revision cannot provision,
resume, suspend, or retire the current binding.

### 6.6 Private resource ledger

```text
PrivateWarehouseResource
  tenant_id
  binding_id
  binding_revision
  operation_id
  resource_id
  resource_kind
  provider_resource_handle
  parent_resource_handle
  creation_state               planned | created | ambiguous | absent
  retention_deadline
  cleanup_status               pending | retained | complete | failed
  cleanup_failure_classification
  created_at
  updated_at
```

`WarehouseResourceKind` includes at least:

- compose project;
- warehouse container;
- private network;
- warehouse data volume;
- credential file;
- TLS private key and certificate;
- backup artifact;
- restore compose project;
- restore container;
- restore private network; and
- restore data volume.

Every resource is recorded as `planned` before creation. This includes every isolated
restore resource. After an ambiguous result, reconciliation inspects only the recorded stable
identity and records `created` or `absent`; it never discovers and adopts arbitrary nearby
resources.

The SQLite repository enables foreign keys before creating child tables. Binding revision,
operation, claim, evidence, and resource writes that must be replay-safe commit in one
transaction. Public evidence is never used as a secret or infrastructure registry.

## 7. Failure classification

Every warehouse provider entry point raises `WarehouseProviderError` carrying one
`WarehouseFailureClassification` value:

```text
transient_transport
transient_unavailable
throttled
ambiguous_outcome
authorization_denied
statement_rejected
invalid_provider_response
integrity_failure
permanent_configuration
```

Driver exceptions and raw provider text never cross the provider boundary. Transient,
throttled, and ambiguous classifications may move an operation into retry or reconciliation;
they may not transition a binding to `failed`. `failed` requires a terminal configuration,
authorization, statement, invalid-response, or integrity verdict supported by sanitized
evidence. Cleanup failure never replaces the primary failure.

## 8. Lifecycle and provider operations

### 8.1 Minimal provider protocol

The public provider-neutral protocol has six operations:

```text
provision
reconcile
validate
suspend
resume
retire
```

Backup, isolated restore, restore verification, and restore cleanup are mandatory phases of
initial `validate`, not independent lifecycle commands. Resume-mode `validate` repeats them
only for the storage, corruption, backup, restore, and version-change conditions defined in
section 6.2. Resource inspection and cleanup are internal parts of `reconcile` and `retire`.
This keeps PostgreSQL and ClickHouse in lockstep on a small observable surface while allowing
different physical mechanisms.

### 8.2 State-to-operation mapping

| Binding state or transition | Provider operation | Required result |
| --- | --- | --- |
| `draft → provisioning` | None | Freeze engine and region before an operation may be claimed |
| `draft → retired` | None | `abandon_draft` retires a binding that was never provisioned; it admits no evidence because no operation or resource exists, and refuses any draft holding a live operation |
| `provisioning` | `provision`, then `reconcile` on replay or ambiguity | Exact resources recorded; principals and namespaces created; bootstrap credentials rotated |
| `provisioning → validating` | None | Provision receipt matches the binding revision and engine |
| `validating` | `validate` | Initial validation evidence including backup, restore, and restore cleanup |
| `validating → ready` | No plain transition | `record_validation` applies the injected readiness policy, atomically appends evidence, advances the revision, and stamps `provisioned_at` |
| `ready → suspended` | `suspend` | New work blocked; process stopped or fenced; volumes and retention-governed data preserved |
| `suspended` | `resume`, then `validate` in resume mode | Fresh resume evidence bound to the suspended revision |
| `suspended → ready` | No plain transition | `record_resume_validation` atomically appends fresh evidence and advances the revision |
| `ready or suspended → retiring` | `retire` | Work drained or fenced; retention disposition recorded for every resource |
| `retiring → retired` | No plain transition | `record_retirement` atomically appends retirement evidence after every resource is complete, retained to a deadline, or has an attributable cleanup failure |
| `failed → retired` | `retire`, then no plain transition | Direct retirement performs the same exact-ledger disposition; `record_retirement` admits the terminal revision because no `retiring` state exists |

Plain `transition` rejects `validating → ready`, `suspended → ready`,
`retiring → retired`, and `failed → retired`. Only the corresponding evidence-admission
methods can enter `ready` or `retired`.

`draft → retired` is the one path into `retired` that admits no evidence. Addendum section 6.4.1
ratifies it to abandon a binding that was never provisioned, and such a binding has no operation, no
resource, and no cleanup to attest, so requiring retirement evidence would make the ratified
transition unreachable. `abandon_draft` owns that revision, asserts the state is `draft` and that no
live operation exists, and leaves `provisioned_at` null.

### 8.3 Provisioning order and recovery

The sequence is:

1. Load the expected draft revision and transition it to `provisioning`.
2. Allocate a deterministic operation identifier and claim the new provisioning revision.
3. Record all intended resources as `planned`.
4. Provision or reconcile exact resources idempotently.
5. Rotate inherited/bootstrap credentials and create the principal and namespace matrix.
6. Advance to `validating` using the matching provision result.
7. Run every initial readiness probe.
8. Record the backup resource before creating the encrypted backup.
9. Record all restore resources before creating the isolated restore target.
10. Restore and verify representative data, metadata, identities, roles, and integrity markers.
11. Remove or retention-classify every restore resource and record the cleanup digest.
12. Apply the explicit readiness policy, atomically record validation evidence, and enter
    `ready` with `provisioned_at` set.

A crash after the public transition but before the claim leaves an immutable provisioning
binding with no operation. Recovery may allocate and claim a new operation for that exact
revision. A crash after claim reuses or reconciles the same operation, which requires the repository
to find it: the operation identifier derives from a sequence that advances on every allocation, so a
resumed orchestrator must look up the live operation for `(tenant_id, binding_id)` before allocating
anything. At most one live operation exists per binding, enforced by a partial unique index.
Revising engine or region after step 1 is impossible.

## 9. Retention-aware retirement

`retired` means:

- the binding accepts no activation or warehouse work;
- active services are stopped or fenced;
- cleanup was initiated only for exact ledger records;
- every resource has a terminal cleanup disposition of `complete`, `retained`, or `failed`;
- retained resources carry the contractual retention deadline; and
- cleanup failures carry a sanitized closed classification.

It does not mean all customer data was immediately deleted. A later authorized cleanup pass
acts only after each deadline and can prove zero residual resources. Verified deletion after
the contractual period remains a separate evidence event.

Suspend never destroys volumes. Retirement never uses prefixes, globs, tenant strings, or
Compose discovery as deletion authority.

## 10. Readiness conditions and proof ownership

| Condition | Offline proof | Required live proof |
| --- | --- | --- |
| Engine and image pins | Strict models reject mismatches | Engine query and inspected immutable image digest match pins |
| Tenant/network isolation | Tenant-scoped repository and command-shape tests | Host-private binding; unrelated project and network access denied |
| Seven principals | Grant-plan translation tests | Positive and denial statements under every temporary principal |
| Governed namespaces | Canonical expected matrix | Engine metadata matches exact namespace and grants |
| TLS and credential rotation | Configuration and secret-redaction tests | TLS connection succeeds; plaintext path and inherited credential fail |
| Encryption at rest | Evidence contract and production-admission denial | Deferred for local Compose; future production adapter must prove substrate encryption |
| Control/ledger capability | Provider-neutral ledger conformance | Idempotent write/read and forbidden cross-role mutation |
| Monitoring | Typed observation and failure tests | Engine metrics are collected through the monitoring path |
| Capacity alert | Deterministic threshold tests | A bounded synthetic threshold crossing produces and clears an alert observation |
| Backup | Receipt and digest validation | Engine-native encrypted backup is created and inspected |
| Restore | Verification-result model tests | Isolated target answers representative queries and preserves metadata/integrity markers |
| Restore cleanup | Exact-ledger and crash tests | Recorded restore containers, networks, and volumes are absent or retention-classified |
| Replay/reconciliation | Fault-injection tests at every checkpoint | Restart after selected real create/backup/restore phases converges without duplicates |
| Resume | Stale-evidence and ungated-transition denials | Fresh positive, denial, monitoring, version, and integrity probes pass after restart |

If the required live suite has not run, every live-only condition is reported as unproven.
Offline fakes may prove orchestration and payload validation but cannot satisfy a live
readiness disposition.

## 11. Cross-engine supported subset

The common Plan 3A conformance suite asserts observable outcomes rather than identical SQL:

- one dedicated binding cannot inspect or mutate another binding's resources;
- every governed namespace exists and no unexpected writable namespace is exposed;
- each principal succeeds only for the operations in section 5.1;
- no non-administration principal can create principals, change grants, or drop control
  objects;
- ingestion cannot write beyond raw and its bounded ledger surface;
- transformation cannot administer, back up, or alter principal definitions;
- customer SQL and BI can read only allowlisted consumption objects;
- catalog can inspect allowed metadata but cannot read warehouse rows;
- backup can read only what the engine-native backup requires and cannot write or administer;
- only the private backup command boundary can resolve the backup credential handle;
- a repeated operation does not duplicate a project, principal, namespace, backup, or restore
  target;
- a restored target preserves the representative row set, schema metadata, principal/grant
  profile, and integrity marker; and
- retirement acts only on recorded resources and honors retention deadlines.

The supported ClickHouse subset excludes multi-statement transactional guarantees,
transactional DDL rollback, deferred foreign-key enforcement, and row-by-row update/delete
semantics. Later destination and transformation plans may admit only contracts expressible
through append-only generations, deterministic replacement, or another separately proven
ClickHouse mechanism. PillarMesh returns `No Valid Plan` rather than simulating an
unprovable PostgreSQL guarantee.

## 12. Compose promotion and compatibility

The first implementation milestone characterizes the current OpenMetadata controller before
moving it. Tests pin command construction, explicit environment behavior, timeout and
interruption handling, output sanitization, inspect parsing, and ambiguous cleanup behavior.
The provider then imports the shared controller without changing its external lifecycle.

The shared API is not generalized beyond the behavior required by OpenMetadata and the first
warehouse provider. PostgreSQL reaches green before ClickHouse is added; any additional
abstraction must be justified by a concrete difference observed in both consumers.

## 13. Engine ordering and milestones

1. **Specification and conformance contracts.** Amend the addendum, repository documentation,
   public field-shape assertions, transition tables, principal matrix, and failure vocabulary.
2. **Shared Compose boundary.** Characterize, promote, and switch OpenMetadata without behavior
   drift.
3. **Warehouse control durability.** Add evidence models, atomic ready/resume admission,
   deterministic operation identities, claims, private operations, and the resource ledger.
4. **PostgreSQL reference lifecycle.** Implement the complete real lifecycle and make the
   provider-neutral conformance suite green against PostgreSQL.
5. **ClickHouse portability lifecycle.** Implement the same outcomes using ClickHouse-native
   mechanisms and record explicit exclusions.
6. **Two-engine fault and recovery proof.** Exercise replay, ambiguity, stale claims, restore
   leaks, retention, and exact cleanup across both engines.
7. **Required CI and witnessed acceptance.** Measure the full lifecycle on the hosted runner,
   require it for affected paths, add manual reproduction, and publish sanitized evidence.

PostgreSQL is implemented first as the reference. ClickHouse consumes the already-green
contract; the portability gate runs only after both adapters pass the same fixtures.

## 14. CI cost and gating

The implementation begins by measuring one complete PostgreSQL-plus-ClickHouse provision,
validate, backup, restore, resume, retire, and cleanup cycle on the same hosted-runner class
used by CI. The required gate has a ten-minute target.

The lifecycle job is required for changes to:

- `services/warehouse-control/**`;
- `providers/postgresql/**`;
- `providers/clickhouse/**`;
- the shared Compose boundary in `packages/provider-sdk/**`;
- warehouse emulator and acceptance fixtures; or
- the warehouse lifecycle workflow itself.

Every pull request still runs offline conformance. Documentation-only and unrelated service
changes do not start the live job. If the measured job exceeds ten minutes or is not reliable
on the hosted runner, Plan 3A stops for design review; it does not silently downgrade the job
to optional. A manually dispatched workflow remains available for witnessed reproduction.

## 15. Specification amendments and conformance locks

The implementation plan must amend the managed-platform addendum and lock each amendment in
`tests/conformance/test_specification_conformance.py`:

1. Section 6.2 names the warehouse private operation/resource ledger, initial and resume
   validation evidence, restore verification, and retirement evidence without exposing
   private fields.
2. Section 6.4 states that initial readiness, resumed readiness, and retirement require
   evidence-admission entry points and that plain transitions cannot enter `ready` or
   `retired`.
3. Section 6.4 states that `retired` is retention-aware and does not imply premature physical
   deletion.
4. Section 6.4 maps each lifecycle transition to the six provider operations.
5. Section 6.4 states fresh resume probes and the conditions that require another isolated
   restore.
6. Section 6.5 distinguishes local lifecycle conformance from production-cloud readiness and
   names the storage-encryption gate.
7. Section 18 replaces the broad runtime identity sentence with the seven canonical principal
   classes and their least-privilege intent.
8. Section 20.6 names monitoring and fixed-profile capacity-alert evidence.
9. Section 20.9 names the common Plan 3A outcome subset and the ClickHouse exclusions.
10. The delivery sequence records that Plan 3A precedes source acquisition, destination data
    movement, transformations, scheduling, Superset, and full operations.

Conformance tests compare documented artifact field names, state transitions, provider
operations, principal classes, failure classifications, readiness conditions, retirement
semantics, and ClickHouse exclusions with the implementation. A spec-only or code-only change
must fail.

## 16. Acceptance criteria

Plan 3A is complete only when:

- all addendum amendments and conformance locks are merged together;
- a stale pre-transition or stale-revision claim cannot provision the wrong engine;
- `provisioned_at` is reachable only through atomic initial validation admission;
- both plain routes into `ready` are denied;
- every durable artifact has strict field, tenant, timestamp, and digest validation;
- a production readiness policy rejects local acceptance evidence and deferred storage
  encryption, while the local policy rejects evidence claiming to be production;
- operation and resource writes survive crash/replay without duplicates or orphan adoption;
- restore resources are recorded before creation and removed or retention-classified exactly;
- transient and ambiguous provider failures never become unsupported terminal verdicts;
- PostgreSQL and ClickHouse pass the same positive, denial, replay, restore, resume, and
  retirement outcomes;
- the live required gate runs on affected pull requests within the accepted cost envelope;
- a fresh witnessed run produces sanitized evidence for both engines;
- the run proves backup usability, restored metadata and data, role isolation, monitoring,
  capacity alerting, and exact restore cleanup;
- local evidence clearly withholds production storage-encryption and production-readiness
  claims; and
- no endpoint, credential, infrastructure identifier, backup path, raw canary, or driver text
  enters a public artifact, log, test report, or exported evidence package.

## 17. Review disposition

The review's sixteen corrections are resolved as follows:

| Review item | Resolution |
| --- | --- |
| Claim before immutability freeze | Transition first; claim carries the provisioning revision |
| Principal contradiction | Ratify the approved seven-class reconciled vocabulary |
| Missing encryption and monitoring | Name evidence fields; live-prove monitoring; explicitly gate production storage encryption |
| Zero residual versus retention | Define retention-aware retirement and later verified deletion |
| Unreachable `provisioned_at` and ungated ready | Add atomic `record_validation`; deny plain ready transition |
| Unnamed durable artifacts | Define initial evidence, resume evidence, restore verification, retirement evidence, private operation, and resource ledger shapes |
| Restore leak window | Record every restore resource before creation |
| Unmapped provider methods | Reduce to six operations and map each lifecycle state |
| Resume proof | Require fresh gated resume evidence |
| Unspecified operation identity | Derive from domain, tenant, and sequence; reject competing live claims |
| Version placement and upgrades | Put version/build/image in evidence; defer upgrades |
| Open-ended failure classification | Define a closed enum and prohibit transient terminal verdicts |
| Fake-only readiness | Separate offline and required live proof per condition |
| Accidental Compose substrate | Promote the characterized controller to provider SDK |
| Undefined conformance outcomes | Enumerate grant, isolation, replay, restore, and ClickHouse subset outcomes |
| Missing decomposition | Sequence specification, substrate, control, PostgreSQL, ClickHouse, recovery, and CI milestones |
