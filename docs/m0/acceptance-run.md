# M0 Witnessed Acceptance Run

> **Historical.** This document records the PostgreSQL-to-Snowflake M0 thin-thread
> experiment. It remains accurate about what was built and is retained so that evidence
> stays reproducible. Snowflake is no longer a product destination, and nothing here
> defines current product scope. See the
> [managed data engineering platform addendum](../architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md).

This run is the M0 capability proof. A responsive process, passing offline suite, successful SQL
statement, pre-existing Snowflake row, or successful merge response is not proof. Read
`setup.md`, inject one complete operator environment, and use a fresh state path, output directory,
and cleanup-ledger path outside the repository. Their owner-private `0700` parents must already
exist; all three run targets themselves must be absent.
The harness derives the same environment identity and owner-private local reservation from the
fixed provider boundary for both operators. It creates that reservation exclusively and retains
file identities for state and ledger paths. It also holds a PostgreSQL advisory lock keyed by the
same identity for the complete provider-touching window, so distinct paths or hosts cannot overlap
against the shared provider environment. That lock lives on a connection used for admission only.
The harness records its backend PID and reasserts that the same backend still owns exactly one
granted advisory lock before fixture insertion, before initial activation, before replay activation,
and before normal release. A lost session fails the run; it is never treated as permission to
reacquire and continue.

The deterministic gate uses the pre-authorized CLI fallback recorded in
`transport-decision.md`. A desktop MCP-host observation is optional and cannot replace this run.

## Fixed fixture and commands

`contract.example.json` is the reviewed fixed-shape example. The harness creates an equivalent
contract using the declared handles and objects and a new opaque contract label generated
independently from the acceptance key.

```sh
uv sync --locked --all-packages
uv run python tests/acceptance/run_m0.py preflight
uv run python tests/acceptance/run_m0.py run
uv run python tests/acceptance/run_m0.py verify
uv run python tests/acceptance/run_m0.py cleanup-status
```

`preflight` is provider-read-only and creates only the transient exclusive local reservation.
`run` performs the bounded synthetic transaction. `verify` re-verifies
the completed package from the private ledger. `cleanup-status` only reads the private ledger and
prints resource digests and dispositions; it never connects to a provider or deletes anything.

## Load-bearing sequence

One `run` command performs these steps and fails closed:

1. Validate every variable name without printing a value, then run the runtime positive and
   expected-denial probes, exact database/account/role/object/grant/ownership attestations, and the
   owner-created environment marker. Both PostgreSQL `session_user` and `current_user` must equal
   the declared runtime or fixture principal; assumed-role sessions fail preflight. The fixture
   privilege inventory requires column-level SELECT on `order_id`, rejects SELECT on every non-key
   column, and rejects every unexpected supported table privilege, including table SELECT and
   MAINTAIN.
   Persist the explicit declared and observed attestation record only in the owner-private cleanup
   ledger. DSNs, passwords, signing keys, credential canaries, and row values are never fields of
   that record.
2. Generate a new acceptance key in memory and execute a fresh parameterized Snowflake query that
   requires the target count to be zero.
3. Register the exact source-row cleanup target in the private ledger, reassert the retained
   provider lock, then insert one synthetic row with the fixture-only PostgreSQL credential. Absence
   therefore precedes insertion.
4. Create the fixed contract, verify it, and invoke the installed product CLI in subprocesses.
   The fixture DSN is never passed to a product subprocess.
5. Reassert the retained provider lock, activate with the raw key on standard input, require
   terminal `succeeded`, and query Snowflake again under a new connection and statement. The
   independent row digest must equal both the manifest acceptance digest and visibility-proof
   digest.
6. Reassert the retained provider lock and repeat the identical activation arguments and standard
   input. Require the original run ID, terminal state, unchanged exact target-row value digest,
   unchanged ledger manifest digest and
   committed identity, unchanged stage listing digest, unchanged cardinality, and no additional
   tagged stage/target/ledger mutation (including an UPDATE that leaves row count unchanged).
7. Verify a contract against the dedicated `ORDERS_UNSUPPORTED_KEY` table. Require `No Valid
   Plan`, precondition 6 unsatisfied, `execution_occurred=false`, unchanged run/event/private-state
   counts, unchanged local segment count, unchanged positive target/negative target/stage/ledger
   digests, and
   unchanged persistent PostgreSQL source-data-read and Snowflake product-data-query counters. The
   harness's marker/catalog/grant/state observations are allowed; harness observations use a
   distinct observer query tag. Only the three exact compiler table-type/column/key metadata query
   shapes are allowlisted; broad `INFORMATION_SCHEMA` access is not excluded. No activation is
   attempted.
8. Export and independently verify the package with credential, row-value, acceptance-key, and
   local-path canaries.
9. Persist exact cleanup identifiers and deadlines only in the owner-readable private ledger.
   The package receives only opaque resource digests and dispositions.

All CLI subprocesses receive an explicit environment allowlist and a bounded timeout. Provider
resources are registered pessimistically before fixture insertion, stage/commit-capable activation,
and package export; ambiguous outcomes are reconciled where possible and otherwise remain
`indeterminate`, with stage resources quarantined. Any `BaseException` leaves a failed ledger with
the resources known at that point. A created or possibly created fixture row becomes immediately
eligible for authorized cleanup only when recorded events definitively prove `commit_attempted`
was never reached and provider reconciliation positively proves both the target row and commit
ledger entry absent. A positive or indeterminate target/ledger observation, missing batch identity,
unavailable event stream, or failed reconciliation conservatively preserves the 30-day
disposition. The harness does not continue to or print a success result after an assertion failure.

## CLI fallback detail

For diagnosis, the equivalent activation command is `activate-stdin`; the retired positional
`activate` form is invalid. Read the key without echo and pipe it on stdin so it never appears in
the process argument list:

```sh
read -r -s PILLARMESH_PRIVATE_ACCEPTANCE_KEY
printf '%s\n' "$PILLARMESH_PRIVATE_ACCEPTANCE_KEY" |
  uv run pillarmesh-m0 activate-stdin "$CONTRACT_DIGEST" "$SUMMARY_DIGEST"
unset PILLARMESH_PRIVATE_ACCEPTANCE_KEY
```

Contract and summary digests are non-secret. Do not paste the acceptance key, a DSN, a password,
a private key, a scan canary, or a row value into a command argument, contract identifier, log, or
gate report.

## Operator 2 and permission-fault boundary

Operator 2 begins from a clean checkout, injects the second credential set, and runs the four
commands above using only committed instructions. Record any undocumented step as a failed gate,
not as an informal workaround.

The normal `run` command never uses owner credentials, revokes permissions, restores grants, or
deletes retained resources. The separate live permission-fault window requires an environment
owner, explicit authorization for the exact operator role, exclusivity, and verified restoration.
If those approvals are absent, the live fault gate remains unverified.

The older diagnostic tests remain opt-in and are not acceptance evidence:

```sh
uv run pytest -m live tests/integration/test_postgresql_live.py
uv run pytest -m live tests/integration/test_snowflake_live.py
uv run pytest -m live tests/integration/test_postgres_snowflake_live.py
```
