# ADR-0009: Package the Quickstart as a Local Demonstration Image

- Status: Accepted
- Date: 2026-09-20
- Context: [Architecture](../../architecture.md), [ADR-0008](ADR-0008-open-source-distribution.md)

## Context

`deploy/` is the infrastructure-neutral home for packaging and deployment material, and it has
been held empty until a decision record selects the technology and topology that belong there.
The repository README meanwhile promises a Docker Compose quickstart, so a reader can start
Heinzel without reading the build first. That promise cannot be kept while the directory must
stay empty, and it should not be kept by quietly inventing a deployment topology nobody decided.

The console now carries a purpose-built demonstration backend (`heinzel_console.demo`) served by
`heinzel-console serve`, which keeps its SQLite stores under one state directory. That is the
thing the quickstart packages.

## Decision

- `deploy/quickstart/` holds a Dockerfile and a compose file for a local demonstration. They are
  the only deployment material `deploy/` carries, alongside its README; this decision lifts the
  hold on that directory for the quickstart and for nothing else.
- The image is built from source by the person running it. It is never pushed to a registry and
  no published tag is offered.
- The quickstart packages the demonstration backend, not a deployment. It is a way to look at the
  product on one machine.
- How Heinzel is deployed for real — topology, orchestration, configuration, secret handling,
  upgrade — stays undecided. A production deployment needs its own decision record, and nothing
  in `deploy/quickstart/` may be presented as a starting point for one.

## Consequences

- The demonstration console has no authentication: the browser names the actor it wants in a
  request header, and anything unrecognised is the architect. Its network boundary is therefore
  the published port, so the quickstart publishes on `127.0.0.1` only.
- `heinzel-console serve` refuses to bind anything but a loopback host, because an unauthenticated
  console must not be reachable off the machine. A container has to bind every interface for its
  published port to reach it, so the guard is bypassed by an explicit `--container` flag and by
  nothing else: the flag is the only writer of the bind address, and without it the loopback check
  runs. The command warns on every start that what it serves is unauthenticated.
- The origin is configuration, not something derived from the bind address. Under `--container`
  the bind address is `0.0.0.0`, which no browser sends, and the port a reader opens is the
  published one rather than the bound one — so `--container` requires `--origin` or
  `HEINZEL_CONSOLE_ALLOWED_ORIGIN` and refuses to start without one. This is the consequence most
  likely to catch someone: commands whose `Origin` header does not match exactly are refused
  `same_origin_required`, which renders as a console where every page loads and every button
  fails. Anyone who changes the published port must change the configured origin with it.
- State is a named volume holding the demonstration's state directory and its SQLite stores.
  `docker compose down` keeps it and the demonstration resumes; `docker compose down -v` removes
  it and the next start is a fresh demonstration.
- The demonstration carries the request path as far as approval, and says so rather than implying
  more. A question travels intake, clarification, proposal preparation and submission, the
  requester's acceptance, the architect's review and admission, and reaches execution ready. It
  stops there: `GET /api/v1/runs`, `GET /api/v1/acquisition-receipts` and
  `GET /api/v1/requests/{request_id}/result` answer `503 capability_not_delivered`, each to the
  actor entitled to ask, naming the dependency they lack, because the demonstration deliberately
  carries no acquisition harness and no answer runtime. An empty list would read as a working
  capability with nothing in it. Data access requests are refused at intake on the same principle,
  because grant application, expiry and revocation are not delivered either — rather than accepted
  into an inbox no action could move.
- The project never distributes this image — each person builds it from source — so no third-party
  notice obligation for the Python dependencies it installs arises here. (The console bundle it
  serves ships its own JavaScript and font notices already, as
  [THIRD_PARTY_NOTICES.md](../../../THIRD_PARTY_NOTICES.md) records.) Publishing it to a registry
  would distribute those dependencies and create that obligation, which this decision does not
  discharge. Publishing a Heinzel image is a separate decision, and it must settle the notice file
  before any tag is pushed.
