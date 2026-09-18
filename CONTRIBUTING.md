# Contributing to Heinzel

## Before You Change Code

1. Read `docs/architecture/repository-layout.md` and the relevant governing specification.
2. Confirm the owning component and its prohibited responsibilities.
3. Prefer the smallest change that preserves the compiler/runtime/state boundaries.
4. Record material architecture changes in an ADR.

## Branches and Commits

- Work on a `feat/`, `fix/`, `chore/`, `docs/`, `refactor/`, or `test/` branch.
- Use Conventional Commits in imperative mood, for example `feat(compiler): add contract parser`.
- Keep one logical change per commit.

## Tests

- Colocate unit tests with their component.
- Put cross-component and system-level tests under `tests/`.
- Include a failure or boundary case for every behavior change.
- Run the repository checks before opening a pull request:

```sh
./tests/repository-structure/test.sh
```

## Pull Requests

Explain the motivation, affected architecture boundary, validation performed, and security implications. A change that adds a top-level area or governed component must update the layout, architecture decision, and validator together.
