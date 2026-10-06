# ADR-0010: Publish a Dashboard Within Its Source Snapshot, and Grant Access Separately

## Status

Proposed on 2026-10-06.

## Context

A governed dashboard is compiled from a delivered answer. `DashboardCompositionService.publish`
refuses unless the answer authority returns a value. That reader answers nothing for a request
that is not delivered, and raises for a result snapshot that has expired -- an absence and a
verification failure, not two shades of the same refusal. Nothing in the product reaches that
call: the composition and the control service are invoked only from their own tests and the
acceptance harness, so no dashboard is published from a delivered answer today.

Building that path requires two decisions no existing record settles, and both outlive the code
that would implement them.

The first is lifetime. An answer's result snapshot carries a retention, after which its evidence is
no longer readable. A dashboard compiled from that snapshot is a second artifact over the same
rows. If it outlives the snapshot freely, the retention is defeated through a side door. If it dies
with the snapshot, it is not a dashboard.

The second is visibility. The dashboard read for a requester requires an access grant.
[ADR-0006](ADR-0006-bi-control.md) places dashboard authority in BI control and states that access
grants remain a separate access-control concern; [ADR-0007](ADR-0007-access-control.md) owns
grants. A publication path that also conferred access would move grant authority into BI control.

A third question -- whether a BI provider ever provisions its own instance, as the PostgreSQL
warehouse provider provisions a warehouse -- is deliberately not decided here. It governs
deployment shape rather than the lifecycle of a published dashboard.

## Decision

**The source snapshot bounds the window to publish, not the life of what is published.**

A publication intent carries the expiry of the result snapshot it was compiled from. Publication
proceeds only while that deadline stands; past it the intent fails as expired and no provider is
invoked. A dashboard is never compiled from evidence that can no longer be read back.

Once published, a dashboard's life is governed by its own contract. `DashboardContract` declares a
freshness requirement and `DashboardPublication` records its own `as_of` and freshness disposition.
Those, not the snapshot's retention, decide when a published dashboard is stale or is archived. The
snapshot's retention bounds when work may begin; the dashboard's own contract bounds how long its
result stands. A published dashboard is not archived because its source snapshot expired; the
snapshot's expiry ends the window to publish and nothing else.

**A dashboard is refreshed only by another delivered answer.** There is no path that recompiles one
from the warehouse directly, and this record does not create one. A dashboard whose freshness
requirement has lapsed is stale until a further question reaches delivery and publishes a new
revision over the same contract. A refresh is therefore a governed journey rather than a schedule,
and a dashboard nobody re-asks for goes stale and stays stale.

**Publication confers no access.**

Publishing creates the dashboard and its receipt. It grants nobody the right to read it. A
published dashboard is visible to workspace roles; the requester whose question produced it sees it
only once access control has granted it, through the same path as any other data access. BI control
never mints a grant, and a publication that cannot be seen is not a failed publication.

## Consequences

- A deferred publication can expire without having run. That is a visible outcome with a reason,
  not a silent omission, and it is the correct one: the alternative is a dashboard whose source
  evidence cannot be produced on request.
- The answer scope policy's result retention becomes the budget for publishing and for every retry
  after a failure. A short retention is no longer only a disclosure setting; it decides how much
  room a failed publication has to recover. The demonstration's hour would leave very little.
- A dashboard and the answer it came from can disagree about staleness, because each carries its
  own freshness. That is intended. A dashboard is a standing artifact and an answer is a reading.
- A standing dashboard that nobody re-asks for decays into a stale one, because refreshing it means
  another approved journey. That is the honest cost of compiling only from answers that were
  governed, and it is the strongest argument anyone will make for a scheduled refresh path. Such a
  path would need its own record, because it would publish from something no requester asked for.
- The stakeholder who asked the question does not automatically see the dashboard built from their
  answer. This is a real cost, paid to keep grant authority in one service. Closing the gap is an
  access-control change, not a BI-control one.
- A publication that fails after its answer was delivered leaves the answer delivered and readable.
  The failure belongs to the dashboard, is classified on the publication record, and is retried
  there. It must not invalidate the delivery, and it must not be swallowed.
- Nothing here makes a dashboard demonstrable. An instance must still exist to publish to, and the
  requester still needs a grant. Both are named above as out of scope and remain so.
