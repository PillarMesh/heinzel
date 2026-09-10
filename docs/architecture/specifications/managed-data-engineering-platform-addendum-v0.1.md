# PillarMesh Managed Data Engineering Platform Addendum v0.1

**Status:** Proposed for final review

**Date:** 2026-08-17

**Applies to:** Enterprise Data Compiler Architecture Specification v0.3, Revenue-to-Cash MVP Implementation Plan v1.4, and the M0 thin-thread specifications

**Decision record:** `docs/architecture/decisions/ADR-0003-managed-data-engineering-platform.md`

**Amendments:**

- 2026-08-28 — Section 6.4 now defines the fail-closed response to version drift after a
  warehouse becomes `ready` and distinguishes Plan 3A local lifecycle proof from the future
  continuously-ready production revalidation loop. Closes correction 11 of the Plan 3A design
  review without expanding Plan 3A into production-cloud operations.
- 2026-08-27 — Section 18 now names seven warehouse principal classes in place of five
  identities. The prior list could not express the separation warehouse provisioning requires:
  `runtime` conflated ingestion with transformation, and backup and restore held no identity of
  its own. The seven classes reconcile the two former five-item lists into the vocabulary approved
  for Plan 3A. Closes correction 2 of the Plan 3A design review.

## 1. Purpose

This addendum changes PillarMesh from a compiler and managed execution plane that assumes an external customer warehouse into a fully managed data engineering platform. PillarMesh manages source integrations, a dedicated analytical warehouse, transformations, catalog, business-process semantics, BI, scheduling, integrity, evidence, maintenance, and recovery. The primary user is a one-person or very small data engineering team that must deliver trustworthy data products without operating a collection of separate infrastructure products.

The competitive category is provider-managed data integration, as exemplified by products such as Fivetran, rather than a database engine such as Snowflake. PillarMesh differs by taking operational responsibility through the destination warehouse and governed consumption layer, and by compiling integrations from approved business-process and semantic contracts rather than treating connector success as the terminal outcome.

This addendum preserves the compiler-centered invariants of the foundational architecture:

- business meaning precedes mechanism;
- legality and feasibility precede execution;
- contracts are durable and physical plans are replaceable;
- AI may propose, but deterministic systems validate, authorize, and execute;
- unsupported semantics produce `No Valid Plan` rather than silent degradation;
- every material decision and execution outcome is attributable and evidenced; and
- an active semantic contract is never mutated in place.

## 2. Superseding decisions

This addendum supersedes the following product-boundary decisions for post-M0 work:

1. PillarMesh now provides a managed analytical warehouse as a mandatory part of the product. It operates supported database engines; it does not implement a database engine.
2. Every tenant receives one dedicated primary warehouse binding. The initial engine catalog is PostgreSQL and ClickHouse.
3. Integrations terminate in the tenant's PillarMesh-operated warehouse. Arbitrary customer-managed destinations are not part of the initial product.
4. PillarMesh provides a narrow contract-trigger service for scheduled, manual, and bounded backfill execution. It does not provide a general DAG or business-workflow scheduler.
5. PillarMesh manages transformations and semantic models. Generated artifacts remain inspectable, versioned, testable, and exportable in a dbt-compatible representation.
6. PillarMesh provides a managed OpenMetadata catalog when a customer has no supported catalog and uses a supported existing catalog when one is present. It does not require two user-facing catalogs.
7. PillarMesh provides a managed Apache Superset deployment for governed dashboards, exploration, and report rendering.
8. The primary customer operating model is supervised autopilot: routine physical work runs within pre-authorized policy; semantic, policy, access, material cost, migration, and destructive changes require risk-tiered human approval.
9. Business requests, incidents, and platform proposals enter one typed request-management boundary. PillarMesh may investigate and draft automatically but may not activate materially new meaning without authorization.
10. A source connection is never sufficient admission evidence. An executable Integration Contract must be grounded in an approved business-process model, catalog authorities, ontology, constraints, and current provider observations.

Historical M0 documents remain records of the PostgreSQL-to-Snowflake thin-thread experiment. They are not silently rewritten and do not define the destination or product scope after this addendum.

## 3. Product promise and limits

### 3.1 Promise

PillarMesh delivers governed data products from operational sources without requiring the customer to assemble or operate connector infrastructure, a warehouse, a scheduler, a transformation runtime, a catalog, a BI service, and an evidence system independently.

The concise product promise is:

> Connect your sources. PillarMesh runs the rest.

The architectural promise is narrower and testable:

> PillarMesh compiles approved business outcomes into legal, versioned integrations, operates their dedicated destination data plane, and continuously proves source-to-consumer conformance.

### 3.2 Ideal initial customer

The initial customer:

- has operational data in a database and one or more SaaS systems;
- needs governed analytical data, dashboards, and periodic reports;
- has one data engineer or a very small data team;
- does not already have an immovable strategic warehouse commitment;
- wants one provider accountable for data movement and destination operations;
- values business integrity, provenance, and recovery evidence; and
- may later require customer-cloud or on-premises placement.

### 3.3 Poor-fit customer

The initial product is not a fit for a customer that:

- requires all data to land in an existing external warehouse;
- needs hundreds of connectors at launch;
- requires unrestricted warehouse administration;
- depends on vendor-specific warehouse features that PostgreSQL or ClickHouse cannot provide;
- needs a general workflow or application-integration platform;
- requires a fully disconnected control plane in the initial release; or
- expects AI to choose business meaning or authorize data access autonomously.

### 3.4 Explicit non-goals

- No new database engine.
- No arbitrary external destination in the initial product.
- No universal connector claim.
- No general BPMN engine, workflow scheduler, API-management platform, MDM suite, or event router.
- No replacement for an authoritative enterprise policy system.
- No silent replacement of an existing enterprise catalog.
- No unrestricted user SQL mutation or agent write authority.
- No automatic semantic migration or warehouse-engine switch.
- No claim of universal exactly-once behavior where a provider lacks the necessary primitive.

## 4. One-person data engineering operating model

### 4.1 Human responsibility

The data engineering architect remains responsible for:

- business-process design;
- business meaning and authoritative ownership;
- identity, key, relationship, history, and deletion decisions;
- governance, residency, retention, and access decisions;
- portfolio prioritization;
- acceptance criteria;
- material cost and risk decisions;
- semantic migrations and exceptions; and
- irreversible retirement or deletion approval.

### 4.2 PillarMesh responsibility

PillarMesh manages:

- warehouse and service provisioning;
- connector installation, authentication, upgrades, and recovery;
- initial load, incremental synchronization, schedules, and bounded backfills;
- transformation generation, testing, deployment, and rollback;
- metadata discovery and catalog publication;
- dashboard compilation and report rendering;
- routine schema drift and physical optimization where pre-authorized;
- checkpointing, replay, reconciliation, and evidence;
- backups, restore verification, patching, monitoring, and capacity management; and
- incident correlation, impact analysis, and bounded remediation proposals.

### 4.3 Supervised autopilot

The engineer's primary surface is a decision inbox rather than a task queue of routine operations. PillarMesh must not wake an operator for a transient retry, connector-token refresh, routine resynchronization, safe additive column, bounded warehouse resize, backup rotation, or Superset worker restart. It must request attention for changed business meaning, unresolved ownership, policy conflict, widened access, material recurring cost, persistent data-loss risk, migration, or an unrecoverable contract violation.

## 5. System model

The managed platform contains six cooperating planes:

1. **Demand plane:** requests, incidents, platform proposals, conversations, assignments, priority, and approvals.
2. **Semantic plane:** business processes, ontology, glossary, identities, relationships, metrics, constraints, policies, and contracts.
3. **Compilation plane:** IIR, legality, feasibility, physical planning, transformation compilation, dashboard compilation, and signed execution graphs.
4. **Managed data plane:** source acquisition, staging, warehouse, state, transformations, reconciliation, and governed consumption.
5. **Experience plane:** architecture cockpit, embedded catalog, embedded analytics, reports, evidence, and decision inbox.
6. **Operations plane:** scheduling, monitoring, backup, restore, upgrade, capacity, incidents, cost, and retirement.

No plane may bypass semantic authority. A dashboard, schedule, connector, or warehouse operation cannot create executable meaning by itself.

### 5.1 Durable catalog and semantic artifact envelope

Every newly documented durable artifact — `SemanticCandidateSet`, `CatalogBinding`,
`AuthorityObservation`, `OntologyReviewBundle`, `ApprovedSemanticVersion`, and
`ManagedIntegrationContract` — uses canonical serialization, is digest-addressable, and rejects
unknown input. Its stable ID is derived from its domain tag, tenant identifier, and
repository-assigned sequence, never from a clock; timestamps remain attributable record metadata
outside that identity rule.

Every catalog or semantic repository and service lookup requires tenant identity in its initial
query. Cross-tenant access is denied before reading or deserializing the artifact payload.

## 6. Tenant setup and managed warehouse

### 6.1 Customer choices

Initial setup asks the tenant administrator to approve:

- warehouse engine: PostgreSQL or ClickHouse;
- supported deployment region;
- workload-oriented capacity profile;
- data-residency requirements;
- retention and recovery profile;
- source and data-product owners;
- approval roles and cost thresholds; and
- notification and incident contacts.

PillarMesh recommends an engine and capacity from declared workload characteristics, but the customer makes the final choice before provisioning. Both initial engines must pass the MVP destination-portability gate before either is offered to customers.

### 6.2 Warehouse binding

Every tenant receives one opaque primary `WarehouseBinding`:

```text
WarehouseBinding
  schema_version             1
  binding_id
  tenant_id
  engine_kind                postgresql | clickhouse
  deployment_mode            pillarmesh_cloud
  region
  capacity_profile           mvp-fixed
  capability_profile_digest
  lifecycle_state
  revision
  created_at
  updated_at
  provisioned_at
```

`capacity_profile` names the profile; `capability_profile_digest` is the digest over
`capacity_profile`, `engine_kind`, and `deployment_mode`, so a binding carries both the
name an operator reads and the value a contract can compare.

`revision`, `created_at`, and `updated_at` exist because bindings are stored append-only
as `(binding_id, revision)`. No row is updated in place, so the lifecycle history of an
object that is immutable after draft survives, and every mutation supplies the caller's
expected revision and fails closed when the stored revision has moved.

