# Engineering rules

These rules apply to every contributor, human or AI coding agent. [CONTRIBUTING.md](CONTRIBUTING.md)
covers the mechanics of setup, checks, commits and pull requests; this file covers how the work
must be done.

Before changing anything, read this file, the relevant decision records in
[docs/architecture/decisions](docs/architecture/decisions/), and the code and tests that own the
behaviour. Documentation describes intended design; it is not evidence that something works.

## Evidence honesty

- A capability is complete only when a fresh transaction reaches its terminal state and leaves
  evidence. Rendering, seeded data, a mock, an HTTP success, or a green check on a proxy is not
  proof.
- Report outcomes as they are. If a test fails, say so. If a step was skipped, say so. A skipped,
  quarantined, flaky or expected-failure test is a gap, not a pass.
- Never weaken a check to make it pass. Fix the cause, or disclose the gap.

## Legality and determinism

- The compiler emits only plans it can prove legal. Anything else is `No Valid Plan`, with a
  reason. Never silently weaken a contract, substitute a provider, bypass policy, or turn an
  unverified assumption into executable authority.
- A new or widened legality rule needs, in the same change, a proof of the property it relies on,
  positive and negative fixtures for each engine, a test that each precondition can fail, and
  review by someone other than its author. An author must not approve their own rule. The
  existing rules predate the public proof record; a new or changed rule includes its proof in the
  pull request. See [services/compiler/legality](services/compiler/legality/README.md).
- AI tools may assist with interpretation, explanation and candidate generation, but are never the
  authority for legality, signing, execution semantics, evidence, recovery or state transitions.
- Identical canonical inputs and compiler versions must produce identical artifacts. Timestamps,
  trace identifiers and other run-specific values belong in the surrounding evidence.

## Change discipline

- Choose the smallest coherent change. Follow existing patterns and boundaries; do not add
  speculative abstractions, placeholder directories or unrelated clean-up.
- Measure the blast radius before a cross-cutting change: count the call sites first.
- A regression test must fail without the fix; check that it does, and for the expected reason.
- Writes that must be replay-safe belong in one transaction. A partially written record that a
  replay returns as complete can never be repaired.
- No secrets in code, tests, fixtures, logs, command arguments or evidence.
- Update documentation, fixtures and examples in the same change as the behaviour they describe.
- Consider the standard library and existing dependencies before adding one. Pin new dependencies
  through `uv.lock` and check their licence is compatible with Apache-2.0.

## Structure

- Adding a top-level area, or a component directly under `apps/`, `services/` or `packages/`,
  requires updating [docs/architecture/repository-layout.md](docs/architecture/repository-layout.md),
  an ADR, and `tests/repository-structure/validate.sh` in the same change.
- Put provider-specific code and conformance fixtures in `providers/<provider>`, and
  provider-authoring interfaces in `packages/provider-sdk`. Promote other code into `packages/` only
  once two real consumers establish a stable boundary.
- Keep dependencies directed inward toward contracts and domain behaviour.
- Colocate unit tests with their component. Cross-component, conformance, fault-injection,
  end-to-end and live integration tests live under `tests/`.

## Python

- Python 3.13, as pinned by `.python-version` and `pyproject.toml`. The workspace and lockfile are
  managed with `uv`; do not add requirements files.
- `uv run mypy` (strict mode, configured in `pyproject.toml`) and `./tests/type-check/check.sh`
  must pass. Avoid `Any`, unchecked casts and blanket `type: ignore`.
- `ruff format` owns formatting and `ruff check` owns linting (rule families `E`, `F`, `I`, `B`,
  `UP`, `RUF`, `SIM`; 100-character lines). Never silence a lint with a blanket ignore; narrow the
  code or add one specific, justified `noqa`.
- Start modules with `from __future__ import annotations`. Prefix module-private names with a single
  underscore, and export public names deliberately through `__init__.py`.
- Name things in full words (`artifact_digest`, `expected_checkpoint`), not abbreviations.
- Prefer frozen, validated Pydantic models for durable contracts and artifacts, and reject unknown
  fields. Declare closed vocabularies as PEP 695 `type` aliases over `Literal`.
- Return immutable containers (`tuple`, frozen models) from public functions. Reserve `list` and
  `dict` for local accumulation.
- Use keyword-only parameters once a signature takes more than two values of the same type.
- Use timezone-aware UTC timestamps, `Decimal` for exact numbers, and canonical serialization for
  anything that is hashed or signed.
- Keep domain logic deterministic. Inject clocks, randomness, filesystem, network, credentials and
  provider SDKs.
- Raise typed, actionable errors. Do not swallow exceptions or use a bare `except`, and never
  continue after an integrity, legality, signature or evidence failure. Best-effort clean-up in an
  exception handler must not replace the exception being reported.
- A provider must not leak its driver's exception types: raise `ProviderError` with an accurate
  classification, and keep transient failures distinguishable from rejections.
- Never build a SQL `LIKE` pattern from caller-supplied text without escaping it.
- Write comments for rationale and invariants, not to restate the code.

## Tests

- Test at the cheapest layer that can prove the behaviour, and cover at least one failure,
  boundary, denial, replay or recovery case alongside the happy path.
- Name tests as a sentence stating the guaranteed behaviour, for example
  `test_interrupted_migration_never_leaves_a_schema_without_its_version_row`.
- Keep the offline suite deterministic and free of network access. Tests that need Docker, pinned
  engine images or external credentials are marked `live` or `emulator`, sit behind an opt-in
  switch, and are selected with `-m "live or emulator"`.
- A test double must never be more permissive than the component it replaces. Where a fake stands
  in for something that parses, validates or persists, assert the payload against the real model.

## Console

The console in `apps/console` is React and TypeScript with a Starlette server. It must pass
`npm run lint`, `npm run typecheck`, `npm run check:contracts` and `npm run test -- --run`.
Services, not the console, are authoritative for validity, lifecycle state, execution and evidence.
