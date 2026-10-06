# Quickstart

A local demonstration of the governed request path, from a stakeholder's question to a governed
answer, packaged by
[ADR-0009](../../docs/architecture/decisions/ADR-0009-container-packaging.md). It is a way to
look at the product on one machine. It is not a deployment, and nothing here is a starting
point for one.

Two containers: the console, and the PostgreSQL warehouse it provisions and answers over.

## Start it

From the repository root:

```sh
docker compose -f deploy/quickstart/compose.yaml up --build
```

The first build compiles the console bundle and installs the Python environment, so it takes a
few minutes; later starts reuse both. The first *start* then provisions the warehouse, acquires
and lands its seeded source and runs dbt to materialize a product, which takes a further half
minute or so; later starts find that product and skip straight to serving. When it reports
itself healthy, open <http://127.0.0.1:8000>.

To publish it somewhere else, set `HEINZEL_PORT`, which moves the published port and the origin
the console accepts together:

```sh
HEINZEL_PORT=9000 docker compose -f deploy/quickstart/compose.yaml up --build
```

A start that fails with `bind: address already in use` means something else holds port 8000;
set `HEINZEL_PORT` to a free port and open the console there instead.

## It has no authentication

Anyone who reaches the published port acts as the data architect. There is no login, no
password and no authorization boundary — the browser names the actor it wants in a request
header, and anything unrecognised is the architect.

The published port is therefore the whole boundary. Compose publishes it on `127.0.0.1` only,
so it is reachable from this machine and nowhere else. Do not publish it on another interface,
do not put it behind a tunnel, and do not expose it to a network.

The console accepts a command only from the one origin it is configured with, and a mismatch
serves a console where every page loads and every button is refused `same_origin_required`.
`HEINZEL_PORT` moves the published port and that origin together, which is why it is the way to
publish the demonstration elsewhere. Setting `HEINZEL_CONSOLE_ALLOWED_ORIGIN` directly means
spelling the origin exactly as a browser sends it — no surrounding space, no trailing slash, no
path, no uppercase, no default port (a console published on port 80 is `http://127.0.0.1`, not
`http://127.0.0.1:80`) — and the console refuses to start on any other spelling rather than
adjusting it, naming the spelling to use instead.

## What it shows

One stakeholder question is waiting in the inbox: *What is the daily order value?*, asked by
`requester-demo` for a weekly operations review. You open the console as the architect,
`architect-demo`.

That question is not just its wording. It carries the governed terms it was composed from — the
approved metric the answer measures and the approved dimension it is broken down by — and those
terms are what the answer resolves. Asking a new one as `requester-demo` means composing it the
same way: the submission form offers exactly the terms this workspace's publication carries, which
here is one metric and one dimension, and a question composed from anything else cannot be built.

From the inbox you can record a clarification, prepare an answer proposal — the demonstration
grounds it in its own approved semantic publication and refuses anything that publication cannot
ground — submit that proposal to the requester, and record your own approval of it.

Admission then waits on the requester. A proposal needs two approvals — the architect's, and the
requester's acceptance of the clarified outcome — and until both are recorded the inbox reports
that admission is unavailable and why. The console cannot act as anyone but the architect, so
the browser cannot give the requester's acceptance. Sending it means naming the other actor in
the header the demonstration switches roles with (this needs `curl` and `jq`).

Send it after you have submitted the proposal, and not before: there is no clarified outcome to
accept until then, so the two fields read from it below are sent as `null` and the console
refuses the command `422 invalid_request`, naming `expected_revision`. `.data[0]` is the
requester's first request, which is the seeded one until you create another; after that, select
by question text instead.

```bash
BASE=http://127.0.0.1:8000
AS_REQUESTER='x-heinzel-actor: requester-demo'
REQUEST=$(curl -fsS -H "$AS_REQUESTER" "$BASE/api/v1/requests/mine" | jq '.data[0]')
curl -fsS -X POST \
  "$BASE/api/v1/requests/$(jq -r .request_id <<<"$REQUEST")/clarified-outcome/acceptance" \
  -H "$AS_REQUESTER" \
  -H "origin: $BASE" \
  -H "content-type: application/json" \
  -H "idempotency-key: requester-acceptance" \
  -H "x-csrf-token: $(curl -fsS -H "$AS_REQUESTER" "$BASE/api/v1/session" | jq -r .data.csrf_token)" \
  -d "$(jq -c '{expected_revision: .clarified_outcome.revision,
                clarified_outcome_digest: .clarified_outcome.statement_digest,
                active_role: "requester", decision: "approve"}' <<<"$REQUEST")"
```

Reload the inbox afterwards: admission is offered, and admitting the proposal delivers the
answer. The admission is where a question differs from a data access request: admitting it
compiles a governed query over the published product, checks it against the answer scope
policy's ceilings, runs it as a read-only role, and delivers the result — so the request reaches
`delivered` rather than stopping at `execution_ready`.

