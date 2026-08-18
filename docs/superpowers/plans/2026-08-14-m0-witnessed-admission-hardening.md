# M0 Witnessed Admission Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the M0 acceptance harness and operator runbooks into conformance with the reviewed
connectivity, authorization, lock-integrity, fixture-cleanup, and private-attestation specification.

**Architecture:** Keep the changes inside the existing acceptance-only adapter and orchestration
boundaries. A retained admission object owns the dedicated PostgreSQL lock session and exposes a
fail-closed integrity assertion; PostgreSQL grant observation distinguishes the fixed key column
from every non-key column; and an explicit allowlisted serializer records attestation details only
in the owner-private cleanup ledger.

**Tech Stack:** Python 3.12, psycopg 3, pytest, Ruff, mypy, uv.

## Global Constraints

- Preserve the exact 35-variable acceptance configuration contract.
- Do not pass fixture or owner credentials to product CLI or MCP subprocesses.
- Do not add a cleanup command or perform owner-authorized provider cleanup.
- Provider admission and attestation failures use value-independent diagnostics.
- Attestation identifiers may exist only in the owner-private ledger, never stdout, stderr, the
  evidence package, or `operations/resources.json`.
- Keep the trusted PostgreSQL source-read audit function and its exact body attestation.
- Run `./tests/repository-structure/test.sh` against a clean representation before committing.

---

### Task 1: Retained provider-admission integrity

**Files:**
- Modify: `tests/acceptance/provider_adapter.py`
- Modify: `tests/acceptance/orchestration.py`
- Test: `tests/acceptance/test_provider_adapter.py`
- Test: `tests/acceptance/test_harness.py`

**Interfaces:**
- Produces: `ProviderAdmission.assert_intact() -> None`.
- Changes: `ProviderActions.admission(environment_identity)` yields `ProviderAdmission`.
- Consumes: the existing dedicated PostgreSQL connection and stable two-integer advisory-lock key.

- [ ] **Step 1: Write the failing adapter tests**

Add tests proving that the yielded admission object checks the original database, backend PID, and
exactly one granted advisory lock on the dedicated session; a missing lock or changed backend PID
raises `HarnessError` and never reacquires with `pg_try_advisory_lock`.

```python
with actions.admission(config.environment_identity) as admission:
    admission.assert_intact()

assert (
    connection.statements.count(
        ("SELECT current_database(), pg_backend_pid(), pg_try_advisory_lock(%s, %s)", lock_key)
    )
    == 1
)
```

- [ ] **Step 2: Verify the adapter tests are RED**

Run:

```sh
uv run pytest tests/acceptance/test_provider_adapter.py \
  -k 'admission_uses_stable_database_lock or admission_fails_when_retained_lock_is_lost' -q
```

Expected: failure because the context currently yields `None` and has no integrity assertion.

- [ ] **Step 3: Implement the retained admission object**

Add a narrow protocol and private implementation. Record the original backend PID during lock
acquisition. `assert_intact()` queries the same dedicated connection and requires the expected
database, unchanged backend PID, and exactly one granted advisory lock owned by that backend. It
must not call `pg_try_advisory_lock` or reacquire after loss.

```python
class ProviderAdmission(Protocol):
    def assert_intact(self) -> None: ...


@dataclass
class _PostgresAdmission:
    connection: Any
    expected_database: str
    expected_backend_pid: int

    def assert_intact(self) -> None: ...
```

- [ ] **Step 4: Write and verify RED orchestration tests**

Extend `FakeProviders` so the admission records each assertion and can fail on a selected assertion.
Assert integrity immediately before fixture insertion and before each mutation-capable activation;
assert that a selected failure prevents the following insert or CLI activation.

- [ ] **Step 5: Wire the admission object through orchestration**

Retain the value returned by `ExitStack.enter_context()`. Call `assert_intact()` immediately before
`insert_fixture()`, before the first `activate-stdin`, and before replay activation. The context
manager performs one final integrity assertion before unlocking and closing its dedicated session.

- [ ] **Step 6: Run focused GREEN tests**

```sh
uv run pytest tests/acceptance/test_provider_adapter.py tests/acceptance/test_harness.py -q
```

Expected: all focused tests pass.

### Task 2: Exact key-column fixture permission

**Files:**
- Modify: `tests/acceptance/provider_adapter.py`
- Modify: `tests/acceptance/test_provider_adapter.py`
- Modify: `tests/acceptance/test_harness.py`
- Modify: `docs/m0/setup.md`
- Modify: `docs/m0/acceptance-run.md`
- Modify: `docs/m0/teardown.md`

**Interfaces:**
- Changes: `POSTGRES_FIXTURE_GRANTS` includes `SELECT_SOURCE_KEY_COLUMN`.
- Consumes: the fixed `orders.order_id` key and the five fixed non-key columns.

