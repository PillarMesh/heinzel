# PillarMesh Initial Monorepo Structure Design

## Objective

Create a technology-neutral repository foundation for PillarMesh that makes the boundaries of the Enterprise Data Compiler (EDC) Foundational Architecture Specification v0.3 explicit without choosing application frameworks, programming languages, deployment platforms, or service topology prematurely.

The scaffold must be immediately understandable to contributors and coding agents, track useful documentation in Git, and include an offline check that detects structural drift.

## Current State

PillarMesh began as an empty checkout. The current branch contains only this design-specification commit; it still has no implementation scaffold, source code, or repository-local contributor guidance. There are therefore no implementation conventions to preserve and no migration or compatibility requirements.

## Chosen Approach

Use one modular monorepo containing independently understandable application, compiler/runtime service, capability-provider, shared-package, documentation, deployment, and test areas.

This approach keeps cross-cutting changes and architectural context together while preserving boundaries that can later become independently buildable, testable, deployable, and owned components. It avoids the coordination overhead of multiple repositories during the founding stage.

Directory names deliberately track the vocabulary and artifact boundaries of the canonical in-tree EDC Foundational Architecture Specification v0.3 and Revenue-to-Cash MVP Implementation Plan v1.4. Repository review checks the naming invariant against those sources. Renaming an architectural area requires a corresponding specification or architecture-decision update so that repository terminology does not drift from the system model.

## Scaffolded Repository Layout

The initial commit will track the repository's top-level boundaries and only those nested directories that contain a real document, workflow, or test. It will not create every anticipated component directory ahead of its first implementation.

```text
pillarmesh/
├── .github/
│   ├── workflows/
│   │   └── repository-structure.yml
│   └── pull_request_template.md
├── apps/
│   └── README.md
├── deploy/
│   └── README.md
├── docs/
│   ├── architecture/
│   │   ├── decisions/
│   │   │   └── ADR-0001-monorepo-structure.md
│   │   ├── specifications/
│   │   │   ├── enterprise-data-compiler-foundational-architecture-v0.3.docx
│   │   │   └── enterprise-data-compiler-revenue-to-cash-mvp-implementation-plan-v1.4.docx
│   │   └── repository-layout.md
│   └── superpowers/specs/
│       └── 2026-08-12-initial-monorepo-structure-design.md
├── packages/
│   └── README.md
├── providers/
│   └── README.md
├── services/
│   └── README.md
├── tests/
│   └── repository-structure/
│       ├── test.sh
│       └── validate.sh
├── .editorconfig
├── .gitattributes
├── .gitignore
├── AGENTS.md
├── CONTRIBUTING.md
├── README.md
└── SECURITY.md
```

The top-level area READMEs are intentional boundary documents, not placeholders for speculative components. They link to the single canonical component map in `docs/architecture/repository-layout.md`. Nested directories are created only with their first real source file, test, configuration, or substantive document; `.gitkeep` files and a tree of repetitive READMEs are not used.

## Intended Component Map

`docs/architecture/repository-layout.md` will record the following intended homes. These are code-ownership boundaries, not commitments to processes, network services, or deployment units. The `services/` map mirrors the concrete components in Revenue-to-Cash MVP Implementation Plan v1.4 Table 4 and §3.2, with names normalized to the EDC vocabulary.

```text
apps/
└── console/                 # Operator-facing product application
services/
├── authoring-mcp/           # Host-neutral contract authoring tools, resources, sessions
├── compiler/                # Contract → semantic IIR → legal plan → signed graph
├── connection-broker/       # OAuth flows, rotation, and opaque connection handles
├── context-exposure/        # Authorization-filtered consumer MCP resources
├── contract/                # Versioned drafts, activation digests, approvals, lifecycle
├── dbt-adapter/             # Version-pinned invocation and manifest/test/lineage observation
├── evidence/                # Append-only decision, execution, reconciliation, incident facts
├── knowledge-graph/         # Metadata snapshots, lineage, and compiler projection
├── provider-registry/       # Versioned declarations, conformance tier, evidence validity
├── reconciliation/          # Declared lifecycle predicates, deadlines, exceptions
├── relay/                   # Optional private-connectivity execution relay
├── runtime/                 # Deterministic execution of signed graphs; operator workers
└── state/                   # Epochs, partitions, leases, checkpoints, cutover, single-writer
providers/
└── <provider>/              # Capability declarations, implementations, conformance fixtures
packages/
├── client-sdk/              # Supported client-facing interfaces
├── contract-model/          # Permanent, user-owned Integration Contract model
├── execution-graph/         # Signed-graph shape, verification, and compatibility
├── iir/                     # Versioned semantic Integration Intermediate Representation
├── observability/           # Shared telemetry conventions and helpers
└── provider-sdk/            # Provider authoring and conformance interfaces
```

## Component Responsibilities

### Applications

- `apps/console` is the operator-facing product application. No web framework or UI package is selected by this scaffold.
- A documentation site and a reusable UI package are deferred until product and framework choices justify them. Product documentation remains ordinary Markdown under `docs/` in the meantime.

### Compiler and Runtime Services

