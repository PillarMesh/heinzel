# PillarMesh M0 Python Evidence Thin Thread Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one deterministic, digest-approved PostgreSQL-to-Snowflake snapshot run with a signed execution graph and a continuous append-only evidence trace.

**Architecture:** A Python modular monolith preserves each canonical component as an independently importable workspace package. The authoring adapters call one contract service; the compiler consumes provider observations; the runtime receives injected provider interfaces and owns single-writer M0 checkpoints; SQLite stores local lifecycle state and per-run evidence.

**Tech Stack:** Python 3.13, uv, Pydantic 2, MCP Python SDK 2, Psycopg 3, Snowflake Connector for Python, cryptography/Ed25519, SQLite, pytest, Hypothesis, mutmut, Ruff, and strict mypy.

## Global Constraints

- Admit only the exact six-column fixture schema and fixed `status` to `order_status` projection in design section 4.2.
- Enforce 10,000 rows, 64 MiB encoded bytes, one staged segment, and 15 minutes per run.
- Provider observations must be at most ten minutes old at verification; graphs expire thirty minutes after verification.
- Activation re-observes both providers; runtime rechecks PostgreSQL drift before opening a snapshot.
- Never persist credentials, private keys, SQL parameter values, or row values in artifacts, evidence, logs, or authoring output.
- `No Valid Plan` causes no source-row read, run creation, or destination mutation.
- Exact repeated activation returns the original run; a different activation receives busy while a run is active.
- Evidence events and checkpoint advancement share one SQLite transaction and continue one hash chain after restart.
- Commit ambiguity is resolved by batch identity and manifest digest before any retry.
- Unit tests are colocated; integration, fault-injection, and end-to-end tests live under repository `tests/`.

---

## File Structure

- Root `pyproject.toml`, `.python-version`, and `uv.lock`: workspace, exact interpreter, tools, and dependency resolution.
- `packages/contract-model`: canonical JSON, digests, contract and fixed-schema types.
- `packages/iir`: semantic relation and projection artifacts.
- `packages/provider-sdk`: provider facts, rows, manifests, receipts, errors, and protocols.
- `packages/execution-graph`: graph model and Ed25519 signer/verifier.
- `services/compiler`: physical plan, evidence requirements, legality outcomes, deterministic compilation.
- `services/evidence`: migrations, lifecycle repositories, atomic checkpoints, evidence hash chains.
- `services/runtime`: sequential state machine, drift checks, retry budget, recovery.
- `services/contract`: draft/verification/activation lifecycle and single-run admission.
- `providers/postgresql`: observations, drift probe, and consistent snapshot reader.
- `providers/snowflake`: deterministic CSV, staging, ledger commit resolution, visibility proof.
- `services/authoring-mcp`: composition root, CLI, MCP tool/resource adapters, redacted settings.
- `tests/`: cross-component fake-provider thin thread, fault injection, and opt-in real-account suites.

### Task 1: Workspace and architecture baseline

**Files:**
- Create: `.python-version`, `pyproject.toml`, component `pyproject.toml` files, `docs/architecture/decisions/ADR-0002-python-m0-runtime.md`
- Modify: `docs/superpowers/specs/2026-08-13-m0-python-thin-thread-design.md`
- Test: `tests/repository-structure/test.sh`

**Interfaces:**
- Produces: workspace package names `pillarmesh-contract-model`, `pillarmesh-iir`, `pillarmesh-provider-sdk`, `pillarmesh-execution-graph`, `pillarmesh-compiler`, `pillarmesh-evidence`, `pillarmesh-runtime`, `pillarmesh-contract-service`, `pillarmesh-provider-postgresql`, `pillarmesh-provider-snowflake`, and `pillarmesh-authoring-mcp`.

- [ ] Add the root uv workspace, strict Ruff/mypy/pytest configuration, and empty substantive package roots.
- [ ] Run `uv lock && uv sync --all-packages` and require a successful exact lock.
- [ ] Run `./tests/repository-structure/test.sh`; expect `OK: repository structure is valid`.
- [ ] Commit the revised design, plan, ADR, workspace, and lock as `chore: establish Python M0 workspace`.

