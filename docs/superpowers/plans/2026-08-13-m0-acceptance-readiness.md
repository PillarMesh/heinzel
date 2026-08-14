# PillarMesh M0 Acceptance-Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing Python M0 thin thread capable of producing a privacy-safe, independently verifiable, two-operator PostgreSQL-to-Snowflake acceptance package.

**Architecture:** Keep the existing Python modular monolith and SQLite evidence boundary. Separate runtime-private locators and probe values from canonical evidence, persist every compiler parent artifact by digest, export only allowlisted canonical artifacts, and drive the bounded real-account proof through a checked-in CLI harness after all offline gates pass.

**Tech Stack:** Python 3.13, uv, Pydantic 2, SQLite, Psycopg 3, Snowflake Connector for Python, Ed25519, pytest, Hypothesis, mutmut, Ruff, and strict mypy.

## Global Constraints

- Continue to admit only the exact M0 six-column PostgreSQL-to-Snowflake snapshot projection.
- Do not add a service, scheduler, workflow engine, provider, top-level directory, or production-data path.
- Raw local paths and raw acceptance keys must not enter canonical artifacts, evidence events, logs, CLI/MCP output, or the sanitized package.
- Runtime-private state remains local SQLite state and is never reachable through artifact, trace, CLI/MCP, or package APIs.
- Exact canonical artifact bytes are exported; no post-hash redaction or rewritten artifact is allowed.
- `No Valid Plan` creates no run, source-row read, or destination mutation.
- Live mutation is forbidden until Tasks 1-7 and every offline gate pass.
- Unit tests remain colocated; cross-component, fault, package, and acceptance tests live under `tests/`.
- Each production behavior follows red-green-refactor and includes a failure or boundary test.
- Independent legality review must be performed by a reviewer other than the rule author.

---

## File Structure

- `services/evidence/src/pillarmesh_evidence/migrations.py`: ordered SQLite schema migrations and checksum verification.
- `services/evidence/src/pillarmesh_evidence/private_state.py`: non-artifact runtime-private state types.
- `services/evidence/src/pillarmesh_evidence/store.py`: transactional artifact, private-state, run, and event persistence.
- `packages/provider-sdk/src/pillarmesh_provider_sdk/models.py`: privacy-safe v2 manifest, receipt, and visibility contracts.
- `packages/provider-sdk/src/pillarmesh_provider_sdk/protocols.py`: provider method signatures separating paths and keys from artifacts.
- `providers/snowflake/src/pillarmesh_provider_snowflake/encoding.py`: deterministic segment bytes and safe manifest.
- `providers/snowflake/src/pillarmesh_provider_snowflake/provider.py`: stage and visibility operations using separately supplied private inputs.
- `services/runtime/src/pillarmesh_runtime/runtime.py`: atomic extraction persistence, recovery, and private-input delivery.
- `services/runtime/src/pillarmesh_runtime/faults.py`: injected no-op-by-default checkpoint fault hook.
- `services/compiler/src/pillarmesh_compiler/models.py`: complete `CompilationBundle` and parent-digest validation.
- `services/compiler/src/pillarmesh_compiler/compiler.py`: deterministic bundle construction.
- `services/contract/src/pillarmesh_contract_service/models.py`: activation summary with signed-envelope artifact identity.
- `services/contract/src/pillarmesh_contract_service/service.py`: atomic compiler-artifact persistence before summary publication.
- `services/evidence/src/pillarmesh_evidence/package_models.py`: strict package index, edges, results, resources, gates, and limitations.
- `services/evidence/src/pillarmesh_evidence/scanner.py`: non-echoing sensitive-value and local-path detection.
- `services/evidence/src/pillarmesh_evidence/package.py`: deterministic export and independent verification.
- `services/authoring-mcp/src/pillarmesh_authoring_mcp/app.py`: package and private-input application operations.
- `services/authoring-mcp/src/pillarmesh_authoring_mcp/cli.py`: stdin activation key plus export/verify commands.
- `tests/fault-injection/`: required checkpoint, persistence, corruption, permission, and tamper matrix.
- `tests/acceptance/run_m0.py`: single real-account acceptance harness.
- `tests/acceptance/test_harness.py`: offline harness orchestration, failure, and cleanup-ledger tests.
- `docs/m0/`: exact configuration, transport, acceptance, evidence, retention, and cleanup procedures.

