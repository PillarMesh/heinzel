# Run Plan 3A witnessed acceptance

Complete [setup](setup.md) in the same shell. The lifecycle authority remains exactly:

```sh
uv run python -m tests.acceptance.run_warehouse_lifecycle run --authorize-retention-cleanup
```

The required gate and a manually reproduced witnessed run invoke that authority through the
workflow-owned bounded runner so the command cannot exceed 600 seconds and Docker usage is sampled
while it is running:

```sh
uv run python -m tests.ci.run_warehouse_lifecycle_witness \
  --output "$HEINZEL_WAREHOUSE_LIFECYCLE_PRIVATE_ROOT/warehouse-lifecycle-cost.json" \
  --timeout-seconds 600 \
  --sample-interval-seconds 1 \
  -- \
  uv run python -m tests.acceptance.run_warehouse_lifecycle run \
    --authorize-retention-cleanup
```

The command runs PostgreSQL first and ClickHouse second. For each engine it witnesses provision,
initial validation, encrypted backup and isolated restore, suspend, gated resume, retention-aware
retirement, deadline-authorized deletion, and exact absence checks. It reconstructs public
artifacts through their real models, compares canonical engine outcomes, exercises control-plane
cross-tenant denial, executes the exact 64-scenario offline control-plane lifecycle fault matrix,
and scans retained evidence plus captured output for private markers.

The CLI writes detailed sanitized evidence to
`$HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_DIRECTORY/warehouse-lifecycle-evidence.json`. Standard output contains one JSON
object only:

```json
{"evidence_digest":"<64 lowercase hex characters>","status":"complete"}
```

Before command success, the driver scans the canonical evidence bytes, writes them atomically, and
stores their exact SHA-256 digest in the encrypted HMAC-authenticated private ledger. A file written
without that final ledger binding is incomplete and must not be published.

## Witness cost envelope

The bounded runner writes one sanitized numeric cost artifact with exactly these fields:

```json
{
  "duration_seconds": 0,
  "peak_docker_memory_bytes": 0,
  "peak_docker_disk_bytes": 0,
  "sample_count": 0,
  "timed_out": false
}
```

The values shown above illustrate types, not expected measurements. On every sample, the runner
authenticates the encrypted private ledger and measures only its exact recorded Plan 3A container
and volume identities. Container memory, container writable-layer bytes, and exact volume bytes are
included; unrelated daemon containers, volumes, images, and build cache are excluded. No names,
identifiers, paths, raw Docker output, or diagnostics belong in the artifact. No memory or disk
threshold has been approved; record the observed peaks without inventing a pass/fail limit.

The only approved command ceiling is ten minutes. Exceeding 600 seconds terminates the bounded
process group, records `timed_out: true`, fails the lifecycle step, and leaves the workflow's
`if: always()` exact teardown step able to recover from the authenticated private ledger. A Docker
sampler error also fails the gate; a zero-valued artifact must never conceal a sampler failure. A
termination or reaping result that cannot prove the witnessed process group absent fails with its
own nonzero status and still leaves exact teardown eligible to run. A run that exceeds the ceiling
requires Plan 3A design review and must not be normalized as an expected 20-to-45-minute run or made
optional.

Before publishing that file, strictly reload it through the authoritative model, recheck its
source and image pins against the private run configuration, recompute the retained fault-matrix
invariants and cleanup binding, verify its exact digest against the authenticated private ledger,
and rerun the aggregate private-marker scan:

```sh
uv run python -m tests.acceptance.run_warehouse_lifecycle validate-evidence
```

Successful validation prints only the evidence digest and `"status":"valid"`. Missing,
noncanonical, malformed, privacy-unsafe, or private-state-inconsistent evidence exits nonzero and
must not be uploaded.

A terminal evidence package must contain exactly two engine results in this order:

1. `postgresql`
2. `clickhouse`

Each result must end in `retired`, contain all six named checks with `passed`, and report
`residual_resource_count: 0`. The package must also retain the canonical 64 fault outcomes; strict
model loading must revalidate their exact set and recompute `failure_matrix_digest`. The aggregate
claims must be exactly:

```text
local_encryption_limitation = deferred_local_acceptance
production_readiness = not_proven
lifecycle_conformance = proven
```

An offline test pass, an HTTP success, a running container, or the presence of a backup file does
not prove Plan 3A. A run is successful only after both new engine transactions reach terminal
states, isolated restore is validated, retained resources are deleted after authorization, exact
absence is observed, and sanitized evidence is finalized.

## Interruption and failure

Do not launch a second run with the same paths. Preserve the shell and private directory, then use
the exact teardown procedure. The owner-only signed reservation is the durable recovery locator;
teardown validates it against the encrypted exact-resource ledger and does not require public
evidence to exist. A missing, ambiguous, or corrupt reservation or private ledger is a fail-closed
condition: do not replace it with broad `docker system prune`, project-wide Compose cleanup, or
filename globs. Investigate the recorded run before deciding how to recover exact resources.

The current local acceptance driver performs best-effort exact cleanup if a provider call raises.
The bounded runner terminates its process group on timeout or sampler failure, but exact resource
recovery still requires the private ledger and the teardown runbook. Abrupt host or process
termination must never be treated as successful acceptance.
