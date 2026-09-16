# Request-to-product acceptance setup

This runbook prepares the current request-first acceptance gate for Task 16. The gate is offline and
fail closed: it begins with an empty product-publication authority, records a new titled request and
its exact approved product intent in request management, and asks the compiler to evaluate a narrow
PostgreSQL product IIR. It does not configure a source, warehouse, catalog, Superset, or credentials.

The complete live Task 16 setup is not delivered. In particular, the repository does not yet have
one authority that creates the dedicated source, runtime, product-reader, Superset, and test-user
roles and binds them to a single request-to-product run. Do not assemble a claimed live run by
combining unrelated fixtures or existing provider rows.

## Prerequisites

- Use the Python version pinned by `.python-version` and `pyproject.toml`.
- Run from the repository root with the locked `uv` workspace.
- Use a checkout whose commit and changes have been recorded for the review.
- Use a new owner-only directory outside the repository for pytest's disposable SQLite authorities.
  Do not reuse a product-publication database.

Prepare the locked environment and record the source revision:

```sh
set -eu
uv sync --locked --all-packages
uv lock --check
git rev-parse HEAD
git status --short

export PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT=/absolute/private/request-to-product
case "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT" in
  /*) ;;
  *) exit 1 ;;
esac
case "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT" in
  "$PWD"|"$PWD"/*) exit 1 ;;
esac
mkdir -m 0700 "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT"
test "$(stat -f '%Lp' "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT")" = 700
test -z "$(find "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT" -mindepth 1 -print -quit)"
touch "$PILLARMESH_REQUEST_PRODUCT_PRIVATE_ROOT/.pillarmesh-request-product-run"
```

Replace the example path with a new location owned by the current operator. The absolute-path,
outside-repository, empty-directory, and marker checks prevent pytest or teardown from acting on an
earlier run's files.

No secret belongs in a command argument, a checked-in file, test output, or acceptance evidence. The
current offline gate needs no secret. Future live setup must inject credentials through the owning
provider's approved environment or secret-store boundary and must add exact-resource cleanup before
it can become an acceptance authority.

## Confirm the implemented scope

The files below are the current executable gate:

```sh
test -f tests/acceptance/run_request_to_product.py
test -f tests/acceptance/test_run_request_to_product.py
```

The following planned composed live suites are currently unavailable:

```text
tests/integration/test_governed_answer_live.py
tests/integration/test_request_to_product_postgresql_live.py
tests/integration/test_request_to_product_clickhouse_live.py
tests/fault-injection/test_request_to_product_recovery.py
```

`tests/integration/test_request_to_dashboard_live.py` exists, but it proves the narrower dashboard
access lifecycle. It does not start from an empty product authority and therefore cannot close Task
16 by itself. Existing component live tests for PostgreSQL materialization, Superset, warehouse
lifecycle, and native answers remain evidence for their named boundaries only.

Continue with [the acceptance run](acceptance-run.md).
