# ADR-0001: Begin with a Modular Monorepo

- Status: Accepted
- Date: 2026-08-12
- Context: [Architecture](../../architecture.md)

## Context

Heinzel compiles declared data outcomes into legal, signed execution and runs them with evidence. Its early work crosses the Integration Contract, compiler artifacts, capability providers, deterministic runtime, state ownership, evidence, governed context, and product experience. Splitting these boundaries across repositories before teams, release cadences, and access requirements are known would add coordination cost and make architectural drift harder to detect.

A generic applications/services/connectors layout was also considered. It conflicts with Heinzel's model: there is no general orchestrator, a capability provider is not exclusively a source or destination, and the permanent Integration Contract is not a generic API-contract package.

## Decision

Use one modular monorepo with top-level `apps/`, `services/`, `providers/`, `packages/`, `deploy/`, `docs/`, and `tests/` areas. Govern immediate component names under `apps/`, `services/`, and `packages/`; keep provider names extensible. Track the detailed placement rules in `docs/architecture/repository-layout.md`.

Create nested component directories only with their first substantive implementation. Track concise top-level boundary READMEs instead of speculative per-component placeholders.

Repository terminology follows `docs/architecture.md` and these decision records. A material boundary or vocabulary change must supersede this ADR rather than silently contradict it.

## Consequences

- Cross-cutting changes and architecture context remain atomic.
- Component boundaries are explicit without asserting a microservice topology or technology stack.
- Structural growth is reviewed through an offline allowlist test.
- Some components may later require independent build and release tooling inside the monorepo.
- A contributor must update the layout, decision record, and validator together when adding a governed boundary.

## Alternatives Considered

### Multiple repositories from inception

Rejected because no evidence yet justifies separate ownership, release, licensing, access, or scale boundaries, while coordination overhead would be immediate.

### Generic application/service/connector monorepo

Rejected because it encodes a scheduler-oriented pipeline product and source/destination connector taxonomy that contradict Heinzel's architecture.

### Unstructured single repository

Rejected because it would not answer where architecture-sensitive code belongs and would allow catch-all control-plane and shared-package areas to accumulate.

## Future Split Criteria

A component should move to another repository only when it has an independently justified boundary, such as a different license, public contribution model, access-control requirement, release cadence, ownership team, or material build-scale constraint.

Until such evidence exists, code and documentation remain together in this monorepo.
