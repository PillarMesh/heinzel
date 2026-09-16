# ADR-0006: Keep Dashboard Authority in BI Control

## Status

Accepted on 2026-09-11.

## Context

PillarMesh must compile certified dashboards from approved data-product and semantic versions while
Apache Superset owns rendering, layout, exploration, and export. Superset object identifiers are
disposable, display titles are mutable, and remote responses can be lost after an effect commits.
Treating the console, request service, or Superset as dashboard authority would make replay and
drift decisions depend on presentation state or provider-local identifiers.

## Decision

`services/bi-control` owns immutable dashboard desired revisions and provider receipts. It records
desired state before invoking a BI provider and records the exact returned receipt before exposing
the dashboard link. A retry returns an existing receipt verbatim or reconciles through a stable key
derived from tenant, dashboard ID, and dashboard contract version.

`packages/provider-sdk` defines the provider-neutral BI contract and conformance behavior.
`providers/superset` maps that contract to Superset datasets, charts, and dashboards. Remote IDs
remain private to the provider. Updates and archive operations proceed only when the observed
managed digest equals the prior desired digest; an unrecognized mutation is an integrity conflict
for operator action. Database credentials cross the boundary only as secret references.

The console reads BI-control projections. Request management may propose dashboard work but owns
neither desired state nor provider effects. Access grants remain a separate access-control concern.

## Consequences

- Exact replay is independent of titles and remote IDs.
- A transient provider failure leaves durable desired state without publishing an unverified link.
- Certified dashboard updates cannot overwrite unrecognized Superset edits.
- Provider replacement can rebuild dashboards from service-owned state and receipts.
- Live Superset deployment, cold-start invocation, and access-control integration require separate
  witnessed work; offline conformance does not establish them.
