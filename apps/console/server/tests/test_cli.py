"""The `heinzel-console` command.

The command serves a console that has no authentication, so the tests that matter here are
the ones about where it may bind and whether it releases the state directory however the
server exits. No test binds a port.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from heinzel_console import cli
from starlette.applications import Starlette


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


def test_serve_binds_the_loopback_host_by_default() -> None:
    assert cli.parse_arguments(["serve", "--state-dir", "/tmp/state"]).host == "127.0.0.1"


def test_container_binds_every_interface() -> None:
    arguments = cli.parse_arguments(["serve", "--state-dir", "/tmp/state", "--container"])
    assert arguments.host == "0.0.0.0"


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
    """A stand-in for the demonstration console that records when it was closed."""

    def __init__(self) -> None:
        self.closed = 0

    def build(self, state_dir: Path, **keywords: object) -> tuple[Starlette, Callable[[], None]]:
        return Starlette(), self.close

    def close(self) -> None:
        self.closed += 1


def _serving(monkeypatch: pytest.MonkeyPatch, run: Callable[..., None]) -> _RecordingConsole:
    console = _RecordingConsole()
    monkeypatch.setattr(cli, "build_demo_app", console.build)
    monkeypatch.setattr("uvicorn.run", run)
    return console


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
    served: list[object] = []
    console = _serving(monkeypatch, lambda app, **keywords: served.append(keywords))
    assert cli.main(["serve", "--state-dir", str(tmp_path), "--no-seed"]) == 0
    assert console.closed == 1
    assert served == [{"host": "127.0.0.1", "port": 8000}]


def test_serving_off_the_loopback_host_is_refused_without_the_container_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _no_server: None
) -> None:
    monkeypatch.setattr(cli, "require_loopback", _refuse)
    with pytest.raises(ValueError, match="no authentication"):
        cli.main(["serve", "--state-dir", str(tmp_path)])


def _refuse(host: str) -> str:
    raise ValueError("the console has no authentication")