### Task 1: Versioned SQLite private-state boundary

**Files:**
- Create: `services/evidence/src/pillarmesh_evidence/private_state.py`
- Modify: `services/evidence/src/pillarmesh_evidence/{migrations.py,models.py,store.py,__init__.py}`
- Test: `services/evidence/tests/test_store.py`

**Interfaces:**
- Produces: `RunPrivateState`, `SQLiteStore.get_private_state(run_id)`, `set_acceptance_key(run_id, key)`, `set_segment_path(run_id, path)`, and an idempotent v1-to-v2 migration.
- Preserves: public `RunRecord` without `acceptance_key`; no private value is returned by run or trace APIs.

- [ ] **Step 1: Write migration and privacy failures**

Add tests that open a v1 fixture database, assert migration to version 2, assert the key moved to `run_private_state`, reopen it idempotently, and assert `RunRecord.model_dump_json()` contains neither `acceptance_key` nor a segment path:

```python
state = migrated.get_private_state("run-1")
assert state.acceptance_key == 42
assert "acceptance_key" not in migrated.get_run("run-1").model_dump_json()
```

- [ ] **Step 2: Verify red**

Run `uv run pytest services/evidence/tests/test_store.py -q`. Require failures because schema version 2, `RunPrivateState`, and `get_private_state` do not exist.

- [ ] **Step 3: Implement ordered migrations and private state**

Define the non-artifact type and store interface exactly as:

```python
@dataclass(frozen=True, slots=True)
class RunPrivateState:
    run_id: str
    acceptance_key: int | None
    segment_path: Path | None
```

Create `run_private_state(run_id PRIMARY KEY REFERENCES runs, acceptance_key INTEGER, segment_path TEXT)`. Verify the v1 checksum before applying v2, migrate existing `runs.acceptance_key`, and update metadata only in the same transaction. Keep v1 migration bytes unchanged so existing checksums remain valid.

- [ ] **Step 4: Remove private data from the public run model**

Remove `RunRecord.acceptance_key`; make `_run_from_row` return only public fields. `set_acceptance_key` and `set_segment_path` perform immutable compare-and-set writes in `run_private_state`, and `get_private_state` is the only read path.

- [ ] **Step 5: Verify green and boundary cases**

Run `uv run pytest services/evidence/tests/test_store.py -q` and `uv run mypy services/evidence/src`. Require success for fresh databases, v1 migration, repeated migration, conflicting key/path rejection, missing run rejection, and public serialization privacy.

- [ ] **Step 6: Commit**

```sh
git add services/evidence
git commit -m "feat(evidence): isolate runtime private state"
```

### Task 2: Privacy-safe provider artifacts and atomic extraction recovery

**Files:**
- Modify: `packages/provider-sdk/src/pillarmesh_provider_sdk/{models.py,protocols.py,__init__.py}`
- Modify: `providers/snowflake/src/pillarmesh_provider_snowflake/{encoding.py,provider.py}`
- Modify: `services/evidence/src/pillarmesh_evidence/store.py`
- Modify: `services/runtime/src/pillarmesh_runtime/runtime.py`
- Test: `packages/provider-sdk/tests/test_models.py`
- Test: `providers/snowflake/tests/{test_encoding.py,test_provider.py}`
- Test: `services/evidence/tests/test_store.py`
- Test: `services/runtime/tests/test_runtime.py`
- Test: `tests/fault-injection/test_runtime_recovery.py`

**Interfaces:**
- Produces: `SegmentManifest(schema_version="2", segment_name="segment.csv", ...)` and `VisibilityProof(schema_version="2", ...)` without path/key fields.
- Changes: `DestinationProvider.verify_visibility(manifest, acceptance_key)` and runtime recovery through `RunPrivateState`.
- Produces: `SQLiteStore.record_extraction(...)` as the atomic safe-artifact/private-path/checkpoint operation.

- [ ] **Step 1: Write privacy and recovery failures**

