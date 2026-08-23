# Plan 2 exact teardown

Teardown is part of witnessed acceptance. A successful catalog read, evidence export, or backup
does not permit broad cleanup. Retain the private resource ledger until every recorded resource is
independently absent.

## Rules

- Resolve targets only from `PILLARMESH_PLAN2_CLEANUP_LEDGER_PATH`.
- Refuse `*`, prefixes, inferred Docker project names, unrecorded provider IDs, and paths outside
  the run's private parent.
- Delete in dependency order: restored target, published catalog objects, tenant identities and
  namespaces, backup artifact, containers, network, volumes, encrypted operation secrets, private
  SQLite files and journals, then the replay directory.
- Record each attempt and terminal result. A transient or unknown result remains incomplete.
- Never use `docker system prune`, broad Compose project discovery, or an OpenMetadata bulk delete.

The private ledger is authenticated deletion authority, not a target list to trust
blindly. Before acting, the runner validates every kind/identifier digest, the exact
run scope, singleton ownership constraints, and the whole-ledger HMAC. A modified,
foreign, duplicated, wildcard, or unsupported entry fails closed.

The runner records a publication-operation claim before the first provider effect. If
publication is interrupted before exact provider identifiers are persisted, recovery
loads the immutable intent through the publication repository, replays the idempotent
publication to convergence, records the discovered exact identifiers, and deletes only
those identifiers. It never queries a private SQLite table directly or infers provider
objects from a naming prefix.

The normal witnessed runner performs cleanup in `finally`. If the run is interrupted, use the same
private paths and invoke its recovery command:

```sh
tests/emulators/openmetadata/run.sh \
  python -m tests.acceptance.run_plan2 cleanup
```

Then inspect the sanitized status only:

```sh
tests/emulators/openmetadata/run.sh \
  python -m tests.acceptance.run_plan2 cleanup-status
```

The status must report `complete` for every ledger entry and
`zero_residual_resources: true`. Status performs fresh Docker discovery for the exact
recorded source and restore projects and fresh filesystem checks for private state,
secret, backup, and evidence-staging paths; it does not accept terminal ledger flags
alone. Provider identifiers and local paths must not be printed.

Catalog objects are deleted and checked for exact absence while the scoped provider
administrator still exists. Only after that terminal provider check may the operation
credentials and private control state be retired. The authenticated terminal ledger
retains that result because a later status command deliberately cannot recreate or
reuse the retired credential merely to issue an unauthenticated provider probe.

If the ledger is missing, unreadable, or does not identify an exact target, stop. Do not recreate
identifiers from naming conventions. Preserve the environment for owner-authorized recovery.

After complete absence is verified, retain the atomically published immutable public
evidence package as required. Do not recursively remove the run parent while that
file remains beneath it. The owner may separately retire the private ledger and empty
directories under the organization's approved procedure, or move the exact immutable
evidence bytes into an approved evidence store before removing the parent. The package
contains only the cleanup-result digest, not the ledger or its identifiers.
