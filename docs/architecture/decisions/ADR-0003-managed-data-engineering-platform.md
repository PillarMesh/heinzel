# ADR-0003: Make the Managed Data Plane Part of the Product

- Status: Accepted
- Date: 2026-08-17
- Context: [Architecture](../../architecture.md)

## Context

Heinzel makes Integration Contracts durable and keeps providers, plans, and execution graphs replaceable. Its initial product boundary assumed that Heinzel integrates with an externally managed warehouse. The first end-to-end thread, the PostgreSQL-to-Snowflake snapshot, consequently targets Snowflake and proves compiler, runtime and evidence behavior, but it leaves a one-person data engineering team responsible for choosing, provisioning, securing, operating, backing up, and exposing the destination platform and its BI and catalog services.

The selected product category is provider-managed data integration. Connector operation alone is insufficient for the intended customer because destination configuration, semantic modeling, cataloging, dashboards, scheduled reports, recovery, and integrity would remain split among products and owners. Heinzel cannot claim accountable source-to-consumer outcomes while the destination data plane remains outside its operational authority.

## Decision

Make a dedicated Heinzel-operated data plane mandatory for every tenant. The initial warehouse choices are PostgreSQL and ClickHouse. Heinzel operates OpenMetadata when the customer has no supported catalog, uses a supported existing catalog when one is declared authoritative, and operates Apache Superset for BI and report rendering. It also operates transformation execution, scheduling, state, evidence, backup, and recovery.

Position Heinzel against managed data-integration products rather than database engines. The platform manages the integration and destination, while the customer owns its data, business meaning, policies, priorities, and approvals.

Adopt supervised autopilot. Heinzel automatically performs bounded investigation, implementation, testing, deployment, operation, recovery, and proven-equivalent physical maintenance. Human authorities approve business meaning, policy, access, material cost, migrations, and destructive actions.

Require an approved business-process model, catalog/ontology authority, constraints, and current provider observations before an Integration Contract can become executable. Source connectivity alone is not admission evidence.

Provide one typed request boundary for business requests, engineering work, incidents, and platform proposals. Provide a narrow trigger service for activated contracts; do not add a general workflow scheduler.

Implement domain services with injected repositories, using standard-library SQLite reference adapters. Every service and repository method that names a durable object takes `tenant_id` and refuses an object that belongs to another tenant. Durable state is append-only per `(object_id, revision)`, and every mutation supplies `expected_revision` from its caller.

Keep catalog adapters and Superset replaceable. Heinzel's durable process, contract, dashboard, approval, and evidence models remain authoritative for Heinzel behavior; external catalog authority remains explicit, and catalog and BI object identifiers are compiled physical artifacts.

Separate catalog operations from semantic authority: `services/catalog-control` owns catalog
binding lifecycle, capability validation, and private resource-ledger cleanup, while
`services/semantic-registry` owns immutable candidate, authority, review, and approved
semantic records. This keeps OpenMetadata replaceable without letting a provider change
approved meaning or contract legality.

Preserve future `customer_cloud` and `customer_on_prem` placement modes in which the customer supplies infrastructure but Heinzel retains operation of the data plane. Do not interpret these modes as bring-your-own-warehouse support.

## Consequences

- Heinzel takes a materially larger operational and security responsibility than an integration-only product.
- A customer can obtain integration, warehouse, catalog, transformation, BI, reporting, recovery, and evidence from one accountable service.
- Customers with an immovable existing warehouse are not part of the initial addressable market.
- Destination portability is demonstrated across supported managed engines, not arbitrary customer destinations.
- Dedicated warehouse, OpenMetadata, and Superset deployments simplify tenant isolation and retirement but increase cost.
- The platform must publish a clear shared-responsibility model, restore evidence, and open-format exit path.
- The repository requires explicit ownership for request management, warehouse control, catalog control, semantic registry, and trigger materialization while reusing the existing compiler, contract, provider, runtime, evidence, knowledge-graph, dbt, and context-exposure boundaries.
- The PostgreSQL-to-Snowflake snapshot remains in the repository as a proof of compiler, runtime and evidence behavior, but Snowflake is not a managed product destination. The snapshot contract's `evidence_retention` value has since been renamed to `snapshot_30_days` without a change to its `schema_version` of `1`, so snapshot contract records written with the earlier value no longer validate against the current contract model.

## Provider inventory

| Path | Role |
| --- | --- |
| `providers/openmetadata` | OpenMetadata-specific capability declaration, adapter, and conformance fixtures; durable catalog, semantic, contract, and evidence models remain outside the provider boundary. |

## Alternatives Considered

### Integration-only with arbitrary customer destinations

Rejected for the initial product because destination configuration and operation would remain outside Heinzel's authority, preventing a complete managed outcome for the one-person team. It may be reconsidered only through a new product-boundary decision.

### Build a new warehouse, catalog, and BI engine

Rejected. Heinzel operates PostgreSQL or ClickHouse, OpenMetadata, and Superset as conformance-tested capabilities. Its differentiation is semantic compilation, managed operation, and evidence rather than reimplementing mature engines.

### Require an external enterprise catalog

Rejected because it excludes the small teams that need the product most. Heinzel supplies managed OpenMetadata and federates with existing authorities when available.

### General integration and workflow platform

Rejected because connector, API, messaging, IoT, and business-workflow breadth would dilute the source-to-consumer data-product outcome and reintroduce ambiguous orchestration semantics.
