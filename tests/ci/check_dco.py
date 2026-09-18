"""Fail unless every commit in a range carries a DCO sign-off matching its author.

Trailer detection is delegated to git's own trailer parser (`%(trailers:...)` in
`git rev-list --format`), so it follows git's own rules rather than a bespoke
scan: a `Signed-off-by` line remains part of the final trailer block even when a
little more prose follows it in the same last paragraph, and trailer keys are
matched case-insensitively. Only a sign-off that appears earlier in the body,
separated from the final trailer block by another paragraph, is excluded --
see `test_a_sign_off_quoted_mid_body_before_trailing_prose_does_not_count`.

Merge commits in the range are checked like any other commit. This is the
conservative choice: a merge commit can carry substantive changes (for example a
squash performed as a merge, or conflict resolutions), and a project accepting
external contributions should not create a blind spot for sign-off by exempting
merge commits from the check.

A shallow clone (`git clone --depth=N`, as `actions/checkout` defaults to for
pull requests) reports git objects outside its truncated history as simply not
existing. Left unhandled, that makes an unsigned commit outside the fetched
depth silently pass -- worse than a false failure, since it is a false pass in
a checker whose entire job is to gate merges. `unsigned_commits` therefore
refuses to run against a shallow repository at all.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from re import Match
from re import compile as re_compile

SHALLOW_HINT = "shallow clone: fetch full history (actions/checkout fetch-depth: 0)"

# Field and record separators for the single `git rev-list` call that reads every
# commit in the range in one process: 0x1f/0x1e are ASCII "unit/record separator",
# vanishingly unlikely to appear in a name, email, or sign-off trailer, unlike a
# printable delimiter such as "|" or a tab.
_FIELD_SEPARATOR = "\x1f"
_RECORD_SEPARATOR = "\x1e"
_REV_LIST_FORMAT = (
    f"%H{_FIELD_SEPARATOR}%an{_FIELD_SEPARATOR}%ae{_FIELD_SEPARATOR}"
    f"%(trailers:key=Signed-off-by,valueonly,unfold,separator={_FIELD_SEPARATOR})"
    f"{_RECORD_SEPARATOR}"
)
_TRAILER_VALUE_PATTERN = re_compile(r"^(?P<name>.+?)\s*<(?P<email>[^<>]+)>$")


class DcoCheckError(Exception):
    """Raised when the range cannot be checked at all; the message is exit-ready."""


def _run_git(args: list[str], *, cwd: Path | None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as error:
        raise DcoCheckError(f"git executable not found: {error}") from None


def _first_line(text: str) -> str:
    stripped = text.strip()
    first_line, _, _ = stripped.partition("\n")
    return first_line


def _is_shallow_repository(cwd: Path | None) -> bool:
    result = _run_git(["rev-parse", "--is-shallow-repository"], cwd=cwd)
    return result.returncode == 0 and result.stdout.strip() == "true"


def _resolve_commit(ref: str, *, cwd: Path | None) -> str | None:
    """Resolve `ref` to a commit SHA, or `None` if it does not name one.

    Rejects an empty ref or one starting with "-" before ever invoking git, so a
    value such as "" or "--output=/tmp/x" can never be interpreted as a git
    option or create a file; every other ref is resolved with
    `--end-of-options` for the same reason. `^{commit}` requires the result to
    be an actual commit, so a tree, blob, or tag-of-a-tree is rejected rather
    than silently accepted.
    """
    if not ref or ref.startswith("-"):
        return None
    result = _run_git(
        ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=cwd,
    )
    if result.returncode != 0:
        return None
    resolved = result.stdout.strip()
    return resolved or None


def _invalid_ref_message(argument_name: str, ref: str, *, cwd: Path | None) -> str:
    if _is_shallow_repository(cwd):
        return SHALLOW_HINT
    return f"invalid ref for {argument_name}: {ref!r} does not resolve to a commit"


def _matches_author(trailer_value: str, author_name: str, author_email: str) -> bool:
    match: Match[str] | None = _TRAILER_VALUE_PATTERN.match(trailer_value.strip())
    if match is None:
        return False
    return (
        match.group("name") == author_name.strip()
        and match.group("email").strip().lower() == author_email.strip().lower()
    )


def _split_rev_list_output(raw: str) -> list[tuple[str, str, str, list[str]]]:
    records: list[tuple[str, str, str, list[str]]] = []
    for chunk in raw.split(_RECORD_SEPARATOR):
        body = chunk.strip("\n")
        if not body:
            continue
        sha, author_name, author_email, trailer_blob = body.split(_FIELD_SEPARATOR, 3)
        candidates = [value for value in trailer_blob.split(_FIELD_SEPARATOR) if value.strip()]
        records.append((sha, author_name, author_email, candidates))
    return records


def _rev_list_records(
    base_sha: str, head_sha: str, *, cwd: Path | None
) -> list[tuple[str, str, str, list[str]]]:
    range_args = ["--end-of-options", f"{base_sha}..{head_sha}"]
    result = _run_git(
        [
            "rev-list",
            "--reverse",
            "--no-commit-header",
            f"--format={_REV_LIST_FORMAT}",
            *range_args,
        ],
        cwd=cwd,
    )
    if result.returncode != 0 and "no-commit-header" in result.stderr:
        # Older git without --no-commit-header: every commit is preceded by its
        # own "commit <sha>" header line, which the format string does not
        # produce and which is therefore always safe to filter out.
        result = _run_git(
            ["rev-list", "--reverse", f"--format={_REV_LIST_FORMAT}", *range_args],
            cwd=cwd,
        )
        if result.returncode != 0:
            raise DcoCheckError(f"git rev-list failed: {_first_line(result.stderr)}")
        filtered = "\n".join(
            line for line in result.stdout.split("\n") if not line.startswith("commit ")
        )
        return _split_rev_list_output(filtered)
    if result.returncode != 0:
        raise DcoCheckError(f"git rev-list failed: {_first_line(result.stderr)}")
    return _split_rev_list_output(result.stdout)


def unsigned_commits(base: str, head: str, *, cwd: Path | None = None) -> tuple[str, ...]:
    if _is_shallow_repository(cwd):
        raise DcoCheckError(SHALLOW_HINT)

    base_sha = _resolve_commit(base, cwd=cwd)
    if base_sha is None:
        raise DcoCheckError(_invalid_ref_message("BASE", base, cwd=cwd))

    head_sha = _resolve_commit(head, cwd=cwd)
    if head_sha is None:
        raise DcoCheckError(_invalid_ref_message("HEAD", head, cwd=cwd))

    missing: list[str] = []
    for sha, author_name, author_email, candidates in _rev_list_records(
        base_sha, head_sha, cwd=cwd
    ):
        if not any(
            _matches_author(candidate, author_name, author_email) for candidate in candidates
        ):
            missing.append(sha)
    return tuple(missing)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_dco.py BASE HEAD", file=sys.stderr)
        return 2
    try:
        missing = unsigned_commits(argv[1], argv[2])
    except DcoCheckError as error:
        print(str(error), file=sys.stderr)
        return 2
    for sha in missing:
        print(f"missing DCO sign-off matching the author: {sha}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
