# PillarMesh Managed Data Engineering Platform Addendum v0.1

**Status:** Proposed for final review

**Date:** 2026-08-17

**Applies to:** Enterprise Data Compiler Architecture Specification v0.3, Revenue-to-Cash MVP Implementation Plan v1.4, and the M0 thin-thread specifications

**Decision record:** `docs/architecture/decisions/ADR-0003-managed-data-engineering-platform.md`

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
  binding_id
  tenant_id
  engine_kind          postgresql | clickhouse
  deployment_mode      pillarmesh_cloud
  region
  lifecycle_state
  capability_profile_digest
  provisioned_at
```

Contracts reference `binding_id`. Endpoints, credentials, administrator identities, infrastructure identifiers, encryption keys, and backup locations are private operational state and never enter semantic artifacts or customer-visible evidence.

### 6.3 Immutability

Before provisioning begins, the customer may change engine or region. Once lifecycle state becomes `provisioning`, tenant, engine, deployment mode, and region are immutable. A later change is a separately authorized migration with a new binding, shadow verification, cutover, rollback window, and retirement.

### 6.4 Lifecycle

```text
draft → provisioning → validating → ready
                     ↘ failed

ready → suspended → ready
ready → retiring → retired
```

Only `ready` bindings accept new activations. Provisioning validates engine identity, version, encryption, network isolation, runtime and administration roles, target and ledger capabilities, backups, monitoring, and positive and denial probes.

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

## 9. Catalog and ontology

### 9.1 Catalog operating model

At setup, PillarMesh asks whether the customer has an authoritative catalog.

- If no supported catalog exists, PillarMesh provisions and operates OpenMetadata as the customer's catalog experience.
- If a supported catalog exists, PillarMesh connects read-only first, observes its capabilities and authorities, and uses that catalog as the customer-facing authority. It does not also provision OpenMetadata by default.
- PillarMesh retains only the narrow versioned semantic and authority records required to compile and prove its own contracts. That private registry is not exposed as a competing general catalog.

The first external catalog adapter after the bundled path is DataHub. Apache Atlas and commercial enterprise catalogs are deferred. A catalog whose APIs cannot provide stable identities, versioned observations, ownership, glossary/classification authority, and lineage cannot satisfy this mode; the customer must use managed OpenMetadata or wait for a compatible adapter.

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

### 9.3 Publication and proposals

PillarMesh publishes source observations, warehouse assets, data products, ownership, glossary associations, classifications, quality results, freshness, lineage, contracts, incidents, and Superset assets to OpenMetadata.

Catalog changes that may alter execution become typed change requests. Meaning, identity, classification, access, relationship, metric, constraint, and deprecation changes require impact analysis and approval. Cosmetic descriptions may synchronize automatically under policy.

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

### 13.6 Governed answers and access fulfillment

A stakeholder data question is operational work, not an unrestricted natural-language query against raw tables. PillarMesh resolves the requester, purpose, authorized scope, applicable process and metric versions, catalog assets, freshness, and quality state before preparing an answer. An answer must identify the governed datasets and metric definitions used, their as-of time, material quality limitations, and lineage or evidence references. If the question cannot be answered from approved assets, PillarMesh creates a dependent data-product or semantic-change request instead of inventing a result.

An access request binds requester, purpose, data product, fields, classification, access mode, duration, and approving authority. PillarMesh proposes the least-privilege grant, previews its effective scope, obtains required approval, applies it through a managed role, validates intended and denied access, records evidence, and expires or revokes it according to policy. Neither an inbox conversation nor an AI recommendation grants access by itself.

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
- Runtime, administration, customer SQL, catalog, and BI identities are separate and least-privilege.
- Secrets are resolved through private handles and never enter contracts, graphs, evidence, tickets, prompts, or logs.
- Source and provider metadata are untrusted input.
- Raw data is excluded from the control plane except through explicitly approved bounded diagnostic paths.
- Catalog and BI embeds use PillarMesh identity and authorization; public links are disabled by default.
- Certified assets inherit contract access policy.
- Destructive actions resolve exact targets and require explicit authority.
- Evidence artifacts are privacy-designed and exported through a fail-closed allowlist.

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
- Tenant provisioning, network isolation, credential rotation, version-pinned upgrades, capacity alerts, and retirement for the fixed MVP profile.

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
3. Warehouse provisioning lifecycle and private operational state.
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
