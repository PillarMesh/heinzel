# Heinzel Contributor Instructions

## Scope and Instruction Precedence

This file governs the entire repository. A more deeply nested `AGENTS.md` may add or override local implementation procedures only for files in its own subtree. Repository-wide architecture, legality, security, verification, Git, and identity rules may be changed only in this root file. Direct user instructions take precedence, followed by the closest applicable `AGENTS.md`, then this file.

Before changing anything, read this file, the closest applicable instructions, the relevant architecture specification or ADR, and the code and tests that own the behavior. Treat documentation as design authority, not as evidence that an implementation or deployed capability works.

## Agentic Development Loop

Use this loop for every implementation task:

1. **Establish ground truth.** Inspect `git status`, the active branch and worktree, nearby instructions, recent relevant changes, and existing tests. Preserve unrelated or pre-existing changes.
2. **Define the outcome.** Restate the observable acceptance criteria, affected boundaries, failure cases, and verification required. Ask only when unresolved ambiguity would materially change architecture, security, data, cost, or external side effects.
3. **Choose the smallest coherent change.** Reuse established boundaries and patterns. Record a short architecture rationale when more than one placement or dependency direction is plausible.
4. **Make failure visible first.** Add or identify a focused test that fails for the missing behavior, including at least one boundary or failure case. Confirm that it fails for the expected reason.
5. **Implement incrementally.** Make the minimum production change needed to pass the focused test. Avoid speculative abstractions, unrelated cleanup, compatibility shims without an active consumer, and silent fallback behavior.
6. **Verify in layers.** Run focused tests, component tests, static checks, the offline suite, and repository-structure validation as applicable. A failed or skipped required gate remains a gap.
7. **Review the resulting diff.** Check architecture placement, security boundaries, determinism, error behavior, observability, public interfaces, documentation, and accidental files before committing.
8. **Report evidence honestly.** State what changed, the exact checks run, their outcomes, and what remains unverified. Never equate compilation, mocks, an HTTP success, existing data, or a skipped live test with end-to-end proof.

Plans should be proportional to risk. A small, local change may need only a few explicit steps; a cross-component, security-sensitive, destructive, or externally mutating change requires a written plan and clear approval boundaries. Do not broaden the requested scope merely because adjacent work appears useful.

## Architecture Authority

The canonical vocabulary sources are the documents under `docs/architecture/specifications/`. Repository boundaries must remain consistent with those specifications and `docs/architecture/repository-layout.md`.

Do not introduce a scheduler, general-purpose workflow orchestrator, source/destination connector taxonomy, or metadata system that competes with authoritative enterprise catalogs.

Execution correctness must remain deterministic. AI may assist with intent interpretation, recommendations, explanations, and candidate generation, but it must not be authoritative for legality, signing, execution semantics, evidence, recovery, or state transitions. Identical canonical inputs and compiler versions must produce identical semantic artifacts; timestamps, trace identifiers, and other run-specific values belong in surrounding evidence.

Reject illegal or infeasible work explicitly with `No Valid Plan` and attributable constraints. Do not silently weaken a contract, substitute a provider, bypass policy, or convert an unverified assumption into executable authority.

## Placement Rules

- Put operator-facing product code in `apps/console`.
- Put deployable architectural capabilities in the declared `services/` component that owns the behavior.
- Put provider-specific declarations, implementations, and conformance fixtures in `providers/<provider>`.
- Put reusable provider-authoring interfaces and conformance tooling in `packages/provider-sdk`.
- Promote other code into `packages/` only after two real consumers demonstrate a stable boundary.
- Colocate unit tests with the component. Use `tests/` for cross-component, conformance, compatibility, fault-injection, end-to-end, and repository-structure suites.

Internal lease, epoch, revalidation, migration-admission, and snapshot-to-CDC control loops belong to `services/state`. They are contract-scoped and evidence-emitting, not a scheduler.

## Legality Rules

Legality rules live under `services/compiler/legality/`. Adding or widening a rule requires, in the same change:

- a reviewed proof note;
- positive and negative conformance fixtures;
- mutation tests for every precondition;
- regression against every previously admitted provider pair; and
- approval from an independent reviewer.

## Change Discipline

- Keep changes incremental and technology-neutral until an ADR selects a technology.
- Do not create speculative nested directories or placeholder applications.
- Adding a top-level area or governed component requires updating the repository layout, an ADR, and the structure validator in the same change.
- Keep public models and interfaces explicit, narrow, immutable where practical, and strict about unknown input. Preserve artifact and lifecycle boundaries instead of passing untyped dictionaries between components.
- Update architecture notes, operator documentation, examples, fixtures, and environment templates in the same change when their behavior or contract changes.
- Do not add a dependency until the standard library and existing dependencies have been considered. Pin it through `uv.lock`, document why it is needed, and verify its license and maintenance posture for material dependencies.

## Python Engineering Standards