`provisioned_at` is null until provisioning succeeds.

Contracts reference `binding_id`. Endpoints, credentials, administrator identities, infrastructure identifiers, encryption keys, and backup locations are private operational state and never enter semantic artifacts or customer-visible evidence.

The private warehouse operation and resource ledgers contain operational handles, resource identities,
and secrets. The following frozen, strict, tenant-scoped evidence artifacts contain only attributable,
privacy-safe digests, counts, and timestamps. They are canonically serializable and digest-addressable.

#### WarehouseValidationEvidence

```text
WarehouseValidationEvidence
  schema_version                    1
  evidence_id
  tenant_id
  binding_id
  binding_revision
  validation_profile                local_acceptance | production
  engine_kind                       postgresql | clickhouse
  engine_version                    ^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][0-9A-Za-z]{1,32})?$ max_length=64
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

#### WarehouseResumeValidationEvidence

```text
WarehouseResumeValidationEvidence
  schema_version                  1
  evidence_id
  tenant_id
  binding_id
  binding_revision
  engine_kind                     postgresql | clickhouse
  engine_version                  ^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][0-9A-Za-z]{1,32})?$ max_length=64
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

#### WarehouseRestoreVerification

```text
WarehouseRestoreVerification
  schema_version                  1
  verification_id
  tenant_id
  binding_id
  binding_revision
  engine_kind                     postgresql | clickhouse
  source_backup_artifact_digest
  representative_data_digest
  schema_metadata_digest
  principal_profile_digest
  integrity_marker_digest
  query_behavior_digest
  verified_at
```

#### WarehouseRetirementEvidence

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

#### WarehouseFailureClassification

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

The validation profile and storage-encryption disposition form a closed pair: production evidence
uses `production` with `proven`; local acceptance evidence uses `local_acceptance` with
`deferred_local_acceptance`. Evidence never contains raw rows, query results, credentials,
endpoints, backup paths, or resource identities.

### 6.3 Immutability

Before provisioning begins, the customer may change engine or region. Once lifecycle state becomes `provisioning`, tenant, engine, deployment mode, and region are immutable. A later change is a separately authorized migration with a new binding, shadow verification, cutover, rollback window, and retirement.

### 6.4 Lifecycle

```text
draft → provisioning → validating → ready
                     ↘ failed

ready → suspended → ready
ready → retiring → retired
```

Only `ready` bindings accept new activations. Provisioning validates engine identity, version,
storage encryption, network isolation, runtime and administration roles, target and ledger
capabilities, backups, monitoring, fixed-capacity alerts, and positive and denial probes.

`validating → ready` is evidence-gated: `record_validation` applies the readiness policy and
admits initial `WarehouseValidationEvidence`. `suspended → ready` is evidence-gated:
`record_resume_validation` admits fresh `WarehouseResumeValidationEvidence`. `retiring → retired`
and `failed → retired` are evidence-gated: `record_retirement` admits
`WarehouseRetirementEvidence`. Plain `transition` cannot enter `ready` or `retired` through
those paths.

Resume repeats fresh TLS, network-isolation, monitoring, positive, denial, engine-version, and
storage-integrity probes. It also repeats isolated backup, restore, verification, and cleanup when
the suspension reason or observed drift concerns storage, corruption, backup, restore, or engine
version.

`retired` means no activation or warehouse work can resume, active services are stopped or fenced,
and each exact ledger resource is complete, retained to its contractual deadline, or has an
attributable cleanup failure. It does not mean data is physically deleted before its retention
deadline.

#### Provider operations

```text
provision
reconcile
validate
suspend
resume
retire
```

Backup, isolated restore, restore verification, and restore cleanup are mandatory phases of
`validate`, not independent lifecycle commands. Resource inspection and cleanup are internal parts
of `reconcile` and `retire`.

Exact observed engine version, build, and image identity belong in validation evidence and must
match the provider's immutable pins. Plan 3A proves that check during initial validation and again
through fresh resume validation; it does not claim continuous production drift detection. Before a
production adapter is admitted, a contract-scoped revalidation loop owned by the state plane must
repeat that comparison while a binding is `ready`. A mismatch fences new activations and run
intents, records sanitized attributable evidence, transitions the binding to `suspended`, and
requires fresh resume validation before work can continue. Version drift is one of the conditions
that requires isolated restore verification during that resume. This is a revalidation control
loop, not a general scheduler.

### 6.4.1 MVP transition table

For the MVP, warehouse binding lifecycle transitions are fixed as follows.

```text
draft        → provisioning, retired
provisioning → validating, failed
validating   → ready, failed
ready        → suspended, retiring
suspended    → ready, retiring
retiring     → retired
failed       → retired
retired      → (terminal)
```

`draft → retired` abandons a binding that was never provisioned, and `failed → retired`
retires one whose provisioning did not succeed. Neither passes through `retiring`, which
exists to drain a binding that carried traffic. `ready → retired` is deliberately absent:
a ready binding is always drained through `retiring`.

### 6.5 Managed components

The dedicated tenant data plane initially contains:

- PostgreSQL or ClickHouse warehouse;
- OpenMetadata catalog for a tenant without a supported external catalog, or the adapter and private authority bindings for the selected external catalog;
- Apache Superset BI service;
- transformation runtime;
- report-rendering worker;
- bounded cache and staging;
- tenant-scoped credentials and keys; and
- backup and recovery integration.

The deployment topology remains technology-neutral until a deployment ADR selects a cloud and orchestration technology.

Local Compose acceptance proves lifecycle conformance but cannot prove cloud-volume storage
encryption. Its validation evidence must use `local_acceptance` with
`deferred_local_acceptance`. Production readiness requires a substrate-native storage-encryption
probe and validation evidence using `production` with `proven`; no environment-name branch or
caller-supplied Boolean can weaken that admission rule.

## 7. Future placement modes

The logical binding reserves future modes:

```text
pillarmesh_cloud
customer_cloud
customer_on_prem
```

Customer-cloud and on-premises modes mean that the customer supplies infrastructure while PillarMesh retains database, runtime, connector, catalog, and BI operational authority. They are not bring-your-own-warehouse modes.

The initial on-premises connectivity mode is narrower: an outbound-only relay reaches a private source while the managed data plane remains in a PillarMesh region. The relay receives signed, bounded work leases, resolves credentials locally, exposes no public listener, and has no planning authority.

A fully disconnected control plane is deferred because it would require local identity, compiler, signer, trigger, state, evidence, registry, update, monitoring, catalog, BI, and warehouse services with a separate release and security model.

## 8. Business-process model

### 8.1 Purpose

A connector establishes reachability and provider capabilities. It does not establish business meaning. Every nontrivial data product must reference an approved `BusinessProcess` version or explicitly declare that it is a source-aligned technical product with no cross-system business-process claim.

### 8.2 Durable process objects

The semantic plane models:

```text
BusinessProcess
BusinessEntity
BusinessEvent
BusinessState
BusinessTransition
RelationshipConstraint
IdentityRule
MetricDefinition
IntegrityConstraint
AuthorityBinding
```

A process records owner, participants, entities, events, valid states, valid transitions, deadlines, reconciliation invariants, exception definitions, and completion criteria.

### 8.3 Example invariants

For order-to-cash:

- every accepted order resolves to exactly one governed customer identity;
- an issued invoice references an accepted order;
- a settled payment references an issued invoice;
- a refund does not exceed settled payment without an approved exception;
- a paid eligible order produces an active subscription within the declared window; and
- recognized revenue is attributable to a valid lifecycle and approved recognition rule.

PillarMesh distinguishes a source defect, integration defect, late-arriving fact, policy violation, process exception, and warehouse corruption. A valid business exception is not automatically repaired as a pipeline defect.

### 8.4 Process-package intake

The MVP accepts one immutable, versioned `BusinessProcessPackage` consisting of a UTF-8 Markdown narrative and a strict JSON manifest. The manifest identifies process name, owner, participants, outcomes, known entities, events, states, rules, source references, and unresolved questions. Each uploaded artifact retains its original bytes, media type, digest, uploader, and receipt time outside the deterministic semantic payload.

PillarMesh may extract and propose process objects, ontology terms, mappings, constraints, and questions from the package. The original upload, extracted candidates, human corrections, approvals, and compiled process version remain distinct and attributable. Re-upload creates a new package version; it never mutates an approved process or silently recompiles an active Integration Contract.

```text
SemanticCandidateSet
  schema_version
  set_id
  tenant_id
  revision
  package_id
  package_version
  original_digest
  manifest_digest
  extractor_id
  extractor_version
  candidates
  unresolved_questions
  created_at
```

## 9. Catalog and ontology

### 9.1 Catalog operating model

At setup, PillarMesh asks whether the customer has an authoritative catalog.

- If no supported catalog exists, PillarMesh provisions and operates OpenMetadata as the customer's catalog experience.
- If a supported catalog exists, PillarMesh connects read-only first, observes its capabilities and authorities, and uses that catalog as the customer-facing authority. It does not also provision OpenMetadata by default.
- PillarMesh retains only the narrow versioned semantic and authority records required to compile and prove its own contracts. That private registry is not exposed as a competing general catalog.

The first external catalog adapter after the bundled path is DataHub. Apache Atlas and commercial enterprise catalogs are deferred. A catalog whose APIs cannot provide stable identities, versioned observations, ownership, glossary/classification authority, and lineage cannot satisfy this mode; the customer must use managed OpenMetadata or wait for a compatible adapter.

### 9.1.1 Catalog binding

`CatalogBinding` is the tenant-scoped, revisioned record that selects and governs a catalog
deployment. It contains no endpoint, provider identifier, credential, or other private
operational state.

```text
CatalogBinding
  schema_version             1
  binding_id
  tenant_id
  provider_kind              openmetadata
  deployment_mode            pillarmesh_managed
  capability_profile_digest
  lifecycle_state
  revision
  created_at
  updated_at
  provisioned_at
```

`provisioned_at` remains null until positive and denial validation succeeds. The private
resource ledger records exact provider-local resource identifiers, creation state, retention
deadline, and cleanup status; retirement cleanup acts only on those recorded identifiers.

### 9.1.2 Catalog lifecycle

Catalog binding transitions are fixed as follows:

