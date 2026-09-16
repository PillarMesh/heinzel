# PostgreSQL product semantic observation local acceptance

This acceptance test creates a new PostgreSQL cluster on a random loopback port and provisions a
dedicated read-only product observer. It records an owned product-generation row and current pointer,
then observes the actual PostgreSQL execution context and retained physical relation through the
concrete provider.

## Prerequisites

- Python and `uv` versions pinned by this repository.
- PostgreSQL `initdb` and `pg_ctl` from one installation on `PATH`, or their directory supplied in
  `PILLARMESH_TEST_POSTGRES_BIN_DIR`.
- A non-root operating-system account. PostgreSQL refuses to initialize a cluster as root.

The test generates a new database password for every run. It uses an owner-only temporary password
file through the shared native-cluster helper and never writes credentials into source, test output,
or observation models.

## Run a fresh transaction

From the repository root:

```sh
PILLARMESH_TEST_POSTGRES_BIN_DIR=/opt/homebrew/bin \
  uv run pytest -m live tests/integration/test_postgresql_product_observation_live.py -q
```

A passing run proves that the provider:

1. uses an authenticated role with only `USAGE` and `SELECT` on the generation ledgers and exact
   product relation;
2. begins a repeatable-read, read-only transaction;
3. matches tenant, product revision, generation, and the expected provider commit reference from
   the owning durable materialization receipt against the generation row and current pointer;
4. reads the real `server_version_num` and server version, derives the engine-build digest with the
   same canonical domain and version payload as PostgreSQL warehouse validation, and observes the
   database, session timezone, database locale, relation and schema OIDs, and file node;
5. reads ordered physical types, nullability, column collation metadata, and relation-bound primary
   or unique constraints with their ordered physical key columns;
6. emits a constraint as eligible only when PostgreSQL reports a validated, nondeferrable primary
   or unique constraint backed by a valid, ready, unique, nonpartial, nonexpression index and every
   key column is observed non-null; the live fixture proves exclusion of its deferrable constraint
   and standalone, partial, and expression unique indexes, while focused boundary tests cover
   nullable and unvalidated constraints and malformed column bindings;
7. recomputes the existing generation commit reference from the receipt's exact plan digest and the
   currently observed relation OID and file node, rejecting a same-name replacement;
8. holds a target-specific session lock from the absence check through dbt and output inspection,
   and rejects a retained or pre-existing materialization target before dbt can run;
9. locks and reinspects the exact physical relation during publication, rejecting replacement
   between the materialization observation and stable-view switch;
10. serializes publication for the tenant and product, preserves the original retention on an exact
   replay, and rejects a delayed older-generation replay before it can repoint the stable view; and
11. rejects a stale observation pointer or revoked relation access, then stops the exact temporary
   cluster after the test.

The immutable schema-version-2 observation reports provider facts only. It does not classify these
measurements as cross-engine semantic capabilities and does not make restricted product SQL
admissible. No ledger migration is needed for relation continuity:
the existing commit reference already binds the plan digest and commit-time relation identity. The
observer accepts those values only through the owning materialization receipt boundary and recomputes
the reference from the current physical identity.

This test makes no ClickHouse, production connectivity, or compiler admission claim. A skip is a
live-evidence gap and must not be reported as a pass.
