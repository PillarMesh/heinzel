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

Then open <http://127.0.0.1:8000>. The first build compiles the console bundle and installs the
Python environment, so it takes a few minutes; later starts reuse both.

## It has no authentication

Anyone who reaches the published port acts as the data architect. There is no login, no
password and no authorization boundary — the browser names the actor it wants in a request
header, and anything unrecognised is the architect.

The published port is therefore the whole boundary. Compose publishes it on `127.0.0.1` only,
so it is reachable from this machine and nowhere else. Do not publish it on another interface,
do not put it behind a tunnel, and do not expose it to a network.

If you change the published port, change `HEINZEL_CONSOLE_ALLOWED_ORIGIN` in `compose.yaml` to
match. The console accepts a command only from the origin it is configured with, and a mismatch
serves a console where every page loads and every button is refused `same_origin_required`.
Spell the origin exactly as a browser sends it: no trailing slash, no path, no uppercase, and no
default port — a console published on port 80 is `http://127.0.0.1`, not `http://127.0.0.1:80`.

## What it shows

One stakeholder question is waiting in the inbox: *What is the daily order count?*, asked by
`requester-demo` for a weekly operations review. You open the console as the architect,
`architect-demo`.

From the inbox you can record a clarification, prepare an answer proposal — the demonstration
grounds it in its own approved semantic publication and refuses anything that publication cannot
ground — submit that proposal to the requester, and record your own approval of it.

Admission then waits on the requester. A proposal needs two approvals — the architect's, and the
requester's acceptance of the clarified outcome — and until both are recorded the inbox reports
that admission is unavailable and why. The console cannot act as anyone but the architect, so
the browser cannot give the requester's acceptance. Sending it means naming the other actor in
the header the demonstration switches roles with (this needs `curl` and `jq`):

```sh
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
  -d "$(jq -c '{expected_revision: .revision,
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

## Reset it

```sh
docker compose -f deploy/quickstart/compose.yaml down -v
```

The demonstration keeps its state — SQLite stores under `/var/lib/heinzel` — in a named volume.
`down -v` removes that volume, so the next start is a fresh demonstration with the seeded
question waiting again. `down` without `-v` keeps it, and the next start resumes exactly where
you left off.

A request that reached a terminal state is not re-seeded: the seed recognises its own question
whatever state it reached, so `down -v` is the way back to a clean demonstration.