### Task 2: Canonical contract model

**Files:**
- Create: `packages/contract-model/src/pillarmesh_contract_model/{canonical.py,models.py,errors.py,__init__.py}`
- Test: `packages/contract-model/tests/{test_canonical.py,test_models.py,test_properties.py}`

**Interfaces:**
- Produces: `canonical_bytes(value: JsonValue) -> bytes`, `digest(value: JsonValue | BaseModel) -> str`, `IntegrationContract`, `ProjectionField`, and `FIXED_PROJECTION`.

- [ ] Write tests proving timezone normalization, decimal string stability, sorted object keys, forbidden floats, and digest repeatability across serialization round trips.
- [ ] Run `uv run pytest packages/contract-model/tests -q`; require initial failures.
- [ ] Implement closed Pydantic models with literal snapshot/upsert/not-observed semantics and validators that enforce the exact fixture projection.
- [ ] Rerun the package tests and `uv run mypy packages/contract-model/src`; require success.
- [ ] Commit as `feat(contract): add canonical M0 contract model`.

### Task 3: IIR and provider contracts

**Files:**
- Create: `packages/iir/src/pillarmesh_iir/{models.py,lowering.py,__init__.py}`
- Create: `packages/provider-sdk/src/pillarmesh_provider_sdk/{models.py,protocols.py,errors.py,__init__.py}`
- Test: colocated `tests/test_iir.py`, `tests/test_provider_models.py`

**Interfaces:**
- Consumes: `IntegrationContract`, `ProjectionField`, `digest`.
- Produces: `lower_contract(contract) -> IntentIR`, `ProviderObservation`, `SourceBoundary`, `OrderRow`, `SegmentManifest`, `CommitReceipt`, `VisibilityProof`, `SourceProvider`, and `DestinationProvider`.

- [ ] Write failing tests showing physical connection handles do not affect IIR identity and driver objects cannot enter provider models.
- [ ] Implement immutable IIR and provider boundary types plus runtime-checkable protocols.
- [ ] Run both package test suites and strict mypy.
- [ ] Commit as `feat(provider): define IIR and provider boundaries`.

### Task 4: Signed graphs and deterministic compiler

**Files:**
- Create: `packages/execution-graph/src/pillarmesh_execution_graph/{models.py,signing.py,__init__.py}`
- Create: `services/compiler/src/pillarmesh_compiler/{models.py,legality.py,compiler.py,__init__.py}`
- Create: `services/compiler/legality/{proof-notes/M0-PG-SNAPSHOT-SNOWFLAKE-001.md,fixtures/positive.json,fixtures/negative-*.json,rules/M0-PG-SNAPSHOT-SNOWFLAKE-001.json}`
- Test: component tests for ten preconditions, evidence-set mismatch, determinism, signature tampering, wrong key, expiry, and schema version.

**Interfaces:**
- Produces: `evaluate_legality(contract, source, destination, now) -> LegalityDecision`, `compile_contract(...) -> CompilationResult`, `GraphSigner.sign(graph) -> SignedExecutionGraph`, and `GraphVerifier.verify(signed, now) -> ExecutionGraph`.

- [ ] Write one negative test for each numbered precondition and assert `NoValidPlan.execution_occurred is False`.
- [ ] Implement the curated rule with three-state preconditions and smallest-change diagnostics.
- [ ] Implement byte-reproducible IIR, plan, and unsigned graph content; keep timestamps outside deterministic semantic inputs except the explicit expiry.
- [ ] Implement Ed25519 signing and verification and the post-admission evidence-set invariant.
- [ ] Run compiler/graph tests, strict mypy, and a focused mutmut run against `legality.py`.
- [ ] Commit as `feat(compiler): compile the M0 legal signed graph`.

### Task 5: Durable lifecycle and evidence store