Assert canonical manifest/proof bytes contain neither a temporary directory nor the decimal acceptance key. Add a restart test that persists extraction, reconstructs the runtime, and verifies staging receives the same private `Path` while visibility receives the same private integer.

```python
payload = canonical_bytes(manifest)
assert str(output_dir).encode() not in payload
assert str(acceptance_key).encode() not in payload
```

- [ ] **Step 2: Verify red**

Run the provider, evidence, runtime, and recovery test files. Require failures on the existing `segment_path`, `acceptance_key`, and one-argument visibility protocol.

- [ ] **Step 3: Implement v2 artifacts**

Define exact safe fields:

```python
class SegmentManifest(ProviderModel):
    schema_version: Literal["2"] = "2"
    batch_id: str
    segment_name: Literal["segment.csv"] = "segment.csv"
    segment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int
    encoded_bytes: int
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_boundary_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    acceptance_value_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class VisibilityProof(ProviderModel):
    schema_version: Literal["2"] = "2"
    batch_id: str
    value_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_id: str
    verified_at: datetime
```

Keep the raw key as an encoder argument only. Use `manifest.segment_name` for the remote stage path, pass local `Path` only to `stage`, and pass the raw key only to `verify_visibility`.

- [ ] **Step 4: Make extraction persistence atomic**

Implement one `BEGIN IMMEDIATE` operation that saves source boundary and manifest canonical bytes, stores the private path, advances `snapshot_opened` to `extraction_completed`, and appends the linked event. Roll back every write on failure.

- [ ] **Step 5: Reject v1 export/resume ambiguity explicitly**

Migration extracts v1 acceptance keys and segment paths into private state while leaving v1 manifests immutable. Runtime loading a v1 in-progress manifest raises `MigrationError("schema-v1 manifest cannot resume under privacy-safe runtime")`; later package export reports it as non-exportable.

- [ ] **Step 6: Verify green**

Run all changed component tests, `uv run mypy`, and `uv run pytest tests/fault-injection/test_runtime_recovery.py -q`. Require atomic rollback, cross-restart identity, and privacy assertions to pass.

- [ ] **Step 7: Commit**

```sh
git add packages/provider-sdk providers/snowflake services/evidence services/runtime tests/fault-injection
git commit -m "feat(runtime): separate private segment state"
```

### Task 3: Complete compiler artifact bundle and publication transaction

**Files:**
- Modify: `services/compiler/src/pillarmesh_compiler/{models.py,compiler.py,__init__.py}`
- Modify: `services/contract/src/pillarmesh_contract_service/{models.py,service.py}`
- Modify: `services/evidence/src/pillarmesh_evidence/store.py`
- Test: `services/compiler/tests/test_compiler.py`
- Test: `services/contract/tests/test_service.py`
- Test: `services/evidence/tests/test_store.py`

**Interfaces:**
- Produces: `CompilationBundle(contract_digest, verified_at, iir, physical_plan, legality_decision, signed_graph)` with computed digest properties.
- Produces: `SQLiteStore.publish_verification(artifacts, summary_digest, summary_payload)` as one transaction.
- Changes: `ActivationSummary.signed_graph_artifact_digest` identifies canonical signed-envelope bytes; `graph_digest` continues to identify unsigned graph content.

- [ ] **Step 1: Write parent and publication failures**

Add tests that load every summary parent by digest and compare exact canonical bytes. Inject failure on the third artifact write and assert no activation summary or summary reference is visible.

```python
assert digest(bundle.iir) == bundle.iir_digest
assert digest(bundle.physical_plan) == bundle.signed_graph.graph.physical_plan_digest
assert digest(bundle.legality_decision) == bundle.signed_graph.graph.legality_decision_digest
```

- [ ] **Step 2: Verify red**

Run compiler, evidence, and contract-service tests. Require failures because intermediates are not returned or persisted and publication is not transactional.

- [ ] **Step 3: Implement `CompilationBundle`**

Store canonical objects, expose digest properties, and use a Pydantic after-validator to reject mismatched graph parents. Remove duplicate caller-controlled digest fields from the former `CompilationResult`.

- [ ] **Step 4: Implement atomic publication**

`publish_verification` validates `hashlib.sha256(payload).hexdigest() == artifact_digest` for every canonical payload, inserts all content-addressed artifacts, inserts the activation summary last, and commits once. On conflict or error, roll back the entire publication transaction.

