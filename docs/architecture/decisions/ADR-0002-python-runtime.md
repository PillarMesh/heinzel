# ADR-0002: Use a Uniform Python Modular Monolith

- Status: Accepted
- Date: 2026-08-13
- Context: [Architecture](../../architecture.md)

## Context

Heinzel's first end-to-end thread, the PostgreSQL-to-Snowflake snapshot, had to prove the contract,
intermediate representation, legality, signed-graph, provider, runtime, and evidence boundaries with
one real transaction. Cross-language schemas, service APIs, distributed coordination, and deployment
topology would not have contributed to that proof.

The repository layout declares independently owned components but does not require that they be
separate processes or use different languages. The first runtime had one process, one worker, and
one writer to local control state.

## Decision

Implement Heinzel uniformly in Python 3.13 as independently importable packages in one `uv`
workspace. Use a modular monolith: component ownership remains explicit, while composition
functions wire the components together.

Use Pydantic for closed domain artifacts, Psycopg for PostgreSQL, the Snowflake Python Connector for
Snowflake, Ed25519 signatures from `cryptography`, and SQLite for local control and evidence
persistence. Pin all resolved dependencies in `uv.lock`.

Keep phases, attempts, batch identities, and checkpoints in `services/runtime` at first, and create
`services/state` when a second worker, a cross-process lease or epoch, fencing, or migration
admission creates a contended state boundary. `services/state` now exists and owns runs, attempts,
leases and epochs.

Expose contract authoring through a minimal MCP surface (`services/authoring-mcp`) that calls the
same contract-service operations as any other caller; no domain behaviour moves into the authoring
adapter.

## Consequences

- One language and lockfile cover every component and test.
- Compiler and runtime artifacts remain typed and independently digestible even though components
  share a process.
- Driver-specific types remain behind provider interfaces, preserving an evidence-driven future
  language split.
- SQLite is local reference persistence and makes no production durability or tamper-resistance
  claim.
- A later language split requires measured evidence of a Python limitation and a superseding ADR.

## Alternatives Considered

### Go for runtime and Python for control plane

Deferred because it introduces a cross-language artifact boundary before any Python limitation has
been measured.

### Separate Python services

Rejected for the first thread because network contracts, authentication, deployment, and
partial-failure handling did not contribute to its acceptance claim.

### One undifferentiated Python package

Rejected because it would not validate the architectural ownership boundaries the first thread
existed to test.