**Files:**
- Create: `services/evidence/src/pillarmesh_evidence/{migrations.py,store.py,models.py,__init__.py}`
- Create: `services/evidence/migrations/001_m0.sql`
- Test: `services/evidence/tests/{test_migrations.py,test_evidence.py,test_runs.py}`

**Interfaces:**
- Produces: `SQLiteStore`, `append_event`, `advance_checkpoint_with_event`, `verify_chain`, artifact save/load methods, and run compare-and-set methods.

- [ ] Write failing tests for update/delete triggers, migration checksum mismatch, newer schema refusal, terminal-state regression, active-run uniqueness, and cross-restart chain continuation.
- [ ] Implement explicit transactions and canonical hash-linked `EvidenceEvent` storage.
- [ ] Make event append and checkpoint transition atomic in one transaction.
- [ ] Run evidence tests and strict mypy.
- [ ] Commit as `feat(evidence): add atomic M0 evidence store`.

### Task 6: PostgreSQL provider and deterministic segment encoding

**Files:**
- Create: `providers/postgresql/src/pillarmesh_provider_postgresql/{provider.py,settings.py,__init__.py}`
- Create: `providers/snowflake/src/pillarmesh_provider_snowflake/{encoding.py,settings.py,__init__.py}`
- Test: unit tests for schema normalization, safe identifiers, drift, ordering, exact CSV bytes, size/row limits, and keyed value digests.

**Interfaces:**
- Produces: `PostgresProvider.observe`, `drift_probe`, `open_snapshot`; `encode_segment(rows, boundary, acceptance_key, output) -> SegmentManifest`.

- [ ] Write driver-independent unit tests with fake DB-API cursors and golden CSV bytes.
- [ ] Implement Psycopg composed identifiers, read-only repeatable-read snapshot metadata, ordered batch streaming, and redacted identities.
- [ ] Implement RFC 4180 UTF-8 encoding with fixed decimals and UTC microseconds; fail before commit at either resource ceiling.
- [ ] Add opt-in `PILLARMESH_TEST_POSTGRES_DSN` integration tests.
- [ ] Run offline unit tests; run real PostgreSQL tests only when the environment variable is present.
- [ ] Commit as `feat(provider): add PostgreSQL snapshot and segment encoding`.

### Task 7: Snowflake commit protocol

**Files:**
- Create: `providers/snowflake/src/pillarmesh_provider_snowflake/provider.py`
- Test: unit fake-connector tests and `tests/integration/test_snowflake_live.py`

**Interfaces:**
- Produces: `SnowflakeProvider.observe`, `stage`, `commit_or_resolve`, and `verify_visibility`.

- [ ] Write tests for exact stage prefix, manifest conflict, transaction statements, ledger-present resolution, ledger-absent retry eligibility, conflicting digest failure, and independent visibility query IDs.
- [ ] Implement connector calls with checked metadata identifiers and bound values; prohibit runtime DDL.
- [ ] Add opt-in live tests guarded by `PILLARMESH_TEST_SNOWFLAKE_ACCOUNT` and related dedicated-role settings.
- [ ] Run offline provider tests and strict mypy.
- [ ] Commit as `feat(provider): add idempotent Snowflake commit protocol`.

### Task 8: Runtime state machine and recovery

**Files:**
- Create: `services/runtime/src/pillarmesh_runtime/{models.py,retry.py,runtime.py,__init__.py}`
- Test: component tests and `tests/fault-injection/test_runtime_recovery.py`

**Interfaces:**
- Produces: `Runtime.execute(run_id, signed_graph, acceptance_key) -> RunResult` and `Runtime.resume(run_id) -> RunResult`.

- [ ] Write failing tests asserting graph verification precedes provider resolution, drift precedes snapshot, checkpoints share evidence transactions, delays are one/five seconds, and resume preserves run/batch/evidence identities.
- [ ] Implement the sequential phase state machine with injected clock, sleeper, providers, verifier, and store.
- [ ] Implement ambiguity resolution and non-conforming outcomes without false success.
- [ ] Run runtime and fault-injection tests and strict mypy.
- [ ] Commit as `feat(runtime): execute and recover the M0 graph`.

