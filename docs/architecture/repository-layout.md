# PillarMesh Repository Layout

This document is the canonical repository placement guide. Component directories are created lazily with their first substantive implementation; the map defines permitted ownership boundaries before those directories exist.

## Applications

| Path | Owns | Must not own |
| --- | --- | --- |
| `apps/console` | Operator-facing product experience | Compiler, runtime, state, credentials, or provider implementations |

## Services

The service map mirrors the concrete control-plane components in Revenue-to-Cash MVP Implementation Plan v1.4 Table 4 and the data-plane boundaries in §3.2, with names normalized to the EDC vocabulary.

| Path | Owns | Must not own |
| --- | --- | --- |
| `services/access-control` | Connected enterprise-entitlement observations and current snapshots; governed access-grant application, expiry, revocation, and receipts | Request approval, warehouse credentials, provider-local identifiers, or execution state |
| `services/authoring-mcp` | Host-neutral authoring tools/resources, sessions, authorization filtering, draft mutations | Semantic validity or execution state |
| `services/bi-control` | Versioned dashboard desired state, provider reconciliation, publication receipts, and archive lifecycle | Business meaning, access grants, provider-local identifiers, or dashboard rendering |
| `services/catalog-control` | Tenant catalog-binding lifecycle, provider selection, capability validation, and private resource-ledger cleanup | Semantic authority, approved meaning, contract legality, or public provider identifiers |
| `services/compiler` | Contract parsing, semantic IIR, legality, feasibility, plan selection, deployment compilation | Runtime retries or record-level nondeterminism |
| `services/connection-broker` | OAuth attempts/callbacks, token exchange and rotation, opaque connection handles | Raw secrets in MCP results |
| `services/context-exposure` | Authorization-filtered governed-answer, catalog, metric, and impact resources and agent tools for delegated principals, with freshness and provenance; delegates request creation and clarification replies to request management | Unrestricted SQL, statement text, approvals, request state, or action authority beyond delegated question requests |
| `services/contract` | Versioned drafts, process-package intake, activation digests, approvals, lifecycle | Credentials or physical scheduling |
| `services/dbt-adapter` | Version-pinned invocation and manifest/test/lineage observation | Business transformation semantics or SQL authoring |
| `services/evidence` | Append-only contract, decision, execution, reconciliation, and incident facts | Unverifiable health synthesis |
| `services/knowledge-graph` | Provenance-bearing compiler projection, metadata snapshots, lineage, context graph, and impact analysis | Replacement enterprise catalog, authoritative metadata mutation, or approval requirements that replace an owning service's |
| `services/provider-registry` | Versioned declarations, conformance tier, evidence validity | Trust based on provider assertion alone |
| `services/reconciliation` | Declared lifecycle predicates, deadlines, exceptions, evidence links | Source mutation or probabilistic matching |
| `services/relay` | Restricted private-network capability invocation of signed fragments | Planning authority or general scheduling |
| `services/request-management` | Typed stakeholder questions, answer scope policies, answer intent validation, policy admission, governed answer delivery, data-access requests, business and engineering requests, incidents, platform proposals, conversations, assignment, dependency edges, and request lifecycle | Semantic approval authority, general workflow definitions, or execution state |
| `services/runtime` | Signed-graph verification, deterministic operators, grants, governed query execution and result snapshots, execution evidence | Semantic reinterpretation or physical plan selection |
| `services/semantic-registry` | Immutable semantic candidates, per-information-kind authority resolution, ontology review bundles, approved semantic versions, and catalog drift proposals | Catalog provisioning, provider-local identifiers, execution, or physical plan selection |
| `services/state` | Epochs, partitions, leases, checkpoints, cutover, migration admission, contract-scoped control loops | Global execution serialization or general scheduling |
| `services/trigger` | Versioned trigger policies, deterministic scheduled-window identities, misfire and overlap materialization into run intents | DAG authoring, plan selection, provider access, or direct execution |
| `services/warehouse-control` | Tenant warehouse-binding lifecycle, managed data-plane provisioning, private infrastructure inventory, backup/restore coordination, upgrade and retirement admission | Database-engine implementation, business semantics, or raw credentials in public artifacts |

The compiler legality table has a stable internal boundary:

```text
services/compiler/legality/
├── fixtures/
├── proof-notes/
└── rules/
```

## Providers

`providers/<provider>` contains provider-specific capability declarations, adapters or implementations, configuration schemas, and conformance fixtures. Provider names are intentionally extensible; source/destination subdivisions are forbidden because one provider can advertise several capability types. Managed platform capabilities such as OpenMetadata and Superset use the same provider boundary: the durable catalog, dashboard, contract, and evidence models remain in their owning packages or services, while provider-local identifiers and APIs remain quarantined here.

## Shared Packages

| Path | Owns | Must not own |
| --- | --- | --- |
| `packages/client-sdk` | Supported client-facing interfaces | Control-plane implementation |
| `packages/contract-model` | Permanent, user-owned Integration Contract model and validation | API/event/configuration contracts by implication |
| `packages/execution-graph` | Disposable signed-graph shape, digest/signature verification, compatibility | Planning logic or execution state |
| `packages/iir` | Compiler-owned, versioned semantic IIR and serialization | Physical Plan or Execution Graph state |
| `packages/observability` | Shared telemetry conventions and helpers | Evidence authority |
| `packages/provider-sdk` | Provider authoring interfaces, declaration helpers, conformance utilities | Provider-specific code |

## Other Top-Level Areas

- `deploy/`: infrastructure-neutral deployment material after an ADR selects technology.
- `docs/`: architecture, decisions, specifications, and substantive product/operations/security material.
- `tests/`: repository structure and cross-component integration, compatibility, conformance, fault-injection, and end-to-end suites. Unit tests remain colocated.

## Structural Change Rule

Adding a top-level area or governed immediate component under `apps/`, `services/`, or `packages/` requires, in the same change:

1. an update to this document;
2. ADR-0001 or a superseding ADR; and
3. an update to `tests/repository-structure/validate.sh`.
