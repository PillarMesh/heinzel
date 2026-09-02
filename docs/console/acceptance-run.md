# Data architect console — governed-local acceptance run

This is the offline acceptance procedure for the console's `governed_local` mode. It
proves that console commands reach the owning services' transactions, and that the
resulting state is recorded by those services rather than by the console.

**This is not a live claim.** Every run below is offline: disposable SQLite databases,
a local-acceptance warehouse provider harness, and no external account. A live claim
requires a new transaction against a real provider account, traced to its terminal
state, with sanitized evidence from every hop. Nothing here establishes one. Read
`known-gaps.md` beside this file before interpreting any result as coverage.

## What the run proves

`tests/end-to-end/test_console_governed_journey.py` drives a real Starlette application
built by `create_app(backend=GovernedConsoleBackend(...))`. Every command is an HTTP
`POST` carrying an `Origin` header, a session CSRF token and an `Idempotency-Key`, and
every claim about what happened is verified by **reopening the owning service's SQLite
database from disk** after the response. A console projection is never accepted as
evidence for a transaction.

| Step | Console request | Independent verification |
| --- | --- | --- |
| Managed warehouse | `POST /api/v1/setup/warehouse-binding` | A freshly opened `SQLiteWarehouseRepository` reports the binding `ready` with `provisioned_at` set |
| Operation projection | `GET /api/v1/operations/{handle}` | The response carries the opaque handle only; the provider resource handle and the binding identifier are absent from the body |
| Request intake | `POST /api/v1/requests` | A freshly opened `SQLiteRequestRepository` holds the request under the trusted tenant and requester |
| Access request scope | `POST /api/v1/requests` (`data_access`) | The stored payload keeps the exact requested fields, access mode and expiry |
| Conversation reply | `POST /api/v1/requests/{id}/conversation` | The stored conversation entry holds the exact body |
| Stale reply refused | same route, wrong digest | `409` with `recovery_action: reload`, and the reopened repository holds no entry |
| Clarified outcome acceptance | `POST /api/v1/requests/{id}/clarified-outcome/acceptance` | A reopened fulfillment repository holds an approval under the requester principal whose `subject_digest` equals the digest of the stored clarified outcome |
| Architect decision | `POST /api/v1/inbox/{id}/decisions` | The same reopened repository holds an approval under the architect authority whose `subject_digest` equals the digest of the proposal subject and whose `proposal_digest` equals the digest of the compiled proposal |
| Requester privacy | `GET /api/v1/requests/mine`, `.../clarified-outcome`, `GET /api/v1/inbox/{id}` | The unapproved candidate text appears in none of them; the inbox returns `404` to a requester |
| Undelivered capabilities | process package, retry, runs, data products, catalog, dashboards, previews, links | Each returns `503` with `capability_not_delivered`; `GET /api/v1/workspace` reports each as `not_delivered` |
| Idempotency key | two sequential `POST /api/v1/requests` under one key | Two distinct owning identities are minted, so the console is not acting as a replay authority |
| Tenant isolation | `GET /api/v1/requests/{id}/conversation` for another tenant's request | `404` |
| Transient failure | warehouse confirmation with the repository closed | `503` `downstream_unavailable` with `recovery_action: retry`, never a denial or a verdict |

The terminal state this journey reaches for a fulfillment request is *every required
approval recorded against the exact proposal*, with the request still
`awaiting_approval`. Admission to execution is a separate owning transaction that the
console contract exposes no command for; see `known-gaps.md`.

## Running it

From the repository root:

```sh
uv sync --locked --all-packages
uv lock --check
uv run pytest apps/console/server/tests tests/end-to-end/test_console_governed_journey.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
./tests/repository-structure/test.sh
```

The browser gates (`npm ci`, `npm run lint`, `typecheck`, `check:contracts`, Vitest,
`build`, Playwright) belong to the console web workspace and are run from
`apps/console`. They are not part of this procedure and their result is not evidence
for anything recorded above.

## Recorded run — 2026-09-02

| Command | Result |
| --- | --- |
| `uv run pytest apps/console/server/tests tests/end-to-end/test_console_governed_journey.py -q` | 258 passed |
| `uv run pytest -m "not live"` | 2981 passed, 14 deselected |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed |
| `uv run mypy` | passed, 147 source files |
| `uv lock --check` | passed |
| `./tests/repository-structure/test.sh` | reported `UNEXPECTED: .superpowers`, an untracked directory that predates this change |

The 14 deselected tests are the live-marked integration suites. They are an explicit
gap in this run, not passing evidence.

## Reading a governed failure

Governed-local mode never falls back to fixture content. The states below stay distinct
and must not be collapsed when triaging:

- `503` with `capability_not_delivered` — no owning transaction exists. See
  `known-gaps.md`; this will not resolve by retrying.
- `503` with `downstream_unavailable` and `recovery_action: retry` — a transient owning
  failure. It is never recorded as a denial or a non-conformance verdict.
- `409` with `recovery_action: reload` — the owning revision or the exact reviewed
  digest moved. Reload and review the new revision before resubmitting.
- `404` — the resource does not exist, is another tenant's, or the actor holds no
  authority over it. These answer identically on purpose, so probing cannot enumerate.
- `422` — the submitted command is malformed or names something the owning transaction
  cannot express.
