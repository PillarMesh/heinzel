# ADR-0002: Use a Uniform Python Modular Monolith for M0

- Status: Accepted
- Date: 2026-08-13
- Governing design: `docs/superpowers/specs/2026-08-13-m0-python-thin-thread-design.md`

## Context

M0 must prove the contract, IIR, legality, signed-graph, provider, runtime, and evidence boundaries with one real PostgreSQL-to-Snowflake transaction. It must not spend the six-week evidence gate on cross-language schemas, service APIs, distributed coordination, or deployment topology.

The governing repository layout declares independently owned components but does not require that they be separate processes or use different languages. The M0 runtime has one process, one worker, and one writer to local control state.

## Decision

Implement M0 uniformly in Python 3.13 as independently importable packages in one `uv` workspace. Use a modular monolith: component ownership remains explicit, while one composition root wires the components into the CLI and MCP server.

Use Pydantic for closed domain artifacts, Psycopg for PostgreSQL, the Snowflake Python Connector for Snowflake, Ed25519 signatures from `cryptography`, and SQLite for local M0 control and evidence persistence. Pin all resolved dependencies in `uv.lock`.

For M0 only, keep phases, attempts, batch identities, and checkpoints in `services/runtime`. Create `services/state` when a second worker, cross-process lease or epoch, fencing, or live-migration admission creates a contended state boundary.

Pull the minimal MCP authoring surface into M0 to test digest-bound activation. If the documented Week 5 release valve is invoked, a local CLI calls the same contract-service operations; no domain behavior moves into the authoring adapter.

## Consequences

- One language and lockfile cover every M0 component and test.
- Compiler and runtime artifacts remain typed and independently digestible even though components share a process.
- Driver-specific types remain behind provider interfaces, preserving an evidence-driven future language split.
- SQLite is explicitly local M0 persistence and makes no production durability or tamper-resistance claim.
- Python throughput is measured under the fixed M0 ceilings; a later language split requires measured evidence and a superseding ADR.
- MCP authoring and the collapsed state boundary are deliberate deviations or reductions recorded in the governing design, not silent changes to the foundational architecture.

## Alternatives Considered

### Go for runtime and Python for control plane

Deferred because it introduces a cross-language artifact boundary before M0 has measured a Python limitation.

### Separate Python services

Rejected for M0 because network contracts, authentication, deployment, and partial-failure handling do not contribute to the thin-thread acceptance claim.

### One undifferentiated Python package

Rejected because it would not validate the architectural ownership boundaries M0 exists to test.