### Task 9: Contract lifecycle service

**Files:**
- Create: `services/contract/src/pillarmesh_contract_service/{models.py,service.py,__init__.py}`
- Test: `services/contract/tests/test_service.py`

**Interfaces:**
- Produces: `ContractService.create_draft`, `get_draft`, `verify`, `get_activation_summary`, `activate`, `get_run`, and `get_trace`.

- [ ] Write tests for immutable versions, observation freshness, graph expiry, activation drift, supersession, exact-repeat idempotency, and different-contract busy behavior.
- [ ] Implement lifecycle persistence and orchestration without embedding compiler/provider/runtime rules.
- [ ] Run contract-service tests and strict mypy.
- [ ] Commit as `feat(contract): add digest-bound M0 activation`.

### Task 10: CLI, MCP, and composition root

**Files:**
- Create: `services/authoring-mcp/src/pillarmesh_authoring_mcp/{app.py,cli.py,mcp_server.py,settings.py,__init__.py}`
- Test: `services/authoring-mcp/tests/{test_cli.py,test_mcp.py,test_redaction.py}`

**Interfaces:**
- Produces: `build_application(settings)`, CLI command `pillarmesh-m0`, and MCP server command `pillarmesh-mcp`.

- [ ] Write adapter tests proving extra fields fail, every operation delegates to `ContractService`, repeated activation returns the original run, and credential canaries never appear in output.
- [ ] Implement environment-only settings, composition, JSON CLI output, and MCP v2 stdio tools/resources.
- [ ] Run adapter tests and an in-memory MCP client round trip.
- [ ] Commit as `feat(authoring): expose M0 lifecycle over CLI and MCP`.

### Task 11: Thin-thread assurance and operator documentation

**Files:**
- Create: `tests/end-to-end/test_thin_thread.py`, `tests/integration/test_postgres_snowflake_live.py`, `docs/m0/{setup.md,acceptance-run.md,teardown.md,evidence-package.md}`
- Modify: `README.md`, `.env.example`, `.github/workflows/repository-structure.yml`

**Interfaces:**
- Consumes: the public composition root and all real provider settings.
- Produces: reproducible offline fake-provider proof plus an opt-in witnessed live-account command.

- [ ] Write the fake-provider end-to-end test from fresh row through verified trace and the negative `No Valid Plan` path with provider call counters at zero.
- [ ] Add live-account tests that skip with an explicit reason unless both dedicated account configurations exist.
- [ ] Document two-operator setup, key generation/rotation/revocation, least-privilege object provisioning, acceptance capture, retention, and teardown using variable placeholders only.
- [ ] Add CI commands for structure, lock validation, lint, strict type checks, and offline tests; never place real credentials in CI.
- [ ] Run `uv lock --check`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest`, and `./tests/repository-structure/test.sh`.
- [ ] Commit as `test: verify the M0 evidence thin thread`.

### Task 12: Live acceptance and gate report

**Files:**
- Create at execution time: ignored `output/m0/<run-id>/` evidence package and a committed sanitized gate report under `docs/m0/reviews/` only after a witnessed run.

**Interfaces:**
- Produces: one live run identifier, sanitized trace, dependency-lock digest, schema digests, test results, limitations, and fourteen gate dispositions.

- [ ] Confirm the provisioning prerequisite and two independent operator credential sets without printing their values.
- [ ] Execute the live test command documented in `docs/m0/acceptance-run.md`.
- [ ] Verify a newly inserted key was absent before the run and visible with the matching digest afterward.
- [ ] Replay the same batch and execute one legality-negative contract.
- [ ] Scan the package for credential canaries and row values, then record each gate as passed or failed with direct evidence.
- [ ] Commit only the sanitized review as `docs: record M0 acceptance evidence`; do not claim M0 complete if any gate is unverified.
