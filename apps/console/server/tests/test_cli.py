"""The `heinzel-console` command.

The command serves a console that has no authentication, so the tests that matter here are
the ones about where it may bind, which origin the browser must use, and whether it releases
the state directory however the server exits. Only the test that proves an unavailable port
is reported rather than raised binds one.
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import socket
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from heinzel_console import cli
from starlette.applications import Starlette

_ORIGIN_VARIABLE = "HEINZEL_CONSOLE_ALLOWED_ORIGIN"
_DIST_VARIABLE = "HEINZEL_CONSOLE_DIST"
_WAREHOUSE_VARIABLE = "HEINZEL_DEMO_WAREHOUSE_DSN"


@pytest.fixture(autouse=True)
def _no_configured_origin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the environment's own configuration out of every test that does not set it."""
    monkeypatch.delenv(_ORIGIN_VARIABLE, raising=False)
    monkeypatch.delenv(_DIST_VARIABLE, raising=False)
    monkeypatch.delenv(_WAREHOUSE_VARIABLE, raising=False)
    yield


@pytest.fixture
def _no_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Refuse to build or serve anything, so argument tests never touch a state directory."""
    monkeypatch.setattr(cli, "build_demo_app", _unreachable_builder)
    yield


def _unreachable_builder(
    *arguments: object, **keywords: object
) -> tuple[Starlette, Callable[[], None]]:
    raise AssertionError("the application must not be built")


def test_serve_without_a_state_directory_exits_two() -> None:
    with pytest.raises(SystemExit) as failure:
        cli.parse_arguments(["serve"])
    assert failure.value.code == 2


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as failure:
        cli.parse_arguments([])
    assert failure.value.code == 2


def test_serve_binds_the_loopback_host_by_default() -> None:
    assert cli.parse_arguments(["serve", "--state-dir", "/tmp/state"]).host == "127.0.0.1"


def test_container_binds_every_interface() -> None:
    arguments = cli.parse_arguments(["serve", "--state-dir", "/tmp/state", "--container"])
    assert arguments.host == "0.0.0.0"


@pytest.mark.parametrize("abbreviation", ["--c", "--cont", "--contain"])
def test_an_abbreviated_container_flag_never_removes_the_network_boundary(
    abbreviation: str,
) -> None:
    """Argparse abbreviation would otherwise apply to the one flag that binds every interface."""
    with pytest.raises(SystemExit) as failure:
        cli.parse_arguments(["serve", "--state-dir", "/tmp/state", abbreviation])
    assert failure.value.code == 2


def test_a_rejected_argument_is_never_echoed(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.parse_arguments(["serve", "--state-dir", "/tmp/secret-token-dir", "--nope"])
    reported = capsys.readouterr()
    assert "command arguments are invalid" in reported.err
    assert "secret" not in reported.err + reported.out
    assert "--nope" not in reported.err + reported.out


def test_require_loopback_refuses_a_host_that_is_not_loopback() -> None:
    with pytest.raises(ValueError, match="no authentication"):
        cli.require_loopback("0.0.0.0")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_require_loopback_returns_each_loopback_host(host: str) -> None:
    assert cli.require_loopback(host) == host


def test_the_warning_names_the_missing_authentication(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=cli.__name__):
        cli.warn_when_unauthenticated(host="127.0.0.1")
    assert "no authentication" in caplog.text
    assert "127.0.0.1" in caplog.text


class _RecordingConsole:
    """A stand-in for the demonstration console that records how it was built and closed."""

    def __init__(self) -> None:
        self.closed = 0
        self.built: list[tuple[Path, dict[str, object]]] = []

    def build(self, state_dir: Path, **keywords: object) -> tuple[Starlette, Callable[[], None]]:
        self.built.append((state_dir, dict(keywords)))
        return Starlette(), self.close

    def close(self) -> None:
        self.closed += 1


def _serving(monkeypatch: pytest.MonkeyPatch, run: Callable[..., None]) -> _RecordingConsole:
    console = _RecordingConsole()
    monkeypatch.setattr(cli, "build_demo_app", console.build)
    monkeypatch.setattr("uvicorn.run", run)
    return console


def _served(monkeypatch: pytest.MonkeyPatch) -> tuple[_RecordingConsole, list[object]]:
    served: list[object] = []
    console = _serving(monkeypatch, lambda app, **keywords: served.append(keywords))
    return console, served


def test_the_stores_are_closed_when_the_server_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fail(*arguments: object, **keywords: object) -> None:
        raise RuntimeError("the port is already in use")

    console = _serving(monkeypatch, _fail)
    with pytest.raises(RuntimeError, match="already in use"):
        cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"])
    assert console.closed == 1


def test_the_stores_are_closed_when_the_server_returns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    console, served = _served(monkeypatch)
    assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"]) == 0
    assert console.closed == 1
    assert served == [{"host": "127.0.0.1", "port": 8000}]


def test_the_stores_are_closed_when_the_container_is_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SIGTERM is how a container stops, and uvicorn re-raises it once its own handler is gone."""

    def _terminated(*arguments: object, **keywords: object) -> None:
        # Raising SIGTERM with its default disposition still in place would kill the test
        # session outright, so an unhandled signal must fail this test rather than end it.
        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
        signal.raise_signal(signal.SIGTERM)

    console = _serving(monkeypatch, _terminated)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed", "--port", "8741"])
    assert stopped.value.code == 143
    assert console.closed == 1


