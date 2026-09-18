# Contributing to Heinzel

Thank you for helping. This guide covers setup, checks, and how changes land. Read
[AGENTS.md](AGENTS.md) for the engineering rules every change must follow.

## Setup

You need Python 3.13 and [uv](https://docs.astral.sh/uv/). The console also needs Node.js
24.20.0 (pinned in `apps/console/.node-version`). Live tests need Docker.

```bash
uv sync --all-packages
(cd apps/console && npm ci)
```

## Checks

Every pull request must pass the offline checks:

```bash
uv lock --check
uv run ruff check . && uv run ruff format --check .
uv run mypy && ./tests/type-check/check.sh
./tests/repository-structure/test.sh
uv run pytest -m "not live" -q
```

If you change the console, also run:

```bash
cd apps/console
npm run lint && npm run typecheck && npm run check:contracts && npm run test -- --run
```

Tests marked `live` are opt-in. They start pinned engine images in Docker, and some need an
explicit switch or external credentials; the [README](README.md#live-tests) lists them. For
example:

```bash
HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE=1 uv run pytest -m live -q \
  tests/integration/test_postgresql_compiled_product_journey_live.py
```

## Commits and pull requests

- [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): subject`, imperative
  mood, subject at most 72 characters, and a body explaining why. One logical change per commit.
- Every change lands through a pull request. Describe what changed, why, and how you verified it.
- Behaviour changes need tests. A bug fix needs a regression test that fails without the fix.
- User-visible changes add an entry to [CHANGELOG.md](CHANGELOG.md).
- Pull requests are squash-merged; the pull request title becomes the commit on `main`, so write it
  as a Conventional Commit subject.

## Developer Certificate of Origin

Contributions are accepted under the Apache License 2.0. Every commit must be signed off,
certifying the [Developer Certificate of Origin](https://developercertificate.org/):

```bash
git commit -s
```

This adds a `Signed-off-by: Your Name <you@example.com>` trailer, which must match the commit
author's name and email. CI checks every commit in a pull request with `tests/ci/check_dco.py` and
blocks the pull request if any commit is unsigned. To sign off commits you have already made, run
`git rebase --signoff upstream/main` (where `upstream` is this repository) and force-push your
branch.

## Use of AI tools

Contributors, including maintainers, may use AI tools. The person who signs off a commit certifies
it under the DCO and is accountable for every line, exactly as if they had written it unaided.
[AGENTS.md](AGENTS.md) applies to AI agents as much as to people.

## Reporting security issues

Do not open a public issue for a vulnerability. Follow [SECURITY.md](SECURITY.md).
