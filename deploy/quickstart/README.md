# Quickstart

A local demonstration of the governed request path, packaged as one container by
[ADR-0009](../../docs/architecture/decisions/ADR-0009-container-packaging.md). It is a way to
look at the product on one machine. It is not a deployment, and nothing here is a starting
point for one.

## Start it

From the repository root:

```sh
docker compose -f deploy/quickstart/compose.yaml up --build
```

The first build compiles the console bundle and installs the Python environment, so it takes a
few minutes; later starts reuse both. When it reports itself healthy, open
<http://127.0.0.1:8000>.

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

Reload the inbox afterwards: admission is offered, and admitting the proposal moves the request
to `execution_ready`.

## Where it stops

`execution_ready` is the end of the demonstration. It carries the request path — intake,
clarification, proposal preparation and submission, acceptance, approval and admission — and
nothing beyond it.

The demonstration deliberately carries no acquisition harness and no answer runtime, and it says
so rather than showing an empty page that reads like a working capability with nothing in it:

- `GET /api/v1/runs` and `GET /api/v1/acquisition-receipts` answer `503
  capability_not_delivered` to the architect, who is the actor entitled to ask.
- `GET /api/v1/requests/{request_id}/result` answers `503 capability_not_delivered` to the
  requester it belongs to, and `404` to an architect, who is not entitled to ask for it.

Data access requests are refused at intake for a different reason: grant application, expiry and
revocation are not delivered either, so accepting one into an inbox no action could move would be
the same false promise.

[docs/status.md](../../docs/status.md) states what Heinzel does today, and how each claim is
proved.

## Stop it

```sh
docker compose -f deploy/quickstart/compose.yaml down
```

The demonstration keeps its state — SQLite stores under `/var/lib/heinzel` — in a named volume,
which `down` leaves in place. The next start resumes exactly where you left off.

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
