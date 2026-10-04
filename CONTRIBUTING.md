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

CI also scans the tree for secrets, in the same job and before any of the above. It is the one
offline check with no `uv` entry point, so run it the way CI does -- over the committed tree, so
that a file `git archive` omits is omitted here too:

```bash
tree="$(mktemp -d)" && git archive HEAD | tar -x -C "$tree"
docker run --rm --network none -v "$tree:/repo:ro" \
  "$(grep -om1 'ghcr.io/gitleaks/gitleaks@sha256:[0-9a-f]*' \
    .github/workflows/repository-structure.yml)" \
  dir /repo --config /repo/.gitleaks.toml --no-banner --redact
```

The image digest is read out of the workflow rather than written here, so this runs the scanner
CI runs. A different version reports differently, which is the one way this check can agree with
itself locally and still fail in CI.

`.gitleaks.toml` allows the exact synthetic values the default rules mistake for secrets, each
named with its rule and its file. Moving one of those files moves its entry: the allowlist
matches on path and value together, so the entry stops covering the value and the scan reports
it. Read the flagged line before adding an entry, and never allowlist a value you have not
confirmed is fake.

If you change the console, also run:

```bash
cd apps/console
npm run lint && npm run typecheck && npm run check:contracts && npm run test -- --run
```

Tests marked `live` or `emulator` are opt-in. They start pinned engine images in Docker, and some
need an explicit switch or external credentials; the [README](README.md#live-tests) lists them.
Select them with `-m "live or emulator"`, as the `Live journeys` workflow does: several
Docker-backed suites are marked only `emulator`, so `-m live` alone deselects them. For example:

```bash
HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE=1 uv run pytest -m "live or emulator" -q \
  tests/integration/test_postgresql_compiled_product_journey_live.py
```

CI runs the journeys that need only Docker in the `Live journeys` workflow: nightly, on demand,
and when a maintainer applies the `run-live` label to a pull request. A later push does not
re-run them; a maintainer reviews it and removes and re-applies the label. The run fails if any
selected test skips, because a skipped journey is a gap, not a pass.

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

To bring your branch up to date, rebase it onto `upstream/main` rather than using the "Update
branch" button on the pull request. That button adds a merge commit authored by you but without
your sign-off, which fails the check.

## Use of AI tools

Contributors, including maintainers, may use AI tools. The person who signs off a commit certifies
it under the DCO and is accountable for every line, exactly as if they had written it unaided.
[AGENTS.md](AGENTS.md) applies to AI agents as much as to people.

## Reporting security issues

Do not open a public issue for a vulnerability. Follow [SECURITY.md](SECURITY.md).
