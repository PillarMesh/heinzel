# Release audit

The suites in this directory gate publication. They cover the things that are cheap to get wrong
once and expensive to retract: internal strings in a tree that is about to become public, a
dependency whose licence will not travel with Apache-2.0 code, distribution metadata that
misdescribes the package, and bundled fonts whose licence text must ship beside them.

| Suite | Guarantees |
| --- | --- |
| `test_public_tree.py` | The tracked tree carries no internal material: the company name outside its allowed references, personal email addresses, the private terms loaded at run time, and any binary file that has not been reviewed. |
| `test_dependency_licenses.py` | Every third-party dependency is licensed compatibly with distributing Apache-2.0 code. |
| `test_distribution_metadata.py` | The published package's declared metadata matches what the release expects. |
| `test_third_party_notices.py` | IBM Plex ships under OFL-1.1 with its notice and licence text alongside it. |

## Running it

```sh
uv sync --all-packages
uv run pytest tests/release -q
```

Use `--all-packages`. A bare `uv sync` uninstalls every workspace member, and the licence suite
then reports failures for packages that are merely absent.

Two tests skip when `apps/console/node_modules` is absent, which is the normal state in CI. They
compare the shipped licence files against the installed packages byte for byte, and without an
install there is nothing to compare against. Those files stay pinned by locked version and by
sha256, so the pinning check still runs.

## The private terms file

> **No private terms file is configured today, so this is dormant.** No workflow, script or
> configuration here sets any of the three variables below -- the command further down shows how
> to set them by hand -- and no terms file ships with it. Every run to date has
> used the two built-in patterns alone. A green audit therefore means the tree is clear of the
> company name and personal addresses; it says nothing about account, tool, process, milestone or
> gate names. The rest of this section describes machinery that is ready to use, not coverage that
> is in place. To put it in place, write a terms file and set the variables.

`test_public_tree.py` holds only two patterns itself: the company name outside its allowed
references, and personal email addresses. Everything specific to how this company operates
internally is intended to live in a private terms file outside this repository, so public CI
never has it.

The format is one term per line, `reason<TAB>regex`. Blank lines and lines beginning with `#` are
ignored. The tab is literal and required; a line without one is a configuration error rather than a
term. Several lines may share a reason.

Three environment variables control it.

| Variable | Effect |
| --- | --- |
| `HEINZEL_PRIVATE_TERMS_FILE` | Path to the terms file. Unset, the gate runs with the two built-in patterns alone. |
| `HEINZEL_PRIVATE_TERMS_SHA256` | That file's sha256, as 64 hexadecimal characters. Verified whenever it is set. |
| `HEINZEL_REQUIRE_PRIVATE_TERMS` | Set to `1`, the other two become mandatory: a missing one is a configuration error rather than a quieter pass. |

A full run:

```sh
sha256sum /path/to/private-terms.txt

HEINZEL_REQUIRE_PRIVATE_TERMS=1 \
HEINZEL_PRIVATE_TERMS_FILE=/path/to/private-terms.txt \
HEINZEL_PRIVATE_TERMS_SHA256=<the digest printed above> \
  uv run pytest tests/release -q
```

### Why the digest is required

Every structural check on the terms file passes on a file that is well formed but incomplete. A
file that lost lines on its way in still loads, with fewer terms than were configured. One cut
inside a pattern can still compile, leaving a valid but weaker regex. Either way the scan covers
less than intended and the gate reports a pass, which is the one outcome this gate must never
produce. Comparing the digest is what turns a partial file into a failure.

The digest is carried beside the file rather than committed here. The terms file changes
independently of this repository, so a digest in the tree would need a matching commit on every
change to a file this repository never sees, and would be stale the first time someone forgot.

It detects corruption, not tampering. Anyone who can set one variable can set both, so it is not
an integrity control against a hostile party.

## Failure modes

| Configuration | Result |
| --- | --- |
| Nothing set | Built-in patterns only; passes without private coverage. |
| `REQUIRE=1`, no terms file | Fails: the terms file must be set. |
| `REQUIRE=1`, terms file set, no digest | Fails: the digest must be set. |
| Terms file set, digest does not match | Fails: the file did not survive transport intact. |
| Terms file set, line without a tab | Fails: the line is not `reason<TAB>regex`. |
| Terms file set, uncompilable regex | Fails: the pattern is named with its error. |
| Terms file set but empty, or only comments | Fails: the file has no terms. |
