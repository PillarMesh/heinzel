# M0 Acceptance Transport Decision

> **Historical.** This document records the PostgreSQL-to-Snowflake M0 thin-thread
> experiment. It remains accurate about what was built and is retained so that evidence
> stays reproducible. Snowflake is no longer a product destination, and nothing here
> defines current product scope. See the
> [managed data engineering platform addendum](../architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md).

Decision: the checked-in M0 acceptance gate uses the pre-authorized `pillarmesh-m0` CLI fallback.

The harness invokes the installed CLI in subprocesses so the witnessed path uses the same contract
service, verification, activation, runtime, evidence, and package boundaries as the product CLI.
The private acceptance key is supplied only through `activate-stdin`; credentials and scan canaries
are inherited through the process environment. Contract and fixture labels are opaque random
values generated independently from the key.

Desktop MCP-host automation is not part of the deterministic gate because host UI state,
conversation state, and client automation are not stable acceptance inputs. A real desktop stdio
run may be recorded as an additional observation, but it cannot replace the checked-in harness,
the live provider transaction, package verification, or second-operator reproduction.

This decision exercises the release valve approved by the original M0 design. It does not change
the acceptance claim, widen the contract, add a transport, or weaken any gate. Replacing the CLI
fallback as the required gate needs a new recorded decision and equivalent deterministic evidence.
