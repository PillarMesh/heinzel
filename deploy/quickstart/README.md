# Quickstart

A local demonstration of the governed request path, from a stakeholder's question to a governed
answer, packaged by
[ADR-0009](../../docs/architecture/decisions/ADR-0009-container-packaging.md). It is a way to
look at the product on one machine. It is not a deployment, and nothing here is a starting
point for one.

Two containers: the console, and the PostgreSQL warehouse it provisions and answers over. A third
is optional and off by default — the Superset the demonstration can publish a dashboard to, behind
a Compose profile. [Publishing a dashboard to Superset](#publishing-a-dashboard-to-superset-and-what-it-costs)
says what it adds and what it costs.

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

## Provisioning the warehouse through warehouse-control, and what it costs

By default the demonstration is *given* a warehouse: `HEINZEL_DEMO_WAREHOUSE_DSN` names the
`warehouse` container above, the console provisions schemas, roles and grants inside it, and
warehouse-control never sees it. That is why the workspace card reports **Managed warehouse —
Not delivered** and the setup surface answers `503 capability_not_delivered`: no governing
service owns that database, and reporting a binding for it would claim a managed warehouse where
a temporary local database is.

Setting `HEINZEL_DEMO_WAREHOUSE_CONTROL=1` instead takes the other path. warehouse-control holds
a binding, drives it from `draft` to `ready`, and `PostgreSQLWarehouseProvider` creates the
warehouse itself by driving the Compose project at
[`warehouse-control/compose.yaml`](warehouse-control/compose.yaml) — a PostgreSQL container over
TLS with client certificates, eight separated principal classes with positive and denial probes,
and a backup restored into a second instance and verified before the binding is admitted. The
console then reports that binding, so the setup surface answers and its `foundation` stage
completes. The two settings are exclusive; a console given both refuses to start.

**This is opt-in, and it has to be.** Creating containers means this process runs `docker`, so:

- It needs a reachable Docker daemon. A console running in a container needs that daemon's
  socket bind-mounted into it (`/var/run/docker.sock`), and **access to the Docker socket is
  root on the host**. Anything that reaches the console — and the console has no authentication,
  so that is anyone who reaches the published port — could start a container that mounts the
  host's filesystem. The quickstart's `compose.yaml` therefore does not mount it, and this
  setting is not part of the quickstart's own environment.
- Compose bind-mounts are resolved by the daemon, on the host. The provider mounts its private
  directory — TLS key, client certificate, bootstrap password — into the warehouse container by
  path, so that path has to mean the same thing to the daemon as it does to the console. It does
  when the console runs from a checkout on the host. It does not when the console runs in a
  container whose state directory is a named volume, because the host has no such path.
- The compose project has to be on disk beside the console. The quickstart image does not carry
  `deploy/`, so this path is for running `heinzel-console serve` from a checkout.

Run it from a checkout, where all three hold:

```sh
HEINZEL_DEMO_WAREHOUSE_CONTROL=1 uv run heinzel-console serve --state-dir ./.heinzel-state
```

Without a reachable daemon the console refuses to start and says so, naming the socket and the
setting to unset. It is the same refusal if `docker` is not installed at all.

This path answers the seeded question, over the warehouse warehouse-control provisioned. It
acquires, lands, materializes through dbt and publishes a product generation in that warehouse,
and the governed journey runs through it to delivered rows -- so the one path that produces a
binding warehouse-control owns is also the one the demonstration answers from.

Two limits. It answers only in the start that provisioned the warehouse: provisioning rotates the
administering login to an operation secret minted per start and written nowhere, so a later start
adopts a `ready` binding holding none of the credentials that warehouse accepts. It reports the
binding and reports every answer capability as not delivered, which is what it reports with no
warehouse at all. Keeping those secrets in the state directory would make it resumable, and this
path deliberately keeps them in the process instead; discard the state directory and the Compose
project together, both of which the refusal names. A provisioning that stopped half-way is refused
outright for the same reason.

It does publish a dashboard, and it is the only path that can: a dashboard dataset connection
cites a warehouse-control binding, and the default path's database has none. Two manual steps,
because Superset runs in this file's Compose project while the console runs from a checkout, and
they do not reach that warehouse the same way. The console reaches it on the loopback port the
provider published, which belongs to the host; from inside a container that address is the
container. Superset reaches it by name on a network they share.

Start Superset and the console, then, once the console has provisioned its warehouse:

```sh
# The network the provider created for it, and the Superset container to put on that network.
docker network connect "$(docker network ls --format '{{.Name}}' | grep -- '-private$')" \
  heinzel-quickstart-superset-1

# Superset's own copy of the client certificate the warehouse's `pg_hba.conf` verifies. libpq
# refuses a private key any group or world can read, and only the user presenting it can own such
# a file -- so the console publishes the material readable and Superset makes the private copy.
docker exec -u superset heinzel-quickstart-superset-1 sh -ceu '
  install -d -m 0700 /app/superset_home/warehouse-tls
  install -m 0600 /heinzel-private/warehouse/* /app/superset_home/warehouse-tls/'
```

Set `HEINZEL_DEMO_SUPERSET_WAREHOUSE_TLS_DIRECTORY` to that last directory before starting the
console. Without it the console publishes nothing for Superset to copy and reports dashboard
publication as not delivered, rather than publishing a dashboard whose every query would be
refused -- a refusal that would arrive at whoever opened it.

That copy of a client key is readable by anything that can read the directory the console shares
with Superset. It is the same compromise this demonstration already makes with Superset's own
server key, it is a key minted per start by an authority minted per start, and it is why neither
belongs in a deployment: a deployment issues Superset its own client certificate and resolves it
from a secret store.

The default path publishes no dashboard, for an unrelated reason given below.

[docs/demonstration-gaps.md](../../docs/demonstration-gaps.md) records what is proved about this
path and what is not. The offline suite proves the Compose operations it issues and their order,
that a failure at each one is classified and reported rather than swallowed, and that the setup
surface answers from a binding already carried to `ready`. It does not prove the live
provisioning, which needs a daemon; what proves that provider is the warehouse-lifecycle
acceptance run, against the same provider over its own Compose project.

## Publishing a dashboard to Superset, and what it costs

A dashboard needs a BI service to publish to, and the demonstration carries none by default. One
is available behind the `dashboards` Compose profile. Without that profile `docker compose up`
creates neither of its two services and the demonstration is exactly what the rest of this file
describes, which is the point: the profile is opt-in because of what it costs.

**It adds about 1.3 GB of image.** That is the official Superset image plus the PostgreSQL driver
it does not carry, measured as `docker images` reports the built result — downloaded and built on
the first `--profile dashboards up` and reused afterwards. It also adds a second long-running
container with a 1.5 GB memory limit, beside a console, a warehouse and a dbt run already sharing
this machine.

It needs two secrets, and neither has a default or a value in version control: a password for the
one Superset admin account, and Superset's own secret key. Generate them into the shell that starts
the demonstration, so they are in the environment and not in a file:

```sh
export HEINZEL_SUPERSET_ADMIN_PASSWORD=$(openssl rand -base64 24)
export HEINZEL_SUPERSET_SECRET_KEY=$(openssl rand -base64 48)
docker compose -f deploy/quickstart/compose.yaml --profile dashboards up --build
```

Then open <https://127.0.0.1:8088> and sign in as `admin` with the password that command generated
(`echo "$HEINZEL_SUPERSET_ADMIN_PASSWORD"` prints it again). Set `HEINZEL_SUPERSET_PORT` to publish
Superset on another port; nothing is spelled twice, so unlike `HEINZEL_PORT` it moves on its own.

Four things to know before you run it.

- **Keep both secrets for the life of the demonstration.** Superset's metadata database is in a
  named volume, and the secret key is what encrypts the credentials stored in it — the warehouse
  password a registered database connection carries among them. A start with a different key is
  not refused: it initializes without a word of complaint, and leaves Superset holding material
  encrypted under a key it no longer has. So export them once and reuse them, or reset with
  `--profile dashboards down -v` and generate both again. Unset is the one failure here that tells
  you exactly what to do: each container refuses before it does anything, naming the variable.
- **Superset serves TLS, from material the console mints.** `SupersetCredentials` refuses a base
  URL that is not HTTPS, which holds on a Compose network nothing outside can reach as much as
  anywhere else. There is no key custody in a demonstration, so the console mints a throwaway
  authority and a server certificate for it at startup, into a volume Superset reads read-only;
  Superset waits for that material rather than exiting without it, because the two containers
  start at the same time. The authority is not one a browser knows, so a browser will warn before
  it loads <https://127.0.0.1:8088>. That warning is correct: the certificate was minted by this
  demonstration minutes ago and signed by nothing else.
- **One admin account and a published port are its whole boundary.** Compose publishes Superset on
  `127.0.0.1` only, for the same reason it publishes the console that way. Do not publish it on
  another interface, and do not reuse either secret anywhere else.
- **Stop and reset it with the profile named.** Its metadata database and the minted TLS material
  are two more named volumes, and they have to go with the console's state: a Superset that kept
  the certificate it was serving while the console minted a new authority would be a Superset the
  console no longer trusts. A `down` that does not name `--profile dashboards` does not reach these
  containers at all, and says nothing about it — see [Stop it](#stop-it) for what that looks like.

What the console does with that Superset is the publication path's own, and
[docs/demonstration-gaps.md](../../docs/demonstration-gaps.md) is where that is stated rather than
here. This profile is the Superset to publish to; it does not by itself make the demonstration
publish.

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

Name the profile if you started it: `down` without `--profile dashboards` leaves the Superset
containers running and their volumes in place, and does not report them — they are the project's
own, so they are not orphans, and `--remove-orphans` does not reach them either. With `-v` it is
worse than incomplete: it removes the console, reports `Resource is still in use` for the TLS
volume and the network the running Superset still holds, and exits 0. That reads as a reset and is
not one.

```sh
docker compose -f deploy/quickstart/compose.yaml --profile dashboards down
```

## Reset it

```sh
docker compose -f deploy/quickstart/compose.yaml --profile dashboards down -v
```

`-v` removes those volumes as well, so the next start is a fresh demonstration with the seeded
question waiting again. The profile is named for the reason above, and is harmless when Superset
was never started; drop it only if you are certain it never was.

A request that reached a terminal state is not re-seeded: the seed recognises its own question
whatever state it reached, so `down -v` is the way back to a clean demonstration.

Reset it the same way after updating the repository. The volume keeps whatever the previous
version wrote, and a newer image is not obliged to read it, so a demonstration that behaves
oddly after a rebuild is one to start again from an empty volume.