```text
draft        → provisioning, retired
provisioning → validating, failed
validating   → ready, failed
ready        → suspended, retiring
suspended    → ready, retiring
retiring     → retired
failed       → retired
retired      → (terminal)
```

### 9.2 Authority

| Information | Authority |
| --- | --- |
| Observed source schema and source facts | Source system |
| Business meaning and process semantics | Approved business owner |
| Enterprise policy | Connected policy authority |
| Imported glossary/classification | Declared external catalog authority |
| Approved PillarMesh process and contract | PillarMesh semantic registry |
| Physical plan and generated models | PillarMesh compiler |
| Runtime outcome | PillarMesh evidence chain |
| Search, browsing, and catalog presentation | OpenMetadata |
| Dashboard rendering and exploration | Superset |

PillarMesh never turns an unreviewed catalog description, inferred lineage edge, or AI suggestion into executable authority.

Authority is resolved by `(information_kind, source_kind)`, never by one global ranking.
Only the following source kinds are admitted, in decreasing precedence, for each information
kind:

| Information kind | Admitted source kinds, in decreasing precedence |
| --- | --- |
| Business meaning | Owner decision; approved semantic version; process package |
| Process semantics | Owner decision; approved semantic version; process package |
| Imported glossary | Declared catalog authority; owner decision |
| Imported classification | Declared catalog authority; owner decision |
| Identity | Owner decision; approved semantic version; process package |
| Relationship | Owner decision; approved semantic version; process package |
| Metric | Owner decision; approved semantic version; process package |
| Integrity constraint | Owner decision; approved semantic version; process package |

An inadmissible source is invalid. A same-kind same-rank disagreement is unresolved and
requires the business owner; an exact higher-precedence same-kind observation resolves while
retaining the lower observations as evidence. A disagreement between two information kinds
is unresolved only when those kinds contradict one another as defined in section 9.2.1.
Expired observations are rejected. Candidate extraction is proposal evidence, never an
authority source.

```text
AuthorityObservation
  schema_version
  observation_id
  tenant_id
  information_kind
  source_kind
  subject_ref
  assertion
  authority_ref
  observed_digest
  observed_at
  valid_until
```

### 9.2.1 Contradiction groups

Two information kinds contradict one another only when they make competing claims about the
same aspect of a subject. Kinds in different groups describe different aspects and cannot
disagree: an imported glossary definition such as "money returned to a customer" does not
contradict a structural claim such as "an entity with its own lifecycle". Treating any
difference in wording between kinds as a conflict would escalate every candidate in a tenant
that already operates a catalog, and no ontology review bundle could be approved.

These groups partition the information kinds exactly; every kind appears in exactly one.

```text
business_meaning, process_semantics, imported_classification
imported_glossary
identity
relationship
metric
integrity_constraint
```

Within a group the admitted winners must agree; a disagreement is unresolved and requires the
business owner. Across groups no comparison is made, and every observation remains bound to
the resolution as considered evidence regardless of its group.

### 9.2.2 Governing information kind

A resolution binds exactly one observation as the authority. It is taken from the information
kind the candidate is itself a claim about, so an entity never records the declared catalog
authority as the source of its business meaning, and a classification never records the
process package as the source of its imported classification. Where that kind was not
observed the selection falls back to its contradiction group, and failing that to a
deterministic choice among the admitted winners.

Attribution is decided separately from agreement. By the time an observation is selected the
admitted winners already agree, so a wrong selection changes only which authority the
approval records -- which is the property this resolution exists to establish.

| Candidate kind | Governing information kind |
| --- | --- |
| entity | business_meaning |
| event | business_meaning |
| state | process_semantics |
| relationship | relationship |
| identity_rule | identity |
| integrity_constraint | integrity_constraint |
| metric | metric |
| classification | imported_classification |

### 9.3 Publication and proposals

PillarMesh publishes source observations, warehouse assets, data products, ownership, glossary associations, classifications, quality results, freshness, lineage, contracts, incidents, and Superset assets to OpenMetadata.

Catalog changes that may alter execution become typed change requests. Meaning, identity, classification, access, relationship, metric, constraint, and deprecation changes require impact analysis and approval. Cosmetic descriptions may synchronize automatically under policy.

A post-publication catalog edit in any of those governed categories creates a
`schema_semantic_change` request in `investigating`, bound to its before and after
observations and to the affected approved semantic and contract versions. It never rewrites
an approved version or auto-applies the edit.

Ontology review uses that request type and follows this exact path:

```text
submitted → investigating → proposed → awaiting_approval
awaiting_approval → executing → verifying → delivered
awaiting_approval → investigating → no_valid_plan
awaiting_approval → rejected
```

`DecisionKind` remains the request-level vocabulary `approve`, `reject`, and
`request_changes`. Ontology item outcomes use the distinct `ReviewItemDecision` vocabulary
`accept`, `reject`, `revise`, `merge`, and `unresolved`. An `unresolved` item returns the
request to `investigating`. When required meaning remains unresolved, the request must transition
from `investigating` to `no_valid_plan` and cannot progress to `proposed` or execution.

```text
OntologyReviewBundle
  schema_version
  bundle_id
  tenant_id
  revision
  candidate_set_digest
  authority_observation_digests
  items
  required_authority_refs
  status
  created_at
  updated_at

ApprovedSemanticVersion
  schema_version
  semantic_version_id
  tenant_id
  version
  process_package_ref
  candidate_set_digest
  review_bundle_digest
  entities
  events
  states
  relationships
  identity_rules
  constraints
  metrics
  classifications
  authority_bindings
  approval_ids
  created_at
```

### 9.4 Lineage

The catalog displays:

```text
source object
→ source contract
→ acquisition
→ raw generation
→ canonical entity
→ transformation model
→ governed data product
→ metric
→ Superset dataset
→ chart
→ dashboard
→ scheduled report
```

Every edge identifies producer, observation time, validity, confidence, and contract or run evidence. Inferred lineage is visibly labeled and cannot satisfy a proof obligation until validated.

## 10. Integration Contract formation

An Integration Contract may be compiled only when:

- source objects and generations are resolved;
- relevant business process and entity versions are selected;
- identity and relationship rules are approved;
- history and deletion semantics are explicit;
- ownership and authority are attributable;
- classification, residency, retention, and access are resolved;
- provider capabilities are fresh enough for the requested claim; and
- unresolved facts are represented as `Unknown`, not optimistic assumptions.

The contract binds process version, ontology references, source observations, mappings, integrity constraints, destination product, freshness, quality, scheduling, access, evidence, failure policy, and approvals.

Compilation still produces `Ready to Activate`, `Needs Approval`, or `No Valid Plan`.

```text
ManagedIntegrationContract
  schema_version
  contract_id
  tenant_id
  version
  formation_status
  semantic_version_ref
  source_observation_refs
  mappings
  integrity_constraints
  destination_product
  freshness
  quality
  trigger_policy
  access_policy
  evidence_policy
  failure_policy
  approval_ids
```

## 11. Managed integrations

### 11.1 Connector lifecycle

PillarMesh owns:

- installation and versioning;
- authentication and credential rotation;
- least-privilege and denial validation;
- metadata discovery;
- initial historical load;
- incremental cursor or change state;
- schedule and bounded backfill;
- source and destination quotas;
- schema-drift response;
- retry, replay, and ambiguous outcome resolution;
- resynchronization;
- connector upgrades;
- reconciliation; and
- operational evidence.

### 11.2 Initial source strategy

The MVP supports one database source and one SaaS source:

- PostgreSQL snapshot plus scheduled incremental acquisition; and
- Stripe cursor-based acquisition for the approved revenue-to-cash objects.

Logical CDC, MySQL, Salesforce, files, and event sources follow after the MVP unless implementation planning demonstrates that one can replace, rather than add to, the initial scope.

### 11.3 Destination strategy

The MVP must compile the same supported semantic data product to either a tenant-managed PostgreSQL or ClickHouse warehouse. Each provider implements the same observable destination contract but may use different physical mechanisms.

PostgreSQL and ClickHouse must reuse the same canonical Integration Contract, semantic fixtures, expected results, and observable destination conformance suite. This is the MVP proof that the customer can choose a warehouse without binding PillarMesh's semantic product to one engine.

PostgreSQL is the transactional reference. ClickHouse uses native ingestion and deduplication semantics. A ClickHouse limitation produces `No Valid Plan` for an incompatible contract; PillarMesh does not imitate a transactional guarantee it cannot prove.

### 11.4 Plan 4A source acquisition contract

Plan 4A ends at immutable source-aligned canonical JSON Lines segments, a batch manifest, a private
candidate checkpoint, and source-acquisition evidence. It does not write PostgreSQL or ClickHouse,
run a transformation, evaluate a schedule, prove freshness, reconcile a destination, expose a
warehouse object, or deliver a consumer result. A strict acceptance consumer may acknowledge an
exact manifest, but that acknowledgement proves only source-checkpoint admission.

All models below are frozen, reject unknown fields, and use timezone-aware UTC timestamps. Digests
are lowercase SHA-256 values over canonical model bytes.

#### SourceConnectionBinding

```text
SourceConnectionBinding
schema_version
binding_id
tenant_id
provider_kind
connection_handle
account_mode
lifecycle_state
approved_object_refs
capability_profile_digest
source_observation_ref
credential_revision
revision
created_at
updated_at
```

Tenant, provider kind, connection handle, account mode, approved object references, and creation time
are immutable. Credentials, endpoints, account identifiers, and private resource names remain
behind the opaque handle. Credential rotation increments `credential_revision`, clears prior
validation authority, and returns the binding to `validating`.

Private persistence contains only fixed-shape `endpoint-ref:<sha256>` and
`credential-ref:<sha256>` secret-store handles. Raw endpoints, DSNs, API keys, account identifiers,
and provider resource names are invalid at this boundary. The broker resolves the capability and
selects the capability probe from its construction-time provider registry; a validation caller
cannot substitute a probe. Resolver and probe failures expose only a stable boundary operation.

#### SourceBindingValidationEvidence

```text
SourceBindingValidationEvidence
schema_version
evidence_id
tenant_id
binding_id
binding_revision
credential_revision
provider_kind
positive_probe_succeeded
positive_probe_digest
denial_probe_succeeded
denial_probe_digest
source_observation_ref
capability_profile_digest
observed_at
```

