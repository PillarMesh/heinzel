# Plan 2 sanitized evidence package

The witnessed runner writes one canonical JSON package beneath
`HEINZEL_SEMANTIC_FORMATION_OUTPUT_DIR`. The package proves the run without exposing catalog payloads,
provider identifiers, credentials, endpoints, private paths, or raw business-process narrative.

## Public allowlist

Only these fields may be exported:

- schema and evidence-package version;
- opaque run ID and tenant pseudonyms;
- clean source revision, source-tree digest, lock digest, Python version, and OpenMetadata version;
- provider-build and exact running-image-set observation digests;
- process-package, candidate-set, authority-resolution, review-bundle, approved-semantic,
  contract, publication-intent, publication-receipt, and drift-request digests;
- fixed lifecycle states and reason codes;
- positive, denial, fresh-readback, backup, isolated-restore, search, credential,
  representative-query, and cleanup result digests;
- boolean assertions for `round_trip_verified`, `auto_applied = false`,
  `execution_occurred = false`, tenant isolation, exact cleanup, and zero residual resources; and
- timestamps already present in governed public artifacts.

The successful and Refund NVP tenants must be distinguishable only by fresh opaque pseudonyms. The
package must show that the successful tenant reached ready contract/publication/restore and the
Refund tenant reached `cross_kind_conflict` and `no_valid_plan` without execution or publication.

## Forbidden content

Reject the package if any field or value contains:

- passwords, tokens, authorization headers, Fernet material, or credential canaries;
- OpenMetadata provider IDs, user emails, endpoint URLs, Docker names, image registry credentials,
  local paths, volume/network IDs, or backup locations;
- raw process Markdown, manifest content, semantic definitions, catalog response bodies, SQL, or
  business row values; or
- private resource-ledger entries or cleanup commands.

Before calling the run accepted, validate the package against a strict allowlist, canonicalize it,
and scan both keys and values for forbidden names and run-specific canaries. Re-open the written
bytes and verify their digest; in-memory serialization is not file evidence.

The private ledger separately binds each public digest to exact operational resources and cleanup
results. Its exact entries and configured run scope are authenticated with an HMAC derived from
the external secret-store key. It is owner-only state and must never be embedded in, attached to,
or distributed with the public package.

The two cleanup booleans are derived claims. `exact_cleanup_verified` requires every
pre-registered ledger entry to be terminally complete. `zero_residual_resources`
additionally requires fresh Docker absence checks for the exact source and restore
projects plus fresh absence checks for every private artifact, backup, and secret path.
Provider-object absence is proved immediately before provider credentials are retired
and bound into the authenticated terminal ledger; it is not inferred from a later
unauthenticated API failure. Successful and NVP execution/publication booleans are
derived from workflow outcomes and persisted publication-journal effect counts;
neither value may be inserted as an unconditional success constant.

The runner validates the complete canonical bytes before teardown, stages them in an
owner-only temporary file, and atomically publishes those exact bytes only after fresh
cleanup verification. Once published, the evidence file is immutable.
