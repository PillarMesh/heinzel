"""Fail unless every commit in a range carries a DCO sign-off matching its author.

Merge commits in the range are checked like any other commit. This is the
conservative choice: a merge commit can carry substantive changes (for example a
squash performed as a merge, or conflict resolutions), and a project accepting
external contributions should not create a blind spot for sign-off by exempting
merge commits from the check.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_TRAILER_PATTERN = re.compile(r"^Signed-off-by:\s*(?P<name>.+?)\s*<(?P<email>[^<>]+)>\s*$")


def _git(*args: str, cwd: Path | None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _matches_author(trailer_value: str, author: str) -> bool:
    trailer_match = _TRAILER_PATTERN.match(f"Signed-off-by: {trailer_value.strip()}")
    author_match = re.match(r"^(?P<name>.+?)\s*<(?P<email>[^<>]+)>\s*$", author.strip())
    if trailer_match is None or author_match is None:
        return False
    names_match = trailer_match.group("name") == author_match.group("name")
    emails_match = trailer_match.group("email").lower() == author_match.group("email").lower()
    return names_match and emails_match


def unsigned_commits(base: str, head: str, *, cwd: Path | None = None) -> tuple[str, ...]:
    shas = _git("rev-list", "--reverse", f"{base}..{head}", cwd=cwd).split()
    missing: list[str] = []
    for sha in shas:
        author = _git("show", "-s", "--format=%an <%ae>", sha, cwd=cwd).strip()
        trailer_values = _git(
            "log",
            "-1",
            "--format=%(trailers:key=Signed-off-by,valueonly,separator=%x00)",
            sha,
            cwd=cwd,
        ).strip("\x00\n")
        candidates = [value for value in trailer_values.split("\x00") if value.strip()]
        if not any(_matches_author(candidate, author) for candidate in candidates):
            missing.append(sha)
    return tuple(missing)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_dco.py BASE HEAD", file=sys.stderr)
        return 2
    try:
        missing = unsigned_commits(argv[1], argv[2])
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.strip() if isinstance(error.stderr, str) else ""
        print(f"check_dco.py: invalid ref or git failure: {stderr}", file=sys.stderr)
        return 2
    for sha in missing:
        print(f"missing DCO sign-off matching the author: {sha}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
