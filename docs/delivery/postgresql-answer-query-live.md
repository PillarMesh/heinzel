# PostgreSQL governed-answer local acceptance

This acceptance test creates a new PostgreSQL cluster bound only to a random loopback port. It
compiles a signed governed query from strict compiler inputs, executes it through the concrete
`answer_runtime` PostgreSQL provider and governed runtime, and reads the resulting durable receipt
and result snapshot. It does not use an existing database, Docker, shared credentials, or a
production service.

## Prerequisites

- Python and `uv` versions pinned by this repository.
- PostgreSQL `initdb` and `pg_ctl` binaries from one installation available on `PATH`, or their
  directory configured through `HEINZEL_TEST_POSTGRES_BIN_DIR`.
- A non-root operating-system account. PostgreSQL refuses to initialize a cluster as root.

The test generates new bootstrap, runtime, and signing secrets in memory for every run. PostgreSQL
receives its bootstrap password through an owner-only temporary file. Credentials and query text
are absent from command arguments, pytest output, and repository artifacts.

## Run a fresh transaction

From the repository root:

```sh
uv sync --locked --all-packages
uv run pytest -m live tests/integration/test_postgresql_answer_query_live.py -q
```

When Homebrew PostgreSQL is installed outside `PATH`, use:

```sh
HEINZEL_TEST_POSTGRES_BIN_DIR=/opt/homebrew/bin \
  uv run pytest -m live tests/integration/test_postgresql_answer_query_live.py -q
```

A passing run proves all of the following in one fresh transaction:

1. `initdb` creates a new cluster and `pg_ctl` exposes it only on `127.0.0.1` and a reserved random
   port.
2. Setup creates a dedicated `answer_runtime` login with `USAGE` and `SELECT` only on the governed
   consumption object.
3. The real compiler produces and Ed25519-signs a deterministic PostgreSQL plan with bound
   suppression parameters, and the SQLite plan repository reloads the durable artifact before use.
4. `GovernedQueryExecutor` verifies the reloaded plan's signature and generation, executes through
   `PostgreSQLAnswerQueryProvider`, and stores one successful receipt and typed result snapshot.
5. Exact replay returns the identical receipt without recording another attempt.
6. The runtime login cannot write the governed object, read an ungranted private object, or create
   an administrative role.
7. Cleanup stops the exact temporary cluster even when an assertion or provider call fails, and a
   post-cleanup connection attempt is denied before the temporary directory is removed.

The test skips when the PostgreSQL binaries are unavailable or it is running as root. A skip is a
live-evidence gap and must not be reported as a pass. This local PostgreSQL result does not establish
ClickHouse compatibility, production connectivity, external authorization behavior, or delivery to
a requester.
