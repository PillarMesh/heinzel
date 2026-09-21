"""Contract tests for the Docker Compose quickstart.

The image build sends the repository root as its build context. Without a
`.dockerignore` that context is the whole working tree, local virtual environment and
installed packages included, and `COPY apps/console` then overwrites the image's
freshly installed `node_modules` with whatever happens to sit on the machine that ran
the build.
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]

_REQUIRED_PATTERNS = frozenset({".venv", "**/node_modules", ".git", ".mypy_cache"})


def _ignore_patterns(text: str) -> frozenset[str]:
    """The patterns Docker would read, with blank lines and comments removed.

    A comment is stripped before it is recognised, so an indented comment stays a
    comment rather than becoming a pattern named `# ...`.
    """
    lines = (line.strip() for line in text.splitlines())
    return frozenset(line for line in lines if line and not line.startswith("#"))


def _excludes(pattern: str, path: PurePosixPath) -> bool:
    """Whether `pattern` excludes `path`, for the two pattern shapes this file uses."""
    if pattern.startswith("**/"):
        return pattern.removeprefix("**/") in path.parts
    return path.parts[0] == pattern


def _tracked_paths() -> tuple[PurePosixPath, ...]:
    listing = subprocess.run(
        ("git", "ls-files", "-z"),
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return tuple(PurePosixPath(entry) for entry in listing.split("\0") if entry)


def test_the_build_context_excludes_local_environments() -> None:
    patterns = _ignore_patterns((ROOT / ".dockerignore").read_text(encoding="utf-8"))
    assert patterns >= _REQUIRED_PATTERNS


def test_the_build_context_keeps_every_file_the_repository_tracks() -> None:
    """An exclusion that matches tracked source removes it from the image in silence.

    `**/dist` is the live hazard: the console's bundle is built inside the image, so the
    host's copy must not travel, but a component that ever commits a `dist` directory
    would lose it from the build with no error at all.
    """
    patterns = _ignore_patterns((ROOT / ".dockerignore").read_text(encoding="utf-8"))
    unsupported = [
        pattern
        for pattern in patterns
        if pattern.startswith(("!", "#")) or "*" in pattern.removeprefix("**/")
    ]
    assert not unsupported, f"pattern shapes this test cannot reason about: {unsupported}"

    excluded = {
        str(path): pattern
        for path in _tracked_paths()
        for pattern in patterns
        if _excludes(pattern, path)
    }
    assert excluded == {}
