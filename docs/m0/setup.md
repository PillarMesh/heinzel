# M0 Dedicated Account Setup

M0 uses synthetic, non-sensitive data in disposable PostgreSQL and Snowflake objects. An
environment owner provisions the objects; an acceptance operator never receives owner
credentials. Create two independent operator sets before the witnessed window. Operators share
the dedicated tables, but use distinct PostgreSQL runtime roles, Snowflake users, signing keys,
SQLite files, output directories, and cleanup ledgers.

## Exact variable inventory

`.env.example` is the authoritative, literal 35-name inventory. Each operator supplies every name
below from an external secret manager or ephemeral shell; no committed environment file contains
values. Marker, file-format, owner-role, and reservation names are fixed or derived and are not
additional variables.

- Private local state/output: `PILLARMESH_STATE_PATH`, `PILLARMESH_OUTPUT_DIR`,
  `PILLARMESH_CLEANUP_LEDGER_PATH`.
- Signing: `PILLARMESH_SIGNING_KEY_ID`, `PILLARMESH_SIGNING_PRIVATE_KEY_B64`.
- Runtime PostgreSQL: `PILLARMESH_POSTGRES_DSN`, `PILLARMESH_POSTGRES_DATABASE`,
  `PILLARMESH_POSTGRES_RUNTIME_PRINCIPAL`, `PILLARMESH_POSTGRES_OWNER_PRINCIPAL`,
  `PILLARMESH_POSTGRES_CONNECTION_HANDLE`, `PILLARMESH_POSTGRES_SCHEMA`,
  `PILLARMESH_POSTGRES_TABLE`, `PILLARMESH_POSTGRES_DENIAL_SCHEMA`.
- Fixture-only PostgreSQL: `PILLARMESH_POSTGRES_FIXTURE_DSN`,
  `PILLARMESH_POSTGRES_FIXTURE_PRINCIPAL`.
- Runtime Snowflake: `PILLARMESH_SNOWFLAKE_ACCOUNT`, `PILLARMESH_SNOWFLAKE_USER`,
  `PILLARMESH_SNOWFLAKE_PASSWORD`, `PILLARMESH_SNOWFLAKE_OWNER_USER`,
  `PILLARMESH_SNOWFLAKE_ROLE`, `PILLARMESH_SNOWFLAKE_WAREHOUSE`,
  `PILLARMESH_SNOWFLAKE_DATABASE`, `PILLARMESH_SNOWFLAKE_SCHEMA`,
  `PILLARMESH_SNOWFLAKE_STAGE`, `PILLARMESH_SNOWFLAKE_TARGET_TABLE`,
  `PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE`, `PILLARMESH_SNOWFLAKE_LEDGER_TABLE`,
  `PILLARMESH_SNOWFLAKE_CONNECTION_HANDLE`, `PILLARMESH_SNOWFLAKE_DENIAL_DATABASE`.
- Evidence scan canaries: `PILLARMESH_CREDENTIAL_CANARIES_JSON`,
  `PILLARMESH_ROW_VALUE_CANARY`.
- Operator metadata: `PILLARMESH_OPERATOR_PSEUDONYM`, `PILLARMESH_HOST_PSEUDONYM`,
  `PILLARMESH_MCP_PROTOCOL_VERSION`, `PILLARMESH_OWNER_AUTHORIZATION_REFERENCE`.

`PILLARMESH_OWNER_AUTHORIZATION_REFERENCE` is a non-secret reference to approved cleanup scope.
It is not an owner credential and does not authorize the harness to revoke grants, delete rows, or
remove staged files. Those actions still require an owner to approve the exact ledger entries.

The harness derives one environment identity from the case-normalized canonical JSON of the declared PostgreSQL
database/schema/source and Snowflake account/database/schema/stage/target/negative-target/ledger,
namespaced by `pillarmesh-m0-environment-v1` and SHA-256. It derives the local reservation pathname
from that digest and also holds a PostgreSQL advisory lock for the same digest. Thus operators on
different paths or hosts still contend at the provider boundary. The lock uses a dedicated retained
connection. The harness records that connection's backend PID and fails closed unless the same
database and backend still own exactly one granted advisory lock before each mutation-capable
boundary; it never tests by reacquiring the lock. The fixed derived objects are
`pillarmesh_m0.environment_marker`, `pillarmesh_m0.runtime_source_read_count()`,
`PILLARMESH_M0.TRANSFER.M0_CSV`, `PILLARMESH_M0.TRANSFER.ENVIRONMENT_MARKER`, and owner role
`PILLARMESH_M0_OWNER`.