- [ ] **Step 5: Link all lifecycle evidence**

Persist `intent_ir`, `physical_plan`, admitted `legality_decision`, `signed_execution_graph`, and `activation_summary`; include IIR, plan, decision, signed-envelope, and graph digests in `verification_completed`/`graph_signed` event attributes without duplicating canonical payloads.

- [ ] **Step 6: Verify green and determinism**

Run affected tests and strict mypy. Compile identical inputs in separate calls and assert byte-identical IIR, plan, decision, unsigned graph, and signed envelope when the signing key and explicit verification time are identical.

- [ ] **Step 7: Commit**

```sh
git add services/compiler services/contract services/evidence
git commit -m "feat(compiler): persist complete artifact bundle"
```

### Task 3A: Reconcile governed privacy schemas before export

**Files:**
- Modify: `packages/provider-sdk/src/pillarmesh_provider_sdk/models.py`
- Modify: `providers/postgresql/src/pillarmesh_provider_postgresql/provider.py`
- Modify: `providers/snowflake/src/pillarmesh_provider_snowflake/provider.py`
- Modify: `services/evidence/src/pillarmesh_evidence/{migrations.py,store.py}`
- Modify: all unit, integration, end-to-end, runtime, and fault fixtures constructing `SourceBoundary`
- Test: `packages/provider-sdk/tests/test_models.py`
- Test: `providers/postgresql/tests/test_provider.py`
- Test: `providers/snowflake/tests/test_provider.py`
- Test: `services/evidence/tests/test_store.py`

**Interfaces:**
- Produces: canonical `SourceBoundary(schema_version="2", key_range_digest=<sha256>)` with no `key_min` or `key_max`.
- Produces: opaque `CommitReceipt.ledger_identity` as `digest({"provider": "snowflake", "ledger": qualified_name})`.
- Enforces: SQLite version 1 is refused with an explicit fresh-state diagnostic; it is never migrated.

- [ ] **Step 1: Write privacy and version-refusal failures**

Assert `SourceBoundary` canonical bytes contain neither raw key bound, PostgreSQL computes the digest of the ordered `(key_min, key_max)` pair, Snowflake receipts contain no qualified identifier, and opening a historical v1 database raises `MigrationError` naming found version `1`, required version `2`, and a fresh state path.

- [ ] **Step 2: Verify red**

Run the provider SDK, PostgreSQL, Snowflake, and evidence store suites. Require failures on v1 boundaries, qualified ledger identity, and v1 migration success.

- [ ] **Step 3: Implement the governed v2 models**

Replace `SourceBoundary.key_min` and `.key_max` with:

```python
schema_version: Literal["2"] = "2"
key_range_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
```

The PostgreSQL provider sets `key_range_digest=digest({"key_min": key_min, "key_max": key_max})`. Fakes and fixtures use the same canonical formula, including empty ranges with both values `None`.

- [ ] **Step 4: Make receipt identity opaque**

Snowflake computes `ledger_identity=digest({"provider": "snowflake", "ledger": self._qualified(self._settings.ledger_table)})`. Tests assert the qualified database/schema/table string is absent from canonical receipt bytes.

- [ ] **Step 5: Refuse version 1 stores**

Remove the v1-to-v2 migration path. When metadata version is `1`, raise `MigrationError("database schema version 1 is unsupported; required version 2; start with a fresh state path")` before reading or mutating application tables. Preserve the historical fixture only to prove refusal and zero mutation.

- [ ] **Step 6: Verify green and enumerate old fields**

Run all affected component, runtime, end-to-end, fault, and package tests, strict mypy, and `rg -n 'key_min|key_max|MIGRATION_V1|migrate_v1' packages providers services tests`. Remaining hits are permitted only inside the historical refusal fixture and PostgreSQL's private aggregate-query local variables.

- [ ] **Step 7: Commit**

```sh
git add packages/provider-sdk providers/postgresql providers/snowflake services/evidence services/runtime tests
git commit -m "fix(evidence): enforce privacy-safe artifact schemas"
```

### Task 4: Deterministic evidence exporter, verifier, and scanner

