# PillarMesh

PillarMesh is an Enterprise Data Compiler: a contract-first platform that compiles declared data outcomes into legal, feasible, signed execution graphs and executes them deterministically with attributable evidence.

The current product boundary is set by the [managed data engineering platform addendum](docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md). PillarMesh operates the tenant's analytical warehouse rather than integrating with an arbitrary customer-managed destination, and the initial engine catalog is PostgreSQL and ClickHouse.

The repository contains the data architect control-plane foundation: immutable managed-warehouse bindings, immutable business-process package intake, and a typed architect inbox with attributable conversation and revision-bound decisions. It also contains the completed M0 evidence thin thread, which is [historical](#historical-m0).

## Start Here

- [Managed data engineering platform addendum](docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md)
- [Managed data plane decision](docs/architecture/decisions/ADR-0003-managed-data-engineering-platform.md)
- [Repository layout](docs/architecture/repository-layout.md)
- [Monorepo decision](docs/architecture/decisions/ADR-0001-monorepo-structure.md)
- [Initial structure design](docs/superpowers/specs/2026-08-12-initial-monorepo-structure-design.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)

## Historical M0

M0 was the PostgreSQL-to-Snowflake thin-thread experiment: one curated PostgreSQL snapshot contract verified, compiled into a signed graph, executed into Snowflake, and reconstructed from append-only evidence. It proved compiler, runtime, and evidence behaviour against an externally managed destination.

These records remain valid as historical evidence. They are not rewritten, and they do not define the destination or product scope after the addendum above. Snowflake is no longer a product destination.

- [M0 account setup](docs/m0/setup.md)
- [M0 LocalStack Snowflake smoke test](docs/m0/local-emulator.md)
- [M0 acceptance run](docs/m0/acceptance-run.md)
- [M0 evidence package](docs/m0/evidence-package.md)
- [M0 acceptance transport decision](docs/m0/transport-decision.md)
- [M0 teardown](docs/m0/teardown.md)

## Validate the Repository

Install the exact locked workspace and run all offline gates:

```sh
uv sync --locked --all-packages
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
./tests/repository-structure/test.sh
```

Real-account tests are deliberately opt-in and require the dedicated environment described in `docs/m0/setup.md`. An offline pass is not evidence that PostgreSQL-to-Snowflake execution works against real accounts.
