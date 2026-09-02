# Plan 3A Design Review: Managed Warehouse Provisioning and Validation

Instructions for the agent revising the Plan 3A design before it is converted into a
task-level implementation plan. Self-contained: every claim below cites a file, a
specification section, or a line you can read.

**Citation basis.** Every citation below was first written against `main` at commit `f0b82ff`
and has been re-resolved against `main` at `7ebc366`, the merge of the Plan 3A implementation
(pull request #12), which is the tree a reader has today. Line numbers below are that tree's.
Relative to `f0b82ff`, `warehouse-control` gained `orchestration.py`, `readiness.py`,
`retirement.py`, `private_state.py`, `evidence.py`, `protocols.py`, `errors.py`, and
`secrets.py` alongside the existing `service.py`, and `provisioner.py` line 342 moved to 343
and line 1063 moved to 988. Re-resolve any citation again if the file has moved since.

**Current disposition, verified against `main` at `7ebc366`.** This review's original verdict
applied to `main` at `f0b82ff`. The implementation has since closed every blocking correction
and most required design corrections. Do not re-apply a closed item merely because its original
finding remains below.

| Correction | Current disposition |
| --- | --- |
| 1 | Closed: lifecycle freezes the binding before the revision-bound operation claim. |
| 2 | Closed: section 18.1 and `WarehousePrincipalClass` use the approved seven-class vocabulary. |
| 3 | Closed: readiness records a profile-specific encryption-at-rest disposition. |
| 4 | Closed: retirement records retention-governed residual resources instead of claiming immediate deletion. |
| 5 | Closed: only atomic validation admission can stamp `provisioned_at` and enter `ready`. |
| 6 | Closed: named public evidence and private operation, claim, and resource artefacts are durable. |
| 7 | Closed: restore resources are recorded before creation and the isolated target fails closed. |
| 8 | Closed: six provider operations are mapped to lifecycle states and replay behavior. |
| 9 | Closed: resume requires fresh positive and denial probes and durable resume evidence. |
| 10 | Closed: caller-supplied operation identities have durable, revision-bound exclusive claims. |
| 11 | Design closed: version, build, and image evidence plus fresh resume checks exist; section 6.4 now defines the future production revalidation and fail-closed suspension response without claiming it as Plan 3A proof. |
| 12 | Closed: `WarehouseFailureClassification` is a closed vocabulary that preserves transient and terminal distinctions. |
| 13 | Design closed, proof in progress: offline/live ownership is explicit and PostgreSQL has fresh live lifecycle evidence; ClickHouse live proof remains required. |
| 14 | Partially closed: the Compose boundary is promoted and used by OpenMetadata and PostgreSQL; the combined PostgreSQL-plus-ClickHouse hosted-runner cost remains to be measured. |
| 15 | Design closed, proof in progress: common outcomes and ClickHouse exclusions are explicit; ClickHouse conformance remains required. |
| 16 | Closed: PostgreSQL-first milestones, portability consequence, amendments, and non-goals are explicit. |
| 17 | Closed by the milestone estimate below; measured CI cycle time remains an acceptance input, not an estimate. |

**Current verdict:** Plan 3A is implementable and implementation is underway. No blocking design
correction remains. PostgreSQL alone does not prove destination portability. Completion still
requires ClickHouse conformance and live
evidence, two-engine recovery and retention proof, hosted-runner cycle measurement, the required
path-filtered gate, and witnessed acceptance. The continuously-ready production revalidation loop
is explicitly outside Plan 3A and remains a production-admission requirement. Documentation and
prior local test output are not substitutes for those remaining proofs.

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

Each correction is tagged. **Blocking** means it contradicts the specification or makes a
headline capability unprovable; the implementation plan cannot be written until it is
resolved. **Required** means it must appear in the implementation plan but does not block
writing it. Corrections 1, 2, 4, and 13 are Blocking.

### 1. Blocking — Step order 1 to 2 breaks the immutability freeze

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

### 2. Blocking — The five principals contradict section 18

**Problem.** The design names administrator, ingestion, transformation, consumption, and
backup principals.

**Why it is wrong.** Addendum section 18 names five different separated identities: runtime,
administration, customer SQL, catalog, and BI. Both lists have five entries and neither is a
subset of the other. The grant matrix, the denial probes, and the access-request work in
section 20.6 all depend on which list is canonical.

**Do.** The canonical list is the user's decision, not yours. That decision is now recorded under
"Ratified user decisions" below. Ratify it as an addendum amendment in the same change that
implements it, so the code never asserts a principal set the specification does not name.

### 3. Required — Two readiness conditions the specification requires are missing

**Problem.** The readiness list covers TLS connectivity but not encryption at rest, and does
not mention monitoring at all.

**Why it is wrong.** Addendum section 6.4: provisioning validates "engine identity, version,
encryption, network isolation, runtime and administration roles, target and ledger
capabilities, backups, monitoring, and positive and denial probes." Section 20.6 additionally
commits to capacity alerts for the fixed MVP profile.

**Do.** Add encryption at rest and monitoring to the readiness conditions with named
evidence fields, or state explicitly that they are deferred, with the reason and the gate
that admits them later.

### 4. Blocking — "Zero residual managed resources" collides with retention

**Problem.** Readiness requires "exact cleanup with zero residual managed resources" at
retirement.

**Why it is wrong.** Addendum section 19 requires verified deletion only after the
contractual period, and the Plan 2 ledger already carries a `retention_deadline` on every
private resource (`services/catalog-control/src/pillarmesh_catalog_control/repository.py:85`).
For a warehouse holding customer data, retirement cannot mean immediate zero residual.

**Do.** State what `retired` asserts: cleanup initiated and recorded against exactly the
recorded identifiers, with residual resources still governed by their retention deadline and
a recorded cleanup failure classification when cleanup does not complete.

### 5. Required — `provisioned_at` is currently unreachable and `validating` to `ready` is ungated

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

### 6. Required — Name the durable artefacts instead of describing them

**Problem.** "Private operational state contains endpoints, credentials, infrastructure
identifiers..." and "customer-visible artifacts contain only stable identities, digests,
validation dispositions..." are prose where Plan 2 has types.

**Why it is wrong.** Plan 2 shipped `PrivateCatalogResource`, `PrivateCatalogOperation`,
`CatalogResourceKind`, and `CatalogValidationEvidence` with named digest fields
(`provider_version`, `provider_build_digest`, `provider_image_set_digest`,
`positive_probe_digest`, `denial_probe_digest`, `stable_identity_probe_digest`,
`backup_probe_digest`). Plan 3A's headline capability is isolated restore verification and
there is no field for its result to land in. None of this exists on the warehouse side:
`services/warehouse-control/src/pillarmesh_warehouse_control/repository.py` has two tables and
no resource ledger, no operation claims, and no evidence table. It does already set
`PRAGMA foreign_keys = ON`, so child tables can rely on it.

**Do.** Enumerate the warehouse equivalents with field shapes, including the restore
verification result, and mirror the catalog naming so the two services stay legible together.
Addendum section 9.1.1 documents the catalog's private resource ledger in one sentence;
section 6.2 needs the same sentence for the warehouse.

### 7. Required — The restore instance needs ledger records and a stated isolation boundary

**Problem.** Exact identifiers are recorded at step 3, then an entire second instance is
created at step 9 with no stated recording.

**Why it is wrong.** A crash between steps 9 and 11 leaks containers and volumes that nothing
can name, which is exactly what "remove the restore instance exactly" exists to prevent.

**Do.** Apply record-before-create to the restore instance too, with its own resource kinds,
creation state, and cleanup status in the same ledger.

**Also define what "isolated" asserts.** The word currently carries the entire data-safety
argument for restoring customer data into a second live engine, and nothing states its content.
State at minimum: the restore instance is network-isolated from the primary and reachable only
by the verifying principal; it uses distinct credentials that grant nothing on the primary; no
restore-verification step may write to the primary or to the binding's ledger beyond its own
resource records; and the verification result names the restore instance, never the primary.
Encode a test that a restore-verification run against a deliberately misconfigured restore
instance fails closed rather than falling back to the primary.

### 8. Required — Map provider operations onto lifecycle states

**Problem.** The interface lists nine operations (provision, inspect, validate, backup,
restore-and-verify, suspend, resume, retire, cleanup) against the catalog's five, with no
statement of which transition runs which.

**Why it matters.** `failed` goes straight to `retired` without passing through `retiring`
(section 6.4.1), so cleanup for a failed binding has exactly one transition in which to
happen. A nine-method protocol implemented twice is also a large surface to keep in lockstep
for the portability gate.

**Do.** Give a state-to-operation table. Justify `inspect` and `cleanup` as distinct from
reconciliation and retirement, or fold them in.

### 9. Required — Say whether resume re-probes

**Problem.** The design is silent on what `suspended` to `ready` proves.

**Why it matters.** Nothing gates that transition today, so a resumed binding is `ready` on
validation evidence that may long predate the suspension. Addendum section 17: "Infrastructure
health cannot declare semantic success."

**Do.** Decide and ratify: either re-run positive and denial probes on resume, or state that
suspension is defined not to invalidate them and why. Also state that suspend must not
destroy volumes.

### 10. Required — State where `operation_id` comes from

**Problem.** "Claim one operation for the binding" does not say who mints the identifier.

**Why it matters.** Identity in this repository derives from `{domain, tenant_id, sequence}`
and never from a clock. The catalog takes `operation_id` from the caller and enforces
uniqueness with `UNIQUE (tenant_id, binding_id, operation_id)` plus a claims table that
survives a crash (`repository.py`, `private_catalog_operation_claims`).

**Do.** State the same, and state the rejection rule: a second, different operation for a
binding that already holds a live claim fails rather than proceeding. Reference
`OpenMetadataProvisioner.retire_unrecorded_operation_claim`
(`providers/openmetadata/src/pillarmesh_provider_openmetadata/provisioner.py:988`) as the
reconciliation precedent instead of inventing a new one.

### 11. Required — Say where the engine version lives

**Problem.** "Pinned engine identity and version" appears in readiness, and "capability
claims" appears in the customer-visible artefact, with no field named.

**Why it matters.** `capability_profile_digest` is computed over `capacity_profile`,
`engine_kind`, and `deployment_mode` only
(`WarehouseControlService._capability_profile_digest`), so it is deliberately version-free and
stable across patch upgrades. The version and image digest belong in the evidence, and
validation must assert the observed values match the pin, or a floating tag drifts silently.

**Do.** Say this explicitly, and say whether version-pinned upgrade (section 20.6) is in Plan
3A's scope at all. The lifecycle in section 6.4.1 has no upgrade path.

**Then say what happens when the pin and the engine diverge after `ready`.** Validation-time
assertion covers provisioning; the common case is an engine patched underneath a live binding.
Section 17 applies here too: infrastructure health cannot declare semantic success, so a
binding whose observed version no longer matches its pin is not silently still `ready`. State
whether that condition is detected by reconciliation, what it records, and whether it demands
revalidation or only an operator notification.

### 12. Required — Failure classification must be a closed enum

**Problem.** "Sanitized failure classifications" is unspecified.

**Why it matters.** `AGENTS.md`, "Durability, Boundaries, and Failure Classification": a
provider boundary must not leak driver exception types, transport, availability, and
throttling failures must stay distinguishable from statement-level rejections, and consumers
must honour a classification rather than flatten it. A terminal verdict may only be recorded
for classifications that support it.

**Do.** Define the classification as a `StrEnum` at the provider boundary, and state that
`failed` may not be recorded for a transient classification.

### 13. Blocking — Four of the twelve steps are worthless against a test double

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

### 14. Required — Decide the compose substrate deliberately and measure its cost

**Problem.** "One dedicated local warehouse instance" silently commits to a substrate.
Addendum section 6.5 says deployment topology stays technology-neutral until a deployment ADR
selects one, and ADR-0003 does not.

**Why it matters.** The de facto precedent is `DockerComposeController` in
`providers/openmetadata/src/pillarmesh_provider_openmetadata/provisioner.py:343`, roughly 350
lines of subprocess handling, with `COMPOSE_PROJECT`, `COMPOSE_CONTAINER`, `COMPOSE_VOLUME`,
and `COMPOSE_NETWORK` already in `CatalogResourceKind`. Two more copies of that code is the
bad outcome. `AGENTS.md` permits promotion into `packages/` once two real consumers
demonstrate a stable boundary, and the warehouse provisioner is that second consumer.

**Do.** Either promote the controller into `packages/provider-sdk` as part of this plan or
justify not doing so. Either way, count the cost before committing: state how long a full
PostgreSQL-plus-ClickHouse provision, validate, backup, restore, teardown cycle takes, and
whether that belongs in per-pull-request CI or in an opt-in gate.

### 15. Required — "Same conformance suite" must assert outcomes, not mechanisms

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

### 16. Required — The design has no decomposition, no non-goals, and no engine ordering

**Problem.** It is a design with no milestones, no stated exclusions, and no sequencing.

**Why it matters.** Section 20.9 requires both engines, so the ordering decides when the
portability gate can first run, which is the entire reason two engines are being built.

**Do.** State whether ClickHouse follows PostgreSQL to green or the two proceed in lockstep,
list the non-goals explicitly, and list up front every addendum amendment and conformance test
the plan will produce so they are part of the decomposition rather than trailing it.

State the ordering's consequence too, rather than treating ordering as scheduling detail: until
both engines pass, the MVP's destination-portability claim — that a customer can choose a
warehouse without binding PillarMesh's semantics to one engine — is asserted and not proven.

### 17. Required — Cost the plan

**Problem.** The design states no team size, duration, or sequencing cost.

**Why it matters.** Plan 3A builds two provisioners, a second engine, a live harness, and
possibly a `packages/provider-sdk` promotion. The M0 design carries a team size and a costed
implementation sequence; a plan of this size without one is inconsistent with the estate's own
discipline, and correction 14's cycle-time measurement is only useful next to a total.

**Do.** Attach an approximate effort and duration to each milestone from correction 16,
and state what the estimate excludes.

## Milestone estimate

The estimate is for the original Plan 3A scope, not the work remaining at `7ebc366`. It assumes
one experienced engineer, prompt architecture review, an available Docker-capable CI runner, and
no provider-release incompatibility beyond the explicit PostgreSQL and ClickHouse differences.

| Milestone | Approximate engineering effort |
| --- | --- |
| Specification and conformance contracts | 2–3 engineer-days |
| Shared Compose boundary | 3–5 engineer-days |
| Warehouse-control durability and evidence admission | 5–8 engineer-days |
| PostgreSQL reference lifecycle | 6–10 engineer-days |
| ClickHouse portability lifecycle | 7–12 engineer-days |
| Two-engine fault, replay, recovery, and retention proof | 4–7 engineer-days |
| Required CI gate and witnessed acceptance | 4–6 engineer-days |

The total is approximately 31–51 engineer-days: seven to eleven calendar weeks for one engineer
including review and stabilization, or four to seven calendar weeks for two engineers after the
shared contracts and Compose boundary are settled. The estimate excludes production-cloud
provisioning, continuously-ready drift reconciliation, in-place upgrades, BYOC, source acquisition,
data movement, transformations, scheduling, OpenMetadata and Superset operations, security audit,
and general-availability hardening. Hosted lifecycle duration is measured evidence and may force a
design review; it is not replaced by this estimate.

## Ratified user decisions

The four decisions that originally blocked revision are settled:

1. **Principal list (correction 2).** Use the reconciled seven-class vocabulary in section 18.1.
2. **Resume semantics (correction 9).** Re-run fresh positive and denial probes before
   `suspended → ready`; repeat isolated restore when the suspension reason or drift concerns
   storage, corruption, backup, restore, or engine version.
3. **Live gate (correction 13).** Require the provisioning lifecycle gate for affected paths,
   subject to the measured ten-minute hosted-runner budget; do not silently make it optional.
4. **Compose substrate (correction 14).** Promote the characterized process boundary into
   `packages/provider-sdk` and keep provider-specific lifecycle semantics in each provider.

## Definition of done for the implementation plan

- Every correction above is addressed or explicitly declined with a reason.
- Every new durable artefact has a stated field shape, lifecycle, and tenant-scoping rule.
- Every artefact shape and transition table documented in the addendum has a matching check
  in `tests/conformance/`.
- Each capability has a success case and at least one denial, invalid-input, immutable-state,
  replay, or cross-tenant case, per `AGENTS.md`.
- Each readiness condition names the suite that proves it, and conditions proven only live
  are marked as such.
- Every ratified user decision is recorded as an addendum amendment or an ADR, dated,
  in the same change that implements it.
- No Blocking correction remains open.
- Spec and code cannot disagree silently: the addendum, the implementation, and the
  conformance suite change together.
