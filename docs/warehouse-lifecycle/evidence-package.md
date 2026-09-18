# Plan 3A evidence package

The public package is the single canonical JSON file `warehouse-lifecycle-evidence.json`. It is immutable after
publication and contains no credentials, endpoints, filesystem paths, tenant identifiers, canary
rows, TLS private keys, provider handles, or raw diagnostics.

## Public fields

- `schema_version`, `run_id`, and the full `source_commit`
- ordered PostgreSQL and ClickHouse engine results
- the canonical offline control-plane fault-matrix artifact with exactly 64 sanitized outcomes
- cross-engine conformance, tenant-isolation, failure-matrix, privacy-scan, and cleanup digests
- start and completion timestamps
- the explicit local-encryption, production-readiness, and lifecycle-conformance dispositions

An engine result contains only public artifact digests, the terminal state, pinned image and
observed engine version, named check dispositions, duration, and residual-resource count. Digests
are correlation and integrity values; they do not reveal private provider identities.

The fault-matrix artifact retains 32 outcomes for each engine: 20 actual lifecycle-checkpoint
occurrences and 12 authoritative provider-classification branches. Each outcome contains only its
engine, stable scenario/checkpoint or operation/classification, expected and observed state,
replay disposition, deterministic session-generation proof, sanitized effect/resource/operation
counts, cleanup-proof digest, and disposition. It contains no timestamp, tenant, resource or
operation identifier, endpoint, path, provider handle, nonce, or raw diagnostic. Strict package
validation revalidates the exact canonical outcome set and recomputes `failure_matrix_digest`.

## Verify the package

Run the same strict validation used by CI before publishing the package:

```sh
uv run python -m tests.acceptance.run_warehouse_lifecycle validate-evidence
```

This reloads `WarehouseLifecycleEvidence`, requires canonical encoding, recomputes the retained fault-matrix
digest and invariants, and rescans the public bytes for configured private markers. Before accepting
the file, validation authenticates the signed reservation and encrypted HMAC-protected private
ledger, requires the ledger's expected public-evidence digest, and compares the exact canonical file,
run identity, and cleanup digest with that authenticated state. Any mutation after the run, including
to a privacy, engine, or artifact field, invalidates the package. Use the printed digest as the
expected value:

```sh
python - "$HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_DIRECTORY/warehouse-lifecycle-evidence.json" <<'PY'
import hashlib
import pathlib
import sys

payload = pathlib.Path(sys.argv[1]).read_bytes()
print(hashlib.sha256(payload).hexdigest())
PY
```

Then validate the strict model and terminal claims without printing the package:

```sh
uv run python - "$HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_DIRECTORY/warehouse-lifecycle-evidence.json" <<'PY'
import pathlib
import sys
from tests.acceptance.run_warehouse_lifecycle import WarehouseLifecycleEvidence

evidence = WarehouseLifecycleEvidence.model_validate_json(pathlib.Path(sys.argv[1]).read_bytes())
assert [result.engine_kind for result in evidence.engine_results] == ["postgresql", "clickhouse"]
assert all(result.terminal_state == "retired" for result in evidence.engine_results)
assert all(result.residual_resource_count == 0 for result in evidence.engine_results)
assert len(evidence.failure_matrix.outcomes) == 64
assert evidence.failure_matrix.execution_domain == "offline_control_plane"
assert evidence.lifecycle_conformance == "proven"
assert evidence.production_readiness == "not_proven"
assert evidence.local_encryption_limitation == "deferred_local_acceptance"
PY
```

The encrypted private ledger, signed reservation/recovery locator, state and secret directories,
backups, raw logs, and Docker diagnostics are recovery state, not public evidence. They contain
exact private resource scope plus the authenticated expected public-evidence digest and must remain
owner-only. CI may upload only the validated `warehouse-lifecycle-evidence.json` file and the separate numeric
cost artifact (`duration_seconds`, peak Docker memory and disk bytes, `sample_count`, and
`timed_out`); do not attach any private state, Docker names or identifiers, paths, diagnostics, or
raw sampler output to a pull request or retain them as acceptance artifacts. Peak memory and disk
are observations only; no unapproved threshold may be inferred from them. The observations are
scoped from the HMAC-authenticated ledger to exact Plan 3A containers and volumes; unrelated daemon
state and shared image/build-cache usage are not part of the cost artifact.

## Interpretation limits

This package proves a fresh local Compose lifecycle only. It does not prove cloud control-plane
security, durable production storage encryption, high availability, production backup retention,
capacity behavior, managed monitoring, or a customer production deployment. Those claims require
their own live transaction and terminal evidence.
