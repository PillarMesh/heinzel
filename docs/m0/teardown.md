# M0 Cleanup and Teardown

> **Historical.** This document records the PostgreSQL-to-Snowflake M0 thin-thread
> experiment. It remains accurate about what was built and is retained so that evidence
> stays reproducible. Snowflake is no longer a product destination, and nothing here
> defines current product scope. See the
> [managed data engineering platform addendum](../architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md).

Cleanup is owner-authorized and operates only on exact identifiers already present in the private
cleanup ledger. The harness deliberately exposes no cleanup command. Start with the read-only,
sanitized view:

```sh
uv run python tests/acceptance/run_m0.py cleanup-status
```

`cleanup-status` accepts only an absolute, non-symlink ledger outside the repository, owned by the
current user with mode `0600`, beneath an owner-private `0700` directory. A path or permission
mismatch fails closed before parsing.

A normally exiting command removes its exact reservation file. After a killed process, treat a
remaining reservation as active: both operators must first confirm that no acceptance or legacy
diagnostic process is running and the environment owner must confirm the provider window is idle.
Only then may the current user remove that one owner-owned, regular `0600` reservation file. Never
replace it with a symlink or clear it merely because its timestamp looks old.

Confirm the owner authorization reference, retention deadline, creation state, and exact private
identifier for each selected digest outside recorded terminals. Never infer a target from a
schema-wide pattern or act on a shared object.

Authorized cleanup tooling must acquire the same environment-derived PostgreSQL advisory lock on a
dedicated retained connection before touching a provider. It must record the backend PID and, just
before the first cleanup mutation, query that same session to prove the expected database and
exactly one granted advisory lock remain. A lost session stops cleanup; tooling must not reacquire
mid-operation. This repository intentionally provides no mutation-capable cleanup command, so do
not substitute an unlocked sequence of copied SQL for that tooling.

## Per-run cleanup schedule

1. Within 24 hours of a successful run, remove only the exact Snowflake stage prefix recorded for
   that batch. Failed or indeterminate staged files remain quarantined for at most seven days.
2. At 30 days, delete the recorded synthetic source row with the fixture credential and the exact
   recorded target row and commit-ledger entry with owner-authorized cleanup credentials.
3. Delete the recorded local SQLite file, output directory, and package through a recoverable
   mechanism where available. Retain a package longer only after its final sensitive-value scan.
4. Update each private-ledger disposition to `completed` only after a fresh absence query succeeds.

Delete the source row under the fixture principal, whose only read authority is column-level SELECT
on `order_id`. Confirm absence independently under the runtime read-only principal. All statements
bind values; no raw key belongs in a command argument or transcript:

```python
fixture_postgres_cursor.execute(
    "DELETE FROM heinzel_m0.orders WHERE order_id = %s",
    (acceptance_key,),
)
runtime_postgres_cursor.execute(
    "SELECT count(*) FROM heinzel_m0.orders WHERE order_id = %s",
    (acceptance_key,),
)
snowflake_cursor.execute(
    "SELECT count(*) FROM HEINZEL_M0.TRANSFER.ORDERS WHERE order_id = %s",
    (acceptance_key,),
)
snowflake_cursor.execute(
    "SELECT count(*) FROM HEINZEL_M0.TRANSFER.COMMIT_LEDGER WHERE batch_id = %s",
    (batch_id,),
)
```

Every count must be zero. A successful DELETE, REMOVE, or local filesystem operation without these
fresh queries is not cleanup proof. A cleanup failure records the digest, due date, and failure
classification and prevents a cleanup-complete claim.

## Full environment teardown

Dropping stages, formats, tables, schemas, warehouses, roles, users, or the database is separate
from per-run cleanup and requires explicit owner authorization for the resolved dedicated
environment. Stop all acceptance activity, verify no retained run still depends on the objects,
then remove grants and principals. Destroy a private signing key only after deciding whether
historical graph verification must remain possible; retained evidence needs only the public key.

Never run full teardown against a shared schema, table, role, warehouse, stage, or database.