A binding reaches `ready` only through evidence for its exact tenant, public revision, credential
revision, and provider kind. Both the intended-access and denied-access probes must succeed. The
ready binding and evidence commit atomically; stale or cross-tenant requests disclose no binding.

#### Source connection binding transitions

```text
draft → validating, retired
validating → ready, failed, retired
ready → validating, suspended, retired
suspended → validating, retired
failed → validating, retired
retired →
```

Only validation evidence may produce `ready`; a generic lifecycle transition cannot. A retired
binding is terminal. Every transition away from `ready`, including credential rotation, first
invalidates the binding's state-owned acquisition authority epoch. If invalidation fails, the
lifecycle transition fails closed. Invalidation compares the exact ready revision, is idempotent
for a retry of that revision, and cannot revoke authority already admitted for a newer revision.

#### AcquisitionSourceObservation

```text
AcquisitionSourceObservation
schema_version
tenant_id
source_binding_ref
provider_kind
object_observations
```

The source observation binds the tenant, source binding, provider kind, and the canonically ordered
observation for every requested logical object. Its canonical digest is the exact
`source_observation_digest` admitted by `AcquisitionIntent`. Each nested observation must use UTC,
must not be dated after intent admission, and must agree with the activated schema, opaque
connection handle, provider kind, and capability allowlist.

#### AcquisitionObjectObservation

```text
AcquisitionObjectObservation
schema_version
logical_object_ref
provider_observation
```

There is exactly one object observation for each requested logical object, ordered by
`logical_object_ref`. The provider observation remains the shared provider-neutral metadata model;
private endpoint, credential, account, and cursor material is never copied into this envelope.

#### AcquisitionIntent

```text
AcquisitionIntent
schema_version
intent_key
tenant_id
run_intent_ref
contract_ref
contract_digest
source_binding_ref
source_observation_digest
acquisition_mode
object_refs
prior_checkpoint_revision
prior_checkpoint_digest
record_ceiling
encoded_byte_ceiling
admitted_at
```

The intent key binds a domain tag, tenant, run-intent identity, contract digest, source binding,
mode, canonically ordered object references, and prior checkpoint revision. Admission time is not
identity. Revision zero has no prior checkpoint digest; every later revision requires one. The
intent can narrow an activated contract but cannot widen objects, fields, modes, or limits.

#### AcquisitionField

```text
AcquisitionField
name
value_type
nullable
```

#### AcquisitionObjectSchema

```text
AcquisitionObjectSchema
logical_object_ref
schema_digest
fields
record_key_fields
source_updated_at_field
operation_semantics
```

#### AcquisitionFieldValue

```text
AcquisitionFieldValue
name
value
```

#### AcquisitionRecord

```text
AcquisitionRecord
schema_version
logical_object_ref
record_key
source_created_at
source_updated_at
operation
fields
```

Fields are an ordered tuple of exact scalar name/value entries. The admitted scalar vocabulary is
null, boolean, integer, decimal, string, and timestamp. Arrays, maps, binary values, floating-point
values, unknown fields, and provider expansions are refused. Record keys and source fields are
private and never appear in public evidence.

#### AcquisitionBoundary

```text
AcquisitionBoundary
schema_version
logical_object_ref
acquisition_mode
schema_digest
lower_cursor_digest
upper_cursor_digest
query_shape_digest
snapshot_identity_digest
key_range_digest
private_boundary_ref
record_count
opened_at
closed_at
```

This model supersedes `SourceBoundary` for acquisition while leaving the M0 signed-graph call sites
on `SourceBoundary`. Raw cursor values, snapshot identity, key bounds, applied lag details, and
provider pagination state remain behind `private_boundary_ref`; only their digests cross the
provider boundary.

#### AcquisitionSegmentManifest

```text
AcquisitionSegmentManifest
schema_version
segment_name
logical_object_ref
record_schema_digest
boundary_digest
encoding
content_digest
record_set_digest
record_count
encoded_bytes
```

#### AcquisitionBatchManifest

```text
AcquisitionBatchManifest
schema_version
batch_id
intent_key
tenant_id
contract_ref
contract_digest
source_binding_ref
source_observation_digest
acquisition_mode
prior_checkpoint_revision
prior_checkpoint_digest
candidate_checkpoint_digest
segment_manifests
total_record_count
total_encoded_bytes
prepared_at
```

Segment names derive from a fixed ordinal and logical-object digest. Segment order is the canonical
logical-object order. `content_digest` covers exact UTF-8 JSON Lines bytes including the final
newline; `record_set_digest` covers the ordered record models. Batch totals equal the exact segment
sums, and batch identity excludes preparation time.

#### AcquisitionPreparedReceipt

```text
AcquisitionPreparedReceipt
schema_version
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

#### AcquisitionAcknowledgement

```text
AcquisitionAcknowledgement
schema_version
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

#### AcquisitionCheckpointReceipt

