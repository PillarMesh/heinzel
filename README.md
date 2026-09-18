# Heinzel

Heinzel is an open-source, governed data engineering platform. Business users ask for the data they
need; a data architect reviews and approves the request; Heinzel acquires the source data, lands it
in a warehouse it manages, and builds a governed data product, with each step recorded as
verifiable evidence.

The name comes from the Heinzelmännchen of Cologne, who by legend finished the town's work
overnight.

> **Status:** early and under active development. Some capabilities are complete, others partial,
> and one request cannot yet travel all the way to a published data product. Read
> [docs/status.md](docs/status.md) before relying on any of them.

## What it does

- **Request to product.** Requests are clarified, typed into a product intent, and approved by an
  architect before anything runs.
- **Contract-first execution.** Acquisition runs only under an activated contract bound to an
  approved intent.
- **Legal plans only.** The compiler emits only plans a legality rule admits, or refuses with a
  reason (`No Valid Plan`).
- **Evidence by default.** Runs record hash-chained evidence, and execution graphs, governed query
  plans and compiled models are signed.
- **Governed access.** Access is proposed, approved, time-bound and revocable.

Warehouse engines: PostgreSQL today; ClickHouse partially. Catalog publication uses OpenMetadata and
dashboards use Apache Superset.

## Getting started

You need Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/PillarMesh/heinzel.git
cd heinzel
uv sync --all-packages
uv run pytest -m "not live" -q
```

The offline suite needs no network, Docker or credentials. The console in `apps/console` has its
own [README](apps/console/README.md).

A Docker Compose quickstart is planned.

## Live tests

Tests marked `live` exercise real engines and services, and are opt-in. They need Docker and pull
pinned, digest-addressed images; some also need local PostgreSQL binaries (`initdb`, or set
`HEINZEL_TEST_POSTGRES_BIN_DIR`). A test whose switch or credentials are missing skips itself.

| Switch or credential | Enables |
| --- | --- |
| `HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE=1` | PostgreSQL acquisition, LAND, leased-run resume, the compiled product journey and product SQL conformance on pinned PostgreSQL and ClickHouse images |
| `HEINZEL_RUN_ACCESS_EMULATORS=1` | Access grants on PostgreSQL and ClickHouse |
| `HEINZEL_RUN_DESTINATION_EMULATORS=1` | PostgreSQL and ClickHouse destination providers and the ClickHouse dbt adapter |
| `HEINZEL_OPENMETADATA_SECRET_STORE_KEY`, `HEINZEL_OPENMETADATA_BOOTSTRAP_ADMIN_PASSWORD`, `HEINZEL_OPENMETADATA_EMULATOR=1` | OpenMetadata catalog tests, after starting `tests/emulators/openmetadata/run.sh` |
| `LOCALSTACK_AUTH_TOKEN` | The LocalStack Snowflake emulator, run by `tests/emulators/localstack-snowflake/run.sh` |
| `HEINZEL_TEST_SNOWFLAKE_*`, `HEINZEL_SNOWFLAKE_*` and related settings | A real Snowflake account for the PostgreSQL-to-Snowflake snapshot; each test's skip message names what is missing |

For example:

```bash
HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE=1 uv run pytest -m live -q \
  tests/integration/test_postgresql_composed_acquisition_live.py
```

The warehouse lifecycle acceptance run provisions PostgreSQL and ClickHouse in Docker; see
`.github/workflows/warehouse-lifecycle.yml` for the environment it needs.

## Documentation

- [Capability status](docs/status.md)
- [Architecture](docs/architecture.md)
- [Repository layout](docs/architecture/repository-layout.md)
- [Design decisions](docs/architecture/decisions/)
- [Legality rules](services/compiler/legality/README.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md) · [Engineering rules](AGENTS.md) · [Security](SECURITY.md) ·
  [Code of Conduct](CODE_OF_CONDUCT.md)

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Heinzel is a product of PillarMesh ([pillarmesh.com](https://pillarmesh.com)).
