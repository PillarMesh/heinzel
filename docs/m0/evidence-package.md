# M0 Evidence Package

`uv run python tests/acceptance/run_m0.py run` exports one ignored package beneath the operator's
external `PILLARMESH_OUTPUT_DIR`, then independently verifies it. Re-run the deterministic verifier
with:

```sh
uv run python tests/acceptance/run_m0.py verify
```

The package includes canonical contract, observation, compiler, legality, graph, source-boundary,
manifest, commit, visibility, and event-trace artifacts; commit and lock digests; sanitized
operator/host pseudonyms; the CLI transport decision; limitations; and resource dispositions.
`operations/resources.json` contains only resource digests, creation state, retention deadline,
cleanup status, and cleanup operation.

The exact PostgreSQL row key, Snowflake target/ledger/stage identifiers, acceptance key, local
paths, owner authorization reference, and explicit declared/observed provider-attestation record
remain only in `PILLARMESH_CLEANUP_LEDGER_PATH`. The attestation record allowlists identifiers,
ownership, grants, marker results, and audit-definition findings; it never serializes the complete
environment and cannot contain DSNs, passwords, private signing keys, credential canaries, or row
values. That owner-readable file must be outside the repository with mode 0600. Never share it as
evidence.

The exporter scans artifacts, trace, operations metadata, package index, and final verification
result for all credential canaries, the synthetic row-value canary, raw acceptance key and common
encodings, connection-string schemes, private-key markers, and local path prefixes. A finding
fails export and leaves no completed package. Independent verification requires the same canaries
from the secret-injection session and resolves every artifact parent and event-chain link.

Retention is exact:

- successful package, local state, synthetic PostgreSQL row, Snowflake target row, and commit
  ledger entry: 30 days;
- successful staged segment: remove by its exact batch prefix within 24 hours;
- failed or indeterminate staged segment: quarantine for at most seven days;
- failed fixture row before a recorded `commit_attempted` event: eligible for immediate authorized
  cleanup only when provider reconciliation also positively proves both target-row and
  commit-ledger effects absent;
- fixture row with a positive or indeterminate target/ledger effect, missing batch identity,
  unavailable event stream, or failed reconciliation: retain for 30 days;
- unneeded local segment: eligible for immediate authorized cleanup.

The private ledger is written even when `run` fails. `cleanup-status` is a non-destructive view of
its opaque digests. Cleanup and fresh absence confirmation follow `teardown.md`. A witnessed
package may outlive 30 days only after the final secret and row-value scan passes.
