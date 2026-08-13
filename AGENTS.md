# PillarMesh Contributor Instructions

## Architecture Authority

The canonical vocabulary sources are the documents under `docs/architecture/specifications/`. Repository boundaries must remain consistent with those specifications and `docs/architecture/repository-layout.md`.

Do not introduce a scheduler, general-purpose workflow orchestrator, source/destination connector taxonomy, or metadata system that competes with authoritative enterprise catalogs.

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
- Run `./tests/repository-structure/test.sh` before committing.
- Use Conventional Commits and do not commit directly to `main`.

## GitHub Account

Use the GitHub account `ks2002119` for this repository's `gh` commands and authenticated remote Git operations, including fetch, push, pull-request, workflow, and repository-administration actions.

Before an authenticated remote operation, run:

```sh
gh auth switch --hostname github.com --user ks2002119
gh auth setup-git
test "$(gh api user --jq '.login')" = "ks2002119"
```

Some execution environments can restore a different active account between shell invocations. When several remote commands must use the same identity, switch and verify the account in the same shell invocation as those commands. Git commit authorship (`user.name` and `user.email`) is separate from GitHub authentication and must not be changed merely to select this account.
