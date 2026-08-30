# Plan 3A Design Review: Managed Warehouse Provisioning and Validation

Instructions for the agent revising the Plan 3A design before it is converted into a
task-level implementation plan. Self-contained: every claim below cites a file, a
specification section, or a line you can read.

**Verdict:** the shape is right and matches the pattern Plan 2 already established for
`CatalogBinding`. Do not restart it. It is not yet implementable: it contradicts the
specification in three places, leaves the durable artefacts unnamed, and does not say how
any of its headline capability gets proven. Revise as set out here, resolve
"Blocked on the user", then write the implementation plan.

## What already exists

Read these before changing anything.

| Artefact | Path |
| --- | --- |
| Product boundary (design authority) | `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md` |
| The binding this plan drives | `services/warehouse-control/` |
| The pattern to reuse, delivered in Plan 2 | `services/catalog-control/`, `providers/openmetadata/` |
| Existing PostgreSQL provider (read/write) | `providers/postgresql/` |
| Spec/code conformance suite | `tests/conformance/test_specification_conformance.py` |
| Live harness precedent | `tests/emulators/openmetadata/`, `tests/emulators/localstack-snowflake/` |
| Repository-wide rules | `AGENTS.md` |

`AGENTS.md` governs. This document does not restate it.

## Why the approach is right

Keeping `services/warehouse-control` authoritative for lifecycle, provider-neutral
operations, and sanitized evidence, with engine behaviour behind replaceable adapters, is
the same division Plan 2 shipped: `CatalogControlService` owns the lifecycle and
`OpenMetadataProvisioner` owns the mechanism, meeting at the `CatalogProvisioner` and
`CatalogValidator` protocols in
`services/catalog-control/src/pillarmesh_catalog_control/protocols.py`. Section 20.9 requires
PostgreSQL and ClickHouse to pass a common destination conformance suite, which only holds
if the adapters are interchangeable behind one contract. Keep this.

Placement is also already settled, so do not spend the plan re-arguing it:
`tests/repository-structure/validate.sh` allowlists components only under `services/` and
`packages/` (lines 90-91), and `providers/README.md` explicitly refuses a source/destination
split. A warehouse provisioner in `providers/postgresql` beside the existing read/write code,
and a new `providers/clickhouse`, need no ADR and no validator change.

## Required corrections

### 1. Step order 1 to 2 breaks the immutability freeze

**Problem.** The sequence claims the operation first and transitions `draft` to
`provisioning` second.

**Why it is wrong.** Immutability is keyed to lifecycle state. Addendum section 6.3: once
state becomes `provisioning`, tenant, engine, deployment mode, and region are immutable, and
`WarehouseControlService.revise_draft`
(`services/warehouse-control/src/pillarmesh_warehouse_control/service.py:74`) enforces it by
rejecting any state that is not `draft`. Between the claim and the transition the binding is
still mutable.

**Failing scenario to encode as a test.** The operation is claimed, the process dies before
the transition, an operator revises the still-draft binding from PostgreSQL to ClickHouse,
and the resumed operation provisions PostgreSQL against a binding that now says ClickHouse.

**Do.** Transition first, then claim, and carry `binding_revision` on the claim so a stale
claim cannot apply, the way `CatalogValidationEvidence.binding_revision` already does in
`services/catalog-control/src/pillarmesh_catalog_control/models.py`.

### 2. The five principals contradict section 18

**Problem.** The design names administrator, ingestion, transformation, consumption, and
backup principals.

**Why it is wrong.** Addendum section 18 names five different separated identities: runtime,
administration, customer SQL, catalog, and BI. Both lists have five entries and neither is a
subset of the other. The grant matrix, the denial probes, and the access-request work in
section 20.6 all depend on which list is canonical.

**Do.** Pick one list, reconcile it against section 18, and ratify the result as an addendum
amendment in the same change. Do not let the code assert a principal set the specification
does not name.

### 3. Two readiness conditions the specification requires are missing

**Problem.** The readiness list covers TLS connectivity but not encryption at rest, and does
not mention monitoring at all.

**Why it is wrong.** Addendum section 6.4: provisioning validates "engine identity, version,
encryption, network isolation, runtime and administration roles, target and ledger
capabilities, backups, monitoring, and positive and denial probes." Section 20.6 additionally
commits to capacity alerts for the fixed MVP profile.

**Do.** Add encryption at rest and monitoring to the readiness conditions with named
evidence fields, or state explicitly that they are deferred, with the reason and the gate
that admits them later.

### 4. "Zero residual managed resources" collides with retention

**Problem.** Readiness requires "exact cleanup with zero residual managed resources" at
retirement.

**Why it is wrong.** Addendum section 19 requires verified deletion only after the
contractual period, and the Plan 2 ledger already carries a `retention_deadline` on every
private resource (`services/catalog-control/src/pillarmesh_catalog_control/repository.py:78`).
For a warehouse holding customer data, retirement cannot mean immediate zero residual.

**Do.** State what `retired` asserts: cleanup initiated and recorded against exactly the
recorded identifiers, with residual resources still governed by their retention deadline and
a recorded cleanup failure classification when cleanup does not complete.

