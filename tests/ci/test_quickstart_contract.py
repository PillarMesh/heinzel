"""Contract tests for the Docker Compose quickstart.

The image build sends the repository root as its build context. Without a
`.dockerignore` that context is the whole working tree, local virtual environment and
installed packages included, and `COPY apps/console` then overwrites the image's
freshly installed `node_modules` with whatever happens to sit on the machine that ran
the build.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import yaml
from heinzel_console.cli import _DEFAULT_PORT

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/quickstart/compose.yaml"
DOCKERFILE = ROOT / "deploy/quickstart/Dockerfile"

# The pattern shapes `_excludes` reasons about: a literal path of one or more segments,
# optionally at any depth (`**/`) and optionally ending in `*`. Anything else -- another
# wildcard, a character class, a negation -- is a pattern this file cannot judge, and an
# allowlist refuses it rather than ignoring it.
_SUPPORTED_SHAPE = re.compile(r"(?:\*\*/)?[^*?\[\]!]+\*?\Z")

# The two directories the image drops on purpose. Every other tracked path has to reach
# the build context.
_DELIBERATE_EXCLUSIONS = frozenset({"**/tests", "apps/console/e2e*"})

# `${NAME:-default}`, the one substitution compose.yaml uses. Both the published port and
# the origin are written with it, so that neither can be moved without the other.
_SUBSTITUTION = re.compile(r"\$\{(?P<name>[A-Z_][A-Z0-9_]*):-(?P<default>[^}]*)\}")

# `host:published:container`, where `published` may be a substitution rather than a number.
_PORT_MAPPING = re.compile(r"(?P<host>[0-9.]+):(?P<published>\S+):(?P<container>[0-9]+)")


def _ignore_patterns(text: str) -> frozenset[str]:
    """The patterns Docker would read, with blank lines and comments removed.

    A comment is stripped before it is recognised, so an indented comment stays a
    comment rather than becoming a pattern named `# ...`.
    """
    lines = (line.strip() for line in text.splitlines())
    return frozenset(line for line in lines if line and not line.startswith("#"))


def _matches_from(pattern: str, parts: tuple[str, ...]) -> bool:
    """Whether a literal path with an optional trailing `*` covers `parts` from its start."""
    *literal, last = pattern.split("/")
    if len(parts) <= len(literal):
        return False
    if list(parts[: len(literal)]) != literal:
        return False
    candidate = parts[len(literal)]
    return candidate.startswith(last[:-1]) if last.endswith("*") else candidate == last


def _excludes(pattern: str, path: PurePosixPath) -> bool:
    """Whether `pattern` excludes `path`, for the pattern shapes this file allows."""
    if pattern.startswith("**/"):
        remainder = pattern.removeprefix("**/")
        return any(_matches_from(remainder, path.parts[index:]) for index in range(len(path.parts)))
    return _matches_from(pattern, path.parts)


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


def _console_service() -> dict[str, object]:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["console"]
    assert isinstance(service, dict)
    return service


def _port_mapping() -> re.Match[str]:
    """The single published port entry, split into host, published and container parts."""
    ports = _console_service()["ports"]
    assert isinstance(ports, list) and len(ports) == 1, ports
    mapping = _PORT_MAPPING.fullmatch(str(ports[0]))
    assert mapping is not None, f"unreadable port mapping: {ports[0]!r}"
    return mapping


def _resolved(value: str) -> str:
    """`value` with each substitution replaced by its default, as compose reads it when
    nothing is set in the environment."""
    return _SUBSTITUTION.sub(lambda match: match["default"], value)


def _entrypoint_argv() -> list[str]:
    """The image's entrypoint as the argument list it is, not as the text that spells it.

    Read from the `ENTRYPOINT` line rather than from the whole file, because this
    Dockerfile explains `--container` in a comment: a search of the file passes on an
    image whose entrypoint has lost the flag, and that image binds loopback inside the
    container, where the published port reaches nothing.
    """
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    entrypoint = re.search(r"^ENTRYPOINT\s+(\[.*\])\s*$", dockerfile, re.MULTILINE)
    assert entrypoint is not None, "the image declares no ENTRYPOINT in exec form"
    argv = json.loads(entrypoint.group(1))
    assert isinstance(argv, list) and all(isinstance(item, str) for item in argv), argv
    return argv


def test_the_build_context_excludes_local_environments() -> None:
    patterns = _ignore_patterns((ROOT / ".dockerignore").read_text(encoding="utf-8"))
    assert patterns >= {".venv", "**/node_modules", ".git", ".mypy_cache"}
    assert patterns >= _DELIBERATE_EXCLUSIONS


def test_the_build_context_keeps_the_sources_the_image_needs() -> None:
    """An exclusion that matches tracked source removes it from the image in silence.

    `**/dist` is the live hazard: the console's bundle is built inside the image, so the
    host's copy must not travel, but a component that ever commits a `dist` directory
    would lose it from the build with no error at all. The suites are excluded on
    purpose, because nothing the image runs reaches them; everything else must survive.
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
    unexpected = {
        path: pattern for path, pattern in excluded.items() if pattern not in _DELIBERATE_EXCLUSIONS
    }
    assert unexpected == {}, (
        f"these tracked files would leave the build context with no error at all: {unexpected}"
    )
    # An exclusion that matches nothing is a misspelling, and what it was written to keep
    # out of the image is in it.
    idle = sorted(_DELIBERATE_EXCLUSIONS - set(excluded.values()))
    assert idle == [], f"these exclusions match no tracked file, so they are misspelled: {idle}"


