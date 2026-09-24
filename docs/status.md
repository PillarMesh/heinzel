# Capability status

This page states what Heinzel does today, and how each claim is proved.

- **Delivered**: an automated test drives a fresh transaction through the whole capability to its
  terminal state, with no test double standing in for a part of the capability itself.
- **Partial**: the core works and is tested, but part of the capability is missing or is proved
  only in part.
- **Not yet**: it does not work yet.

The **Proof** column says where that evidence comes from. **Offline** tests run in CI on every
pull request against the owning services and their SQLite reference stores. **Live** tests run
against real engines in Docker, are opt-in, and are not run on every pull request (see
[Live tests](../README.md#live-tests)). The warehouse lifecycle acceptance run is the exception: CI
runs it whenever a change touches it.

## Capabilities

| Capability | Status | What works today | Proof |
| --- | --- | --- | --- |
| Request intake and titles | Delivered | Requests are validated, bound to a digest, stored and read back exactly. | Offline |
| Clarification and revisions | Partial | Conversations persist in order. A stale edit is refused as a conflict and the client re-reads the current revision; retrying a failed operation from the console is not delivered. | Offline |
| Unsupported request refusal | Partial | A refused question ends in `No Valid Plan` before any proposal or provider call. Deciding which questions are unsupported is done only by the test harness; no product component does it yet. | Offline |
| Business process packages | Delivered | Markdown packages and manifests are stored byte-exact, versioned and digested; a failed store publishes nothing. | Offline |
| Product intent and activation | Delivered | Intents are approved against recorded semantic versions and source observations. The intent-bound activation service activates an acquisition contract only from an approved intent. | Offline |
| PostgreSQL acquisition | Delivered | Fresh rows are prepared, replayed without new artifacts, and acknowledged on the pinned PostgreSQL image; rejected credentials are classified and refused. | Live |
| Stripe acquisition | Not yet | The Stripe provider reads object snapshots and events against a mocked API, but is not composed into acquisition and has no live test. | Offline |
| LAND into the warehouse | Partial | A prepared batch lands once, replays safely, and advances the checkpoint only after LAND. Destination routing is deployment configuration; no owning service binds a contract to its destination yet. | Live |
| Restricted product SQL compiler | Partial | Emits guarded, unsigned PostgreSQL and ClickHouse statements for the project-and-sum shape. Every product compilation ends in `No Valid Plan` until the candidate rule's open gates close (see [legality rules](../services/compiler/legality/README.md)). | Offline and live |
| Transform and materialization | Partial | dbt models materialize with checked outputs on PostgreSQL, without compiler admission or catalog publication. There is no live ClickHouse materialization. | Live |
| Runs, triggers and recovery | Partial | Daily and run-now triggers produce run identities, and a leased run resumes from its last durable boundary after losing its lease. There is no running scheduler, and the console only displays runs. | Live |
| Governed answers | Partial | Native PostgreSQL questions are compiled to signed query plans, executed on a fresh cluster and delivered after a current entitlement check. The enterprise policy authority in that test is a test double, as are the catalog and dashboard; a real deployment needs a connected enterprise policy authority. | Live |
| Result tables and CSV | Delivered | Results render as governed tables and download as exact CSV. | Live (opt-in native console browser suite) |
| Catalog publication | Delivered | Approved semantics publish to OpenMetadata and are observed back; tenant isolation holds. | Live (OpenMetadata in Docker); isolation offline |
| Superset dashboards | Partial | Governed dashboards publish to, and read back from, a fresh Superset; dashboard access applies and revokes. Single sign-on is not implemented. | Live |
| Access control | Partial | Grants are approved and apply and revoke on PostgreSQL, ClickHouse and Superset. Revocation at expiry is proved only offline. Entitlement needs a connected enterprise policy authority. | Live and offline |
| Operations and recovery | Partial | Incidents are listed, and the console admits retry, cancel and reconcile actions. No test yet raises an incident from a real failure and recovers it. | Offline |
| Context graph and impact | Partial | Impact analysis across seven subject kinds. | Offline |
| Agent interface (MCP) | Partial | Eight governed context tools. The production application cannot start yet: no authority adapters are implemented. | Offline |
| Demonstration console | Partial | One container serves the governed request path from submission to approval: the seeded question is clarified, an answer proposal is prepared, submitted, accepted and approved, and admission reaches `execution_ready`. It is a demonstration, not a deployment: no authentication, no warehouse, and demo-grade collaborators. Runs and answer delivery are not part of it: runs and acquisition receipts answer `503 capability_not_delivered` to the architect, and a request's result answers `503` to the requester it belongs to. Data access requests are refused at intake for a separate reason — grant application, expiry and revocation are not delivered. See the [quickstart](../deploy/quickstart/README.md). | Offline and live (opt-in quickstart smoke test) |
| End-to-end PostgreSQL journey | Not yet | One request reaching a published data product end to end. | — |
| ClickHouse parity | Not yet | Warehouse lifecycle, destination, access and statement conformance work on ClickHouse; acquisition and the full journey do not. | — |

## Acceptance journeys

These runs exercise one capability across services. Their runners live in `tests/acceptance/`.

| Journey | What it proves | How it runs |
| --- | --- | --- |
| Semantic formation | A business-process package becomes approved semantics, an approved contract, and a verified catalog publication, with cleanup. | Offline against a catalog double; a live run needs OpenMetadata in Docker. |
| Warehouse lifecycle | PostgreSQL and ClickHouse are provisioned, validated, backed up and restored, suspended and resumed, retired, and cleaned up, with signed evidence. | Docker; CI runs it when a change touches it. |
| Request fulfillment | Intake, clarification and approval hold their invariants with no external effect. It proves governance, not delivery. | Offline. |
| Source acquisition | Acquisition, LAND and recovery keep their write boundaries. | Offline against an in-memory database. |
| PostgreSQL-to-Snowflake snapshot | A PostgreSQL snapshot contract is verified, compiled into a signed graph, executed into Snowflake, and reconstructed from evidence. Snowflake is not a managed destination. | Offline twin; the live run needs a real Snowflake account. |
