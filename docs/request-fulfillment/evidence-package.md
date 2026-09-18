# Plan 3B evidence package

The Plan 3B harness packages the real `FulfillmentEvidenceReceipt` models in canonical order and
prints only the SHA-256 digest of those canonical bytes. The package is held in memory for this
offline slice; no tenant-private artifact is written to disk.

The packaged receipts use a strict allowlist:

- schema version, opaque evidence/request/proposal/dependency/approval identifiers;
- request revision, proposal revision, authority references, reason codes, resulting state, and
  timestamp; and
- one of `execution_ready`, `dependency`, `denial`, `no_valid_plan`, or `cancelled`.

They exclude answer text, requester purpose, field lists, object scopes, classification details,
policy observations, grounding and policy digests, provider identifiers, endpoints, credentials,
and raw diagnostics. The evidence packager validates every receipt and scans the canonical bytes
before returning them.

Verify the command output without printing private in-memory models:

```sh
result=$(uv run python -m tests.acceptance.run_request_fulfillment)
python - "$result" <<'PY'
import json
import re
import sys

payload = json.loads(sys.argv[1])
assert payload["schema_version"] == "1"
assert payload["status"] == "complete"
assert re.fullmatch(r"[0-9a-f]{64}", payload["evidence_digest"])
assert set(payload) == {"schema_version", "status", "evidence_digest"}
PY
```

The digest establishes deterministic integrity for this offline run. It does not establish that an
external effect occurred, that a retained package was independently witnessed, or that production
services are healthy. Do not publish test tracebacks or private SQLite bytes as evidence.