After the provider object names and account identifier are injected, derive the marker value
offline without opening a provider connection. The command prints only the non-secret digest:

```sh
uv run python - <<'PY'
import os
from tests.acceptance.config import derive_environment_identity

print(derive_environment_identity(os.environ))
PY
```

## PostgreSQL owner setup

Before executing this SQL, connect explicitly to the dedicated database and make the database
assertion pass. Then replace the explicit identifier `M0_ACCEPTANCE_DATABASE` with the same
owner-supplied identifier. Do not infer the target from a prior or default session.

```sql
\connect M0_ACCEPTANCE_DATABASE
SELECT current_database() = 'M0_ACCEPTANCE_DATABASE'
   AND current_user = 'pillarmesh_m0_owner'
   AND (
     SELECT pg_catalog.pg_get_userbyid(d.datdba) = current_user
     FROM pg_catalog.pg_database AS d
     WHERE d.datname = current_database()
   ) AS dedicated_owner_connected;
-- Stop unless dedicated_owner_connected is true.

CREATE SCHEMA pillarmesh_m0;
CREATE SCHEMA unrelated_private;
CREATE TABLE pillarmesh_m0.orders (
    order_id BIGINT PRIMARY KEY,
    customer_ref VARCHAR(65535) NOT NULL,
    amount NUMERIC(18,2) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    status VARCHAR(65535) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE pillarmesh_m0.environment_marker (
    environment_id VARCHAR(64) PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO pillarmesh_m0.environment_marker (environment_id)
VALUES ('<derived-64-hex-environment-identity>');
REVOKE ALL ON DATABASE M0_ACCEPTANCE_DATABASE FROM PUBLIC;
REVOKE ALL ON SCHEMA pillarmesh_m0, unrelated_private FROM PUBLIC;
REVOKE ALL ON TABLE pillarmesh_m0.orders, pillarmesh_m0.environment_marker FROM PUBLIC;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
GRANT pg_read_all_stats TO pillarmesh_m0_owner;
CREATE FUNCTION pillarmesh_m0.runtime_source_read_count()
RETURNS BIGINT LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
  SELECT coalesce(sum(s.calls), 0)::bigint
  FROM public.pg_stat_statements AS s
  JOIN pg_catalog.pg_roles AS r ON r.oid = s.userid
  WHERE pg_catalog.pg_has_role(session_user, r.oid, 'MEMBER')
    AND s.query ILIKE '%pillarmesh_m0%orders%'
    AND s.query ~* '^[[:space:]]*(select|declare)'
$$;
REVOKE ALL ON FUNCTION pillarmesh_m0.runtime_source_read_count() FROM PUBLIC;

CREATE ROLE pillarmesh_m0_runtime_1 LOGIN;
CREATE ROLE pillarmesh_m0_runtime_2 LOGIN;
CREATE ROLE pillarmesh_m0_fixture LOGIN;
GRANT CONNECT ON DATABASE M0_ACCEPTANCE_DATABASE TO
    pillarmesh_m0_runtime_1, pillarmesh_m0_runtime_2, pillarmesh_m0_fixture;
GRANT USAGE ON SCHEMA pillarmesh_m0 TO
    pillarmesh_m0_runtime_1, pillarmesh_m0_runtime_2, pillarmesh_m0_fixture;
GRANT SELECT ON pillarmesh_m0.orders TO pillarmesh_m0_runtime_1, pillarmesh_m0_runtime_2;
GRANT SELECT ON pillarmesh_m0.environment_marker TO
    pillarmesh_m0_runtime_1, pillarmesh_m0_runtime_2;
GRANT EXECUTE ON FUNCTION pillarmesh_m0.runtime_source_read_count() TO
    pillarmesh_m0_runtime_1, pillarmesh_m0_runtime_2;
GRANT INSERT, DELETE ON pillarmesh_m0.orders TO pillarmesh_m0_fixture;
GRANT SELECT (order_id) ON pillarmesh_m0.orders TO pillarmesh_m0_fixture;
```

