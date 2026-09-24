# ADR-0008: Open-Source Distribution

- Status: Accepted
- Date: 2026-09-18
- Context: [Architecture](../../architecture.md)

## Context

Heinzel is developed in the open so that teams can run it themselves.
Heinzel is a product of PillarMesh, which also builds a hosted service on it.

## Decision

- Heinzel is licensed under Apache-2.0. Contributions are accepted under the same license with a
  Developer Certificate of Origin sign-off ([CONTRIBUTING.md](../../../CONTRIBUTING.md)).
- The hosted service is built separately and depends only on published Heinzel releases. Nothing it
  contains may be required for Heinzel to run.
- The supported public API is the `packages/*` libraries, the `compose_*` composition functions of
  the runtime service, and the `typing.Protocol` ports of the runtime, contract and state services.
  It follows semantic versioning; everything else is internal.
- When the hosted service needs behaviour Heinzel cannot express, Heinzel gains a new port, reviewed
  in public.

## Consequences

- Self-hosted Heinzel and the hosted service run the same code.
- Public API changes need a deprecation notice one minor release before removal.
- Behaviour a hosted deployment needs must be expressible through public ports, so extension points
  are designed and reviewed in the open.
