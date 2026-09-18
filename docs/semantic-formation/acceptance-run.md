# Plan 2 witnessed acceptance

This run proves a new, tenant-scoped Plan 2 transaction. It is not a health check,
an offline test report, an OpenMetadata HTTP response, or a replay of existing rows.
The operator must follow [`setup.md`](setup.md) first and must retain the private
ledger until [`teardown.md`](teardown.md) has verified exact absence.

The acceptance has two separate claims:

- **Offline proof:** deterministic service and end-to-end tests use local repositories
  and a validating catalog double. They prove model, lifecycle, tenant, replay,
  conflict, and privacy contracts without Docker or network access.
- **Witnessed proof:** one fresh run starts the pinned OpenMetadata emulator, creates
  new tenant-scoped catalog resources, performs the complete semantic journey, reads
  the provider back with a fresh client, restores into an isolated target, exports
  allowlisted evidence, and removes only ledger-recorded resources.

Never combine these claims. The offline suite cannot prove OpenMetadata creation,
provider authorization, backup restore, or Docker cleanup. The witnessed run does not
replace the offline suite.

## Offline proof

From the repository root, verify the Task 9 paths before running them:

```sh
repository_root=$(git rev-parse --show-toplevel)
cd "$repository_root"
test -f tests/end-to-end/test_catalog_semantic_formation.py
test -f tests/acceptance/semantic_formation_orchestration.py
test -f tests/acceptance/run_semantic_formation.py
```

Run the focused Plan 2 journey. This command must not start Docker or contact
OpenMetadata:

```sh
env -u HEINZEL_OPENMETADATA_EMULATOR \
  uv run pytest tests/end-to-end/test_catalog_semantic_formation.py -q
```

The focused result must include all of these outcomes:

1. A successful tenant uploads one immutable revenue-to-cash package, receives the
   exact package version, extracts deterministic candidates, resolves authority by
   information kind, completes owner review, forms a `ready_to_activate` contract,
   and receives no provider side effect from the offline catalog double beyond the
   contract's public publication receipt.
2. A fresh Refund NVP tenant exposes the cross-kind Refund conflict, keeps the review
   request in the governed unresolved path, reaches `No Valid Plan`, and records
   `execution_occurred = false` with no publication receipt.
3. Tenant B cannot read Tenant A's package-derived candidates, authority observations,
   review bundle, approved semantic version, contract, catalog binding, publication,
   or request. The denial occurs before the other tenant's payload is deserialized.
4. Replaying extraction, review submission, contract formation, and publication
   converges to the original identities and does not duplicate semantic effects.
5. Stale approval, unknown fields, cross-tenant identifiers, and unrecorded cleanup
   targets fail closed.

Then run the repository's complete offline gates:

```sh
uv sync --locked --all-packages
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
./tests/repository-structure/test.sh
```

A skipped, deselected-for-the-wrong-reason, failed, or unavailable gate is a gap. Do
not label the offline proof complete from a passing focused test alone.

## Witnessed run

The Task 9 runner owns the lifecycle and is the only accepted mutation entry point.
It must be started from the same Bash session that holds the injected secrets and
fresh private paths from `setup.md`:

```sh
repository_root=$(git rev-parse --show-toplevel)
cd "$repository_root"
tests/emulators/openmetadata/run.sh python -m tests.acceptance.run_semantic_formation
```

The checked-in launcher requires `DOCKER_CONFIG`, the bootstrap admin password, and
the Fernet secret-store key. Compose additionally requires the three database
password variables. The launcher changes to the repository root and runs the command
through locked `uv`; do not invoke a system Python or a different Compose file.

The command is successful only if it exits zero after writing a sanitized package
under `HEINZEL_SEMANTIC_FORMATION_OUTPUT_DIR` and a private ledger under
`HEINZEL_SEMANTIC_FORMATION_CLEANUP_LEDGER_PATH`. A process that reaches `ready`, prints an
HTTP 200, or leaves a `processing` marker is not successful.

### Focused live product-catalog publication

Use the managed lifecycle integration test when the narrower claim is that a fresh
approved product authority can be published through the OpenMetadata product adapter.
It provisions and validates a new managed binding with the same approved emulator
lifecycle, publishes the native Domain and DataProduct plus the immutable snapshot,
observes them with a fresh client, verifies exact replay, deletes the exact discovered
product objects, and retires the binding with terminal cleanup verification:

```sh
tests/emulators/openmetadata/run.sh pytest \
  tests/integration/test_openmetadata_product_catalog_live.py -q
```

The launcher supplies credentials from the operator's existing environment. The test
does not accept an endpoint or credential argument, attach to an unrecorded catalog,
or leave the managed binding running after completion. A skipped, interrupted, or
cleanup-failed invocation is not live publication evidence.

## Required witnessed transaction

The runner must print or persist only opaque run and tenant pseudonyms. It must record
the following sequence in the private ledger and export only the allowlisted digests
and statuses described in [`evidence-package.md`](evidence-package.md).

### 1. Fresh catalog binding and readiness

Create two fresh tenant IDs for this cycle:

- a successful tenant, whose new process package will reach publication; and
- a Refund NVP tenant, whose new package will reach the unresolved cross-kind
  conflict and `No Valid Plan`.

Create one new managed catalog binding for the successful tenant and run the provider lifecycle:

`draft -> provisioning -> validating -> ready`.

The Refund NVP tenant intentionally has no catalog binding or provider effect; its
unresolved authority conflict must terminate before that boundary. Provisioner evidence
for the successful tenant must establish the running OpenMetadata version, build
revision, and build timestamp from the provider API; the exact running image set from
fresh Docker inspection; loopback-only exposure; an owner/admin identity; a tenant runtime
identity, and both positive and denial probes. `ready` is invalid until the positive
and denial evidence is persisted. The provider-specific resource references stay in
the private ledger.

### 2. New process package and candidates

Upload new UTF-8 Markdown and strict JSON manifest bytes for each tenant. Validate the
manifest directly from those bytes with unknown fields rejected, persist the exact
source bytes unchanged, and record both the semantic manifest digest and raw-source
digest. Record the package digest and version, and retain the original bytes only in
private run state.
The deterministic extractor must produce a new candidate-set digest from attributable
markers; unrestricted prose, AI output, or an existing package cannot be the source
of authority.

The successful tenant must include the revenue-to-cash entities and the Refund rule
needed by the formation fixture. The Refund NVP tenant must include the conflicting
Refund information-kind observations used by the authority resolver. Do not rename a
conflict to make it pass.

### 3. Authority, owner review, and approval

Resolve authority separately for business/process meaning, catalog glossary, catalog
classification, ownership, and other information kinds. Preserve the exact authority
observation digest and its source references. The successful tenant's owner review
must be delivered, submitted by the named owner role, and accepted against the exact
candidate and authority revisions. The approved semantic version must bind the
candidate-set, review-bundle, policy, and approval digests.

Before contract formation, persist a tenant-scoped source observation derived from
the exact process-package source and manifest digests. Contract formation must verify
that the referenced observation is current for the same tenant and package; a missing,
stale, cross-tenant, or digest-mismatched observation fails closed.

The Refund NVP tenant must record the unresolved cross-kind conflict and route the
review request through the governed `investigating` path. An empty owner-decision
set is intentional for this tenant. It must end at `No Valid Plan`; it must not form
an approved semantic version, ready contract, publication intent, provider object,
warehouse effect, or execution attempt.

### 4. Ready contract and publication

For the successful tenant, form exactly one `ready_to_activate` managed Integration
Contract bound to the approved semantic version, current source observations, policy,
quality, freshness, access, and evidence requirements. The contract is not an
activation and this Plan 2 run does not execute a warehouse load.

Publish only after the catalog binding is `ready`, the approved semantic version is
persisted, and the contract is ready. The publication intent must be persisted before
provider effects and must contain the binding revision, semantic-version digest,
contract digest, ordered stable semantic identities, and operation ID.

For every ensured namespace, glossary term, classification, owner assignment,
contract reference, and lineage object:

- read it back through the provider adapter;
- compare logical identity, meaning, ownership, classification, provenance, contract
  reference, and lineage against the intended canonical values;
- fail closed on a missing, swapped, persistently wrong, or ambiguous response; and
- commit one public receipt only after the complete round trip matches.

Provider IDs, OpenMetadata URLs, credentials, and backup paths remain private. The
public receipt contains stable Heinzel references, provider version, intent digest,
round-trip observation digest, and `round_trip_verified = true`.

