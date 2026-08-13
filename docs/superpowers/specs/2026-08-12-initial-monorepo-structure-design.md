# PillarMesh Initial Monorepo Structure Design

## Objective

Create a technology-neutral repository foundation for PillarMesh that makes product boundaries explicit without choosing application frameworks, programming languages, deployment platforms, or service topology prematurely.

The scaffold must be immediately understandable to contributors and coding agents, track useful documentation in Git, and include an offline check that detects structural drift.

## Current State

The repository is an empty checkout with no commits, source files, local instructions, or existing changes. There are therefore no implementation conventions to preserve and no migration or compatibility requirements.

## Chosen Approach

Use one modular monorepo containing independently understandable application, service, connector, shared-package, documentation, deployment, and test areas.

This approach keeps cross-cutting changes and architectural context together while preserving boundaries that can later become independently buildable, testable, deployable, and owned components. It avoids the coordination overhead of multiple repositories during the founding stage.

## Repository Layout

```text
pillarmesh/
├── .github/
│   ├── ISSUE_TEMPLATE/
│   ├── workflows/
│   ├── CODEOWNERS
│   └── pull_request_template.md
├── apps/
│   ├── api/
│   ├── console/
│   └── docs-site/
├── connectors/
│   ├── destinations/
│   ├── shared/
│   └── sources/
├── deploy/
│   ├── docker/
│   ├── kubernetes/
│   └── terraform/
├── docs/
│   ├── architecture/
│   │   └── decisions/
│   ├── operations/
│   ├── product/
│   ├── security/
│   ├── superpowers/specs/
│   └── vision/
├── packages/
│   ├── client-sdk/
│   ├── connector-sdk/
│   ├── contracts/
│   ├── observability/
│   └── ui/
├── services/
│   ├── connector-worker/
│   ├── metadata/
│   ├── orchestrator/
│   └── runtime/
├── tests/
│   ├── compatibility/
│   ├── end-to-end/
│   ├── integration/
│   └── repository-structure/
├── .editorconfig
├── .gitignore
├── AGENTS.md
├── CONTRIBUTING.md
├── README.md
└── SECURITY.md
```

## Component Responsibilities

### Applications

- `apps/console`: user-facing web console.
- `apps/api`: control-plane API exposed to the console, SDKs, and automation clients.
- `apps/docs-site`: public product and developer documentation application.

### Services

- `services/orchestrator`: pipeline scheduling, coordination, retries, and state transitions.
- `services/runtime`: execution of data movement and transformation work.
- `services/connector-worker`: isolated execution of source and destination connectors.
- `services/metadata`: schemas, catalog, lineage, and related metadata capabilities.

These directories describe intended responsibility boundaries, not a decision that every component must become a network microservice.

### Connectors and Shared Packages

- `connectors/sources` and `connectors/destinations`: provider-specific integration implementations.
- `connectors/shared`: behavior reused specifically by connectors.
- `packages/contracts`: versioned API, event, and configuration contracts.
- `packages/connector-sdk`: connector authoring interfaces and test utilities.
- `packages/client-sdk`: supported client-facing interfaces.
- `packages/ui`: reusable console and documentation UI components.
- `packages/observability`: common telemetry conventions and helpers.

No package manager or cross-language build strategy is selected in this scaffold.

### Documentation and Deployment

- `docs/vision`: company and long-term product direction.
- `docs/product`: product behavior and requirements.
- `docs/architecture`: system architecture and architectural decision records.
- `docs/security`: threat models, policies, and security decisions.
- `docs/operations`: deployment, incident, recovery, and support guidance.
- `deploy`: future container, orchestration, and infrastructure definitions.

## Tracked Files

Directories will contain concise `README.md` files that explain responsibility, dependencies, and the conditions under which code belongs there. Empty `.gitkeep` files will not be used.

Root documents will establish:

- Project purpose and navigation in `README.md`.
- Repository-specific guidance for human and AI contributors in `AGENTS.md`.
- Contribution workflow and boundary expectations in `CONTRIBUTING.md`.
- Private vulnerability-reporting guidance in `SECURITY.md`.
- Neutral editor and ignore defaults in `.editorconfig` and `.gitignore`.

No license will be selected because repository licensing is a product and legal decision outside this scaffold.

## Validation

An offline shell test under `tests/repository-structure/` will fail when a required top-level document or component directory is absent. It will derive the repository root from its own location so it works from any current directory.

The boundary test will run against a temporary incomplete fixture and confirm that the validator exits nonzero with a useful missing-path message. This specifically catches a validator that accidentally checks the real repository regardless of the supplied target.

A minimal GitHub Actions workflow will execute the offline structure test for pull requests and pushes. It will not install language runtimes or dependencies.

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