**Files:**
- Create: `services/evidence/src/pillarmesh_evidence/{package_models.py,scanner.py,package.py}`
- Modify: `services/evidence/src/pillarmesh_evidence/{store.py,__init__.py}`
- Modify: `services/evidence/pyproject.toml`
- Test: `services/evidence/tests/{test_package.py,test_scanner.py}`
- Test: `tests/integration/test_evidence_package.py`

**Interfaces:**
- Produces: `PackageMetadata`, `ScanInput`, `PackageResult`, `export_package(store, run_id, destination, metadata, scan_input, verifier)`, and `verify_package(path, verifier, scan_input)`.
- Produces: read-only store methods to enumerate a run's allowlisted artifacts and trace.

- [ ] **Step 1: Write fail-closed package tests**

Create a complete fake run fixture and assert deterministic payload files. Parameterize missing, modified, swapped, orphaned, unknown-kind, v1-manifest, broken-event-chain, missing-terminal-proof, local-path, raw-key, exact-canary, encoded-canary, connection-string, and private-key-marker failures.

- [ ] **Step 2: Verify red**

Run `uv run pytest services/evidence/tests/test_package.py services/evidence/tests/test_scanner.py tests/integration/test_evidence_package.py -q`. Require import failures for the absent package API.

- [ ] **Step 3: Implement strict package models**

Use frozen `extra="forbid"` models. Define an allowlisted `ArtifactEntry(kind, digest, relative_path, sha256)`, `ArtifactEdge(parent_digest, child_digest, relationship)`, and package index version `1`. Hash every payload except `package.json` and `verification/result.json`; the result records the final package-index digest.

- [ ] **Step 4: Implement non-echoing scans**

`scan_bytes(relative_path, payload, scan_input)` returns rule identifiers and byte offsets only. It must scan exact values plus Base64 and URL-encoded forms without ever formatting the source canary into an error or result.

- [ ] **Step 5: Implement export and independent verification**

Write canonical JSON to a temporary sibling, verify artifact digests, graph signatures, parent edges, event sequence/hash chain, terminal visibility linkage, file hashes, and scans, then atomically rename. Delete the temporary sibling on failure without touching an existing completed package.

- [ ] **Step 6: Verify green and mutation resistance**

Run package/scanner/integration tests, strict mypy, and manually mutate one byte in each fixture artifact to prove verification fails.

- [ ] **Step 7: Commit**

```sh
git add services/evidence tests/integration/test_evidence_package.py
git commit -m "feat(evidence): export verifiable M0 packages"
```

### Task 5: Authoring commands and runtime-before-provider hardening

**Files:**
- Modify: `services/authoring-mcp/src/pillarmesh_authoring_mcp/{app.py,cli.py,mcp_server.py}`
- Modify: `services/runtime/src/pillarmesh_runtime/runtime.py`
- Test: `services/authoring-mcp/tests/{test_cli.py,test_mcp.py,test_redaction.py}`
- Test: `services/runtime/tests/test_runtime.py`

**Interfaces:**
- Produces: CLI commands `activate-stdin`, `export-evidence`, and `verify-evidence`.
- Preserves: existing MCP lifecycle methods; acceptance keys remain absent from MCP resources and tool results.
- Guarantees: modified, expired, wrong-key, and incompatible graph rejection before provider resolution.

- [ ] **Step 1: Write CLI privacy and resolver-order failures**

Use `CliRunner(input="42\n")` and assert activation delegates with integer `42` while stdout/JSON contains no `42`. Add separate source/destination call counters for each graph rejection class and require both stay zero.

- [ ] **Step 2: Verify red**

Run authoring and runtime tests. Require absent-command and provider-resolution failures.

- [ ] **Step 3: Implement stdin-only acceptance input**

Read exactly one decimal line from stdin, reject non-integer, negative, trailing, or empty input, and never include it in error text. Remove the positional acceptance-key argument and update existing callers to use stdin; no CLI path may accept the raw key in process arguments.

- [ ] **Step 4: Add package commands**

Commands accept only run ID, package directory, and non-secret metadata paths as arguments. Canaries arrive through inherited environment or stdin. JSON output contains package/index/result digests and named check dispositions only.