def test_the_previous_termination_handler_is_restored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = signal.getsignal(signal.SIGTERM)
    _served(monkeypatch)
    assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"]) == 0
    assert signal.getsignal(signal.SIGTERM) is before


def test_every_serve_option_reaches_the_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    console, served = _served(monkeypatch)
    assert (
        cli.main(
            [
                "serve",
                "--state-dir",
                str(tmp_path),
                "--port",
                "9111",
                "--dist",
                str(dist),
                "--no-seed",
            ]
        )
        == 0
    )
    assert console.built == [
        (
            tmp_path,
            {
                "seed": False,
                "origin": "http://127.0.0.1:9111",
                "dist": dist,
                "warehouse_dsn": None,
            },
        )
    ]
    assert served == [{"host": "127.0.0.1", "port": 9111}]


def test_the_demonstration_is_seeded_unless_it_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    console, _ = _served(monkeypatch)
    assert cli.main(["serve", "--state-dir", str(tmp_path)]) == 0
    assert console.built == [
        (
            tmp_path,
            {
                "seed": True,
                "origin": "http://127.0.0.1:8000",
                "dist": None,
                "warehouse_dsn": None,
            },
        )
    ]


def test_the_container_takes_its_origin_from_the_environment_not_the_bind_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bind address is `0.0.0.0`, which no browser ever sends as its origin."""
    monkeypatch.setenv(_ORIGIN_VARIABLE, "http://127.0.0.1:8000")
    console, served = _served(monkeypatch)
    assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed", "--container"]) == 0
    assert console.built == [
        (
            tmp_path,
            {
                "seed": False,
                "origin": "http://127.0.0.1:8000",
                "dist": None,
                "warehouse_dsn": None,
            },
        )
    ]
    assert served == [{"host": "0.0.0.0", "port": 8000}]


def test_the_warehouse_connection_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DSN carries a password, so it is never a command argument a process listing shows."""
    monkeypatch.setenv(_WAREHOUSE_VARIABLE, "postgresql://postgres:secret@127.0.0.1/heinzel")
    console, _ = _served(monkeypatch)

    assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"]) == 0

    assert console.built[0][1]["warehouse_dsn"] == (
        "postgresql://postgres:secret@127.0.0.1/heinzel"
    )