### 5. Fresh independent readback and drift

Construct a new OpenMetadata client and a new provider adapter after publication.
Read every published reference through that fresh client and compare the normalized
observations with the original receipt. A read from the publishing client alone is
not independent evidence.

Make one material catalog edit in the successful tenant's published semantic object,
then observe drift through the service. The result must be a typed
`schema_semantic_change` request in `investigating`, bound to before/after observation
digests and the affected semantic and contract versions, with `auto_applied = false`.
The edit must not rewrite approved semantics or silently update the contract.

The Refund NVP tenant must remain unpublished. No drift request may be manufactured
for an object that was never published.

### 6. Intended and denied access

Use the provisioned tenant runtime identity for the positive probe. It must read the
successful tenant's own namespace and published objects and must not receive
administrator capability. Record only normalized result digests and fixed denial
codes.

Run the negative probes before declaring the catalog binding ready and again after
publication:

- the runtime identity cannot administer OpenMetadata;
- the successful tenant runtime cannot read an existing denial-fixture tenant
  namespace owned by a separate managed administrator;
- an unknown or unrecorded tenant/resource reference is denied before payload access.

The Refund NVP tenant has no runtime identity, namespace, or provider object because it
must remain effect-free. The separate denial-fixture tenant exists only in private
provider state, so the authorization denial proves isolation rather than object
absence. An allowed cross-tenant read, an admin-capable runtime, a missing denial
probe, or a denial caused only by a missing object is a failed acceptance gate.

### 7. Backup, isolated restore, and representative query

Take a backup after successful publication and drift observation. Register its exact
private artifact path in the ledger before the backup operation. Do not place the path
in the evidence package.

Restore into a new, isolated Compose project and new volume set. The restore target
must not share the source project, source volumes, source network, or provider
credentials. Start only the isolated target needed for verification, wait for the
same terminal readiness condition, and use a fresh client.

Rebuild the pinned OpenMetadata search indexes for glossary, glossary term,
classification, tag, and user data to terminal completion. The runner then compares
the complete normalized pre-backup and post-restore observation tuples for exact
equality and independently resolves every restored semantic identity through the
search document endpoint. A CLI exit alone is not search proof.

Before querying the isolated target, rotate its platform administrator and all three
managed service-identity passwords. Verify each new credential authenticates and each
source credential is rejected specifically as an authentication failure. The source
stack is stopped during this check and restarted with its unchanged credentials only
after the isolated target has been removed.

The representative query must retrieve a newly published successful-tenant semantic
object by its stable logical identity and verify its normalized definition, owner,
classification, provenance, contract reference, and lineage. It must also confirm
that the Refund NVP tenant's unpublished object is absent from the successful
tenant's namespace. Backup-file presence, a successful import, or a health response
without this fresh query is not restore proof.

Record source and isolated-target project, volume, and network identifiers only in
the private ledger. The evidence package receives the restore result and query
digest, not infrastructure identifiers.

The public evidence binds isolated restore equality, search resolution, and credential
rotation/rejection as three separate result digests. None may be inferred from a
successful restore command or folded into a generic query boolean.

## Witnessed result decision

Call the witnessed journey successful only when all of these are true:

- both new tenants were exercised and their tenant isolation probes passed;
- the successful tenant reached catalog `ready`, owner-reviewed approval,
  `ready_to_activate`, publication, fresh readback, material-drift request, and
  representative restored query;
- the Refund NVP tenant reached the explicit conflict and `No Valid Plan` without
  execution or publication;
- backup and isolated restore were verified with a fresh client and representative
  query;
- the sanitized evidence package passed its allowlist and secret scans; and
- exact ledger-driven teardown completed with zero residual containers, volumes, and
  networks, provider objects, backup files, secret files, private SQLite files, SQLite
  journals, and replay artifacts.

Before teardown, validate and write the canonical evidence bytes to an owner-only
temporary file. Tear down while private recovery state still exists, freshly verify
external absence, then atomically rename those same bytes to the final evidence file.
The final package is immutable. If finalization is interrupted after teardown,
recovery removes only the ledger-registered staging/final paths and does not recreate
the deleted control databases.

If any step fails or is interrupted, retain the private ledger, classify the run as
failed or indeterminate, and follow `teardown.md`. Do not continue to a success
report after a failed assertion.
