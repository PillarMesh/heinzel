# Plan 3B offline teardown

Plan 3B acceptance creates only process-local in-memory SQLite databases. Normal process exit is
the complete teardown: there are no containers, volumes, catalog objects, warehouse resources,
credentials, grants, files, or external messages to delete.

After an interrupted or failed run:

1. confirm the acceptance process has exited;
2. discard its incomplete standard output;
3. do not treat an earlier digest as evidence for the interrupted run; and
4. start a fresh acceptance command from the repository root.

Do not run Docker cleanup, delete provider resources, revoke grants, remove credentials, or alter
warehouse state for Plan 3B. This harness never creates those effects. If external resources are
present, they came from another workflow and must be handled only by that workflow's exact
teardown authority.