The answer belongs to the requester who asked for it, so read it as them:

```bash
curl -fsS -H "$AS_REQUESTER" "$BASE/api/v1/requests/$(jq -r .request_id <<<"$REQUEST")/result" \
  | jq '.data.rows'
```

Three rows, one per day the seeded source carries: `30.000000000`, `125.500000000` and
`99.000000000`. Asking as the architect answers `404` — a result belongs to whoever asked.

## What the answer went through

Before the console listened at all, it provisioned the warehouse with five least-privilege
logins, seeded a source table, acquired its approved columns through the runtime's governed
acquisition — under an acquisition contract activated over an observation of that source, which
refuses the whole acquisition if the connecting role can reach more than its declaration —
landed them under a receipt and acknowledged them against the source checkpoint, compiled the
product through the compiler's own entry point, ran that statement with dbt, and published the
generation through the product authorities.

Admitting the question then resolved the requester's entitlement from a signed authority over
loopback TLS, validated the question against the approved scope policy, read which column
answers which approved term out of the durable query binding, compiled the statement, bounded
its scan by the product relation's measured size, and admitted the plan under the policy's
ceilings.

None of that is visible in the console, and almost all of it is load-bearing: the answer refuses
outright if the entitlement, the materialization receipt, the freshness observation, the query
binding or the scan bound is missing, or if the product's relation can still be written by a role
that can log in.

The compiled model the answer verifies is the exception, and it is the one worth knowing about.
Given no signed model, the provider returns no decimal magnitude checks and answers anyway — so a
demonstration that lost it on restart would come back enforcing nothing about its own output
magnitudes and look exactly the same doing it. That is why it is stored rather than held in the
process that signed it.

## Where it stops

The answer is the end of the demonstration. Beyond it:

- `GET /api/v1/runs` answers `503 capability_not_delivered` to the architect, who is the actor
  entitled to ask. `GET /api/v1/acquisition-receipts` now serves what the startup acquisition
  recorded. What neither reflects is a second acquisition: the demonstration acquires and lands
  its source once and never again — the acknowledgement advances the source checkpoint, and from
  there the provider admits no second snapshot — so it carries no run harness that would make
  runs a list worth showing.
- The answer scope policy carries no disclosure classifications, though the demonstration's
  contract classifies its product `commercial`. Carrying one requires a policy authority's
  approval, and the demonstration has two actors: an architect and a requester. So it shows no
  disclosure control over a classified product.
- The product is one generation of three rows, materialized once. There is no refresh, no second
  generation and no scheduled run.
- Nothing reads a question's words. They are the label of what was asked; the governed terms
  beside them are what is resolved. A question submitted without them — which the request model
  still accepts, as every question did before the builder existed — is refused at the answer
  rather than answered about something else.
- Admitting a question is the governed answer's admission, not the fulfillment service's, and
  they are exclusive: each needs a request that has not been admitted yet, so whichever runs
  first makes the other refuse. The console still checks the actor's role, the revision, the
  proposal digest and the approvals it has projected against that proposal. What it does not
  check on this path is what the fulfillment service would have: whether each approver still
  holds the authority they approved under, and whether the proposal's impact authority or policy
  snapshot has drifted since it was compiled. No fulfillment admission receipt is recorded
  either, so a question's evidence names the governed admission alone. Composing both needs a
  service that can judge admissibility without consuming it, which is not delivered.

Data access requests are refused at intake for a different reason: grant application, expiry and
revocation are not delivered, so accepting one into an inbox no action could move would be a
false promise.

[docs/status.md](../../docs/status.md) states what Heinzel does today, and how each claim is
proved.

## Stop it

```sh
docker compose -f deploy/quickstart/compose.yaml down
```

The demonstration keeps its state — SQLite stores under `/var/lib/heinzel` — in a named volume,
and the warehouse keeps its data in another. `down` leaves both in place, and the next start
resumes exactly where you left off, finding the product it already materialized.

Discard them together. The console refuses to start when its state directory and its warehouse
disagree about whether the product has been materialized, because materializing again would
either commit a generation the warehouse already holds or publish authority for a relation that
is not there — and it names `down -v`, which removes both.

## Reset it

```sh
docker compose -f deploy/quickstart/compose.yaml down -v
```

`-v` removes that volume as well, so the next start is a fresh demonstration with the seeded
question waiting again.

A request that reached a terminal state is not re-seeded: the seed recognises its own question
whatever state it reached, so `down -v` is the way back to a clean demonstration.

Reset it the same way after updating the repository. The volume keeps whatever the previous
version wrote, and a newer image is not obliged to read it, so a demonstration that behaves
oddly after a rebuild is one to start again from an empty volume.
