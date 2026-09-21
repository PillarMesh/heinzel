"""The `heinzel-console` command.

The command serves a console that has no authentication, so the tests that matter here are
the ones about where it may bind, which origin the browser must use, and whether it releases
the state directory however the server exits. No test binds a port.
"""

from __future__ import annotations

import logging
import signal
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from heinzel_console import cli
from starlette.applications import Starlette

_ORIGIN_VARIABLE = "HEINZEL_CONSOLE_ALLOWED_ORIGIN"


@pytest.fixture(autouse=True)
def _no_configured_origin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the environment's own origin out of every test that does not set one."""
    monkeypatch.delenv(_ORIGIN_VARIABLE, raising=False)
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
        (tmp_path, {"seed": False, "origin": "http://127.0.0.1:9111", "dist": dist})
    ]
    assert served == [{"host": "127.0.0.1", "port": 9111}]


def test_the_demonstration_is_seeded_unless_it_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    console, _ = _served(monkeypatch)
    assert cli.main(["serve", "--state-dir", str(tmp_path)]) == 0
    assert console.built == [
        (tmp_path, {"seed": True, "origin": "http://127.0.0.1:8000", "dist": None})
    ]


def test_the_container_takes_its_origin_from_the_environment_not_the_bind_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bind address is `0.0.0.0`, which no browser ever sends as its origin."""
    monkeypatch.setenv(_ORIGIN_VARIABLE, "http://127.0.0.1:8000")
    console, served = _served(monkeypatch)
    assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed", "--container"]) == 0
    assert console.built == [
        (tmp_path, {"seed": False, "origin": "http://127.0.0.1:8000", "dist": None})
    ]
    assert served == [{"host": "0.0.0.0", "port": 8000}]


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
    def _refuse(*arguments: object, **keywords: object) -> tuple[Starlette, Callable[[], None]]:
        raise PermissionError(13, "Permission denied", str(tmp_path / "private-token-dir"))

    monkeypatch.setattr(cli, "build_demo_app", _refuse)
    monkeypatch.setattr("uvicorn.run", _unreachable_builder)
    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"]) == 1
    assert "state directory" in caplog.text
    assert "Permission denied" in caplog.text
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