The environment owner must configure `pg_stat_statements` in `shared_preload_libraries`, restart
before the witnessed window if the extension was newly enabled, and confirm its counters are not
reset during the exclusive run. The owner role needs `pg_read_all_stats` so its SECURITY DEFINER
function can see statements issued by the runtime roles. A `pg_stat_activity` snapshot is not
sufficient: a short-lived read can disappear between samples. The function exposes only the
cumulative call count for data statements executed as the login role or a role it can assume and
referencing the dedicated source; it
exposes no query text or row values. Catalog/marker/grant observations are allowed metadata. The
negative gate fails if this persistent source-data-read count changes.

Preflight requires `current_database()` to match `PILLARMESH_POSTGRES_DATABASE`, both that database
and the dedicated schema to be owned by the declared owner, and the marker row to match the derived
environment identity. Both dedicated tables must be owner-owned base tables. The counter must be an
owner-owned, zero-argument, SQL, `STABLE`, `BIGINT`, SECURITY DEFINER function with the exact fixed
body and `search_path` above; a same-name constant function fails its body-digest check. Runtime,
not fixture, executes the source/marker/audit reads. The fixture session performs only identity and
grant metadata probes, the bounded INSERT/DELETE, and key-column access needed to delete by
`order_id`. Preflight also requires the denial schema to exist before proving the runtime role lacks
`USAGE`; absence is not accepted as denial.

Both PostgreSQL connections must return identical `session_user` and `current_user` values, and
each value must equal its declared non-owner principal. A DSN that assumes another role is rejected
even when that effective role could otherwise pass the grants. The trusted counter includes
statements attributed by `pg_stat_statements` to any role the authenticated runtime login can
assume, so role switching cannot hide source reads.

The runtime roles receive no INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, CREATE, or
MAINTAIN access and no unrelated-schema access. The fixture role is separate from both runtime
roles and the owner. Its exact privilege probe rejects every supported table privilege other than
source INSERT/DELETE and rejects column `SELECT` except on `order_id`. It also rejects table-level
SELECT, MAINTAIN, marker-table privileges, and any unexpected database, schema, column, or function
privilege. PostgreSQL requires `SELECT` on a column referenced by a `DELETE` predicate; without this
key-only grant the fixture principal could insert a row but could not perform its authorized
per-run cleanup.

The harness executes a parameterized positive probe and a parameterized privilege-denial probe:

```python
cursor.execute("SELECT count(*) FROM pillarmesh_m0.orders WHERE order_id = %s", (-1,))
cursor.execute(
    "SELECT has_schema_privilege(current_user, %s, 'USAGE')",
    (os.environ["PILLARMESH_POSTGRES_DENIAL_SCHEMA"],),
)
assert cursor.fetchone()[0] is False
```

## Snowflake owner setup

Create two distinct users separately and grant both the runtime role. The negative target is
intentionally identical except for a UNIQUE rather than PRIMARY KEY constraint, making only
legality precondition 6 unsupported. Runtime code performs no DDL.

