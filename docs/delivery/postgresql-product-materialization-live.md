# PostgreSQL product materialization live acceptance

This acceptance starts a new PostgreSQL cluster on loopback, creates dedicated source-acquisition,
LAND, and materialization roles, reads a newly inserted source generation through the PostgreSQL
acquisition provider, writes it through the PostgreSQL destination provider, and executes one
compiler-signed model through the locked dbt executable. It then verifies the observed table before
atomically switching the stable consumption view and recording the retained generation pointer. A
file-backed runtime ledger proves exact replay without a second generation.

## Prerequisites

- Python and workspace packages installed with `uv sync --locked --all-packages`.
- PostgreSQL `initdb`, `pg_ctl`, and `postgres` binaries in one directory.
- The locked `dbt-core==1.10.13` and `dbt-postgres==1.10.2` executable installed by the workspace.
- A local machine where an isolated loopback PostgreSQL cluster may be created and removed.

Run:

```sh
HEINZEL_TEST_POSTGRES_BIN_DIR=/opt/homebrew/bin \
  uv run pytest -m live \
  tests/integration/test_postgresql_product_materialization_live.py -q
```

The test supplies each role password through an environment variable referenced by the private dbt
profile. Passwords are random per run and do not enter commands, receipts, assertion output, or
source control. The cluster helper stops the server and removes its temporary data directory on
exit. Both dbt subprocesses set `DBT_SEND_ANONYMOUS_USAGE_STATS=false`.

Resolving the dbt packages currently selects mypy 1.19.1 instead of 1.20.2 and pathspec 0.12.1
instead of 1.1.1. Both remain within the repository's declared constraints; run the strict mypy and
lock checks after dependency updates.

The receipt records the exact signed model and input generation digests, the pinned dbt version,
manifest and run-results digests, deterministic lineage digest, observed schema and row count,
provider commit reference, generation number, commit time, and minimum retention time. This fixture
does not define signed dbt quality tests, so the receipt explicitly records zero assertions and
`not_asserted`; it is not evidence that product quality passed. It also records no freshness success:
freshness must come from the owning source watermark observation.

The acceptance derives that source watermark from the `source_updated_at` values returned by the
acquisition provider. Only after LAND returns the immutable generation receipt does it persist a
`SourceFreshnessObservation` bound to the exact LAND receipt digest and generation identifier. It
closes and reopens the freshness repository and verifies the measured watermark and data observation
reference. Materialization time is never substituted for the source watermark.

OpenMetadata is intentionally unavailable in this local acceptance. Catalog publication raises the
runtime's typed publication error and remains pending while the committed generation and stable view
stay queryable. No fixture catalog response is counted as publication success.

PostgreSQL added the table-level `MAINTAIN` privilege in version 17. The acquisition provider probes
it on PostgreSQL 17 and newer. On older servers the version-gated predicate evaluates false because
that privilege cannot exist; all available mutation, schema, database, role, unrelated-relation,
and sequence denials remain enforced. The local PostgreSQL 14 run therefore proves the complete
fresh source-acquisition through materialization path without weakening its available privilege
attestation.

The signed SQL model is an execution acceptance fixture. The product compiler currently rejects
product SQL while its restricted-SQL legality review is pending, so this run proves the signed dbt
execution and materialization boundary rather than product-plan compilation legality.