### 5. `provisioned_at` is currently unreachable and `validating` to `ready` is ungated

**Problem.** Step 12 says "transition validating to ready and set `provisioned_at`", as if
the existing transition path could do that.

**Why it is wrong.** In `services/warehouse-control/src/pillarmesh_warehouse_control/service.py`,
`_rebuild_binding` always carries `binding.provisioned_at` forward, so no code path can set
it, and `transition` permits `validating` to `ready` with no evidence at all. Addendum
section 6.2 says `provisioned_at` is null until provisioning succeeds.

**Do.** Adopt the catalog shape verbatim: plain `transition` refuses `validating` to `ready`
("ready transition requires validation evidence"), and a `record_validation` entry point
writes the evidence and the `ready` revision in one call, stamping `provisioned_at`. See
`CatalogControlService.record_validation`. State this in the plan, because otherwise step 12
is a new path nothing forces callers through.

### 6. Name the durable artefacts instead of describing them

**Problem.** "Private operational state contains endpoints, credentials, infrastructure
identifiers..." and "customer-visible artifacts contain only stable identities, digests,
validation dispositions..." are prose where Plan 2 has types.

**Why it is wrong.** Plan 2 shipped `PrivateCatalogResource`, `PrivateCatalogOperation`,
`CatalogResourceKind`, and `CatalogValidationEvidence` with named digest fields
(`provider_version`, `provider_build_digest`, `provider_image_set_digest`,
`positive_probe_digest`, `denial_probe_digest`, `stable_identity_probe_digest`,
`backup_probe_digest`). Plan 3A's headline capability is isolated restore verification and
there is no field for its result to land in. None of this exists on the warehouse side:
`services/warehouse-control/src/pillarmesh_warehouse_control/repository.py` has two tables,
no resource ledger, no operation claims, no evidence table, and no `PRAGMA foreign_keys = ON`,
which becomes load-bearing the moment child tables appear.

**Do.** Enumerate the warehouse equivalents with field shapes, including the restore
verification result, and mirror the catalog naming so the two services stay legible together.
Addendum section 9.1.1 documents the catalog's private resource ledger in one sentence;
section 6.2 needs the same sentence for the warehouse.

### 7. The restore instance's resources need ledger records before creation

**Problem.** Exact identifiers are recorded at step 3, then an entire second instance is
created at step 9 with no stated recording.

**Why it is wrong.** A crash between steps 9 and 11 leaks containers and volumes that nothing
can name, which is exactly what "remove the restore instance exactly" exists to prevent.

**Do.** Apply record-before-create to the restore instance too, with its own resource kinds,
creation state, and cleanup status in the same ledger.

### 8. Map provider operations onto lifecycle states

**Problem.** The interface lists nine operations (provision, inspect, validate, backup,
restore-and-verify, suspend, resume, retire, cleanup) against the catalog's five, with no
statement of which transition runs which.

**Why it matters.** `failed` goes straight to `retired` without passing through `retiring`
(section 6.4.1), so cleanup for a failed binding has exactly one transition in which to
happen. A nine-method protocol implemented twice is also a large surface to keep in lockstep
for the portability gate.

**Do.** Give a state-to-operation table. Justify `inspect` and `cleanup` as distinct from
reconciliation and retirement, or fold them in.

### 9. Say whether resume re-probes

**Problem.** The design is silent on what `suspended` to `ready` proves.

**Why it matters.** Nothing gates that transition today, so a resumed binding is `ready` on
validation evidence that may long predate the suspension. Addendum section 17: "Infrastructure
health cannot declare semantic success."

**Do.** Decide and ratify: either re-run positive and denial probes on resume, or state that
suspension is defined not to invalidate them and why. Also state that suspend must not
destroy volumes.

### 10. State where `operation_id` comes from

**Problem.** "Claim one operation for the binding" does not say who mints the identifier.

**Why it matters.** Identity in this repository derives from `{domain, tenant_id, sequence}`
and never from a clock. The catalog takes `operation_id` from the caller and enforces
uniqueness with `UNIQUE (tenant_id, binding_id, operation_id)` plus a claims table that
survives a crash (`repository.py`, `private_catalog_operation_claims`).

**Do.** State the same, and state the rejection rule: a second, different operation for a
binding that already holds a live claim fails rather than proceeding. Reference
`OpenMetadataProvisioner.retire_unrecorded_operation_claim`
(`providers/openmetadata/src/pillarmesh_provider_openmetadata/provisioner.py:1063`) as the
reconciliation precedent instead of inventing a new one.

### 11. Say where the engine version lives

**Problem.** "Pinned engine identity and version" appears in readiness, and "capability
claims" appears in the customer-visible artefact, with no field named.

**Why it matters.** `capability_profile_digest` is computed over `capacity_profile`,
`engine_kind`, and `deployment_mode` only
(`WarehouseControlService._capability_profile_digest`), so it is deliberately version-free and
stable across patch upgrades. The version and image digest belong in the evidence, and
validation must assert the observed values match the pin, or a floating tag drifts silently.