```text
AcquisitionCheckpointReceipt
schema_version
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

Preparation publishes durable artifacts and prepared state but moves no checkpoint. Exact
acknowledgement, prepared-state transition, checkpoint revision increment, checkpoint receipt, and
acknowledgement evidence commit in one state transaction. A duplicate acknowledgement returns the
same receipt only after canonical equality. Stale, cross-tenant, wrong-consumer, wrong-contract, or
digest-mismatched acknowledgement has no durable effect. Empty batches may advance exactly one
revision after acknowledgement so every run intent has an unambiguous predecessor.

The durable prepared state also binds the source-binding and credential revisions, binding and
contract authority epochs, provider kind, cursor version, and the activated contract's exact
acknowledgement consumer. These values are state-owned authority, not caller assertions. Binding or
contract invalidation advances its epoch before retirement or cancellation becomes externally
effective, so a pending acknowledgement holding the earlier epoch loses atomically. Exact replay of
an acknowledgement that already committed may still return its existing receipt after later
retirement; it creates no new checkpoint effect and must revalidate the complete committed fact set.
The contract service owns the retirement call through an injected state-authority boundary and
fails retirement closed when that boundary is absent or unavailable. It first resolves a
tenant-qualified activated lifecycle record at an exact expected revision; unknown, cross-tenant,
inactive, or stale requests never reach state authority. Activation registers that lifecycle and
its state authority, and a retired contract digest cannot reactivate. Runtime admission may only
observe an already active contract authority; it cannot create one implicitly.

#### AcquisitionNoValidPlan

```text
AcquisitionNoValidPlan
schema_version
reason_codes
failed_constraints
```

#### ResynchronizationRequired

```text
ResynchronizationRequired
schema_version
reason_code
source_binding_ref
affected_object_refs
last_proven_checkpoint_digest
required_scope
created_at
```

Both governed outcomes are tenant-private. Public evidence may expose allowlisted reason codes but
not failed constraints, last-proven checkpoint digests, required scope, raw cursors, or provider
identity. `No Valid Plan` means the activated semantics cannot be proven. Resynchronization means a
previously admitted source interval can no longer be proven. Authorization, throttling, transient
availability, drift, and integrity failures remain distinct and move no checkpoint.

### 11.5 Refusal ceilings and replay

`record_ceiling` and `encoded_byte_ceiling` are refusal limits, not pagination limits. PostgreSQL
uses its repeatable-read count to refuse before row streaming. Stripe refuses while consuming the
unknown page total. Either path aborts provider iteration and leaves no published segment,
manifest, receipt, or checkpoint. Truncation followed by checkpoint advancement is forbidden.

Only one canonical prepared batch can exist for a tenant, contract, source binding, and prior
checkpoint revision. Same-intent replay returns the exact prepared receipt or safely republishes
digest-identical orphan artifacts. Different candidate cursor, segment, count, or manifest is an
integrity failure.

### 11.6 PostgreSQL acquisition semantics

An admitted PostgreSQL source is an approved base table with an exact projection, one non-null
primary or unique B-tree key, a timezone-aware `updated_at`, a contract assertion that the timestamp
never decreases, and a dedicated principal with intended SELECT plus denied mutation,
administration, trigger, reference, and unrelated-schema capabilities.

Initial acquisition opens `REPEATABLE READ READ ONLY`, validates authority and schema, captures the
snapshot identity, key bounds, and count, then streams primary-key order. Incremental acquisition
uses native row-value semantics:

```text
lower_cursor < (updated_at, primary_key) <= lagged_upper_cursor
ORDER BY updated_at, primary_key
```

The upper cursor is the greatest visible pair whose timestamp is at or below
`snapshot_time - max_write_transaction_duration`. The activated contract must declare a positive
maximum duration; zero or absence is `No Valid Plan`. When permitted, the oldest concurrent
transaction start may tighten the private bound. This bounded delay prevents a transaction that
assigned an earlier timestamp but committed after the snapshot from being skipped permanently.

Backward timestamp movement violates the contract and is detected only through reconciliation.
Physical deletes are not observable in Plan 4A. A contract requiring physical-delete capture is
`No Valid Plan`; PillarMesh does not claim delete correctness from an upsert-only cursor.

Order, Subscription, and Account Segment facts come from approved PostgreSQL tables. Subscription
includes lifecycle status so a later plan can compute churn. A tenant whose subscription facts live
only in Stripe is outside Plan 4A.

### 11.7 Stripe acquisition semantics

Stripe admits exactly Customer, Invoice, Charge, and Refund. It excludes names, email, postal and
phone data, card and bank details, descriptions, receipts, unrestricted metadata, nested expansions,
PaymentIntent, Subscription, Dispute, BalanceTransaction, and secrets. One metadata key may be
admitted only by a separately approved cross-source identity rule. `customer.deleted` may normalize
to a minimal private tombstone; no other hard-delete guarantee is claimed.

Requests pin API version `2026-02-25.clover`. Initial snapshots capture one upper creation time,
paginate official v1 list endpoints with private object cursors, constrain every approved resource
by `created <= upper`, normalize only tested versions and allowlisted fields, and sort by
`(created, object_id)` independently of reverse-chronological page order.

The private event cursor is `(last_event_created, last_event_id, api_version_set_digest)`.
Incremental acquisition requires a positive contract-declared overlap, reads inclusively from
`last_event_created - event_overlap_window`, and captures an upper event time. The exact prior event
must appear in the overlap before continuity is accepted. A missing prior event, an event older than
the overlap start, an unsupported creation-time API version that cannot be reconstructed, or a
cursor outside Stripe's documented 30-day Events window creates `ResynchronizationRequired`.

Events deduplicate by ID before already-committed events are discarded. Different content under the
same event ID is integrity failure. Canonical order is `(event_created, event_id, object_kind,
object_id)`. Reconciliation repeats complete bounded snapshots for all four resources and proves
only source completeness and internal digest consistency, never warehouse equality.

### 11.8 Source-provider conformance and evidence boundary

Every source provider proves fresh observation, intended and denied access, exact object and field
allowlists, stable bounds, deterministic ordering, complete pagination, refusal ceilings, empty
acquisition, exact replay, abandoned-iterator cleanup, drift before and during acquisition,
malformed-payload rejection, precise error classification, cross-tenant denial, private cursor
containment, prepared-versus-acknowledged behavior, reconciliation or explicit unsupported
semantics, and sanitized public evidence.

Public acquisition evidence contains tenant, run-intent reference, contract reference, source
binding reference, mode, logical object references, opaque prepared/checkpoint receipt references,
prior/resulting revisions, allowlisted reason codes, outcome, and creation time. It contains no
batch or manifest digest, row or byte count, segment content, record key, source field, cursor or
cursor digest, provider account or request ID, endpoint, credential, SQL, response body, or private
artifact path. Neither preparation nor acknowledgement claims destination persistence,
reconciliation, visibility, transformation, freshness, or consumer delivery.

## 12. Managed transformations and warehouse integrity

### 12.1 Transformation authority

PillarMesh generates, tests, versions, deploys, and operates transformation models. Models are deterministic compiler artifacts derived from approved contracts. AI may propose mappings or SQL, but generated output must pass typed validation, deterministic tests, policy checks, and approval where meaning changes.

The MVP exports a dbt-compatible project representation and uses versioned dbt artifacts where practical. The dbt adapter remains an execution and observation capability, not the authority for business meaning.

### 12.2 Integrity layers

The warehouse has four logical layers:

1. **Raw evidence:** source-aligned immutable generations, source identities, boundaries, and minimal normalization.
2. **Conformed core:** canonical entities, governed identities, relationships, history, reference data, and quarantine.
3. **Data products:** process-specific models, approved metrics, reconciliation, and contract-bound quality tests.
4. **Consumption:** certified Superset datasets, dashboards, reports, governed SQL, APIs, and MCP resources.

### 12.3 Continuous constraints

PillarMesh evaluates uniqueness, referential integrity, cardinality, domain constraints, valid process transitions, temporal ordering, identity resolution, source-to-core reconciliation, core-to-product reconciliation, freshness, completeness, classification, and metric consistency.

Raw queryability does not authorize a dashboard or report to bypass the conformed and product layers.

## 13. Request and ticket workspace

### 13.1 Intake

One typed request boundary accepts:

- authenticated business-user requests;
- data engineering requests;
- platform-generated incidents and proposals; and
- future Slack, email, and API adapters.

The MVP includes native UI/API intake and platform-generated tickets. Slack and email intake are deferred, but their future adapters must create the same typed object rather than bypassing authorization or semantics.

Request-management owns the native question/access intake checksum. Its
`RequestIntakeContent` consists of `title` (string, or null for untitled internal intake)
and the typed `payload` (`StakeholderQuestion` or `DataAccessRequest`). The lowercase
SHA-256 digest uses the contract-model canonical serializer: UTF-8 JSON without extra
whitespace, lexicographically sorted keys, preserved text and array order, and UTC
expiry timestamps with six fractional digits. Generated request IDs, tenant/actor
context, lifecycle state, revisions, and submission timestamps are outside this content
checksum. Trusted context still determines identity and visibility.

A declared checksum must match before an intake allocates an identity or persists a
request. Native console commands must declare it; existing internal callers without a
declared checksum remain supported. The checksum is neither approval nor retry authority,
and identical content submitted separately still produces separate request identities.
No durable-record migration or rewrite is required.

### 13.2 Request types

- stakeholder data question;
- new integration;
- new or changed data product;
- dashboard or report;
- access request;
- backfill or resynchronization;
- incident;
- schema or semantic change;
- capacity or cost proposal; and
- retirement.

### 13.3 Lifecycle

```text
submitted
→ clarifying
→ investigating
→ proposed
→ awaiting_approval
→ executing
→ verifying
→ delivered
→ monitoring
```

Terminal alternatives include rejected, no-valid-plan, cancelled, failed, and retired. A request may create dependent requests, but dependency edges do not form a general workflow language.

### 13.3.1 MVP transition table

For the MVP, request lifecycle transitions are fixed as follows. Persisted values use snake_case; `no_valid_plan` is never hyphenated.

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

Any non-terminal state may also move to `cancelled`. `rejected`, `no_valid_plan`, `cancelled`, `failed`, and `retired` are terminal and have no outgoing transitions.

### 13.4 Automated work

PillarMesh may automatically validate authorization, discover metadata, profile bounded samples, search the catalog, detect duplicates and dependencies, draft process and ontology changes, compile candidate contracts, estimate cost and freshness, run isolated tests, and prepare previews.

It may not activate new semantic meaning, widen access, accept policy conflicts, approve material cost, migrate bindings, or perform irreversible deletion without the required human authority.

### 13.5 Conversation

PillarMesh asks business-meaning questions directly to the requester while allowing the data engineer to observe, intervene, or take over. Technical, policy, ownership, and access questions route to the named responsible role. The engineer must not become a manual message relay.

Request-management records a conversation entry's author role as historical provenance
in the same transaction as its actor, body, and advanced request revision. Authenticated
adapters supply their trusted active role; a browser claim cannot override it. The
closed vocabulary is requester, data architect, data owner, policy approver, budget
approver, and PillarMesh for system-authored entries. Recording a role neither grants
permission nor replaces the required approval or authorization checks.

Older entries and internal callers without role provenance remain readable with an
unrecorded role. Console responses expose this as null and display “Role not recorded,”
without inferring from actor identity or today's role membership. An absent role is
omitted from durable serialization to preserve existing bytes and digests. Reads never
backfill historical roles, and this change does not widen the roles permitted to post.

### 13.6 Governed answers and access fulfillment

The architect review surface may project the owning proposal's answer, access scope,
or disclosure denial after the owning authorization check. Artifact references retain
all three identity fields: artifact ID, version, and digest. Console row keys derived
from these references are presentation-only and must not be used as catalog or dashboard
lookup identities. Only a published catalog mapping may provide those destinations.
Recorded approval status must match the reviewed proposal and current request revision,
or the exact approval IDs and source revision recorded by its admission or denial
receipt. This display does not replace the owning service's current authority check.
Requester projections continue to withhold unapproved candidate content.

Architect preparation commands delegate clarification, answer proposal generation, and
submission to request-management. Each requires the trusted architect role and the
current request revision. The owning service authors the candidate and its constraints;
the browser records only the clarified request and scope. Preparation grants no execution
or disclosure authority. Approval controls appear only after submission for approval,
and admission retains its exact reviewed-digest checks. A dependency or No Valid Plan
is projected explicitly without substituting an answer or silently retrying preparation.
Only preparation capabilities actually composed at the server are offered in the UI.


A stakeholder data question is operational work, not an unrestricted natural-language query against raw tables. PillarMesh resolves the requester, purpose, authorized scope, applicable process and metric versions, catalog assets, freshness, and quality state before preparing an answer. An answer must identify the governed datasets and metric definitions used, their as-of time, material quality limitations, and lineage or evidence references. If the question cannot be answered from approved assets, PillarMesh creates a dependent data-product or semantic-change request instead of inventing a result.

An access request binds requester, purpose, data product, fields, classification, access mode, duration, and approving authority. PillarMesh proposes the least-privilege grant, previews its effective scope, obtains required approval, applies it through a managed role, validates intended and denied access, records evidence, and expires or revokes it according to policy. Neither an inbox conversation nor an AI recommendation grants access by itself.

### 13.7 Plan 3B governed fulfillment contract

Plan 3B is a control-plane preparation and authorization boundary. It binds a stakeholder request to
immutable approved semantic, catalog-publication, contract, process, policy, entitlement, freshness,
quality, lineage, and data observations. It produces one governed proposal, dependency, denial, or
`No Valid Plan`; it does not execute a query, apply a grant, or deliver a result.

All artifacts below are frozen, reject unknown fields, use timezone-aware UTC timestamps, and carry
tenant-qualified identities. Durable IDs use repository sequences. Digests are lowercase SHA-256
over canonical serialization.

#### FulfillmentGroundingSnapshot

```text
FulfillmentGroundingSnapshot
  schema_version
  snapshot_id
  tenant_id
  catalog_publication_id
  catalog_publication_intent_digest
  catalog_round_trip_observation_digest
  semantic_version_ref
  integration_contract_ref
  process_package_ref
  governed_dataset_refs
  metric_refs
  classification_refs
  lineage_refs
  freshness_observation_ref
  quality_observation_refs
  authorization_policy_ref
  data_observation_refs
  as_of
  created_at
```

Every reference must be reachable from the exact persisted publication intent and receipt. The
snapshot is immutable; provider IDs and live OpenMetadata payloads remain private to the adapter.

#### FulfillmentPolicySnapshot

```text
FulfillmentPolicySnapshot
  schema_version
  snapshot_id
  tenant_id
  requester_id
  requester_principal_ref
  purpose_digest
  approved_policy_refs
  entitlement_observation_refs
  classification_rule_refs
  permitted_data_product_refs
  permitted_access_modes
  maximum_expiry
  policy_authority_classifications
  observed_at
  valid_until
```

The snapshot comes only from approved tenant policy and current entitlement observations. Missing,
expired, conflicting, or cross-tenant inputs fail closed; an empty allowlist is not a caller-
overridable default.

#### ClarifiedOutcomeStatement

```text
ClarifiedOutcomeStatement
  schema_version
  statement_id
  tenant_id
  request_id
  request_revision
  restated_request
  purpose_digest
  in_scope_summary
  out_of_scope_summary
  created_at
```

The requester accepts this exact statement digest without seeing the candidate. A candidate edit
does not invalidate that acceptance. A change to the restated request, purpose, or scope creates a
new statement and requires fresh acceptance. No proposal may be created from `clarifying`.

#### StakeholderAnswerDraft

```text
StakeholderAnswerDraft
  subject_kind
  answer_text
  governed_dataset_refs
  metric_refs
  as_of
  freshness_disposition
  material_quality_limitations
  lineage_refs
  disclosure_classifications