def test_a_blank_warehouse_connection_reads_as_no_warehouse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unset variable and one set to nothing are the same statement.

    Passed on as `""` it reaches psycopg as a connection naming no host, which fails somewhere
    inside provisioning rather than reading as a console with no warehouse configured.
    """
    monkeypatch.setenv(_WAREHOUSE_VARIABLE, "")
    console, _ = _served(monkeypatch)

    assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"]) == 0

    assert console.built[0][1]["warehouse_dsn"] is None


def test_the_origin_option_is_preferred_over_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(_ORIGIN_VARIABLE, "http://127.0.0.1:8000")
    console, _ = _served(monkeypatch)
    assert (
        cli.main(
            [
                "serve",
                "--state-dir",
                str(tmp_path),
                "--no-seed",
                "--container",
                "--origin",
                "http://localhost:9000",
            ]
        )
        == 0
    )
    assert console.built[0][1]["origin"] == "http://localhost:9000"


def test_a_container_without_a_browser_origin_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
) -> None:
    """Serving with an origin taken from `0.0.0.0` would refuse every command it received."""
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert cli.main(["serve", "--state-dir", str(tmp_path), "--container"]) == 2
    assert _ORIGIN_VARIABLE in caplog.text
    assert "--origin" in caplog.text


def test_a_state_directory_that_cannot_be_opened_is_reported_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """The operator typed this path, and two candidate directories are otherwise guesswork."""

    def _refuse(*arguments: object, **keywords: object) -> tuple[Starlette, Callable[[], None]]:
        raise PermissionError(13, "Permission denied", str(tmp_path / "state-directory"))

    monkeypatch.setattr(cli, "build_demo_app", _refuse)
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"]) == 1
    assert "state directory" in caplog.text
    assert "Permission denied" in caplog.text
    assert str(tmp_path / "state-directory") in caplog.text
    assert caplog.records[-1].exc_info is None


def test_a_state_directory_failure_without_a_message_never_formats_the_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """`strerror` is `None` for an `OSError` raised by hand, and the fallback must not
    format an exception whose only argument is a sentence naming a path."""

    def _refuse(*arguments: object, **keywords: object) -> tuple[Starlette, Callable[[], None]]:
        raise OSError("could not open /tmp/private-token-dir/requests.sqlite3")

    monkeypatch.setattr(cli, "build_demo_app", _refuse)
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"]) == 1
    assert "OSError" in caplog.text
    assert "private-token-dir" not in caplog.text


def test_a_missing_bundle_names_the_setting_that_configures_it(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
) -> None:
    """A bundle that does not exist would serve a blank page, so it stops the command instead."""
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert (
            cli.main(["serve", "--state-dir", str(tmp_path), "--dist", str(tmp_path / "absent")])
            == 2
        )
    assert "HEINZEL_CONSOLE_DIST" in caplog.text


def test_main_guards_the_bound_host_against_the_loopback_hosts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    guarded: list[str] = []
    checked = cli.require_loopback

    def _record(host: str) -> str:
        guarded.append(host)
        return checked(host)

    monkeypatch.setattr(cli, "require_loopback", _record)
    _served(monkeypatch)
    assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"]) == 0
    assert guarded == ["127.0.0.1"]


def test_main_reads_the_process_arguments_when_it_is_given_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    console, _ = _served(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["heinzel-console", "serve", "--state-dir", str(tmp_path)])
    assert cli.main() == 0
    assert console.built[0][0] == tmp_path


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_an_origin_given_no_value_is_refused_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
    blank: str,
) -> None:
    """`--origin "$HEINZEL_CONSOLE_ALLOWED_ORIGIN"` with the variable unset reaches here.

    An empty value returned verbatim leaves `create_app` to fall back to its own default
    origin, which bypasses the refusal above and serves a console that renders every page
    and refuses every command.
    """
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert (
            cli.main(["serve", "--state-dir", str(tmp_path), "--container", "--origin", blank]) == 2
        )
    assert "--origin" in caplog.text
    assert _ORIGIN_VARIABLE in caplog.text


@pytest.mark.parametrize(
    "unusable",
    [
        "127.0.0.1:8000",
        "http://127.0.0.1:8000/",
        "http://127.0.0.1:8000/console",
        "http://127.0.0.1:8000?opened=1",
        "http://127.0.0.1:8000#top",
        "HTTP://127.0.0.1:8000",
        "http://LOCALHOST:8000",
        " http://127.0.0.1:8000",
        "http://127.0.0.1:8000 ",
        "ftp://127.0.0.1:8000",
        "http://",
    ],
)
def test_an_origin_no_browser_would_ever_send_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
    unusable: str,
) -> None:
    """The gate compares the `Origin` header literally, so any other spelling refuses
    every command while every page still renders."""
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert cli.main(["serve", "--state-dir", str(tmp_path), "--origin", unusable]) == 2
    assert "origin" in caplog.text


def test_an_unusable_origin_is_reported_with_the_spelling_the_browser_would_send(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
) -> None:
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert cli.main(["serve", "--state-dir", str(tmp_path), "--origin", "HTTP://X:8000/"]) == 2
    assert "http://x:8000" in caplog.text


def test_an_origin_carrying_credentials_is_refused_without_repeating_them(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
) -> None:
    """No browser sends userinfo in an `Origin`, and a password must never reach a log."""
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert (
            cli.main(
                [
                    "serve",
                    "--state-dir",
                    str(tmp_path),
                    "--origin",
                    "http://operator:hunter2@127.0.0.1:8000",
                ]
            )
            == 2
        )
    assert "hunter2" not in caplog.text
    assert "operator" not in caplog.text


def test_the_configured_origin_is_held_to_the_shape_the_option_is_held_to(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
) -> None:
    """The two inputs reach the same gate, so they cannot disagree about what is usable."""
    monkeypatch.setenv(_ORIGIN_VARIABLE, "http://127.0.0.1:8000/")
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert cli.main(["serve", "--state-dir", str(tmp_path), "--container"]) == 2
    assert "origin" in caplog.text


def test_the_accepted_origin_is_logged_where_a_port_mismatch_can_be_read(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A browser opened on another port is refused `same_origin_required`, which names
    neither origin, so the one the console accepts has to be readable at startup."""
    _served(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=cli.__name__):
        assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed", "--port", "9111"]) == 0
    assert "http://127.0.0.1:9111" in caplog.text
    assert "refused" in caplog.text