- Use the Python version pinned by `.python-version` and `pyproject.toml`; do not widen it implicitly.
- Manage the workspace and lockfile with `uv`. Do not introduce parallel requirements files or invoke an unlocked environment in CI.
- Keep strict mypy compatibility. Avoid `Any`, unchecked casts, blanket ignores, and dynamically shaped data at component boundaries.
- Prefer frozen, validated Pydantic models for durable contracts and artifacts. Reject unknown fields unless the governing contract explicitly permits them.
- Use timezone-aware UTC timestamps, `Decimal` for exact numeric semantics, canonical serialization for hashes and signatures, and explicit enums for closed vocabularies.
- Keep domain logic pure and deterministic where possible. Isolate clocks, randomness, filesystem access, network access, credentials, and provider SDKs behind injected boundaries.
- Raise typed, actionable errors. Do not swallow exceptions, log secrets, use bare `except`, or continue after integrity, legality, signature, or evidence failures.
- Keep modules cohesive and dependencies directed inward toward contracts and domain behavior. Promote shared code only after two real consumers establish a stable boundary.

## Code Style

Formatting is owned by `ruff format` and linting by the `E`, `F`, `I`, `B`, `UP`, `RUF`, and `SIM` rule families at a 100-character line length. Never hand-format around the formatter, and never silence a lint with a blanket ignore; narrow the code or justify a specific, scoped `noqa` in the same change.

- Name things in full words that a reader unfamiliar with the module can follow: `artifact_digest`, `expected_checkpoint`, `acceptance_key`. Do not abbreviate, and do not use single-character names outside comprehensions.
- Prefix module-private names with a single underscore. Keep public surfaces minimal and export them deliberately through `__init__.py`.
- Start modules with `from __future__ import annotations`. Let the import sorter own ordering; do not add manual grouping comments.
- Declare closed vocabularies as PEP 695 `type` aliases over `Literal`, and constrain artifact fields with `Field(pattern=...)` rather than validating shape at the call site.
- Prefer keyword-only parameters (`*`) once a signature takes more than two values of the same type, so call sites cannot silently transpose arguments.
- Return immutable containers (`tuple`, frozen models) from public functions. Reserve `list` and `dict` for local accumulation.
- Write comments only for rationale and invariants -- why a transaction is required, why an error is classified a particular way, what breaks if an ordering changes. Do not restate what the code does. The same applies to docstrings; add one when a function's contract is not obvious from its signature, and omit it when it is.
- Name tests as a sentence stating the guaranteed behavior (`test_interrupted_migration_never_leaves_a_schema_without_its_version_row`). Separate arrange, act, and assert with blank lines, and keep fixture builders as module-level helpers.

## Durability, Boundaries, and Failure Classification

These rules encode defects that have already reached review in this repository.

- Writes that must be replay-safe belong in one transaction. If a caller can re-enter a code path and be handed an existing record verbatim, then every write that record's later validation depends on must have committed with it; a partially written record that the replay path returns as complete can never be repaired.
- Never build a SQL `LIKE` pattern from caller-supplied text. Escape `%`, `_`, and the escape character, and declare `ESCAPE`. Identifiers that look inert routinely contain `_`.
- A provider boundary must not leak its driver's exception types. Every provider entry point raises `ProviderError` with an accurate classification, and transport, availability, and throttling failures must stay distinguishable from statement-level rejections. Collapsing them to `permanent` silently disables every retry policy built on top.
- Consumers must honor a classification rather than flatten it. A terminal verdict about a counterparty -- non-conformance, rejection, integrity failure -- may only be recorded for classifications that actually support that verdict; a transient failure is not evidence of one.
- Best-effort cleanup in an exception handler (rollback, close, unlink) must be suppressed so it cannot replace the exception being reported.

## Testing and Verification

Every behavior change requires tests at the cheapest layer that can prove it:

- Colocate unit and property tests with their component.
- Put cross-component, conformance, compatibility, fault-injection, end-to-end, and live integration tests under `tests/`.
- Cover the happy path and at least one relevant invalid input, boundary, denial, retry, replay, or recovery case.
- For legality rules, satisfy all additional proof, fixture, mutation, regression, and independent-review requirements above. An author or the same agent must not self-certify independent review.
- Test observable contracts rather than private implementation details. Keep tests deterministic; inject clocks and entropy and avoid network access in the offline suite.
- A test double must never be more permissive than the component it replaces. Where a fake stands in for something that parses, validates, or persists, assert the payload against the real model or schema in the same test; otherwise the double proves only that the code ran, and a contract mismatch surfaces for the first time in a live run.
- Prove a regression test before trusting it. Confirm it fails against the unfixed code for the expected reason, then confirm the fix turns it green. A test written after the fix that was never seen red is unverified.
- Treat skipped, quarantined, flaky, or xfailed tests as explicit gaps, not passing evidence.

Before committing a Python change, run the applicable focused tests followed by the complete offline gates:

```sh
uv sync --locked --all-packages
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
./tests/repository-structure/test.sh
```

Run focused mutation testing when changing legality predicates, authorization checks, signature validation, recovery gates, or other critical branching logic. Inspect survivors; a mutation score alone does not establish correctness.

Live-account verification is opt-in. Follow the applicable approved setup, acceptance, and teardown runbooks; for M0 these are `docs/m0/setup.md`, `docs/m0/acceptance-run.md`, and `docs/m0/teardown.md`. A live claim requires a new transaction traced to its terminal state with sanitized evidence from every relevant hop. Existing rows, fixtures, logs from an earlier run, successful connection tests, or offline fakes are not proof of the live capability.

## Security and External Effects

- Use dedicated least-privilege accounts and roles for live development and testing. Verify both intended access and denied access to unrelated schemas, objects, and administration operations.
- Keep credentials in environment variables or the approved secret store. Never place secrets in source, fixtures, command arguments captured in logs, evidence packages, comments, or chat output.
- Redact account identifiers and sensitive data from evidence while retaining stable correlation identifiers needed to reconstruct a run.
- Default diagnostics and audits to read-only behavior. Do not create accounts, alter grants, mutate shared data, send messages, incur material cost, deploy, merge, push, or perform destructive cleanup unless the request clearly authorizes that external effect.
- Resolve exact targets before an authorized destructive action, prefer recoverable operations, and verify the resulting state. Follow the documented teardown procedure for real-account fixtures.
- Treat provider responses, catalog metadata, generated candidates, and persisted artifacts as untrusted input. Validate them at every trust boundary.

## Worktrees, Concurrency, and Delegation

- Assume other humans or agents may be working concurrently. Inspect the checkout before acting and use an isolated worktree for feature work when the current checkout is dirty, shared, or on `main`.
- Never overwrite, stage, revert, move, or delete changes you did not create. If an unrelated change blocks the task, stop and report the exact conflict.
- Do not reuse virtual environments, caches with mutable state, or generated credentials across worktrees when doing so can contaminate results.
- Delegate only bounded, independent work with clear ownership. Avoid overlapping file edits. The primary agent remains responsible for reviewing every delegated result and rerunning integrated verification.
- Do not let multiple agents approve each other's incomplete assumptions in a loop. Requirements, legality proofs, security decisions, and completion claims must remain traceable to evidence and the designated reviewer.

## Git Discipline

- Use branches named `feat/<topic>`, `fix/<topic>`, or `chore/<topic>` and Conventional Commit subjects in imperative mood. Do not commit directly to `main`.
- Keep commits reviewable and limited to one logical change. Stage explicit paths and inspect the staged diff before committing.
- Do not push, open a pull request, merge, deploy, force-push, rewrite shared history, or delete a branch unless the user has authorized that operation.
- Before rebasing, resetting, force-pushing, or deleting refs, fetch and atomically re-check `HEAD`, local and remote branch tips, rebase state, locks, and recent reflog activity. Stop if refs moved unexpectedly or another operation may be active.
- Before publishing, compare the branch with freshly fetched `origin/main`; confirm it contains the intended additions and would not revert unrelated work.

## Completion and Handoff

A change is complete only when all applicable items are true:

- acceptance criteria are met by the implementation, not only by documentation or mocks;
- focused success and failure tests pass;
- offline quality and structure gates pass;
- required mutation, conformance, compatibility, and fault-injection checks pass;
- security, tenant, provider, and external-side-effect boundaries were reviewed;
- documentation and examples match the implemented behavior;
- the final diff contains no unrelated files, generated secrets, caches, or accidental artifacts;
- every required live behavior and independent-review gate is evidenced; optional or non-applicable verification is identified explicitly rather than implied; and
- the handoff names changed files, exact verification commands and results, known limitations, and the next approval-dependent action.

## GitHub Account

Use the GitHub account `ks2002119` for this repository's `gh` commands and authenticated remote Git operations, including fetch, push, pull-request, workflow, and repository-administration actions.

Before an authenticated remote operation, run:

```sh
gh auth switch --hostname github.com --user ks2002119
gh auth setup-git
test "$(gh api user --jq '.login')" = "ks2002119"
```

Some execution environments can restore a different active account between shell invocations. When several remote commands must use the same identity, switch and verify the account in the same shell invocation as those commands. Git commit authorship (`user.name` and `user.email`) is separate from GitHub authentication and must not be changed merely to select this account.

Recognize the drift by its symptom, because it does not look like an authentication failure. An account without access to this private repository reports:

```text
remote: Repository not found.
fatal: repository 'https://github.com/PillarMesh/heinzel.git/' not found
```

`gh` reports the same absence as `GraphQL: Could not resolve to a Repository with the name 'PillarMesh/heinzel'`. Both read as though the repository was renamed or deleted. Never conclude that a repository, branch, or pull request is missing from such an error until after switching to `ks2002119` in the same shell invocation and retrying.