```

All citations must appear in the grounding snapshot. `current` and `stale` are derived from a
freshness observation against the declared objective and require a data observation. A null
freshness observation permits only `unknown`; `unknown` cannot support a factual answer. A semantic-
definition answer may use `not_applicable`.

#### AccessScopePreview

```text
AccessScopePreview
  subject_kind
  requester_principal_ref
  data_product_ref
  access_mode
  requested_fields
  effective_object_refs
  effective_fields
  excluded_scopes
  classifications
  expires_at
```

The preview may narrow but never widen the requested fields, objects, mode, product, or expiry. It
contains no warehouse role, credential, endpoint, provider identifier, or grant statement.

#### DisclosureDenial

```text
DisclosureDenial
  subject_kind
  reason_code
  requester_safe_explanation
  denied_scope_digest
```

The denial remains private until approved. Only its requester-safe explanation becomes visible.

#### ApprovalRequirement

```text
ApprovalRequirement
  authority_ref
  reason_code
  subject_digest
```

The requester's requirement binds the clarified-outcome statement digest. Every other requirement
binds the discriminated candidate subject digest. Requirements are sorted and unique by authority.

#### FulfillmentProposal

```text
FulfillmentProposal
  schema_version
  proposal_id
  tenant_id
  request_id
  request_revision
  revision
  prior_proposal_digest
  clarified_outcome_digest
  grounding_snapshot_digest
  policy_snapshot_digest
  subject
  required_approvals
  created_at
```

The first revision has no prior digest. Every material edit creates a new immutable revision whose
`prior_proposal_digest` names the exact preceding proposal; old approvals remain history but cannot
authorize it.

#### FulfillmentApprovalBinding

```text
FulfillmentApprovalBinding
  approval_id
  tenant_id
  request_id
  request_revision
  proposal_id
  proposal_revision
  proposal_digest
  subject_digest
  actor_id
  authority_ref
  decision
  created_at
```

One binding satisfies one role-scoped requirement. An actor holding two roles records two
attributable decisions, and role membership is checked both at decision and admission time.

#### FulfillmentAdmissionReceipt

```text
FulfillmentAdmissionReceipt
  admission_id
  tenant_id
  request_id
  source_request_revision
  resulting_request_revision
  proposal_id
  proposal_revision
  proposal_digest
  grounding_snapshot_digest
  policy_snapshot_digest
  approval_ids
  admitted_at
  execution_status
```

`execution_status` is only `ready_for_execution`. The receipt proves authorization, not a data-plane
effect.

#### DenialDispositionReceipt

```text
DenialDispositionReceipt
  disposition_id
  tenant_id
  request_id
  source_request_revision
  resulting_request_revision
  proposal_id
  proposal_revision
  proposal_digest
  policy_snapshot_digest
  approval_ids
  requester_safe_explanation
  recorded_at
```

Approving a denial creates this disposition and transitions to `rejected`; it never creates an
execution admission.

#### RequestDependency

```text
RequestDependency
  dependency_id
  tenant_id
  parent_request_id
  parent_request_revision
  child_request_id
  child_request_revision
  kind
  reason_code
  blocking
  created_at
```

Only `semantic_change` and `data_product_change` are permitted. The parent remains
`investigating`; completion does not run or transition it automatically.

#### DataProductChangeRequest

```text
DataProductChangeRequest
  request_type
  purpose
  requested_outcome
  missing_capability_refs
  source_request_id
  source_request_revision
```

This payload represents missing governed dataset, metric, observation, or product capability. The
existing `SchemaSemanticChangeRequest` remains the semantic dependency payload.

#### RequestNoValidPlan

```text
RequestNoValidPlan
  record_id
  tenant_id
  request_id
  source_request_revision
  resulting_request_revision
  reason_codes
  constraint_refs
  smallest_changes
  requester_safe_explanation
  grounding_snapshot_digest
  policy_snapshot_digest
  created_at
```

Conflicting, stale mandatory, unverifiable, malformed, cross-tenant, or unadmitted authority is a
durable fail-closed outcome with sanitized attributable constraints. `requester_safe_explanation`
is optional, bounded to 1,000 characters, and is the only refusal detail this artifact permits a
requester-facing projection to disclose.

#### FulfillmentEvidenceReceipt

```text
FulfillmentEvidenceReceipt
  schema_version
  evidence_id
  tenant_id
  request_id
  request_revision
  outcome
  proposal_id
  proposal_revision
  dependency_id
  authority_refs
  approval_ids
  reason_codes
  resulting_state
  created_at
```

This public allowlist contains no answer text, purpose, field list, object scope, classification
detail, private digest, policy observation, provider ID, credential, endpoint, or grant statement.

#### Plan 3B outcome matrix

| Condition | Governed outcome | Parent state |
| --- | --- | --- |
| Unsettled restatement | Clarification pending | `clarifying` |
| Approved assets and scope | Answer or access proposal | `proposed` |
| Missing semantic meaning | Semantic-change dependency | `investigating` |
| Missing governed data capability | Data-product-change dependency | `investigating` |
| Insufficient disclosure authorization | Denial proposal | `proposed` |
| Conflicting or unverifiable authority | `No Valid Plan` | `no_valid_plan` |
| Material candidate edit | Superseding proposal revision | `investigating` |
| Complete answer or access approvals | Execution-ready admission | `executing` |
| Complete denial approvals | Denial disposition | `rejected` |
| Exact requirement rejected | Rejection | `rejected` |
| Approver requests changes | New investigation | `investigating` |
| Expired equivalent policy | Admission after re-resolution | `executing` |
| Expired changed policy | Superseding proposal revision | `investigating` |
| Cancellation with open proposal | Cancellation evidence only | `cancelled` |

The compiler returns exactly one closed outcome. Missing capability is not a denial, insufficient
authorization is not a provider failure, and unresolved legality is never converted to an empty or
weakened proposal.

#### Plan 3B visibility matrix

| Actor | Clarifications | Clarified outcome | Candidate subject | Approval metadata | Admission |
| --- | --- | --- | --- | --- | --- |
| Requester | Own request | Yes | Never before verified delivery | Own labelled Plan 2 and Plan 3B decisions | Status only |
| Data engineering architect | Yes | Yes | Yes | Yes | Yes |
| Required approver | Relevant conversation | Yes | Exact subject requiring the role | Relevant role | Status |
| Unrelated tenant actor | No | No | No | No | No |

Visibility is implemented through role-specific allowlist models, never private-model serialization
followed by redaction. An admission does not make answer text requester-visible. An approved denial
reveals only its requester-safe explanation.

#### Plan 3B approval matrix

| Subject | Always required | Conditional authority |
| --- | --- | --- |
| Stakeholder answer | Requester clarified-outcome acceptance; `role:data_engineering_architect` | `role:policy_authority` for classified disclosure |
| Access scope | Requester clarified-outcome acceptance; exact data-product owner | `role:policy_authority` for classified, finance, residency, retention, masking, or widened-access implications |
| Disclosure denial | Requester clarified-outcome acceptance; `role:data_engineering_architect` | Applicable `role:policy_authority` |

An answer deliberately does not require the data-product owner because it changes no approved
product meaning. Access always requires the exact product owner because it creates standing
capability against that product. Material recurring-cost approval is deferred because Plan 3B
neither prices nor executes an effect.

#### Plan 3B authority record boundary

| Record | Lifecycle | Cross-lifecycle effect |
| --- | --- | --- |
| `DecisionBinding` | Plan 2 semantic review | Never satisfies a Plan 3B requirement |
| `FulfillmentApprovalBinding` | Plan 3B fulfillment proposal | Never satisfies a Plan 2 semantic review |

`RequestManagementService.record_decision` refuses a current request revision carrying a Plan 3B
proposal. Plan 3B admission reads only fulfillment approval bindings.

#### Plan 3B admission predicate

```text
request_state awaiting_approval
proposal_revision latest
tenant_match request | proposal | grounding | policy | every_binding
snapshot_digests exact
policy_valid_until future_or_reresolve
approval_match authority_ref | subject_digest | proposal_digest | proposal_revision | approve
negative_decisions none
current_actor_role required
cancelled false
```

Every predicate term is required as one admission decision. Matching authority alone is illegal.
If the policy snapshot expires, PillarMesh re-resolves it. A canonically equivalent authorization
apart from the observation window may admit against the fresh snapshot. Any changed disposition,
effective scope, or required authority supersedes the proposal and recollects approvals. Resolution
failure records `No Valid Plan`.

Cancellation is terminal and defeats an open proposal. The abandoned proposal and bindings remain
history, but no admission or denial disposition is recorded; public evidence records `cancelled`.

#### Plan 3B execution non-claims

```text
query_execution
grant_application
requester_delivery
verification
expiry
revocation
```

These remain later lifecycle stages and require their own effect and verification evidence.

## 14. Risk-tiered approval

Every approval binds the exact request, process version, ontology changes, contract, policy observations, estimate, plan, and change digest. A material edit invalidates affected approvals.

| Decision | Required authority |
| --- | --- |
| Clarified outcome and acceptance criteria | Requester |
| Keys, joins, lifecycle, history, deletion, metrics | Data owner |
| Classification, residency, retention, masking, widened access | Policy authority |
| New contract activation and semantic migration | Data engineering architect |
| Material recurring cost | Budget authority |
| Retirement, irreversible deletion, reduced protection | Explicit destructive-action authority |

Routine retries, replay, safe maintenance, bounded resynchronization, pre-authorized scaling, and proven-equivalent physical optimization may execute automatically under tenant policy.

## 15. Scheduling and run intents

PillarMesh compiles freshness and execution requirements into a versioned `TriggerPolicy`.

Initial modes are periodic, calendar, manual, and bounded backfill. Event-driven triggers follow later.

A scheduled window has deterministic identity:

```text
digest(contract_version, trigger_policy_version, scheduled_window)
```

The trigger service creates an idempotent run intent only for an already activated contract. Before provider access, the control plane revalidates contract state, graph compatibility, warehouse readiness, provider evidence, and overlap policy.

Overlap policies are `forbid`, `queue_one`, and compiler-proven `allow_partition_safe`. Misfire policies are `run_immediately`, `coalesce_to_latest`, `run_each_missed_window`, and `require_approval`.

A schedule states when PillarMesh tries. Freshness states what PillarMesh must achieve. A successful invocation does not prove freshness.

## 16. BI and reporting

### 16.1 Superset boundary

Apache Superset is the initial BI capability provider. PillarMesh owns semantic metrics, dimensions, data-product versions, ownership, freshness, access policy, dashboard contracts, report schedules, and delivery evidence. Superset owns interactive query, visualization rendering, dashboard layout, filters, drill-down, and export rendering.

Superset is never authoritative for business meaning, legality, access policy, scheduling, or evidence.

### 16.2 Deployment

The initial isolation model provides a dedicated Superset deployment, metadata database, cache, report worker, and tenant-specific keys for each customer. Shared BI infrastructure may be evaluated later only with explicit isolation evidence.

### 16.3 Dashboard contract

```text
DashboardContract
  dashboard_id
  version
  owner
  audience
  data_product_versions
  metric_versions
  dimensions
  filters
  visual_intents
  drill_paths
  freshness_requirement
  access_policy
  report_delivery_policy
  acceptance_tests
  lifecycle_state
