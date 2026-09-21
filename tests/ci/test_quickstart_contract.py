"""Contract tests for the Docker Compose quickstart.

The image build sends the repository root as its build context. Without a
`.dockerignore` that context is the whole working tree, local virtual environment and
installed packages included, and `COPY apps/console` then overwrites the image's
freshly installed `node_modules` with whatever happens to sit on the machine that ran
the build.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/quickstart/compose.yaml"
DOCKERFILE = ROOT / "deploy/quickstart/Dockerfile"

# The two shapes `_excludes` reasons about: one path segment, optionally at any depth.
# Anything else -- a separator, a wildcard, a character class, a negation -- is a
# pattern this file cannot judge, and an allowlist refuses it rather than ignoring it.
_SUPPORTED_SHAPE = re.compile(r"(?:\*\*/)?[^*?\[\]!/]+\Z")


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
    try:
        listing = subprocess.run(
            ("git", "ls-files", "-z"),
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError:  # a source tarball has no tracked-file list
        pytest.skip("not a git checkout")
    return tuple(PurePosixPath(entry) for entry in listing.split("\0") if entry)


def test_the_build_context_excludes_local_environments() -> None:
    patterns = _ignore_patterns((ROOT / ".dockerignore").read_text(encoding="utf-8"))
    assert patterns >= {".venv", "**/node_modules", ".git", ".mypy_cache"}


def test_the_build_context_keeps_every_file_the_repository_tracks() -> None:
    """An exclusion that matches tracked source removes it from the image in silence.

    `**/dist` is the live hazard: the console's bundle is built inside the image, so the
    host's copy must not travel, but a component that ever commits a `dist` directory
    would lose it from the build with no error at all.
    """
    patterns = _ignore_patterns((ROOT / ".dockerignore").read_text(encoding="utf-8"))
    unsupported = [pattern for pattern in patterns if not _SUPPORTED_SHAPE.fullmatch(pattern)]
    assert not unsupported, f"pattern shapes this test cannot reason about: {unsupported}"

    excluded = {
        str(path): pattern
        for path in _tracked_paths()
        for pattern in patterns
        if _excludes(pattern, path)
    }
    assert excluded == {}


def test_the_quickstart_publishes_on_loopback_only() -> None:
    """A published port is the whole network boundary of an unauthenticated console."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert compose["services"]["console"]["ports"] == ["127.0.0.1:8000:8000"]


def test_the_quickstart_keeps_state_in_a_named_volume() -> None:
    """Without the named volume `docker compose down -v` has nothing to remove, and the
    demonstration cannot be reset without deleting the container's own layer."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert compose["services"]["console"]["volumes"] == ["heinzel-state:/var/lib/heinzel"]
    assert "heinzel-state" in compose["volumes"]


def test_the_quickstart_accepts_the_origin_its_published_port_serves() -> None:
    """The published port and the accepted origin are one setting spelled in two places.

    A console whose origin does not match the port a browser opens renders every page and
    refuses every command `same_origin_required`, so compose states the origin beside the
    port that has to agree with it rather than leaving it to the image's default.
    """
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    environment = compose["services"]["console"]["environment"]
    assert environment["HEINZEL_CONSOLE_ALLOWED_ORIGIN"] == "http://127.0.0.1:8000"


def test_every_quickstart_image_is_digest_pinned() -> None:
    """A tag can be rebuilt; a digest cannot, so only a digest fixes what is installed."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    images = re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)
    images += re.findall(r"^COPY\s+--from=(\S+)", dockerfile, re.MULTILINE)
    referenced = [image for image in images if ":" in image or "/" in image]
    assert referenced, "no image references found"
    assert [image for image in referenced if "@sha256:" not in image] == []


def test_the_image_runs_the_documented_command() -> None:
    """The image must start the demonstration the README describes, bound for a container.

    Read from the `ENTRYPOINT` line rather than from the whole file, because this Dockerfile
    explains `--container` in a comment: a search of the file passes on an image whose
    entrypoint has lost the flag, and that image binds loopback inside the container, where
    the published port reaches nothing.
    """
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    entrypoint = re.search(r"^ENTRYPOINT\s+(.+)$", dockerfile, re.MULTILINE)
    assert entrypoint is not None, "the image declares no ENTRYPOINT"
    assert '"heinzel-console", "serve"' in entrypoint.group(1)
    assert "--container" in entrypoint.group(1)


def test_the_node_version_matches_the_console() -> None:
    """The bundle is built in the image, so its Node must be the console's own."""
    pinned = (ROOT / "apps/console/.node-version").read_text(encoding="utf-8").strip()
    assert f"FROM node:{pinned}-" in DOCKERFILE.read_text(encoding="utf-8")