- `services/authoring-mcp` owns host-neutral contract-authoring tools and resources, resumable sessions, authorization filtering, and draft mutations. It does not own semantic validity or execution state.
- `services/contract` owns versioned drafts, activation digests, approvals, and contract lifecycle. It does not own credentials or physical scheduling.
- `services/compiler` owns the deterministic path from Integration Contract through semantic IIR, legal Physical Plan, and signed Execution Graph. It contains compiler passes, feasibility checks, capability matching, cost-based selection among legal alternatives, and explicit `No Valid Plan` proofs. Its stable `services/compiler/legality/` boundary contains `rules/`, `proof-notes/`, and `fixtures/`. It does not execute data movement.
- `services/connection-broker` owns OAuth attempts and callbacks, token exchange and rotation, and opaque connection handles. Raw secrets never appear in MCP results.
- `services/provider-registry` owns versioned capability declarations, conformance tier, and evidence validity. It does not grant trust from provider assertion alone.
- `services/runtime` validates and executes signed Execution Graph operators and capability bindings, acquires short-lived credentials or grants, emits execution evidence, and reports conformance. It does not choose mappings, join strategies, partitioning, or providers. Operator workers are part of this runtime boundary; there is no separate connector-worker service in the initial architecture.
- `services/state` owns execution state, including epochs, partitions, leases, checkpoints, cutover markers, replay boundaries, and single-writer enforcement. It provides the authority needed to prevent split-brain execution.
- `services/evidence` owns append-only, attributable, replayable contract, compiler-decision, execution, reconciliation, and incident facts. Evidence is distinct from mutable operational state and ordinary application logs; the service does not synthesize health without verifiable facts.
- `services/knowledge-graph` owns metadata snapshots, lineage, and the compiler's knowledge-graph projection. The knowledge graph is a compiler projection used for reasoning and explainability; it is not a replacement for source catalogs or the permanent Integration Contract.
- `services/dbt-adapter` owns version-pinned dbt invocation and manifest, test, lineage, and model-run observation. It does not own business transformation semantics or author SQL.
- `services/reconciliation` owns deterministic evaluation of declared lifecycle predicates, deadlines, exceptions, and evidence links. It neither mutates sources nor performs probabilistic matching.
- `services/context-exposure` owns authorization-filtered finance and sales MCP resources with freshness and provenance. It provides neither unrestricted SQL nor action authority.
- `services/relay` is the optional customer-network relay for private capability access. It executes signed fragments and returns evidence; it does not perform planning or become a general scheduler.

PillarMesh does not contain a global scheduler or general-purpose workflow orchestrator. Scheduling, when required by a deployment, is an external trigger that requests compilation or execution and is not an EDC architectural service. Internal control loops for lease and epoch expiry, migration admission, revalidation start and complete-or-fence deadlines, and snapshot-to-CDC handoff coordination belong to `services/state`; they are contract-scoped and evidence-emitting, not a general scheduler.

### Integration Contract and Shared Models

- `packages/contract-model` contains the permanent, user-owned Integration Contract model and its validation rules. The name is intentionally not `contracts`: API, event, or configuration contracts are different artifacts and will receive explicit homes only when designed.
- `packages/iir` contains the compiler-owned, versioned semantic IIR types and compatible serialization rules. It must not absorb replaceable Physical Plan or disposable Execution Graph state merely because those artifacts are adjacent in the compiler pipeline.
- `packages/execution-graph` contains the disposable signed-graph artifact shape, digest and signature-verification rules, and compatibility policy shared by compiler and runtime. It contains no planning logic, which remains in `services/compiler`, and no execution state, which remains in `services/state`.
- Physical Plan selection and its legality/feasibility proof machinery live with `services/compiler` rather than in a shared package.
- `packages/client-sdk` and `packages/observability` are reserved intended boundaries. They are created only when their first substantive implementation exists.

No package manager, language, cross-language schema system, or build strategy is selected in this scaffold.

### Capability Providers

The EDC uses capability providers rather than source and destination connectors. One provider may advertise any combination of `READ`, `WRITE`, `CDC`, `QUERY`, `EVENT`, `TOOL`, `RESOURCE`, and `ACTION` capabilities, so the repository must not encode a source/destination split.

- `providers/<provider>` contains provider-specific capability declarations, adapters or implementations, configuration schemas, and conformance fixtures. Provider declarations are claims until verified by conformance evidence.
- `packages/provider-sdk` contains provider-authoring interfaces, declaration-model helpers, and reusable conformance-test utilities. It contains no provider-specific code.
- `services/runtime` binds and executes capabilities selected in a signed graph. It does not host reusable provider implementations or provider-specific planning logic.
- Compiler-side capability matching, legality evaluation, and provider selection remain in `services/compiler`, not in providers or runtime.

### Documentation and Deployment