```sql
USE ROLE SECURITYADMIN;
CREATE ROLE PILLARMESH_M0_OWNER;
CREATE ROLE PILLARMESH_M0_RUNTIME;
GRANT ROLE PILLARMESH_M0_OWNER TO USER <PILLARMESH_M0_OWNER_USER>;
GRANT ROLE PILLARMESH_M0_RUNTIME TO USER <OPERATOR_ONE_USER>;
GRANT ROLE PILLARMESH_M0_RUNTIME TO USER <OPERATOR_TWO_USER>;
USE ROLE ACCOUNTADMIN;
GRANT CREATE DATABASE ON ACCOUNT TO ROLE PILLARMESH_M0_OWNER;
GRANT CREATE WAREHOUSE ON ACCOUNT TO ROLE PILLARMESH_M0_OWNER;
USE ROLE PILLARMESH_M0_OWNER;
CREATE DATABASE PILLARMESH_M0;
CREATE SCHEMA PILLARMESH_M0.TRANSFER;
CREATE WAREHOUSE PILLARMESH_M0_WH WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60;
CREATE FILE FORMAT PILLARMESH_M0.TRANSFER.M0_CSV
  TYPE = CSV SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '"'
  EMPTY_FIELD_AS_NULL = FALSE;
CREATE STAGE PILLARMESH_M0.TRANSFER.M0_STAGE
  FILE_FORMAT = PILLARMESH_M0.TRANSFER.M0_CSV;
CREATE TABLE PILLARMESH_M0.TRANSFER.ORDERS (
  order_id NUMBER(19,0) NOT NULL,
  customer_ref VARCHAR(65535) NOT NULL,
  amount NUMBER(18,2) NOT NULL,
  currency VARCHAR(3) NOT NULL,
  order_status VARCHAR(65535) NOT NULL,
  updated_at TIMESTAMP_TZ(6) NOT NULL,
  PRIMARY KEY (order_id)
);
CREATE TABLE PILLARMESH_M0.TRANSFER.ORDERS_UNSUPPORTED_KEY (
  order_id NUMBER(19,0) NOT NULL,
  customer_ref VARCHAR(65535) NOT NULL,
  amount NUMBER(18,2) NOT NULL,
  currency VARCHAR(3) NOT NULL,
  order_status VARCHAR(65535) NOT NULL,
  updated_at TIMESTAMP_TZ(6) NOT NULL,
  UNIQUE (order_id)
);
CREATE TABLE PILLARMESH_M0.TRANSFER.COMMIT_LEDGER (
  batch_id VARCHAR NOT NULL,
  manifest_digest VARCHAR(64) NOT NULL,
  committed_at TIMESTAMP_TZ NOT NULL,
  PRIMARY KEY (batch_id)
);
CREATE TABLE PILLARMESH_M0.TRANSFER.ENVIRONMENT_MARKER (
  environment_identity VARCHAR(64) NOT NULL,
  owner_user VARCHAR NOT NULL,
  owner_role VARCHAR NOT NULL,
  denial_database VARCHAR NOT NULL,
  denial_database_owner_role VARCHAR NOT NULL,
  PRIMARY KEY (environment_identity)
);
-- A separate owner-created denial database must exist. Absence is not a denial proof.
CREATE DATABASE UNRELATED_PRIVATE;
SHOW GRANTS ON DATABASE UNRELATED_PRIVATE;
-- Stop unless the result contains exactly one OWNERSHIP grant to PILLARMESH_M0_OWNER.
INSERT INTO PILLARMESH_M0.TRANSFER.ENVIRONMENT_MARKER
  (environment_identity, owner_user, owner_role, denial_database, denial_database_owner_role)
VALUES
  ('<derived-64-hex-environment-identity>', '<PILLARMESH_M0_OWNER_USER>',
   'PILLARMESH_M0_OWNER', 'UNRELATED_PRIVATE', 'PILLARMESH_M0_OWNER');

GRANT USAGE ON DATABASE PILLARMESH_M0 TO ROLE PILLARMESH_M0_RUNTIME;
GRANT USAGE ON SCHEMA PILLARMESH_M0.TRANSFER TO ROLE PILLARMESH_M0_RUNTIME;
GRANT USAGE ON WAREHOUSE PILLARMESH_M0_WH TO ROLE PILLARMESH_M0_RUNTIME;
GRANT READ, WRITE ON STAGE PILLARMESH_M0.TRANSFER.M0_STAGE TO ROLE PILLARMESH_M0_RUNTIME;
GRANT SELECT, INSERT, UPDATE ON TABLE PILLARMESH_M0.TRANSFER.ORDERS
  TO ROLE PILLARMESH_M0_RUNTIME;
GRANT SELECT ON TABLE PILLARMESH_M0.TRANSFER.ORDERS_UNSUPPORTED_KEY
  TO ROLE PILLARMESH_M0_RUNTIME;
GRANT SELECT, INSERT ON TABLE PILLARMESH_M0.TRANSFER.COMMIT_LEDGER
  TO ROLE PILLARMESH_M0_RUNTIME;
GRANT SELECT ON TABLE PILLARMESH_M0.TRANSFER.ENVIRONMENT_MARKER
  TO ROLE PILLARMESH_M0_RUNTIME;
```

Run `SHOW GRANTS TO USER <PILLARMESH_M0_OWNER_USER>` as the owner/security administrator and stop
unless it shows `PILLARMESH_M0_OWNER`. Transfer ownership of the dedicated database, schema,
warehouse, file format, stage, target tables, commit ledger, environment marker, and denial database
to `PILLARMESH_M0_OWNER` before granting runtime access. Insert the marker only after those owner
checks. Because the marker table is owner-owned and runtime has SELECT only, its exact owner-user,
owner-role, denial-database ownership assertion, and environment-identity row is the owner-created
attestation consumed by preflight.

