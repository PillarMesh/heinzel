# PillarMesh

PillarMesh is an Enterprise Data Compiler: a contract-first platform that compiles declared data outcomes into legal, feasible, signed execution graphs and executes them deterministically with attributable evidence.

The repository now contains the Python 3.13 M0 evidence thin thread: one curated PostgreSQL snapshot contract can be verified, compiled into a signed graph, executed into Snowflake, and reconstructed from append-only evidence.

## Start Here

- [Repository layout](docs/architecture/repository-layout.md)
- [Monorepo decision](docs/architecture/decisions/ADR-0001-monorepo-structure.md)
- [Initial structure design](docs/superpowers/specs/2026-08-12-initial-monorepo-structure-design.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [M0 account setup](docs/m0/setup.md)
- [M0 LocalStack Snowflake smoke test](docs/m0/local-emulator.md)
- [M0 acceptance run](docs/m0/acceptance-run.md)
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
