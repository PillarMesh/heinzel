# M0 Acceptance Transport Decision

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