```

Superset dataset, chart, dashboard, role, and report identifiers are disposable compiled artifacts.

### 16.4 Editing levels

- **Certified:** approved and compiled; immutable in Superset.
- **Draft:** editable by authorized analysts in a bounded workspace.
- **Personal exploration:** user-owned and unable to overwrite certified assets.

Promotion from draft to certified creates a semantic diff and approval request.

### 16.5 Scheduled reports

PillarMesh owns the canonical report schedule, verifies data and authorization, asks Superset to render, validates output, delivers through an approved channel, and records delivery evidence. The MVP supports in-product delivery and one testable email channel. PDF and CSV artifacts are retained according to policy.

A report is blocked or visibly marked according to policy when freshness, quality, access, or compilation state is invalid.

## 17. Operations and evidence

The architect home shows decision count, data-product health, current data age, contract freshness, active and queued runs, incidents, cost proposals, platform health, and restore-rehearsal status.

PillarMesh evidence links:

```text
request
→ process and ontology decisions
→ Integration Contract
→ IIR and legality decision
→ Physical Plan and signed graph
→ source boundary
→ transformation artifacts and tests
→ destination receipt and visibility
→ reconciliation
→ catalog publication
→ dashboard/report publication
→ consumer delivery
```

Infrastructure health cannot declare semantic success. Existing rows, prior reports, successful connector authentication, or a rendered dashboard are not evidence that the current source-to-consumer contract works.

## 18. Security, privacy, and access

- Tenant data planes are dedicated in the initial model.
- Secrets are resolved through private handles and never enter contracts, graphs, evidence, tickets, prompts, or logs.
- Source and provider metadata are untrusted input.
- Raw data is excluded from the control plane except through explicitly approved bounded diagnostic paths.
- Catalog and BI embeds use PillarMesh identity and authorization; public links are disabled by default.
- Certified assets inherit contract access policy.
- Destructive actions resolve exact targets and require explicit authority.
- Evidence artifacts are privacy-designed and exported through a fail-closed allowlist.

### 18.1 Warehouse principal classes

```text
administration
ingestion_runtime
transformation_runtime
backup_restore
customer_sql
catalog
bi
```

| Principal class | Enforceable grant and command boundary |
| --- | --- |
| Administration | Provisions namespaces, roles, and engine configuration; it is never used by ingestion, transformation, catalog, BI, or customer queries. |
| Ingestion runtime | Writes only source-aligned `raw` generations and its bounded ledger records; it cannot administer roles, write `conformed`, `product`, `consumption`, or read customer and BI paths. |
| Transformation runtime | Reads `raw`; writes `conformed`, `product`, and `quarantine`; publishes approved `consumption` objects; it cannot administer, back up, or mutate the control ledger outside its allowlist. |
| Backup and restore | Reads only what engine-native backup requires and drives approved backup and restore through the private backup command boundary; it cannot write, author schemas, administer roles, access customers, or resolve its credential outside that boundary. Restore execution uses a throwaway bootstrap administrator confined to the isolated target; the `backup_restore` principal never receives write or administration authority on the primary. |
| Customer SQL | Reads only explicitly granted `consumption` objects through a non-shared identity; it cannot access `raw`, `conformed`, `product`, quarantine, ledger, role, or backup surfaces. |
| Catalog | Inspects approved schemas, object metadata, and lineage-supporting metadata; it cannot read rows, write the warehouse, administer roles, or access backups. |
| BI | Reads approved `consumption` objects required by certified datasets; it cannot access `raw`, `conformed`, `product`, quarantine, ledger, role, or backup surfaces. |

Non-administration principals do not inherit another capability class. Administration remains
the private control path for provisioning and role management and is never substituted for an
ingestion, transformation, backup, catalog, BI, or customer identity. Each class carries positive
and denial probes in the destination conformance suite, and a provisioned warehouse is not `ready`
until both pass for every class.

## 19. Backup, recovery, and exit

PillarMesh owns warehouse, OpenMetadata, Superset metadata, state, and evidence backups. Recovery proof requires restoration into an isolated target and verification of representative data, contracts, lineage, dashboard compilation, and query behavior. Backup upload alone is insufficient.

Customers own their data. Exit supports an approved final consistent snapshot, Parquet and CSV data export, SQL schema where meaningful, canonical contract and evidence export, lineage export, credential revocation, retention disposition, and verified deletion after the contractual period.

## 20. MVP scope

### 20.1 MVP thesis

The MVP is defined from the data engineering architect's point of view. It proves that one architect can establish a new governed data environment and then operate the team's daily stakeholder workload from one inbox without manually administering the warehouse, catalog, integration runtime, or BI service.

The architect's MVP job is:

> Set up a PillarMesh-managed warehouse and catalog; upload an approved business-process description; turn that process and observed source metadata into governed Integration Contracts and data products; then answer stakeholder data questions, fulfill data-access requests, and review integration, quality, and maintenance decisions through one inbox.

Revenue-to-cash is the acceptance fixture used to prove this journey. It is not the product definition or a hard-coded workflow.

### 20.2 Scope rule

The MVP deliberately contains:

- one tenant deployment model: dedicated PillarMesh cloud;
- two customer-selectable warehouse engines behind one destination contract: PostgreSQL and ClickHouse;
- one fixed capacity profile in one deployment region;
- one uploaded business-process package, exercised with a revenue-to-cash fixture;
- two source classes: PostgreSQL database and Stripe SaaS;
- one managed catalog: OpenMetadata;
- one managed BI service: Apache Superset;
- one governed data product, one certified dashboard, and one daily report;
- one scheduled cadence: daily, plus authorized `Run now` and bounded resynchronization; and
- one native architect inbox for stakeholder questions, access requests, changes, incidents, and approvals.

These are limits on the first releasable product, not changes to the platform's provider-neutral contracts. Every included component must be operated, observed, recovered, and evidenced by PillarMesh.

### 20.3 Included user journey

The architect completes four phases.

#### Phase A: establish the managed environment

1. Create an organization, choose PostgreSQL or ClickHouse, select the supported region and fixed capacity profile, assign approval roles, and approve the immutable warehouse binding.
2. Let PillarMesh provision, secure, validate, monitor, back up, and register the managed warehouse.
3. Let PillarMesh provision OpenMetadata and Superset and bind their identities and authority boundaries.
4. Connect approved PostgreSQL and Stripe sources using least-privilege credentials and positive and denial probes.

#### Phase B: establish business and integration meaning

1. Upload a versioned business-process package containing process narrative, actors, events, states, outcomes, source references, ownership, and known rules.
2. Review PillarMesh's extracted process model, ontology candidates, identities, relationships, lifecycle, constraints, metrics, classifications, and unresolved questions.
3. Answer or route unresolved meaning and policy questions to the named owner through the inbox.
4. Approve the exact process version, ontology authority, Integration Contract, generated warehouse models, quality rules, schedule, access policy, dashboard, report, and cost estimate.

#### Phase C: activate and verify the governed data product

1. Activate the contract and observe historical acquisition, transformation, quarantine, reconciliation, catalog publication, dashboard compilation, and daily incremental execution.
2. Verify governed entities, definitions, owners, lineage, constraints, quality, freshness, and access policy in OpenMetadata.
3. Verify the certified Superset dashboard and daily report against approved metric versions and representative warehouse facts.

#### Phase D: operate the architect inbox

1. Receive a stakeholder data question; inspect the proposed answer, supporting datasets, metric versions, as-of time, quality limitations, lineage, and requester authorization; then approve or correct the response.
2. Receive a time-bounded data-access request; inspect the proposed least-privilege grant and effective-scope preview; obtain the required authority; apply it; and verify both intended and denied access.
3. Receive a request that cannot be answered from current governed assets and approve a dependent semantic, integration, data-product, dashboard, or report proposal rather than allowing an invented answer.
4. Review platform-created drift, quality, freshness, and maintenance items, approving only changes that alter meaning, policy, access, material cost, or risk.
5. Inspect the complete evidence and decision history and witness an isolated restore without opening the administrative consoles of the managed components.

The UI required for this journey is limited to environment setup, process upload and semantic review, the architect inbox, catalog embed, integration and run status, certified dashboard and report, access preview, incidents, and evidence. It is not a general ticketing, process-modeling, catalog, SQL, or BI-authoring product.

### 20.4 Included providers and managed services

- PostgreSQL source: snapshot and scheduled incremental acquisition.
- Stripe source: approved revenue-to-cash objects through cursor-based incremental acquisition.
- PostgreSQL destination.
- ClickHouse destination.
- Dedicated OpenMetadata catalog.
- Dedicated Apache Superset BI service.
- PillarMesh transformation, trigger, report, evidence, backup, and restore runtimes.

The source subset is fixed to the objects and fields needed by the approved story. Generic PostgreSQL replication and unrestricted Stripe coverage are not MVP commitments.

The business-process package accepts one documented, versioned format defined by the MVP contract. Free-form process mining, arbitrary BPMN execution, and automatic acceptance of uploaded meaning are out of scope.

### 20.5 Included semantic and integrity contract

- Customer, Order, Invoice, Payment, Refund, Subscription, and Account Segment entities.
- Approved cross-source customer and financial identity rules.
- Current-state and bounded history behavior required by the story.
- Explicit deletion semantics.
- Daily freshness objective.
- Revenue and churn metric definitions.
- Identity, referential, cardinality, temporal, lifecycle, reconciliation, freshness, and access constraints.
- Quarantine for unresolved records.

The guided template may propose defaults, but activation fails with `No Valid Plan` when required meaning, authority, identity, lifecycle, or reconciliation rules remain unresolved. AI output is never approval or execution authority.

### 20.6 Included managed operations

- Metadata discovery and catalog publication.
- Candidate process, ontology, contract, transformation, and dashboard generation.
- Deterministic legality and feasibility checks.
- Isolated sample validation.
- Initial load, one daily incremental schedule, authorized `Run now`, and bounded backfill.
- Idempotent run intents, retry, replay, checkpoint, and bounded resynchronization.
- Safe additive schema-drift handling under policy.
- Transformation execution and tests.
- Superset compilation and rendering.
- In-product and test-email report delivery.
- Freshness, quality, reconciliation, and evidence.
- Platform-generated incident tickets and bounded remediation proposals.
- Governed stakeholder-answer preparation grounded in authorization, catalog assets, metric versions, freshness, quality, and lineage.
- Least-privilege, time-bounded access proposals, approval binding, grant validation, expiry, and revocation.
- Backup plus witnessed isolated restore.
- Tenant provisioning, network isolation, credential rotation, version-pinned upgrades, retirement,
  monitoring evidence, and fixed-profile capacity-alert evidence.

The MVP does not include automatic capacity changes, multi-region failover, arbitrary cron expressions, user-authored DAGs, unrestricted SQL or code execution, or autonomous semantic repair.

### 20.7 Included approvals

- Requester outcome approval.
- Data-owner semantic approval.
- Data architect activation approval.
- Policy approval for classified fields and finance access.
- Budget approval when a configured threshold is exceeded.
- Explicit retirement approval.

One person may hold several roles in an MVP tenant, but authorization and evidence remain role-scoped.

### 20.8 MVP acceptance journey

1. Create two isolated acceptance tenants with the supported region and fixed capacity profile: one with an immutable PostgreSQL binding and one with an immutable ClickHouse binding.
2. Provision and validate each warehouse plus its OpenMetadata, Superset, roles, encryption, backups, and monitoring.
3. Connect isolated PostgreSQL and Stripe fixtures with intended and denied access probes.
4. Upload the same versioned revenue-to-cash process package in each tenant and verify that original content and extracted candidates remain distinguishable.
5. Resolve business process, ontology, identity, lifecycle, policy, reconciliation, freshness, and metric questions through attributable inbox conversations.
6. Approve exact process, catalog authority, Integration Contract, transformation, dashboard, schedule, access policy, report, and estimate versions.
7. Perform historical load, transformation, reconciliation, catalog publication, and dashboard compilation against each destination using the same approved semantic inputs.
8. Independently query representative warehouse facts; compare canonical semantic results across engines; and verify OpenMetadata lineage, Superset metric versions, and access isolation.
9. Add the same new source facts, invoke the daily trigger in each tenant, and trace both runs to equivalent warehouse freshness and report delivery.
10. Replay the same run intent on each engine and prove no duplicate semantic effect.
11. Submit an authorized stakeholder question about revenue and refunds; verify a correct grounded answer with metric version, as-of time, freshness, quality limitations, datasets, and lineage references.
12. Submit an unanswerable or unauthorized question; verify that PillarMesh creates the required dependent request or denies disclosure rather than fabricating or leaking an answer.
13. Submit a time-bounded finance data-access request; approve the proposed least-privilege role, verify allowed and denied queries, advance the expiry boundary, and verify revocation.
14. Inject one source-authentication or transient failure, additive source drift, a lost destination response, a report-render failure, and a process-integrity violation; verify attribution, bounded recovery or fail-closed behavior, and inbox incident creation.
15. Restore the tenant data plane into an isolated target and verify representative data, contracts, lineage, dashboard compilation, query behavior, access state, and evidence.
16. Export a privacy-safe evidence package linking process upload, decisions, contracts, stakeholder requests, approvals, source boundaries, warehouse effects, catalog assets, grants, and consumer delivery.
17. Reproduce the journey with an independent architect using committed instructions and without direct administration of PostgreSQL, ClickHouse, OpenMetadata, Superset, or the runtime.

### 20.9 MVP exit criteria

The MVP passes only when:

- PostgreSQL and ClickHouse pass the common destination conformance suite and produce equivalent canonical results for the supported semantic subset;
- a versioned business-process upload is traceable to approved ontology elements, constraints, Integration Contracts, data products, and consumer assets without treating extraction as authority;
- no integration activates without approved process and ontology authority;
- the generated warehouse satisfies all declared integrity constraints;
- the certified dashboard and report use the approved metric versions;
- a rendered dashboard alone cannot produce a false success;
- replay, restart, and ambiguous outcomes produce no unexplained duplicate or divergence;
- source drift and process violations are distinguished and attributed;
- tenant, role, catalog, BI, and warehouse isolation tests pass;
- authorized stakeholder questions receive grounded, reproducible answers and unauthorized or unanswerable questions fail closed;
- access requests are purpose-bound, least-privilege, approved, positively and negatively verified, time-bounded, and revocable;
- the isolated restore proves usable data and metadata rather than archive presence;
- PostgreSQL and ClickHouse pass the common Plan 3A outcome subset: isolated bindings,
  governed namespaces, principal grants and denials, idempotent recovery, isolated restore, and
  retention-aware exact-ledger retirement;
- ClickHouse does not claim multi-statement transactional guarantees, transactional DDL rollback,
  deferred foreign-key enforcement, or row-by-row update/delete semantics; contracts requiring
  those guarantees return `No Valid Plan` unless another mechanism is separately proven;
- privacy scans find no secret or raw sensitive value in control-plane artifacts or evidence;
- the one-person architect journey can be completed without manual operation of the warehouse, OpenMetadata, Superset, scheduler, or connector runtime; and
- all required decisions, limitations, and failures remain visible in the request and evidence history.

Passing the MVP proves that one data engineering architect can establish and operate a governed managed-data environment through the architect inbox, using revenue-to-cash as the witnessed fixture, with destination portability across PostgreSQL and ClickHouse. It does not prove general connector coverage, arbitrary business-process formats, unrestricted conversational analytics, arbitrary data-engineering workloads, or in-place warehouse migration.

### 20.10 Deferred from MVP

- MySQL, Salesforce, files, events, and arbitrary REST connectors.
- Logical CDC and continuous streaming.
- Slack and email request intake.
- Existing-catalog federation, including DataHub and customer-managed OpenMetadata.
- Customer-managed destinations.
- Customer-cloud and on-premises data planes.
- Fully disconnected operation.
- Warehouse-engine migration.
- Cross-region replication and disaster recovery.
- Shared multi-tenant warehouse, catalog, or BI infrastructure.
- General process modeling beyond the curated revenue-to-cash template.
- Process mining, executable BPMN orchestration, and unrestricted document-to-contract generation.
- Unrestricted natural-language querying of raw warehouse data.
- General SQL transformation authoring or arbitrary customer code.
- Full native BI authoring beyond governed Superset embedding.
- Mobile applications.
- Autonomous semantic approval.
- General cost-based optimizer and autonomous architectural migration.

### 20.11 Post-MVP expansion gates

Expansion is evidence-gated rather than calendar-gated:

1. **Connector breadth:** add one source class at a time, beginning with the highest validated customer demand. Each source must pass snapshot or cursor correctness, least privilege, replay, drift, resynchronization, reconciliation, and evidence conformance before it is advertised.
2. **Catalog federation:** integrate one supported external catalog without creating a duplicate authority; prove authority binding, conflict handling, lineage round trips, access propagation, and fail-closed behavior.
3. **Scheduling and latency:** add configurable schedules, then CDC or streaming, only after bounded triggers, state, replay, and recovery remain deterministic under load.
4. **Business-process breadth:** add a second curated process only after its ontology, identities, lifecycle, metrics, reconciliations, dashboard, and report can be compiled without weakening the revenue-to-cash contract.
5. **Placement:** add customer-cloud and then on-premises managed data planes after provisioning, upgrade, observability, backup, support access, and exit controls pass the same service boundary as PillarMesh cloud.
6. **Platform scale:** add capacity profiles, high availability, cross-region recovery, and shared infrastructure only with tenant-isolation, cost, noisy-neighbor, and witnessed recovery evidence.

## 21. Delivery sequence

Implementation planning should preserve these dependency stages:

1. Durable process-upload, ontology, authority, stakeholder-question, access-request, approval, warehouse-binding, dashboard, and trigger models.
2. Managed local acceptance environment for PostgreSQL, ClickHouse, OpenMetadata, and Superset.
3. Plan 3A warehouse contracts, provisioning lifecycle, private operational state, and two-engine
   conformance precede source acquisition, destination data movement, transformations, scheduling,
   Superset, and full operations.
4. Catalog publication and authority adapter.
5. Architect inbox, conversation, dependency, stakeholder-answer, access-preview, and approval binding.
6. PostgreSQL and Stripe source providers.
7. PostgreSQL and ClickHouse destination conformance over one semantic corpus.
8. Managed transformation compilation and integrity constraints.
9. Scheduling, incremental state, replay, and recovery.
10. Superset dashboard compilation, stakeholder-answer grounding, access fulfillment, and report delivery.
11. Operations cockpit, incidents, evidence, backup, and restore.
12. Two-engine witnessed acceptance and independent reproduction.

No provider or UI breadth should be added before the revenue-to-cash journey passes end to end on both destinations.

## 22. Success measure

The platform succeeds when the data engineering architect can answer, from one operating surface:

- What business outcomes are requested and by whom?
- What does each governed entity and metric mean?
- Which authorities approved that meaning?
- Which source facts and process constraints support the data product?
- What is currently fresh, valid, at risk, or violated?
- What changed, what is affected, and what decision is required?
- Can the current warehouse, catalog, dashboard, and report be reconstructed and verified?

The architect should not need to answer which worker retried, which connector token rotated, which warehouse node was patched, or which Superset renderer restarted unless a persistent failure crosses the managed service boundary.