def test_a_second_termination_never_aborts_the_clean_up_the_first_one_began(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A container runtime that is not obeyed quickly enough sends SIGTERM again."""
    released: list[str] = []

    def _close() -> None:
        if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
            # Raising it here would kill the test session instead of failing this test.
            released.append("unprotected")
            return
        signal.raise_signal(signal.SIGTERM)
        released.append("closed")

    def _build(state_dir: Path, **keywords: object) -> tuple[Starlette, Callable[[], None]]:
        return Starlette(), _close

    def _terminated(*arguments: object, **keywords: object) -> None:
        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(cli, "build_demo_app", _build)
    monkeypatch.setattr("uvicorn.run", _terminated)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"])
    assert stopped.value.code == 143
    assert released == ["closed"]


def test_the_stores_are_released_when_termination_arrives_before_the_server_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Seeding a demonstration takes long enough for a stop to land inside the build."""
    released: list[str] = []

    def _build(state_dir: Path, **keywords: object) -> tuple[Starlette, Callable[[], None]]:
        if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
            raise RuntimeError("termination was unhandled while the console was being built")
        try:
            signal.raise_signal(signal.SIGTERM)
            raise AssertionError("termination must leave the build as an exception")
        except BaseException:
            # `build_demo_app` closes whatever it opened before re-raising.
            released.append("closed")
            raise

    monkeypatch.setattr(cli, "build_demo_app", _build)
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"])
    assert stopped.value.code == 143
    assert released == ["closed"]


@pytest.mark.parametrize("outside", ["0", "-1", "65536", "99999999", "eight"])
def test_a_port_no_socket_can_carry_is_refused_before_anything_is_created(
    outside: str,
) -> None:
    """A port outside the range reached `bind()` as an `OverflowError` traceback."""
    with pytest.raises(SystemExit) as failure:
        cli.parse_arguments(["serve", "--state-dir", "/tmp/state", "--port", outside])
    assert failure.value.code == 2


def test_a_server_that_cannot_bind_is_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A traceback out of `bind()` reads as a crashed product rather than a port to change.

    The error is a genuine one from the kernel, raised by binding a port this test holds.
    uvicorn catches that particular failure itself and exits 3 after reporting it; this
    covers the bind failures it leaves to its caller.
    """
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        taken = held.getsockname()[1]

        def _bind(application: object, *, host: str, port: int) -> None:
            with socket.socket() as second:
                second.bind((host, port))

        console = _serving(monkeypatch, _bind)
        with caplog.at_level(logging.ERROR, logger=cli.__name__):
            exit_code = cli.main(
                ["serve", "--state-dir", str(tmp_path), "--no-seed", "--port", str(taken)]
            )
    assert exit_code == 1
    assert console.closed == 1
    assert os.strerror(errno.EADDRINUSE) in caplog.text
    assert caplog.records[-1].exc_info is None


def test_a_bundle_named_only_by_the_environment_is_validated_by_the_same_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
) -> None:
    """`create_app` reads the variable too, and reports it naming neither setting."""
    monkeypatch.setenv(_DIST_VARIABLE, str(tmp_path / "absent"))
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"]) == 2
    assert _DIST_VARIABLE in caplog.text
    assert "--dist" in caplog.text


def test_a_bundle_named_by_the_environment_reaches_the_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    monkeypatch.setenv(_DIST_VARIABLE, str(dist))
    console, _ = _served(monkeypatch)
    assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"]) == 0
    assert console.built[0][1]["dist"] == dist


_ACCEPTED_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://127.0.0.1",
    "http://localhost",
    "http://0.0.0.0:8000",
    "http://[::1]:8000",
    "http://[2001:db8::1]:8000",
    "https://example.test",
    "http://xn--r8jz45g.test:8000",
    f"http://{'a' * 300}.test:8000",
    "http://example.test.:8000",
    # An underscore is a legal byte in a DNS label and is how a Docker Compose service is
    # usually named, so a console published beside one is configured with exactly this.
    "http://my_service:8731",
    # `ipaddress` spells this address with a dotted quad and a browser spells it in hextets,
    # so canonicalizing it would refuse the spelling the browser actually sends.
    "http://[::ffff:7f00:1]:8000",
]


@pytest.mark.parametrize("usable", _ACCEPTED_ORIGINS)
def test_every_origin_a_browser_really_sends_is_accepted_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, usable: str
) -> None:
    """A false refusal stops the quickstart as dead as a false acceptance.

    `0.0.0.0` is a bind address the gate can never match on a Mac, and a browser on Linux
    does send it; an IPv6 literal, a punycode name, a trailing-dot name and a 300-character
    name are each what some browser puts in the header, so none may be refused.
    """
    console, _ = _served(monkeypatch)
    assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed", "--origin", usable]) == 0
    assert console.built[0][1]["origin"] == usable


@pytest.mark.parametrize(
    "unusable",
    [
        "http://127.0.0.1:80",
        "https://example.test:443",
        "http://127.0.0.1:",
        "http://127.0.0.1:-1",
        "http://127.0.0.1:99999999",
        "http://127.0.0.1:80o0",
    ],
)
def test_an_origin_whose_port_no_browser_would_send_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
    unusable: str,
) -> None:
    """A browser leaves out the default port of the scheme (RFC 6454 section 6.1).

    `--origin "http://127.0.0.1:$PORT"` with `PORT` unset produces the empty one, and an
    operator who published `-p 80:8000` writes `:80`. Both serve a console that renders
    every page and refuses every command.
    """
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert cli.main(["serve", "--state-dir", str(tmp_path), "--origin", unusable]) == 2
    assert "origin" in caplog.text


@pytest.mark.parametrize(
    "unusable",
    [
        "http://0177.0.0.1:8000",
        "http://2130706433:8000",
        "http://127.1:8000",
        "http://127.0.0.01:8000",
        "http://0x7f.0.0.1:8000",
        "http://[2001:db8:0:0:0:0:0:1]:8000",
        "http://[0:0:0:0:0:0:0:1]:8000",
    ],
)
def test_an_address_spelled_the_way_no_browser_spells_it_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
    unusable: str,
) -> None:
    """A browser resolves each of these and sends one spelling of the address it found.

    `http://127.1` reaches the gate as `http://127.0.0.1` and `http://[0:0:0:0:0:0:0:1]` as
    `http://[::1]`, so a console configured with the spelling above renders every page and
    refuses every command.
    """
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert cli.main(["serve", "--state-dir", str(tmp_path), "--origin", unusable]) == 2
    assert "origin" in caplog.text


@pytest.mark.parametrize(
    ("unusable", "reason"),
    [
        ("127.0.0.1:8000", "does not begin with http:// or https://"),
        ("ftp://127.0.0.1:8000", "scheme is neither http nor https"),
        ("http://", "names no host"),
        ("http://h\u00e9llo.test:8000", "punycode"),
        ("http://127.0.0.1:80o0", "port no browser sends"),
        ("http://127.0.0.1%3a8000", "character no hostname has"),
    ],
)
def test_an_origin_with_no_browser_spelling_at_all_is_told_why(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
    unusable: str,
    reason: str,
) -> None:
    """None of these has a spelling to name, and none has a trailing slash, a path, a
    space, an uppercase letter or a default port either.

    The list of those five correctable defects is all the refusal used to give, so an
    operator was told to correct the one part of the value that was already correct.
    """
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert cli.main(["serve", "--state-dir", str(tmp_path), "--origin", unusable]) == 2
    assert reason in caplog.text
    assert "no trailing slash" not in caplog.text


def test_a_default_port_is_reported_with_the_spelling_the_browser_would_send(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
) -> None:
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert (
            cli.main(["serve", "--state-dir", str(tmp_path), "--origin", "http://127.0.0.1:80"])
            == 2
        )
    assert "for example http://127.0.0.1" in caplog.text
    assert "http://127.0.0.1:80" not in caplog.text


def test_the_derived_loopback_origin_leaves_out_the_default_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--port 80` is served on the loopback host, and the browser sends no port at all."""
    console, _ = _served(monkeypatch)
    assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed", "--port", "80"]) == 0
    assert console.built[0][1]["origin"] == "http://127.0.0.1"


@pytest.mark.parametrize(
    "unprintable",
    [
        "http://127.0.0.1\x00",
        "http://127.0.0.1​:8000",
        "http://127.0.0.1\xad:8000",
        "http://127.0.0.1:8000\\",
        "http://127.0.0.1%3a8000",
        "http://h\u00e9llo.test:8000",
    ],
)
def test_an_origin_carrying_characters_no_host_has_is_refused_before_it_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
    unprintable: str,
) -> None:
    """The accepted origin is echoed to the startup log, so a NUL, a zero-width space, a
    percent escape or a name a browser would send as punycode must never reach it."""
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert cli.main(["serve", "--state-dir", str(tmp_path), "--origin", unprintable]) == 2
    assert unprintable not in caplog.text


