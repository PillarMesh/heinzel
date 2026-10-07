"""Contract tests for the quickstart's optional Superset, behind the `dashboards` profile.

The smoke test beside this file starts the demonstration; this reads what compose would start,
through `docker compose config` rather than through the YAML, because what a profile does to a
project is compose's answer and not something a reader of the file can be sure of. Two renderings
are compared on purpose: the default one, which must be the demonstration exactly as it was before
this profile existed, and the one `--profile dashboards` produces.

`tests/ci/test_quickstart_contract.py` holds the demonstration's own compose requirements. These
are the ones the optional dashboard target adds.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any

from heinzel_console.demo.superset_tls import (
    SUPERSET_AUTHORITY_FILENAME,
    SUPERSET_SERVER_CERTIFICATE_FILENAME,
    SUPERSET_SERVER_KEY_FILENAME,
)

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/quickstart/compose.yaml"
SUPERSET_IMAGE_ROOT = ROOT / "deploy/quickstart/superset"
SUPERSET_DOCKERFILE = SUPERSET_IMAGE_ROOT / "Dockerfile"

PROFILE = "dashboards"
# The demonstration itself, which the profile must leave alone.
DEMONSTRATION_SERVICES = frozenset({"console", "warehouse"})
DASHBOARD_SERVICES = frozenset({"superset", "superset-init"})

TLS_VOLUME = "heinzel-superset-tls"
# Where the console writes the material and where Superset reads it. Two paths, one volume.
CONSOLE_TLS_DIRECTORY = "/var/lib/heinzel-superset-tls"
SUPERSET_PRIVATE_DIRECTORY = "/heinzel-private"

# Neither has a default and neither may be written down: a secret committed beside the service that
# uses it is a secret in version control forever.
ADMIN_PASSWORD_VARIABLE = "HEINZEL_SUPERSET_ADMIN_PASSWORD"
SECRET_KEY_VARIABLE = "HEINZEL_SUPERSET_SECRET_KEY"

# The console reaches Superset by its compose service name, which is the name its certificate is
# minted for -- so the URL and the service cannot be moved apart without a verification failure.
SUPERSET_SERVICE = "superset"
SUPERSET_CONTAINER_PORT = 8088


def _render(*, profile: bool, secrets_set: bool = False) -> dict[str, Any]:
    """What compose would start, with or without the profile and with or without the secrets.

    Every `HEINZEL_` variable is dropped from the environment first. The console's view of Superset
    is written as a substitution of the admin password, so a developer who exported that password
    in the shell running the suite would otherwise see a different project than CI does.
    """
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("HEINZEL_")
    }
    if secrets_set:
        environment[ADMIN_PASSWORD_VARIABLE] = secrets.token_urlsafe(24)
        environment[SECRET_KEY_VARIABLE] = secrets.token_urlsafe(48)
    command = ["docker", "compose", "--file", str(COMPOSE)]
    if profile:
        command += ["--profile", PROFILE]
    result = subprocess.run(
        [*command, "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    configured = json.loads(result.stdout)
    assert isinstance(configured, dict)
    # Returned alongside the rendering so that a test can name the secrets it supplied.
    configured["x-test-environment"] = environment
    return configured


def _service(configured: dict[str, Any], name: str) -> dict[str, Any]:
    service = configured["services"][name]
    assert isinstance(service, dict)
    return service


def _script(service: dict[str, Any]) -> str:
    """The shell script a service's command runs.

    `docker compose config` re-escapes a `$` as `$$`, because its output is itself a compose file,
    so this is the script as the file spells it rather than as the shell receives it.
    """
    command = service["command"]
    assert command[:2] == ["/bin/sh", "-ceu"], command
    assert len(command) == 3, command
    script = command[2]
    assert isinstance(script, str)
    return script


def _mounts(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The service's mounts by the volume or host path they come from."""
    mounts: dict[str, dict[str, Any]] = {}
    for mount in service.get("volumes", ()):
        assert isinstance(mount, dict)
        mounts[str(mount["source"])] = mount
    return mounts


def test_the_default_start_creates_no_superset_service() -> None:
    """`docker compose up` must be the demonstration it was before the profile existed.

    Not a preference about tidiness: the Superset image is a two-gigabyte build and the console
    publishes to it only when told to, so a reader who wants the demonstration must get exactly
    the demonstration.
    """
    configured = _render(profile=False)

    assert set(configured["services"]) == DEMONSTRATION_SERVICES


def test_only_the_dashboards_profile_adds_superset() -> None:
    configured = _render(profile=True)

    assert set(configured["services"]) == DEMONSTRATION_SERVICES | DASHBOARD_SERVICES
    for name in sorted(DASHBOARD_SERVICES):
        assert _service(configured, name)["profiles"] == [PROFILE], name


