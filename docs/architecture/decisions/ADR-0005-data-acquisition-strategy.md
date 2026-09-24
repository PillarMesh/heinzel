# ADR-0005: Own the Load Path, Tier the Acquisition Path

- Status: Proposed
- Date: 2026-08-27
- Context: [Architecture](../../architecture.md)

## Context

Heinzel owns the entire connector lifecycle — installation, authentication, discovery, initial load, incremental state, drift response, replay, resynchronization, upgrades, and evidence. Its first two sources are PostgreSQL and Stripe. Nothing yet stated how the estate grows beyond them.

Connector coverage is the largest strategic gap: building each provider to the full conformance regime is deliberately expensive, so adding providers that way stays expensive permanently while comparable products ship hundreds of connectors.

Heinzel requires each capability to declare semantics that conformance tests verify — ordering scope, duplicates, deletion behaviour, replay, idempotency. Off-the-shelf connectors do not supply these. Airbyte declares a sync mode but not whether deletes are observable, what a mid-sync retry does to the duplicate envelope, or what ordering it guarantees. Singer tap quality is inconsistent. Embedding third-party connectors therefore buys reach and forfeits the proof for those sources.

Connector maintenance is also the largest recurring time sink for a small data team, because upstream APIs change continuously. A product promising one data engineer cannot own fifty connectors.

## Decision

Separate the two halves of ingestion and treat them differently.

**LAND is never delegated.** The write path into the warehouse — batch identity, commit ledger, immutable generations, and publish-on-verify — is Heinzel code permanently. It carries the correctness claims, it is engine-specific, and no third party implements it as the evidence model requires.

**EXTRACT is tiered** by conformance, so that the trade between reach and proof is stated rather than hidden.

- **Certified** — an own provider on the native protocol or API, passing the full conformance suite. Eligible at any risk tier and as either side of a legality substitution. The first two sources stay here: PostgreSQL via psycopg3 and Stripe via its REST API, because the revenue-to-cash reconciliation invariants depend on their semantics.
- **Provisional** — an embedded `dlt` source, or an imported Airbyte low-code YAML manifest lowered into a Heinzel capability declaration. Snapshot and cursor acquisition only, capped at risk tier T1, never eligible for legality substitution, with untested semantics recorded as Unknown.
- **Opaque-wrapped** — a third-party connector whose behaviour is established only by measurement, usable only by contracts that explicitly tolerate the resulting Unknown.

`dlt` is selected for the long tail because it is a Python library that runs inside the existing single-process runtime with no container runtime, no cluster, and no second deployment artifact.

Airbyte low-code manifests are the preferred import path because a manifest declares auth, pagination, record selection, primary key, and cursor field, which is structurally closer to a capability declaration than connector code is.

**Change data capture, when it arrives, uses Debezium Server wrapped as a Capability Provider.** Heinzel does not write a logical-decoding client. Debezium Server runs standalone without Kafka, which matters because Heinzel deliberately does not depend on Kafka.

**Fivetran-category managed SaaS is rejected as an acquisition path**, because vendor-asserted semantics that cannot be verified are incompatible with a product whose thesis is proof, and because it places a third party inside the evidence chain.

## Consequences

- The tier determines who absorbs upstream API churn. Certified means Heinzel does, per source, forever. Provisional means the community does and Heinzel absorbs only the semantic gap. This is what makes an estate larger than a handful of sources compatible with one operator.
- Provisional sources cannot participate in legality substitution and are capped at T1, so the coverage gain is real but bounded, and the boundary is visible in the contract rather than implied.
- Reach grows without weakening any claim, because an unproven semantic is recorded as Unknown rather than assumed.
- Import-to-Provisional is materially cheaper than build-to-Provisional, which is the most promising answer to the coverage gap.
- The evidence chain stays wholly inside Heinzel, since LAND is never delegated.
- A customer expecting a specific connector at Certified quality may receive Provisional instead, and the difference must be stated during scoping rather than discovered during a contract's first `No Valid Plan`.
- Two acquisition mechanisms must be maintained — native providers and the embedded `dlt`/manifest path — which is a real cost accepted in exchange for coverage.

## Reversal triggers

- Provisional sources prove unable to satisfy real tenant contracts, at which point the tier boundary is wrong and either the reduced suite widens or the long tail must be built as Certified.
- An imported manifest path cannot reach Provisional without a full per-connector review, which would remove its cost advantage and leave `dlt` as the only long-tail mechanism.