- [ ] **Step 5: Enforce all graph checks before resolution**

Validate signed-envelope integrity, signature/key, schema compatibility, expiry, and run/contract linkage before calling either resolver. Keep distinct typed diagnostics.

- [ ] **Step 6: Verify green**

Run authoring/runtime tests, strict mypy, and the whole offline suite. Scan captured output for all injected canaries.

- [ ] **Step 7: Commit**

```sh
git add services/authoring-mcp services/runtime
git commit -m "feat(authoring): expose private acceptance controls"
```

### Task 6: Complete offline fault and legality evidence

**Files:**
- Create: `services/runtime/src/pillarmesh_runtime/faults.py`
- Modify: `services/runtime/src/pillarmesh_runtime/{runtime.py,__init__.py}`
- Modify: `services/compiler/tests/test_compiler.py`
- Modify: `services/runtime/tests/test_runtime.py`
- Modify: `tests/fault-injection/test_runtime_recovery.py`
- Create: `tests/fault-injection/test_runtime_fault_matrix.py`
- Create: `services/compiler/legality/reviews/M0-PG-SNAPSHOT-SNOWFLAKE-001-review.md`
- Modify: `services/compiler/legality/proof-notes/M0-PG-SNAPSHOT-SNOWFLAKE-001.md`

**Interfaces:**
- Produces: deterministic `FaultHook = Callable[[str], None]` and `noop_fault_hook(checkpoint: str) -> None`, injected into `Runtime` with the no-op default.
- Produces: requirement-to-mutant mapping and an independent legality disposition tied to commit SHA.

- [ ] **Step 1: Write the full failing fault matrix**

Parameterize stop/failure points before extraction, after extraction persistence, during commit, after commit before receipt persistence, after receipt before visibility, and around evidence writes. Assert terminal/resumable state, stable batch identity, continuous event chain, and no false success.

- [ ] **Step 2: Add corruption, permission, and tamper failures**

Cover corrupt staged bytes, PostgreSQL denial, Snowflake stage denial, Snowflake merge denial, conflicting ledger digest, lost commit response, and modified contract/observation/graph artifacts.

- [ ] **Step 3: Verify red**

Run the fault matrix and require failures at every currently unsupported injection or assertion.

- [ ] **Step 4: Implement only the required injection seam and recovery corrections**

Use existing store/provider wrappers for dependency failures. Inject the no-op-by-default `fault_hook` and invoke it with the stable names `before_extraction`, `after_extraction_persisted`, `during_commit`, `after_commit_before_receipt`, and `after_receipt_before_visibility`; never add production branching keyed to a test environment.

- [ ] **Step 5: Map legality mutations**

Run focused mutmut against legality. For preconditions 1-10, record one bypass/negation mutant and its killing test. Classify every survivor; admission, evidence-set, handle-binding, or attribution survivors block review.

- [ ] **Step 6: Obtain independent legality review**

The non-author reviewer records reviewer identity, date, commit SHA, type correspondence, handle binding, freshness, ledger atomicity, fixtures, mutation mapping, evidence-set invariant, limitations, and `approved` or `changes_required`. The proof note links that record and cannot say approved unless the disposition is approved.

- [ ] **Step 7: Verify green and commit**

Run compiler/runtime/fault tests, focused mutation commands, strict mypy, and the offline suite.

```sh
git add services/compiler services/runtime tests/fault-injection
git commit -m "test: complete M0 fault and legality evidence"
```

### Task 7: Executable clean-room runbooks and acceptance harness

**Files:**
- Modify: `.env.example`
- Modify: `docs/m0/{setup.md,acceptance-run.md,evidence-package.md,teardown.md}`
- Create: `docs/m0/transport-decision.md`
- Create: `docs/m0/contract.example.json`
- Create: `tests/acceptance/{run_m0.py,test_harness.py}`
- Modify: `tests/integration/{test_postgresql_live.py,test_snowflake_live.py,test_postgres_snowflake_live.py}`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `uv run python tests/acceptance/run_m0.py preflight|run|verify|cleanup-status`.
- Produces: private cleanup ledger outside the repository and sanitized resource dispositions inside the package.
- Requires: two distinct operator variable sets and explicit owner authorization for cleanup or permission-fault operations.

