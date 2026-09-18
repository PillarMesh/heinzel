# Run request-to-product acceptance

Complete [setup](setup.md) first. The current executable check is an offline precursor to Task 16,
not the complete live acceptance journey.

## Run the request-first gate

From the repository root:

```sh
uv run pytest tests/acceptance/test_run_request_to_product.py -q \
  --basetemp "$HEINZEL_REQUEST_PRODUCT_PRIVATE_ROOT/pytest"
```

The test invokes `execute_postgresql_request_to_product` with disposable storage. A passing run
proves all of the following together:

1. The tenant's product-publication authority contains no pre-created publication.
2. Request management creates a new titled request.
3. Request management durably records the exact typed product-intent candidate.
4. An architect approval is bound to the same request revision and exact intent.
5. The approved intent maps to the narrow revenue-by-region product IIR.
6. The publication authority is still empty before and after compilation.
7. The compiler returns `No Valid Plan` and reports that execution did not occur.
8. A pre-existing publication and a mismatched approved metric are rejected.

The external interpreter named by the fixture proposes a candidate only. Request management owns
the candidate and approval. The compiler owns the legality decision. The acceptance harness does
not approve legality, execute SQL, publish a product, create a dashboard, or invent terminal state.

## Interpret the expected outcome

`No Valid Plan` is the expected safe outcome while the request journey lacks composition of its
runtime-signed cardinality evidence, ClickHouse magnitude integration with a committed generation and
answer reader, complete D1-D8 JSON-decoding proof and mutation regression, live two-engine result
equivalence, and independent approval. Provider-owned Ed25519 observations exist for PostgreSQL and
ClickHouse, and the compiler can verify a fresh matching envelope. Given strict owning-service source
and target bindings, it composes the one-generation renderer into a revalidated
`ProductPhysicalPlan` candidate and satisfies precondition 13. Runtime can derive, sign, and durably
replay cardinality evidence from committed LAND receipts, and the compiler can satisfy precondition
14 only when that envelope matches the exact candidate. This acceptance runner does not yet supply
those authorities. The candidate is not admitted, signed, or executed. Raw observations and raw
cardinality values remain untrusted. The focused test passing therefore establishes the request-first
ordering and absence of downstream effects. It is not evidence that a data product was delivered.

If the compiler starts returning an executable plan before those gates are satisfied, or if any
product publication exists at any of the three checkpoints, fail the run and investigate. Do not
change the assertion merely to accept execution.

## Run the applicable offline repository gates

After the focused test passes, run the complete offline checks:

```sh
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
./tests/repository-structure/test.sh
```

A skipped, deselected, or live-marked test is an explicit evidence gap. Record the exact command,
source commit, result counts, and failures; do not rewrite old counts in the acceptance ledger as a
new run.

## Live acceptance remains blocked

Do not mark Task 16 delivered until one new correlated transaction starts with no pre-created
product and reaches all of these durable outcomes for both PostgreSQL and ClickHouse:

- fresh source cohort, acquisition observation, LAND receipt, and committed checkpoint;
- deterministic plan and materialization receipt bound to current provider observations;
- authoritative catalog publication and independently reconciled business result;
- native and delegated-agent answers with the same result digest;
- table and authorized CSV, durable dashboard receipt, and usable authenticated dashboard link;
- grant expiry and provider revocation followed by query, download, agent, and dashboard denial;
- recovery at each durable boundary without duplicate effects;
- backup and restore reconciled against the warehouse, OpenMetadata, and Superset;
- sanitized correlation evidence and independent review of legality, access, and mutation survivors.

Existing component live tests may be rerun for diagnosis, but their separate transactions cannot be
spliced into one Task 16 terminal claim. A rendered page, healthy service, existing row, HTTP 2xx,
or provider receipt without the rest of the correlation chain is insufficient.

The PostgreSQL live answer acceptance composes the exact compiler-signed Decimal57/9 declaration
through materialization and the current answer reader. The reader locks the committed relation,
re-observes the signed bounds, reconstructs the magnitude-bearing commit reference, and only then
returns the governed HTTP result. A fresh run produced the expected two exact rows. This proves the
PostgreSQL source-to-answer component path; it does not prove compiler admission or the complete
request-to-product journey described below.

When a composed live runner is implemented, this document must name its exact command, private
workspace requirements, evidence model and validator, timeout, terminal output, and failure recovery
procedure before anyone runs it as a release gate.

After the focused gate, follow [teardown](teardown.md).