**Do.** Say this explicitly, and say whether version-pinned upgrade (section 20.6) is in Plan
3A's scope at all. The lifecycle in section 6.4.1 has no upgrade path.

### 12. Failure classification must be a closed enum

**Problem.** "Sanitized failure classifications" is unspecified.

**Why it matters.** `AGENTS.md`, "Durability, Boundaries, and Failure Classification": a
provider boundary must not leak driver exception types, transport, availability, and
throttling failures must stay distinguishable from statement-level rejections, and consumers
must honour a classification rather than flatten it. A terminal verdict may only be recorded
for classifications that support it.

**Do.** Define the classification as a `StrEnum` at the provider boundary, and state that
`failed` may not be recorded for a transient classification.

### 13. Four of the twelve steps are worthless against a test double

**Problem.** The design says nothing about how any readiness condition is proven.

**Why it matters.** Backup, restore into an isolated instance, verification, and exact removal
exist precisely because they touch a real engine. `AGENTS.md` bars network access in the
offline suite, and warns that a test double must never be more permissive than what it
replaces. Section 20.9 requires that "the isolated restore proves usable data and metadata
rather than archive presence". A fake compose controller that always succeeds proves the code
ran and nothing else.

**Do.** State the test split: an offline suite with fakes, and a live suite under
`tests/emulators/` beside the existing `openmetadata` and `localstack-snowflake` harnesses.
Then, for each readiness condition, name which suite proves it. Say plainly that a condition
provable only in the live suite is unproven until that suite has run.

### 14. Decide the compose substrate deliberately and measure its cost

**Problem.** "One dedicated local warehouse instance" silently commits to a substrate.
Addendum section 6.5 says deployment topology stays technology-neutral until a deployment ADR
selects one, and ADR-0003 does not.

**Why it matters.** The de facto precedent is `DockerComposeController` in
`providers/openmetadata/src/pillarmesh_provider_openmetadata/provisioner.py:342`, roughly 350
lines of subprocess handling, with `COMPOSE_PROJECT`, `COMPOSE_CONTAINER`, `COMPOSE_VOLUME`,
and `COMPOSE_NETWORK` already in `CatalogResourceKind`. Two more copies of that code is the
bad outcome. `AGENTS.md` permits promotion into `packages/` once two real consumers
demonstrate a stable boundary, and the warehouse provisioner is that second consumer.

**Do.** Either promote the controller into `packages/provider-sdk` as part of this plan or
justify not doing so. Either way, count the cost before committing: state how long a full
PostgreSQL-plus-ClickHouse provision, validate, backup, restore, teardown cycle takes, and
whether that belongs in per-pull-request CI or in an opt-in gate.

### 15. "Same conformance suite" must assert outcomes, not mechanisms

**Problem.** "PostgreSQL and ClickHouse can use different physical mechanisms but must
produce the same observable receipts and pass the same conformance suite" is asserted without
content.

**Why it matters.** The engines differ materially in DDL transactionality, backup tooling, and
how a restricted identity is expressed. Section 20.9 requires "equivalent canonical results
for the supported semantic subset", which only means something once the subset is written
down.

**Do.** Enumerate, as concrete assertions, what each principal can and cannot do on each
engine, and state which ClickHouse behaviours are excluded from the supported subset. Do not
leave the exclusions to be discovered during implementation.

### 16. The design has no decomposition, no non-goals, and no engine ordering

**Problem.** It is a design with no milestones, no stated exclusions, and no sequencing.

**Why it matters.** Section 20.9 requires both engines, so the ordering decides when the
portability gate can first run, which is the entire reason two engines are being built.

**Do.** State whether ClickHouse follows PostgreSQL to green or the two proceed in lockstep,
list the non-goals explicitly, and list up front every addendum amendment and conformance test
the plan will produce so they are part of the decomposition rather than trailing it.

## Blocked on the user

Do not decide these alone; each changes what the specification says.

1. **Principal list (correction 2).** Which five separated identities are canonical: the
   design's administrator/ingestion/transformation/consumption/backup, section 18's
   runtime/administration/customer SQL/catalog/BI, or a reconciled set.
2. **Resume semantics (correction 9).** Whether `suspended` to `ready` re-runs positive and
   denial probes.
3. **Live gate (correction 13).** Whether the live provisioning suite runs per pull request
   or as an opt-in gate, given the cycle time from correction 14.
4. **Compose substrate (correction 14).** Whether the shared compose controller is promoted
   into `packages/provider-sdk` now.

## Definition of done for the implementation plan

- Every correction above is addressed or explicitly declined with a reason.
- Every new durable artefact has a stated field shape, lifecycle, and tenant-scoping rule.
- Every artefact shape and transition table documented in the addendum has a matching check
  in `tests/conformance/`.
- Each capability has a success case and at least one denial, invalid-input, immutable-state,
  replay, or cross-tenant case, per `AGENTS.md`.
- Each readiness condition names the suite that proves it, and conditions proven only live
  are marked as such.
- Spec and code cannot disagree silently: the addendum, the implementation, and the
  conformance suite change together.
