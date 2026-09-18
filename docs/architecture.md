# Architecture

Heinzel turns an approved request into a governed data product. Each stage has one owning service,
and stages exchange typed, digest-bound records rather than untyped data. The services are Python
packages in one `uv` workspace; a directory expresses ownership, not a separately deployed process
([ADR-0002](architecture/decisions/ADR-0002-python-runtime.md)).

Not every stage is connected yet. [Capability status](status.md) says which parts work today.

## Flow

1. **Request management** records the request, the clarification conversation and a typed product
   intent. An architect approves the intent against recorded semantic versions and source
   observations.
2. **Contract** activates an acquisition contract only from an approved intent.
3. **Trigger** turns daily, run-now and backfill policies into run intents, and refuses contracts
   that are not active. **State** owns each run, its attempts, leases and epochs, and incidents and
   recovery.
4. **Runtime** executes a run's stages under its lease: acquisition prepares a verified batch, LAND
   writes it to the warehouse once, and the checkpoint advances only after LAND. Runtime also
   executes governed answer queries.
5. **Compiler** lowers intent to restricted, guarded SQL that a legality rule must admit, or refuses
   with `No Valid Plan` and the reasons
   ([legality rules](../services/compiler/legality/README.md)). Admitted plans are signed.
6. **Materialization** runs through the dbt adapter, which executes only signed models and checks
   their outputs before publication. The compiler does not yet admit product models (see
   [status](status.md)).
7. **Evidence** records each run as a hash-chained event log and verifies evidence packages against
   the chain and the signed execution graph.
8. **Console** is the web interface for requesters and architects. It shows projections of the
   owning services and holds no authority of its own.

Alongside the flow:

| Service | Owns |
| --- | --- |
| `warehouse-control` | The managed warehouse lifecycle: provision, validate, back up and restore, suspend, retire. |
| `semantic-registry` | Semantic candidates, authority resolution, review, approved semantic versions and their catalog publication. |
| `catalog-control` | Tenant catalog bindings, provider selection and capability validation. |
| `bi-control` | Dashboard desired state and provider receipts ([ADR-0006](architecture/decisions/ADR-0006-bi-control.md)). |
| `access-control` | Enterprise entitlement observations and governed access grants ([ADR-0007](architecture/decisions/ADR-0007-access-control.md)). |
| `knowledge-graph` | Lineage, the context graph and impact analysis. |
| `context-exposure` | Governed tools for delegated agents over MCP. |
| `authoring-mcp` | Contract authoring tools over MCP. |
| `connection-broker` | Opaque connection handles and credential exchange. |

## Principles

- Nothing runs without an approved intent and an activated contract.
- Every effect is replay-safe: repeating a step returns its recorded result.
- Legality is decided by rules with proof and independent review, never by an AI tool.
- Each record carries its tenant, and services refuse objects that belong to another tenant.
- Provider-specific identifiers and credentials stay behind the provider boundary.

## Code layout

| Area | Contents |
| --- | --- |
| `packages/` | Shared models (contracts, execution graphs, the intermediate representation) and the provider SDK. |
| `providers/` | Engine and tool integrations: PostgreSQL, ClickHouse, Snowflake, Stripe, OpenMetadata, Superset. |
| `services/` | The owning services named above. |
| `apps/console/` | The console's Starlette server and React web app. |
| `tests/` | Acceptance, conformance, integration, end-to-end, fault-injection and release checks. |

[Repository layout](architecture/repository-layout.md) gives each component's responsibilities
and limits. Design decisions are recorded in [docs/architecture/decisions](architecture/decisions/).
