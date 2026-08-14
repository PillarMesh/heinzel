# M0 LocalStack Snowflake Smoke Test

This opt-in test runs the real PillarMesh Snowflake provider against a local LocalStack Snowflake
emulator. It exercises schema observation, staged CSV upload, transactional merge and commit-ledger
write, independent visibility verification, and idempotent replay. It does not change production
provider configuration or the 35-variable M0 acceptance contract.

## Prerequisites

- Docker with Compose v2.
- A LocalStack auth token with a Snowflake emulator trial or license.
- The locked Python workspace installed with `uv`.

Keep `LOCALSTACK_AUTH_TOKEN` in the shell or an external secret manager. Do not put it in an env
file in this repository, a command argument, test output, or evidence package.

## Run

```sh
export LOCALSTACK_AUTH_TOKEN=<secret-from-localstack>
tests/emulators/localstack-snowflake/run.sh
unset LOCALSTACK_AUTH_TOKEN
```

The launcher fails before invoking Docker when the token is absent. It starts the pinned
`localstack/snowflake:2026.06.0` image, binds the emulator only to `127.0.0.1:4566`, mounts the SQL
initialization hook read-only, and waits until the real provider observes the complete expected
schema before running the smoke test. Each invocation owns a unique Compose project, so it cannot
tear down another invocation; the fixed loopback port makes a concurrent second run fail closed.
The launcher removes only its own container and named volume on exit. It does not mount the Docker
socket, and it removes `LOCALSTACK_AUTH_TOKEN` from the readiness and pytest child environments.

LocalStack 2026.06 implements `INFORMATION_SCHEMA.TABLES` and `COLUMNS`, but not the
`TABLE_CONSTRAINTS` and `KEY_COLUMN_USAGE` views used by the production provider. The emulator-only
test adapter also checks `GET_DDL('TABLE', ...)`, but this pinned emulator does not preserve the
fixture's declared primary keys in returned DDL. The observation therefore records key metadata as
unavailable (`key_name = null`, `key_constraint = none`) rather than synthesizing legal evidence.
Production code and real-account observation are unchanged; this compatibility path exists only
under `tests/emulators`.

For diagnosis, run the same steps manually and inspect logs before teardown:

```sh
project="pillarmesh-m0-localstack-manual-$$"
docker compose --project-name "$project" \
  -f tests/emulators/localstack-snowflake/compose.yaml up --detach --wait
env -u LOCALSTACK_AUTH_TOKEN -u VIRTUAL_ENV \
  uv run python tests/emulators/localstack-snowflake/wait_ready.py
env -u LOCALSTACK_AUTH_TOKEN -u VIRTUAL_ENV PILLARMESH_LOCALSTACK_SNOWFLAKE=1 \
  uv run pytest -m emulator tests/emulators/test_localstack_snowflake.py -q
docker compose --project-name "$project" \
  -f tests/emulators/localstack-snowflake/compose.yaml logs snowflake
docker compose --project-name "$project" \
  -f tests/emulators/localstack-snowflake/compose.yaml \
  down --volumes --remove-orphans
```

## Evidence boundary

This is development evidence, not M0 live acceptance evidence. LocalStack mocks authentication and
does not provide the exact role-grant, permission-denial, or query-history behavior required by the
witnessed gate. A passing emulator test therefore must not be reported as proof of:

- real Snowflake account, user, role, ownership, or least-privilege behavior;
- live `GRANT`/`REVOKE` and SQLSTATE `42501` permission faults;
- production query-history visibility or latency;
- production `INFORMATION_SCHEMA` key-constraint observation;
- cross-provider PostgreSQL-to-Snowflake execution;
- independent two-operator execution; or
- any of the fourteen real-account acceptance gates.

The authoritative live procedures remain `setup.md`, `acceptance-run.md`, and `teardown.md`.