def test_enabling_the_profile_changes_nothing_about_the_demonstration() -> None:
    """The two existing services must be rendered identically either way.

    A profile that quietly altered the console -- a mount, an environment entry, a dependency --
    would mean the demonstration a reader starts with dashboards is not the one the smoke test
    proves. Compared as whole rendered services rather than field by field, so an addition nobody
    thought to assert about is caught too.
    """
    default = _render(profile=False)
    dashboards = _render(profile=True)

    for name in sorted(DEMONSTRATION_SERVICES):
        assert _service(dashboards, name) == _service(default, name), name


def test_superset_is_published_on_loopback_only() -> None:
    """The published port is this Superset's whole boundary, as it is the console's.

    `8088:8088` would publish on every interface a Superset holding one admin account whose
    password is in the environment of whoever started it.
    """
    configured = _render(profile=True)

    assert _service(configured, "superset")["ports"] == [
        {
            "mode": "ingress",
            "target": SUPERSET_CONTAINER_PORT,
            "published": "8088",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]
    # The initialization serves nothing, so it publishes nothing.
    assert _service(configured, "superset-init").get("ports", []) == []


def test_superset_serves_the_tls_material_the_console_mints_into_a_shared_volume() -> None:
    """One named volume, written by the console and read by Superset.

    The console is the writer, so its mount is read-write and Superset's is read-only. The three
    filenames are imported from the module that writes them: a rename there must reach this file,
    rather than leaving a wait loop watching for a file nothing will write.
    """
    configured = _render(profile=True, secrets_set=True)
    console = _mounts(_service(configured, "console"))[TLS_VOLUME]
    superset = _mounts(_service(configured, SUPERSET_SERVICE))[TLS_VOLUME]

    assert console["target"] == CONSOLE_TLS_DIRECTORY
    assert console.get("read_only", False) is False, "the console mints this material"
    assert superset["target"] == SUPERSET_PRIVATE_DIRECTORY
    assert superset["read_only"] is True, "Superset serves this material and never writes it"

    # The console is told where to mint, by the path it mounts rather than by a second literal.
    environment = _service(configured, "console")["environment"]
    assert environment["HEINZEL_DEMO_SUPERSET_TLS_DIRECTORY"] == CONSOLE_TLS_DIRECTORY

    script = _script(_service(configured, SUPERSET_SERVICE))
    for filename in (
        SUPERSET_SERVER_KEY_FILENAME,
        SUPERSET_SERVER_CERTIFICATE_FILENAME,
        SUPERSET_AUTHORITY_FILENAME,
    ):
        assert f"[ -f {SUPERSET_PRIVATE_DIRECTORY}/{filename} ]" in script, filename
    certificate = f"{SUPERSET_PRIVATE_DIRECTORY}/{SUPERSET_SERVER_CERTIFICATE_FILENAME}"
    assert f"--certfile {certificate}" in script
    assert f"--keyfile {SUPERSET_PRIVATE_DIRECTORY}/{SUPERSET_SERVER_KEY_FILENAME}" in script


def test_superset_waits_for_that_material_rather_than_exiting_without_it() -> None:
    """The console mints during its own startup and these containers start concurrently.

    Reaching for a certificate that is not there yet is therefore the normal case, and gunicorn
    exits on it. There is no restart policy here -- deliberately, as the console's own absence of
    one explains -- so an unguarded `exec` leaves a Superset that never serves.
    """
    configured = _render(profile=True, secrets_set=True)
    script = _script(_service(configured, SUPERSET_SERVICE))

    waiting, _, serving = script.partition("done")
    assert waiting.startswith(":") or "until" in waiting, script
    assert "until" in waiting and "sleep 1" in waiting, script
    # The wait is before the server, not beside it.
    assert "exec gunicorn" in serving, script
    assert "exec gunicorn" not in waiting, script

    # Superset serves nothing on plain HTTP, so the check has to speak TLS and trust the minted
    # authority: a check over `http://` reports an unhealthy Superset that is serving correctly.
    assert _service(configured, SUPERSET_SERVICE)["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "--fail",
        "--cacert",
        f"{SUPERSET_PRIVATE_DIRECTORY}/{SUPERSET_AUTHORITY_FILENAME}",
        f"https://127.0.0.1:{SUPERSET_CONTAINER_PORT}/health",
    ]


def test_superset_is_initialized_once_before_it_serves() -> None:
    """The metadata database, the admin account and the roles, created by a service that completes.

    `superset db upgrade` against the volume the server is already reading would be a migration
    racing a running server, which is why this is a separate service the server waits on.
    """
    configured = _render(profile=True, secrets_set=True)
    initialization = _script(_service(configured, "superset-init"))

    assert "superset db upgrade" in initialization
    assert "superset fab create-admin" in initialization
    assert "superset init" in initialization
    assert _service(configured, SUPERSET_SERVICE)["depends_on"]["superset-init"] == {
        "condition": "service_completed_successfully",
        "required": True,
    }
    # The console is what mints the material the server waits for, so `up superset` alone must not
    # leave it waiting for a file nothing is writing.
    assert _service(configured, SUPERSET_SERVICE)["depends_on"]["console"] == {
        "condition": "service_started",
        "required": True,
    }


def test_both_superset_secrets_are_required_from_the_environment_and_never_written_down() -> None:
    """Unset, each variable reaches the container unset and the command refuses on it by name.

    Spelled as a pass-through rather than as the emulator's `${VAR:?required}` because compose
    resolves every substitution before it applies profiles, so that spelling would make the
    default `docker compose up` fail for want of a Superset secret. The requirement is therefore
    enforced in the command, and this reads both halves.
    """
    unset = _render(profile=True)
    for name in sorted(DASHBOARD_SERVICES):
        environment = _service(unset, name)["environment"]
        assert environment[ADMIN_PASSWORD_VARIABLE] is None, name
        assert environment[SECRET_KEY_VARIABLE] is None, name
        assert f"{SECRET_KEY_VARIABLE}:?" in _script(_service(unset, name)), name
    assert f"{ADMIN_PASSWORD_VARIABLE}:?" in _script(_service(unset, "superset-init"))

    # Set, the value passed through is the value the container gets -- one variable for the admin
    # Superset is created with and the admin the console signs in as.
    supplied = _render(profile=True, secrets_set=True)
    expected = supplied["x-test-environment"]
    for name in sorted(DASHBOARD_SERVICES):
        environment = _service(supplied, name)["environment"]
        assert environment[ADMIN_PASSWORD_VARIABLE] == expected[ADMIN_PASSWORD_VARIABLE], name
        assert environment[SECRET_KEY_VARIABLE] == expected[SECRET_KEY_VARIABLE], name
    console = _service(supplied, "console")["environment"]
    assert console["HEINZEL_DEMO_SUPERSET_ADMIN_PASSWORD"] == expected[ADMIN_PASSWORD_VARIABLE]

    # And no value for either is written in the file: every mention is a bare name or a
    # substitution of one, never a literal.
    text = COMPOSE.read_text(encoding="utf-8")
    for name in (ADMIN_PASSWORD_VARIABLE, SECRET_KEY_VARIABLE):
        literals = re.findall(rf"^\s*-?\s*{name}[:=]\s*(?!\s*$)(?!\$)(\S.*)$", text, re.MULTILINE)
        assert literals == [], f"{name} is given a value here: {literals}"


def test_the_console_is_told_about_superset_only_when_one_can_start() -> None:
    """The console's view of Superset reads the same variable the Superset services require.

    So a default start leaves it with nothing and reports dashboard publication as not delivered,
    exactly as it did before this profile existed, and a start that can run Superset is a start
    that has told the console where it is. The two cannot be configured apart.
    """
    without = _service(_render(profile=False), "console")["environment"]
    assert without["HEINZEL_DEMO_SUPERSET_BASE_URL"] == ""
    assert without["HEINZEL_DEMO_SUPERSET_TLS_DIRECTORY"] == ""
    assert without["HEINZEL_DEMO_SUPERSET_ADMIN_PASSWORD"] == ""

    with_secrets = _service(_render(profile=True, secrets_set=True), "console")["environment"]
    # HTTPS, because `SupersetCredentials` refuses anything else, and the service name, because
    # that is the name the console mints the certificate for.
    assert (
        with_secrets["HEINZEL_DEMO_SUPERSET_BASE_URL"]
        == f"https://{SUPERSET_SERVICE}:{SUPERSET_CONTAINER_PORT}"
    )


def test_the_superset_image_is_built_from_the_quickstart_and_pinned_by_digest() -> None:
    """The image and the configuration are the quickstart's own, and its base is a digest.

    `tests/emulators/superset/compose.yaml` builds this same context, so the live dashboard
    requirements exercise the artifact a reader starts rather than a test-only copy of it. A tag
    can be rebuilt; only a digest fixes what the build installs.
    """
    configured = _render(profile=True)
    for name in sorted(DASHBOARD_SERVICES):
        service = _service(configured, name)
        assert service["build"]["context"] == str(SUPERSET_IMAGE_ROOT), name
        assert service["build"]["dockerfile"] == "Dockerfile", name
        assert "image" not in service, name
        # Superset holds its metadata database in this volume and loads its configuration from the
        # quickstart's own file; both services need both, because one creates what the other reads.
        mounts = _mounts(service)
        assert mounts["heinzel-superset-home"]["target"] == "/app/superset_home", name
        assert service["environment"]["SUPERSET_CONFIG_PATH"].startswith("/app/pythonpath/"), name
        configuration = mounts[str(SUPERSET_IMAGE_ROOT / "superset_config.py")]
        assert configuration["target"] == service["environment"]["SUPERSET_CONFIG_PATH"], name
        assert configuration["read_only"] is True, name
        # Superset is a heavy process beside a console, a warehouse and a dbt run on one machine.
        assert int(service["mem_limit"]) >= 1024 * 1024 * 1024, name

    bases = re.findall(r"^FROM\s+(\S+)", SUPERSET_DOCKERFILE.read_text(encoding="utf-8"), re.M)
    assert bases, "the Superset image declares no base"
    assert [base for base in bases if "@sha256:" not in base] == [], bases