def test_an_origin_whose_host_cannot_be_parsed_never_repeats_its_credential(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _no_server: None,
) -> None:
    """`urlsplit` raises for a host that NFKC normalization changes, and the `ValueError`
    it raises carries the whole authority - the credential with it - into the log."""
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert (
            cli.main(
                [
                    "serve",
                    "--state-dir",
                    str(tmp_path),
                    "--origin",
                    "http://operator:hunter2@exa\uff0fmple.test:8000",
                ]
            )
            == 2
        )
    assert "hunter2" not in caplog.text
    assert "operator" not in caplog.text
    assert "credentials" in caplog.text


@pytest.mark.parametrize("stopping", [signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT])
def test_the_stores_are_closed_however_the_container_is_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stopping: signal.Signals
) -> None:
    """`docker stop` sends SIGTERM, a Compose `stop_signal:` names whichever it likes, and
    a terminal that closes on a bare `heinzel-console serve` sends SIGHUP."""

    def _stopped(*arguments: object, **keywords: object) -> None:
        # An unhandled signal here would end the test session rather than fail this test.
        assert signal.getsignal(stopping) is not signal.SIG_DFL
        signal.raise_signal(stopping)

    console = _serving(monkeypatch, _stopped)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"])
    assert stopped.value.code == 128 + stopping
    assert console.closed == 1


@pytest.mark.parametrize("stopping", [signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT])
def test_a_termination_during_an_ordinary_clean_up_never_aborts_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stopping: signal.Signals
) -> None:
    """The clean-up is also reached when the server returns for its own reasons - SIGINT,
    its own exit, an exception - and the first stop to land then must not abort it."""
    released: list[str] = []

    def _close() -> None:
        if signal.getsignal(stopping) is signal.SIG_DFL:
            # Raising it here would kill the test session instead of failing this test.
            released.append("unprotected")
            return
        signal.raise_signal(stopping)
        released.append("closed")

    def _build(state_dir: Path, **keywords: object) -> tuple[Starlette, Callable[[], None]]:
        return Starlette(), _close

    monkeypatch.setattr(cli, "build_demo_app", _build)
    monkeypatch.setattr("uvicorn.run", lambda app, **keywords: None)
    assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"]) == 0
    assert released == ["closed"]


@pytest.mark.parametrize("stopping", [signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT])
def test_every_previous_termination_handler_is_restored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stopping: signal.Signals
) -> None:
    before = signal.getsignal(stopping)
    _served(monkeypatch)
    assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"]) == 0
    assert signal.getsignal(stopping) is before
