# PillarMesh

PillarMesh is an Enterprise Data Compiler: a contract-first platform that compiles declared data outcomes into legal, feasible, signed execution graphs and executes them deterministically with attributable evidence.

This repository is a technology-neutral modular monorepo. It currently establishes architecture boundaries and repository governance; runnable product components will be added incrementally.

## Start Here

- [Repository layout](docs/architecture/repository-layout.md)
- [Monorepo decision](docs/architecture/decisions/ADR-0001-monorepo-structure.md)
- [Initial structure design](docs/superpowers/specs/2026-08-12-initial-monorepo-structure-design.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)

## Validate the Repository

Run the offline structural test from any working directory:

```sh
./tests/repository-structure/test.sh
```

No application framework, programming language, package manager, cloud, or deployment topology has been selected yet.
