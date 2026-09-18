# Exact Plan 3A teardown

Teardown is retention-aware and fail closed. It reads only the encrypted private ledger and signed
reservation for the requested run. It never discovers resources by Docker prefix, filesystem glob,
repository location, or broad environment scan.

The exact offline fault-matrix workspace is registered as its own typed ledger resource before the
acceptance driver may create it. Registration clears any earlier terminal cleanup claim. Normal
completion removes that exact directory locally, while the ledger remains replayable so teardown can
prove absence. If cancellation interrupts local cleanup, teardown attempts that recorded path along
with every other resource and does not touch sibling directories.

Keep the original shell environment. The CLI resolves the one exact run identity from the
owner-only signed reservation and validates it against the encrypted private ledger. This remains
available when the witness fails before public evidence is written. An operator may still provide
`--run-id <64-hex-run-id>` as an additional equality check, but recovery never derives authority
from public evidence, filenames, Docker discovery, or resource-name patterns.

Before the retention deadline, this command reports retained resources and exits nonzero. It leaves
an encryption secret in place whenever its paired encrypted backup remains retained:

```sh
uv run python -m tests.acceptance.run_warehouse_lifecycle teardown \
  --authorize-retention-cleanup
```

After the approved deadline, rerun the exact same command. Terminal output is limited to:

```json
{"cleanup_digest":"<64 lowercase hex characters>","status":"complete"}
```

The cleanup package must report zero retained resources, zero failed resources, and
`zero_residual_resources: true`. Repeating teardown with the same run identity is idempotent and
returns the recorded terminal cleanup result.

If the ledger, encryption key, signing key, reservation, or resolved run identity is missing,
ambiguous, mismatched, or corrupt, teardown refuses cleanup. Do not run `docker system prune`,
`docker compose down` against an unverified project, recursive deletion, or wildcard cleanup as a
workaround. Recover and validate the exact private ledger or escalate for manual
resource-by-resource review.

After terminal cleanup and evidence verification, remove the private shell environment. Retain only
the sanitized evidence file according to the evidence retention policy. Removing the encrypted
ledger and reservation is a separate operator action after no further replay or audit is required.