Preflight checks `CURRENT_ACCOUNT_NAME()` and the stable account locator returned by
`CURRENT_ACCOUNT()`, accepting `PILLARMESH_SNOWFLAKE_ACCOUNT` only when it matches one of them. It
also checks `CURRENT_ROLE()`, the runtime user's sole explicit role, each fixed object kind, exact
ownership/grants, and the owner-created marker. Runtime must retain query-history visibility for
its own `pillarmesh-m0` tagged statements so replay and negative gates compare persistent tagged
stage/target/ledger mutation and data-query counters. Harness observations use the distinct
`pillarmesh-m0-acceptance-observer` tag. Only the three exact compiler metadata query shapes for
table type, columns, and key constraints are allowlisted; a broad `INFORMATION_SCHEMA` query is a
forbidden product data query.

The positive visibility query binds the synthetic key, and the denial probe binds the fully
qualified object through `IDENTIFIER(%s)`:

```python
cursor.execute(
    "SELECT count(*) FROM PILLARMESH_M0.TRANSFER.ORDERS WHERE order_id = %s",
    (acceptance_key,),
)
cursor.execute("SELECT count(*) FROM IDENTIFIER(%s)", (denied_catalog_object,))
```

The second statement must fail with SQLSTATE `42501` for the already owner-verified
`PILLARMESH_SNOWFLAKE_DENIAL_DATABASE`. Any other error is inconclusive. Verify the role also cannot
create objects, alter grants, administer the account, or query any unrelated database.

## Signing and two operator shells

Generate a separate 32-byte Ed25519 key for each operator outside the repository and inject its
Base64 raw private bytes through the secret manager. The key value, DSNs, passwords, scan
canaries, and row canary are never command arguments.

Use different local values in the two shells:

```sh
# Operator 1
export PILLARMESH_OPERATOR_PSEUDONYM=operator-one
export PILLARMESH_STATE_PATH=/absolute/private/operator-one/state.db
export PILLARMESH_OUTPUT_DIR=/absolute/private/operator-one/output
export PILLARMESH_CLEANUP_LEDGER_PATH=/absolute/private/operator-one/cleanup-ledger.json
export PILLARMESH_LIVE_DIAGNOSTIC_LEDGER_DIR=/absolute/private/operator-one/live-ledgers
export PILLARMESH_SIGNING_KEY_ID=operator-one-key

# Operator 2, in a clean checkout and an independent secret-injection session
export PILLARMESH_OPERATOR_PSEUDONYM=operator-two
export PILLARMESH_STATE_PATH=/absolute/private/operator-two/state.db
export PILLARMESH_OUTPUT_DIR=/absolute/private/operator-two/output
export PILLARMESH_CLEANUP_LEDGER_PATH=/absolute/private/operator-two/cleanup-ledger.json
export PILLARMESH_LIVE_DIAGNOSTIC_LEDGER_DIR=/absolute/private/operator-two/live-ledgers
export PILLARMESH_SIGNING_KEY_ID=operator-two-key
```

The PostgreSQL runtime principal, Snowflake user, signing key, state path, output path, and cleanup
ledger must differ between operators. The harness derives a common environment identity from the
provider boundary; its local reservation and PostgreSQL advisory lock serialize every acceptance
run against that environment without another supplied variable. Legacy live
diagnostics do not satisfy the gate and must not be launched during a reserved witnessed window.
Operator 2 is a person or independently accountable
reviewer who did not author this runbook; another process controlled by Operator 1 is not gate 14.

Create each private parent in advance with mode `0700`; none may be a symlink. The state file,
output directory, and cleanup ledger must not already exist. `run` takes an exclusive reservation
over their identities, creates files with owner-only permissions, and fails if a parent or output
path is replaced. The live-diagnostic ledger directory is not part of product configuration; it is
required only by opt-in legacy diagnostics so their exact resource ledgers survive pytest cleanup.

Run `uv run python tests/acceptance/run_m0.py preflight` only after the entire inventory is
injected. Preflight aggregates missing names without values, refuses repository-local or reused
paths and any provider object outside the exact dedicated boundary before connecting, executes the
positive/denial probes, and rejects any runtime credential that resolves to a fixture or owner
principal.