def test_the_quickstart_publishes_on_loopback_only() -> None:
    """A published port is the whole network boundary of an unauthenticated console."""
    assert _port_mapping()["host"] == "127.0.0.1"


def test_the_quickstart_keeps_state_in_a_named_volume() -> None:
    """Without the named volume `docker compose down -v` has nothing to remove, and the
    demonstration cannot be reset without deleting the container's own layer."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert compose["services"]["console"]["volumes"] == ["heinzel-state:/var/lib/heinzel"]
    assert "heinzel-state" in compose["volumes"]


def test_the_quickstart_accepts_the_origin_its_published_port_serves() -> None:
    """The published port and the accepted origin are one setting spelled in two places.

    A console whose origin does not match the port a browser opens renders every page and
    refuses every command `same_origin_required`. Two separate literals would both have to
    be read to see that, and would agree with each other by accident when both were wrong,
    so the origin is derived here from the port entry itself.

    The comparison is on the text compose.yaml holds rather than on `docker compose config`
    output, because the offline suite has no Docker, and because identical text is the
    stronger statement: it says the two read one variable, and so agree for every value of
    it rather than for the one value a test happened to try.
    """
    mapping = _port_mapping()
    environment = _console_service()["environment"]
    assert isinstance(environment, dict)
    assert (
        environment["HEINZEL_CONSOLE_ALLOWED_ORIGIN"]
        == f"http://{mapping['host']}:{mapping['published']}"
    )
    # A substituted port must carry a default, or `up` with nothing set publishes no port
    # and the origin names none either.
    published = mapping["published"]
    if "$" in published:
        assert _SUBSTITUTION.fullmatch(published), f"unreadable published port: {published!r}"
    assert _resolved(published).isdigit(), f"the published port is not a number: {published!r}"


def test_the_published_port_reaches_the_port_the_entrypoint_serves() -> None:
    """Compose maps to a container port, and nothing on the command line names that port.

    The entrypoint passes no `--port`, so what listens inside the container is the
    command's own default. Were that default to change, compose would publish a port
    nothing is listening on, and every other test here would still pass.
    """
    argv = _entrypoint_argv()
    assert "--port" not in argv, (
        f"the entrypoint now names a port, so read it from {argv} rather than from the "
        "command's default"
    )
    assert int(_port_mapping()["container"]) == _DEFAULT_PORT


def test_both_readmes_name_the_published_port() -> None:
    """A README naming another port sends the reader to a page nothing serves."""
    url = f"http://127.0.0.1:{_resolved(_port_mapping()['published'])}"
    for readme in (ROOT / "README.md", ROOT / "deploy/quickstart/README.md"):
        assert url in readme.read_text(encoding="utf-8"), f"{readme} does not name {url}"


def test_every_quickstart_image_is_digest_pinned() -> None:
    """A tag can be rebuilt; a digest cannot, so only a digest fixes what is installed."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    images = re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)
    images += re.findall(r"^COPY\s+--from=(\S+)", dockerfile, re.MULTILINE)
    # uv is mounted from its image for the length of one instruction rather than copied in.
    images += re.findall(r"--mount=\S*?from=([^,\s]+)", dockerfile)
    referenced = [image for image in images if ":" in image or "/" in image]
    assert referenced, "no image references found"
    unpinned = [image for image in referenced if "@sha256:" not in image]
    assert unpinned == [], (
        "give each of these a digest as well as a tag: a tag can be rebuilt under this "
        f"image, and what it installs then changes with no edit here: {unpinned}"
    )


def test_the_image_runs_the_documented_command() -> None:
    """The image must start the demonstration the README describes, bound for a container."""
    argv = _entrypoint_argv()
    assert argv[:2] == ["heinzel-console", "serve"], argv
    assert "--container" in argv, argv


def test_the_node_version_matches_the_console() -> None:
    """The bundle is built in the image, so its Node must be the console's own."""
    pinned = (ROOT / "apps/console/.node-version").read_text(encoding="utf-8").strip()
    # A version boundary rather than a literal `-`: `FROM node:24.20.0@sha256:...` is a
    # valid pin of this version and `FROM node:24.20.01-...` is another version entirely.
    assert re.search(
        rf"^FROM node:{re.escape(pinned)}[-@\s]",
        DOCKERFILE.read_text(encoding="utf-8"),
        re.MULTILINE,
    ), f"the Dockerfile does not build the bundle on node {pinned}"


def test_the_python_version_matches_the_repository() -> None:
    """The Dockerfile says its two runtimes are pinned together; this is the other half."""
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert re.search(
        rf"^FROM python:{re.escape(pinned)}[-@\s]",
        DOCKERFILE.read_text(encoding="utf-8"),
        re.MULTILINE,
    ), f"the Dockerfile does not run on python {pinned}"