- [ ] **Step 1: Write offline harness failures**

Test missing-variable aggregation, repository-local path refusal, reused state/output refusal, owner/fixture credential rejection, opaque contract labels, stdin activation, pre/post query ordering, exact replay, negative-plan counters, failed-run resource recording, and non-destructive cleanup-status behavior.

- [ ] **Step 2: Verify red**

Run `uv run pytest tests/acceptance/test_harness.py -q`. Require import failure for the absent harness.

- [ ] **Step 3: Implement preflight and run orchestration**

Use provider adapters for parameterized probes and fixture insertion, but invoke product lifecycle through CLI subprocesses. Register private cleanup resources before mutation, use `try/finally` to persist disposition, and never perform owner operations in `run`.

- [ ] **Step 4: Bound diagnostic live tests**

Add exact per-test resource registration and `finally` cleanup for failed pre-commit fixtures and local files. Successful target rows and package evidence follow the 30-day policy; staged objects receive a 24-hour cleanup disposition. The tests report created resource digests without qualified names.

- [ ] **Step 5: Make runbooks exact**

List every variable name, replace `current_database_name` with an explicit owner-supplied identifier, include parameterized positive/denial probes, separate operator state/output examples, fixed contract fixture, CLI fallback, retention schedule, stage cleanup, and fresh absence confirmation. Commands never place secret values in arguments.

- [ ] **Step 6: Verify green and clean checkout gates**

Run acceptance harness tests, all offline tests, Ruff, formatting, mypy, lock check, and structure validation. Run `preflight` with no credentials and require a nonzero exit listing names only and performing zero provider calls.

- [ ] **Step 7: Commit**

```sh
git add .env.example docs/m0 tests/acceptance tests/integration pyproject.toml uv.lock
git commit -m "test: add bounded M0 acceptance harness"
```

### Task 8: Two-operator live acceptance and sanitized gate report

**Files:**
- Create at run time only: ignored `output/m0/<run-id>/` packages and private cleanup ledgers outside the repository.
- Create after verified runs: `docs/m0/reviews/2026-08-13-m0-acceptance.md`

**Interfaces:**
- Consumes: the committed Task 7 harness and two independent credential sets.
- Produces: two verified package digests, direct evidence for all fourteen gates, cleanup dispositions, and one sanitized committed review.

- [ ] **Step 1: Establish live authorization and clean state**

From a clean checkout, run the no-value preflight for Operator 1 and Operator 2. Confirm distinct runtime identities, signing key IDs, state paths, and output paths; confirm explicit owner authorization before permission faults or cleanup.

- [ ] **Step 2: Run Operator 1**

Execute `preflight`, `run`, and `verify`; require new-key absence, post-start insertion, terminal visibility, digest linkage, exact replay without duplicate effect, negative `No Valid Plan`, verified package, and a private cleanup ledger.

- [ ] **Step 3: Run the live permission campaign**

The environment owner revokes/restores one permission at a time using exact registered principals. Require deterministic PostgreSQL-read, Snowflake-stage, and Snowflake-merge denial evidence and fresh restoration probes.

- [ ] **Step 4: Run Operator 2 from committed instructions**

The independent person uses a clean checkout and records undocumented steps. Require a second verified package with distinct run/batch/key identities.

- [ ] **Step 5: Scan and write the report**

Verify both packages, scan repository/logs/output/artifacts/SQLite evidence/package, and write fourteen `passed` or `failed` dispositions with direct package-relative references. Never convert unverified to partial success.

- [ ] **Step 6: Run final gates and commit only the review**

Run all offline and live suites, mutation mapping, Ruff, formatting, strict mypy, lock, and structure checks from a clean checkout. Inspect `git diff` for sensitive values and stage only the sanitized review.

```sh
git add docs/m0/reviews/2026-08-13-m0-acceptance.md
git commit -m "docs: record M0 acceptance evidence"
```

- [ ] **Step 7: Record cleanup schedule**

Remove successful staged files within 24 hours after explicit authorization, retain successful packages and synthetic rows for 30 days, and verify fresh absence after eventual cleanup. Keep exact cleanup targets only in the private ledger.