- `docs/architecture` contains the EDC architecture, repository layout, and architectural decision records. Its `specifications/` directory tracks the canonical EDC Foundational Architecture Specification v0.3 and Revenue-to-Cash MVP Implementation Plan v1.4.
- The initial scaffold tracks both governing specifications in their original `.docx` format because no reviewed Markdown conversion is available. A later greppable Markdown conversion must be fidelity-checked against the source and replace or accompany the Word files in the same reviewed change.
- Future `docs/vision`, `docs/product`, `docs/security`, and `docs/operations` directories are created when their first substantive documents arrive.
- `deploy` is one infrastructure-neutral boundary for future packaging and deployment definitions. Docker, Kubernetes, Terraform, cloud, and topology subdivisions are not preselected.

## Day-One Contributor Rules

- Unit tests are colocated with the component they exercise, following the conventions of the language or framework selected for that component.
- `tests/` is reserved for repository-structure, cross-component integration, compatibility, conformance, fault-injection, and end-to-end suites that cannot reasonably belong to one component.
- A provider-specific change belongs under `providers/<provider>`; reusable provider-authoring or conformance tooling belongs under `packages/provider-sdk`; signed-graph operator execution belongs under `services/runtime`.
- Contract-scoped control loops for leases, epochs, revalidation, migration admission, and snapshot-to-CDC handoff coordination belong under `services/state`; they must not create a scheduler service.
- Adding or widening a legality rule requires its proof note, positive and negative conformance fixtures, mutation tests for every precondition, and regression against every previously admitted provider pair in the same change, with approval from an independent reviewer. This is a repository review rule, not merely a compiler-local convention.
- Shared code moves into `packages/` only after at least two real consumers demonstrate a stable shared boundary. Convenience alone is not sufficient.
- Every new top-level area requires an update to `docs/architecture/repository-layout.md`, ADR-0001 or a superseding ADR, and the repository-structure allowlist in the same change.

## Tracked Files

Root documents will establish:

- Project purpose and navigation in `README.md`.
- Repository-specific guidance for human and AI contributors in `AGENTS.md`.
- Contribution workflow and boundary expectations in `CONTRIBUTING.md`.
- Private vulnerability-reporting guidance in `SECURITY.md`.
- Neutral editor, text, and ignore defaults in `.editorconfig`, `.gitattributes`, and `.gitignore`.

`.github/pull_request_template.md` will prompt for scope, validation, architectural impact, and security considerations. A minimal workflow will run repository-structure validation. `CODEOWNERS` is deferred until more than one meaningful owner or review boundary exists.

No license will be selected because repository licensing is a product and legal decision outside this scaffold.

## Durable Architecture Decision

`docs/architecture/decisions/ADR-0001-monorepo-structure.md` will be the durable decision record. It will capture:

- the decision to begin with a modular monorepo;
- the EDC-aligned top-level and component boundaries;
- the alternatives considered, including multiple repositories and a generic application/service/connector layout;
- the consequences of lazy nested-directory creation and structural validation; and
- the criteria that would justify splitting a component into another repository.

This design specification is the working review record. ADR-0001 remains the durable repository-level decision and must be superseded, not silently contradicted, if the structure changes materially.

## Validation

`tests/repository-structure/validate.sh` will maintain an explicit allowlist of top-level entries and the declared immediate component names under architecture-sensitive areas such as `apps/`, `services/`, and `packages/`. Intended component directories are optional until implemented, but an undeclared component such as `services/scheduler/` is rejected. Provider implementation names below `providers/` are intentionally extensible. Validation fails if a required path is missing or if an unexpected governed entry exists outside the declared set. `.git` is repository metadata and is ignored rather than included in the allowlist.

Required paths include the root contributor documents and configuration files, every declared top-level area and its boundary README, `docs/architecture/repository-layout.md`, `docs/architecture/decisions/ADR-0001-monorepo-structure.md`, `docs/architecture/specifications/enterprise-data-compiler-foundational-architecture-v0.3.docx`, `docs/architecture/specifications/enterprise-data-compiler-revenue-to-cash-mvp-implementation-plan-v1.4.docx`, this design specification, the validator and its test, the pull-request template, and the repository-structure workflow. Failures name each missing or unexpected path and exit nonzero.

`tests/repository-structure/test.sh` will exercise the validator against temporary fixtures. At minimum it will prove:

1. a complete fixture passes;
2. a fixture with a required path removed fails and names the missing path; and
3. a fixture with an undeclared top-level entry fails and names the unexpected path; and
4. a fixture with an undeclared governed component such as `services/scheduler/` fails and names the unexpected path.

The fixture tests also prove that validation uses the supplied target rather than accidentally inspecting the real repository. The tests are offline shell scripts and require no application language runtime or package installation.

A minimal GitHub Actions workflow will execute the offline structure test for pull requests and pushes.

## Explicit Non-Goals

This scaffold will not:

- Select frontend, backend, workflow, streaming, storage, or infrastructure technology.
- Create runnable application placeholders that imply a framework decision.
- Define network APIs, events, database schemas, or deployment topology.
- Add package-manager lockfiles or generated dependencies.
- Publish, push, or configure external services.
- Commit secrets, environment-specific configuration, or credentials.

## Future Split Criteria

A component should move to another repository only when it has an independently justified boundary, such as a different license, public contribution model, access-control requirement, release cadence, ownership team, or material build-scale constraint.

Until such evidence exists, code and documentation remain together in this monorepo.