- [ ] **Step 1: Write failing grant-shape tests**

Make the accepted fixture fake expose column `SELECT` on `order_id` only. Add negative cases for a
missing key grant, table-level `SELECT`, and column-level `SELECT` on each non-key column. Each must
fail through `validate_attestation()` with the fixed diagnostic.

```python
@pytest.mark.parametrize(
    "column",
    ("customer_ref", "amount", "currency", "status", "updated_at"),
)
def test_preflight_rejects_fixture_select_on_non_key_column(column: str) -> None: ...
```

- [ ] **Step 2: Verify grant tests are RED**

Run the new tests directly. Expected: the key-only accepted case fails because the current expected
grant tuple rejects every column-level `SELECT`.

- [ ] **Step 3: Implement exact column probes**

Replace the undifferentiated any-column signal with explicit effective `has_column_privilege`
checks that are suppressed when table-level `SELECT` is present. Emit `SELECT_SOURCE_KEY_COLUMN`
only for `order_id`; emit distinct unexpected labels for each non-key column. Keep the runtime
principal's table-level `SELECT` shape unchanged.

- [ ] **Step 4: Update owner provisioning and cleanup instructions**

Change PostgreSQL provisioning to:

```sql
GRANT INSERT, DELETE ON pillarmesh_m0.orders TO pillarmesh_m0_fixture;
GRANT SELECT (order_id) ON pillarmesh_m0.orders TO pillarmesh_m0_fixture;
```

State that fixture cleanup binds `order_id`, then the runtime read-only principal independently
confirms absence. Do not add executable cleanup automation.

- [ ] **Step 5: Run focused GREEN tests**

```sh
uv run pytest tests/acceptance/test_provider_adapter.py tests/acceptance/test_harness.py -q
```

Expected: all focused tests pass.

### Task 3: Private attestation record and public-output exclusion

**Files:**
- Modify: `tests/acceptance/provider_adapter.py`
- Modify: `tests/acceptance/orchestration.py`
- Test: `tests/acceptance/test_harness.py`

**Interfaces:**
- Produces: `private_attestation_record(config, attestation) -> dict[str, object]`.
- Consumes: only explicitly allowlisted declared identifiers and observed attestation fields.
- Persists: `context.attestation` in the owner-private cleanup ledger after successful validation.

- [ ] **Step 1: Write the failing private-record test**

Run the real orchestration with fakes, parse the private ledger, and require declared and observed
identity/ownership/grant facts to be present. Assert that DSNs, passwords, signing keys, credential
canaries, and row canaries are absent from the serialized attestation record.

- [ ] **Step 2: Verify the private-record test is RED**

Run the new test directly. Expected: failure because the ledger currently records only the derived
environment identity and operational context.

- [ ] **Step 3: Implement an explicit serializer**

Build the record field-by-field; do not serialize `AcceptanceConfig.environment` wholesale and do
not use an unconstrained future-facing dataclass dump. Validate the attestation first, then attach
the record and persist it before generating the acceptance key or touching provider data.

- [ ] **Step 4: Add the public-leakage regression**

Inspect the exported package and captured CLI output from the harness test. Assert that raw account
locators, role names, qualified object names, and grant inventory entries from the private record do
not occur. This must exercise the package/output boundary rather than grep source code.

- [ ] **Step 5: Run focused GREEN tests**

```sh
uv run pytest tests/acceptance/test_harness.py -q
```

Expected: all harness tests pass.

### Task 4: Integrated verification and documentation consistency

**Files:**
- Modify only if required by failures: files changed in Tasks 1–3.

**Interfaces:**
- Consumes: all preceding task interfaces.
- Produces: one verified implementation commit and one documentation-consistent operator flow.

- [ ] **Step 1: Run static and lock gates**

```sh
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy
git diff --check
```

- [ ] **Step 2: Run the complete offline suite**

```sh
uv run pytest -m 'not live' -q
```

Expected baseline or better: `402 passed, 4 deselected`.

- [ ] **Step 3: Validate repository structure from a clean representation**

Archive `HEAD`, apply the working diff inside a temporary directory, and run
`tests/repository-structure/test.sh` there so the coordinator-owned ignored `.superpowers`
directory cannot create a false failure.

- [ ] **Step 4: Review the implementation diff against the specification**

Confirm every behavioral change has a RED/GREEN test; diagnostics contain no dynamic provider
values; the audit function remains intact; the fixture credential never reaches a child process;
and no live provider, credential, owner operation, or cleanup action was attempted.

- [ ] **Step 5: Commit the implementation**

```sh
git add tests/acceptance docs/m0
git commit -m "fix: harden M0 witnessed admission"
```
